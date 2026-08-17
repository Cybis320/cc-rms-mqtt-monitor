"""An outage must not build a backlog, and a wedged session must not look healthy.

Two failures seen together after the server was unreachable for 5.4 days:

1. The monitor kept publishing into a dead socket every cycle, and paho queued it all.
   On reconnect one host pushed ~72 MB of week-old snapshots. These are RETAINED STATE
   snapshots -- only the newest has any value -- so the backlog was pure harm: it
   drowned the station's current state and the dashboard read "offline" for hours while
   it was capturing normally.

2. That host stayed TCP-connected the whole time, so `Restart=always` never fired and
   nothing noticed. Liveness has to be measured by publishes the broker ACCEPTED
   (QoS-1 PUBACK), not by the process still running.

Runs under pytest, or standalone: `python tests/test_offline_backlog.py`.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "paho" not in sys.modules:
    _paho = types.ModuleType("paho"); _mqtt = types.ModuleType("paho.mqtt")
    _client = types.ModuleType("paho.mqtt.client")
    _client.Client = object
    _client.CallbackAPIVersion = types.SimpleNamespace(VERSION1=1, VERSION2=2)
    _client.MQTTv311 = 4
    _mqtt.client = _client; _paho.mqtt = _mqtt
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _mqtt
    sys.modules["paho.mqtt.client"] = _client

from cc_mqtt_monitor import publisher, monitor              # noqa: E402


class _Client(object):
    """Stand-in paho client whose connection state the test drives."""
    def __init__(self, connected=True):
        self.connected = connected
        self.published = []

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        return types.SimpleNamespace(wait_for_publish=lambda *a, **k: None)


class _PubCfg(object):
    topic_prefix = "stations"
    host_name = "testhost"


class _Pub(publisher.Publisher):
    """Bypass __init__/paho: exercising the offline guard and watchdog clock alone."""
    def __init__(self, connected=True):
        self.config = _PubCfg()
        self.client = _Client(connected)
        self.host_status_topic = "stations/testhost/status"
        self._pending = []
        self._connected = connected
        self._last_publish_ok = None
        self._dropped_offline = 0
        self.announce = True


class _Cfg(object):
    interval_seconds = 60


# --- 1. an outage must not accumulate a backlog ---------------------------------

def test_publishes_are_dropped_not_queued_while_offline():
    p = _Pub(connected=False)
    for _ in range(500):                       # ~8 hours of cycles
        p._publish("stations/X/health", "{}")
    assert p.client.published == [], "nothing may be handed to paho while offline"
    assert p._pending == [], "and nothing may be tracked for a later flush"
    assert p._dropped_offline == 500


def test_publishing_resumes_immediately_once_reconnected():
    p = _Pub(connected=False)
    p._publish("stations/X/health", "{}")
    p.client.connected = True
    p._publish("stations/X/health", '{"now": true}')
    assert len(p.client.published) == 1, "the post-reconnect snapshot must go out"
    assert p.client.published[0][1] == '{"now": true}', "and it must be the CURRENT one"


def test_queue_cap_is_bounded():
    """Even where paho does the queueing, the cap must be finite -- 0 means unlimited."""
    assert 0 < publisher.MAX_QUEUED_MESSAGES <= 1000


# --- 2. a connected-but-wedged session must be caught --------------------------

def test_watchdog_exits_when_connected_but_nothing_lands():
    p = _Pub(connected=True)
    p._last_publish_ok = 0.0                   # last PUBACK in 1970: badly wedged
    try:
        monitor._publish_watchdog(_Cfg(), p)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("a wedged session must exit so systemd restarts it")


def test_watchdog_is_quiet_when_publishes_are_landing():
    import time
    p = _Pub(connected=True)
    p._last_publish_ok = time.time()
    monitor._publish_watchdog(_Cfg(), p)       # must not raise


def test_watchdog_does_not_fire_before_the_first_connection():
    """Startup backoff is not a wedge; there is nothing to be stale yet."""
    p = _Pub(connected=True)
    p._last_publish_ok = None
    monitor._publish_watchdog(_Cfg(), p)


def test_watchdog_does_not_fire_during_a_plain_outage():
    """Broker unreachable is an OUTAGE, not a wedged agent -- restarting cannot help."""
    p = _Pub(connected=False)
    p._last_publish_ok = 0.0
    monitor._publish_watchdog(_Cfg(), p)       # must not raise


def test_puback_resets_the_watchdog_clock():
    p = _Pub(connected=True)
    p._last_publish_ok = 0.0
    p._on_publish(None, None, 1)               # broker accepted a message
    assert p.seconds_since_publish() < 5
    monitor._publish_watchdog(_Cfg(), p)       # now healthy again


def test_disconnect_marks_us_offline_so_the_guard_engages():
    p = _Pub(connected=True)
    p.client.connected = False                 # paho noticed the drop
    p._on_disconnect(None, None, 0)
    assert p.is_connected() is False
    p._publish("stations/X/health", "{}")
    assert p.client.published == []


def test_dropped_tombstone_is_not_recorded_as_cleared():
    """A tombstone is a one-shot the caller marks done; dropping one while offline
    would leave an opted-out station's data retained forever."""
    p = _Pub(connected=False)
    assert p.clear_station("X") is False, "must report that it did NOT go out"
    p.client.connected = True
    assert p.clear_station("X") is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
