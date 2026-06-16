"""A minimal subscriber that prints a live multi-station status table.

This is a reference consumer of the plain-JSON state topics -- handy for a quick
look without standing up a web dashboard.
"""

import json
import time

import paho.mqtt.client as mqtt

from .publisher import _make_client

_COLORS = {"ok": "\033[32m", "degraded": "\033[33m", "error": "\033[31m"}
_RESET = "\033[0m"


def _fmt_age(seconds):
    if seconds is None:
        return "-"
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds / 60)
    return "%dh" % (seconds / 3600)


class Viewer:
    def __init__(self, config):
        self.config = config
        self.states = {}
        self.client = _make_client(
            "%s-viewer-%s" % (config.broker.client_id_prefix, config.host_name))
        if config.broker.username:
            self.client.username_pw_set(
                config.broker.username, config.broker.password)
        if config.broker.tls:
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, *args):
        client.subscribe("%s/+/health" % self.config.topic_prefix)

    def _on_message(self, client, userdata, msg):
        try:
            state = json.loads(msg.payload.decode("utf-8"))
            self.states[state["station_id"]] = state
        except (ValueError, KeyError):
            pass

    def _render(self):
        print("\033[2J\033[H", end="")  # clear screen
        print("CC RMS health  --  %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        header = "%-9s %-9s %-7s %-7s %-7s %-6s %s" % (
            "STATION", "STATUS", "CAPTURE", "FITS_AGE", "DISK_GB", "ERRS", "PROBLEMS")
        print(header)
        print("-" * len(header))
        for sid in sorted(self.states):
            s = self.states[sid]
            color = _COLORS.get(s.get("status"), "")
            problems = "; ".join(s.get("problems", [])) or "-"
            print("%s%-9s %-9s %-7s %-7s %-7s %-6s %s%s" % (
                color,
                sid,
                s.get("status", "?"),
                "up" if s.get("capture_alive") else "DOWN",
                _fmt_age(s.get("newest_fits_age_s")),
                s.get("disk_free_gb", "-"),
                s.get("fatal_error_count", 0),
                problems[:60],
                _RESET,
            ))

    def run(self):
        self.client.connect(
            self.config.broker.host, self.config.broker.port,
            keepalive=self.config.broker.keepalive)
        self.client.loop_start()
        try:
            while True:
                self._render()
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            self.client.loop_stop()
            self.client.disconnect()
