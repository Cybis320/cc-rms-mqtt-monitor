"""The collect -> evaluate -> publish loop."""

import re
import time
import logging

from .discovery import discover_stations
from .collect import collect_station, rms_branch, rms_behind
from .oslevel import collect_host
from .health import build_state, build_host_state
from . import maintenance

log = logging.getLogger("cc_mqtt_monitor")


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _slug(name):
    """Canonical subscription handle from a group label: a multi-word name like
    'Elginfield Contrail Cameras' -> 'Elginfield-Contrail-Cameras' (valid as an
    ntfy topic / Telegram tag, which can't contain spaces)."""
    if not name:
        return None
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or None


def _station_group(station, config):
    """Subscription group: the explicit config `group` override if set (the
    operator's install-time choice), else the station's RMS camera_group_name."""
    return config.group or station.camera_group_name


# RMS ships .config with this placeholder stationID; a station still on it is
# unconfigured (and would collide across every fresh box), so we never publish
# it. It's excluded from transmission AND tombstoned like an opt-out.
_DEFAULT_STATION_IDS = {"XX0001"}


def _publishable(station):
    """True if this station should be transmitted: the operator consents (RMS
    weblog_enable) AND it has a real stationID (not the default placeholder)."""
    return (station.weblog_enable
            and (station.station_id or "").upper() not in _DEFAULT_STATION_IDS)


def _consenting_stations(config):
    """Stations we publish: operator consent (weblog_enable) and a real stationID.
    A non-publishable station is never sent to MQTT and is excluded from the host
    record's station_ids/groups, so it doesn't leak."""
    return [s for s in discover_stations(config.stations_dir, config.rms_dir)
            if _publishable(s)]


def gather(config, maint=None):
    """Discover stations and build a state dict for each (no MQTT involved).
    `maint` is the (bool, reason) maintenance tuple; computed if not passed."""
    stations = _consenting_stations(config)
    disabled = set(config.disabled_checks or [])
    maint, maint_reason = maint if maint is not None else maintenance.detect(config)
    now = time.time()
    branch = rms_branch(config.rms_dir)      # host-wide RMS git branch (one checkout)
    behind = rms_behind(config.rms_dir)      # commits behind upstream (live, no fetch)
    states = []
    for station in stations:
        metrics = collect_station(station, config.log_tail_lines, now,
                                  config.log_warning_ignore)
        state = build_state(metrics, config.thresholds, config.host_name, _iso(now), disabled)
        group = _station_group(station, config)
        state["group"] = group               # human-readable label
        state["group_slug"] = _slug(group)   # canonical subscription handle
        state["maintenance"] = maint         # expected-disruption flag (bridge mutes)
        state["maintenance_reason"] = maint_reason
        if branch:
            state["rms_branch"] = branch     # which RMS code the station is running
        if behind is not None:
            state["rms_behind"] = behind     # commits behind upstream (0 = up to date)
        # Approximate coordinates for the dashboard map: obfuscated to ~1 km
        # (2 decimals). Omitted entirely when the .config has no coords.
        if station.has_location:
            state["lat"] = round(station.latitude, 2)
            state["lon"] = round(station.longitude, 2)
        states.append(state)
    return states


def gather_host(config, maint=None):
    """Build the host-wide (OS) state dict, or None if no station consents to
    being published (so a fully opted-out host leaks nothing)."""
    stations = _consenting_stations(config)
    if not stations:
        return None
    disabled = set(config.disabled_checks or [])
    # UDP RcvbufErrors are host-wide; collect them when any station uses UDP RTSP.
    udp = any(s.protocol == "udp" for s in stations)
    metrics = collect_host(udp=udp)
    state = build_host_state(metrics, config.thresholds, config.host_name,
                             _iso(time.time()), disabled)
    # A host can span several groups; list the distinct ones plus its stations,
    # so the bridge can fan a host-level (OOM) alert out to each.
    groups = sorted({g for g in (_station_group(s, config) for s in stations) if g})
    state["groups"] = groups
    state["group_slugs"] = [_slug(g) for g in groups]
    state["station_ids"] = [s.station_id for s in stations]
    branch = rms_branch(config.rms_dir)
    if branch:
        state["rms_branch"] = branch
    behind = rms_behind(config.rms_dir)
    if behind is not None:
        state["rms_behind"] = behind
    maint, maint_reason = maint if maint is not None else maintenance.detect(config)
    state["maintenance"] = maint
    state["maintenance_reason"] = maint_reason
    return state


def make_test_state(config):
    """A clearly-marked test alert that routes like a real one: it carries this
    host's actual group_slug and a station_id derived from a real station (so it
    reaches both cc-<group_slug> and the network's cc-<prefix> subscribers),
    without clobbering any real station's retained state."""
    stations = _consenting_stations(config)
    now = time.time()
    if stations:
        base_id = stations[0].station_id
        group = _station_group(stations[0], config)
    else:
        base_id = config.host_name
        group = config.group
    return {
        "station_id": "%s-TEST" % base_id,
        "status": "degraded",
        "problems": ["\U0001F514 Test alert from %s at %s -- if you received this, the "
                     "MQTT -> ntfy/Telegram chain works." % (config.host_name, _iso(now))],
        "group": group,
        "group_slug": _slug(group),
        "test": True,
        "host": config.host_name,
        "timestamp": _iso(now),
    }


