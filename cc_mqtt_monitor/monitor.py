"""The collect -> evaluate -> publish loop."""

import time
import logging

from .discovery import discover_stations
from .collect import collect_station
from .health import build_state

log = logging.getLogger("cc_mqtt_monitor")


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def gather(config):
    """Discover stations and build a state dict for each (no MQTT involved)."""
    stations = discover_stations(config.stations_dir, config.rms_dir)
    now = time.time()
    states = []
    for station in stations:
        metrics = collect_station(station, config.log_tail_lines, now)
        states.append(build_state(metrics, config.thresholds, config.host_name, _iso(now)))
    return states


def run_once(config, publisher=None):
    """Collect every station once and (optionally) publish."""
    states = gather(config)
    for state in states:
        log.info("%s: %s %s", state["station_id"], state["status"],
                 ("- " + "; ".join(state["problems"])) if state["problems"] else "")
        if publisher:
            publisher.publish_state(state)
    return states


def run_loop(config, publisher):
    """Run forever, publishing every config.interval_seconds."""
    publisher.connect()
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
