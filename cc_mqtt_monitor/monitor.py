"""The collect -> evaluate -> publish loop."""

import re
import time
import logging

from .discovery import discover_stations
from .collect import collect_station
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


def gather(config):
    """Discover stations and build a state dict for each (no MQTT involved)."""
    stations = discover_stations(config.stations_dir, config.rms_dir)
    disabled = set(config.disabled_checks or [])
    maint, maint_reason = maintenance.detect(config)
    now = time.time()
    states = []
    for station in stations:
        metrics = collect_station(station, config.log_tail_lines, now)
        state = build_state(metrics, config.thresholds, config.host_name, _iso(now), disabled)
        group = _station_group(station, config)
        state["group"] = group               # human-readable label
        state["group_slug"] = _slug(group)   # canonical subscription handle
        state["maintenance"] = maint         # expected-disruption flag (bridge mutes)
        state["maintenance_reason"] = maint_reason
        states.append(state)
    return states


def gather_host(config):
    """Build the host-wide (OS) state dict."""
    stations = discover_stations(config.stations_dir, config.rms_dir)
    disabled = set(config.disabled_checks or [])
    metrics = collect_host()
    state = build_host_state(metrics, config.thresholds, config.host_name,
                             _iso(time.time()), disabled)
    # A host can span several groups; list the distinct ones plus its stations,
    # so the bridge can fan a host-level (OOM) alert out to each.
    groups = sorted({g for g in (_station_group(s, config) for s in stations) if g})
    state["groups"] = groups
    state["group_slugs"] = [_slug(g) for g in groups]
    state["station_ids"] = [s.station_id for s in stations]
    maint, maint_reason = maintenance.detect(config)
    state["maintenance"] = maint
    state["maintenance_reason"] = maint_reason
    return state


def make_test_state(config):
    """A clearly-marked test alert that routes like a real one: it carries this
    host's actual group_slug and a station_id derived from a real station (so it
    reaches both cc-<group_slug> and the network's cc-<prefix> subscribers),
    without clobbering any real station's retained state."""
    stations = discover_stations(config.stations_dir, config.rms_dir)
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


def run_once(config, publisher=None):
    """Collect host + every station once and (optionally) publish."""
    host_state = gather_host(config)
    log.info("host %s: %s %s", config.host_name, host_state["status"],
             ("- " + "; ".join(host_state["problems"])) if host_state["problems"] else "")
    if publisher:
        publisher.publish_host_state(host_state)

    states = gather(config)
    for state in states:
        log.info("%s: %s %s", state["station_id"], state["status"],
                 ("- " + "; ".join(state["problems"])) if state["problems"] else "")
        if publisher:
            publisher.publish_state(state)
    if publisher:
        publisher.flush()
    return host_state, states


def run_loop(config, publisher):
    """Run forever, publishing every config.interval_seconds."""
    from .oslevel import protect_from_oom
    adj = protect_from_oom()
    if adj is not None:
        log.info("Set oom_score_adj=%d (protected from OOM-killer)", adj)
    else:
        log.info("Could not lower oom_score_adj; rely on systemd OOMScoreAdjust")

    # Retry the initial broker connection with backoff, so a transient network/
    # broker issue at boot (DNS not ready, broker restarting) just retries
    # instead of crashing the service. paho's loop auto-reconnects after this.
    backoff = 5
    while True:
        try:
            publisher.connect()
            break
        except Exception as exc:
            log.warning("Broker connect to %s:%d failed (%s); retrying in %ds",
                        config.broker.host, config.broker.port, exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    log.info("Connected to %s:%d; monitoring every %ds",
             config.broker.host, config.broker.port, config.interval_seconds)
    try:
        while True:
            start = time.time()
            try:
                run_once(config, publisher)
            except Exception:  # never let one bad cycle kill the agent
                log.exception("Error during monitor cycle")
            elapsed = time.time() - start
            time.sleep(max(1.0, config.interval_seconds - elapsed))
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down")
    finally:
        publisher.disconnect()