def make_udp_test_state(config, rate=999.0):
    """A host-level UDP RcvbufErrors test alert that routes like the real thing.

    Builds a genuine host record from live metrics, then injects a simulated
    growth `rate` so the real evaluate_host() path produces the actual alert
    payload a bridge would receive -- marked test:true and prefixed TEST, routed
    to this host's groups, and published non-retained so the retained host record
    is untouched."""
    from .oslevel import collect_host, read_udp_stats
    stations = _consenting_stations(config)
    now = time.time()
    metrics = collect_host(udp=True)
    metrics["udp_rcvbuf_errors_per_min"] = rate          # simulated burst
    if metrics.get("udp_rcvbuf_errors") is None:
        metrics["udp_rcvbuf_errors"] = read_udp_stats()[0] or 0
    if metrics.get("udp_rcvbuf_error_pct") is None:
        metrics["udp_rcvbuf_error_pct"] = 0.0
    # Ensure the udp check fires even if it's in disabled_checks for this host.
    disabled = set(config.disabled_checks or []) - {"udp_rcvbuf_errors"}
    state = build_host_state(metrics, config.thresholds, config.host_name,
                             _iso(now), disabled)
    groups = sorted({g for g in (_station_group(s, config) for s in stations) if g})
    state["groups"] = groups
    state["group_slugs"] = [_slug(g) for g in groups]
    state["station_ids"] = [s.station_id for s in stations]
    state["maintenance"] = False
    state["maintenance_reason"] = None
    state["test"] = True
    state["problems"] = ["\U0001F9EA TEST: %s" % p for p in state["problems"]]
    return state


def run_once(config, publisher=None):
    """Collect host + every station once and (optionally) publish."""
    maint = maintenance.detect(config)            # one scan per cycle, shared
    host_state = gather_host(config, maint)
    if host_state is not None:
        log.info("host %s: %s %s", config.host_name, host_state["status"],
                 ("- " + "; ".join(host_state["problems"])) if host_state["problems"] else "")
        if publisher:
            publisher.publish_host_state(host_state)

    states = gather(config, maint)
    for state in states:
        log.info("%s: %s %s", state["station_id"], state["status"],
                 ("- " + "; ".join(state["problems"])) if state["problems"] else "")
        if publisher:
            publisher.publish_state(state)
    if publisher:
        publisher.flush()
    return host_state, states


def _connect_with_retry(config, publisher):
    """Connect with backoff so a transient network/broker issue at boot just
    retries instead of crashing. paho's loop auto-reconnects after this."""
    backoff = 5
    while True:
        try:
            publisher.connect()
            return
        except Exception as exc:
            log.warning("Broker connect to %s:%d failed (%s); retrying in %ds",
                        config.broker.host, config.broker.port, exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def run_loop(config, publisher):
    """Run forever, publishing every config.interval_seconds.

    Connects ONLY while at least one station consents to being published
    (weblog_enable). A fully opted-out host transmits nothing; opting a station
    out at runtime wipes its retained record, and opting the whole host out wipes
    everything and disconnects without an 'offline' marker."""
    from .oslevel import protect_from_oom
    adj = protect_from_oom()
    if adj is not None:
        log.info("Set oom_score_adj=%d (protected from OOM-killer)", adj)
    else:
        log.info("Could not lower oom_score_adj; rely on systemd OOMScoreAdjust")

    connected = False
    cleared = set()   # opted-out station ids whose retained record we've wiped
    try:
        while True:
            start = time.time()
            stations = discover_stations(config.stations_dir, config.rms_dir)
            consenting = [s for s in stations if _publishable(s)]
            opted_out = [s.station_id for s in stations if not _publishable(s)]

            if consenting:
                if not connected:
                    _connect_with_retry(config, publisher)
                    connected = True
                    log.info("Connected to %s:%d; monitoring every %ds",
                             config.broker.host, config.broker.port,
                             config.interval_seconds)
                try:
                    run_once(config, publisher)
                    for sid in opted_out:        # wipe any prior data, once each
                        if sid not in cleared:
                            publisher.clear_station(sid)
                            cleared.add(sid)
                    cleared &= set(opted_out)     # re-arm if a station re-consents
                except Exception:  # never let one bad cycle kill the agent
                    log.exception("Error during monitor cycle")
            elif connected:
                log.info("No station has weblog_enable=true; clearing retained "
                         "data and going silent")
                publisher.go_silent([s.station_id for s in stations])
                connected = False
                cleared = set()
            else:
                log.info("No station has weblog_enable=true; nothing published")

            # Sleep until the next cycle, but wake early if maintenance flips.
            _sleep_until_next(config, start)
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down")
    finally:
        if connected:
            publisher.disconnect()


# How often, during the inter-cycle sleep, to re-check the maintenance state.
_MAINT_POLL_S = 10


def _sleep_until_next(config, cycle_start):
    """Sleep up to interval_seconds; return early if maintenance state changes."""
    baseline = maintenance.detect(config)[0]
    deadline = cycle_start + config.interval_seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(_MAINT_POLL_S, remaining))
        if maintenance.detect(config)[0] != baseline:
            log.info("Maintenance state changed; publishing immediately")
            return
