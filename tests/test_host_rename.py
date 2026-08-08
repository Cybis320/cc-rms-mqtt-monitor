"""Renaming a host must not leave its retained topics published forever.

Host records are retained, so a rename (new hostname, or an edited host_name) leaves the
OLD topic frozen in place. Consumers then see a host "dead for weeks" while all of its
cameras report happily under the new name -- one renamed host was misread exactly that
way, as a 37-day outage, when every camera on it was fine. An empty retained payload is
the tombstone; the bridge already treats it as stop-tracking.

Runs under pytest, or standalone: `python tests/test_host_rename.py`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# publisher imports paho purely to talk to the broker; this test never does. Stub it so
# the suite keeps running with no third-party dependency, like the rest of the tests.
if "paho" not in sys.modules:
    import types
    _paho = types.ModuleType("paho"); _mqtt = types.ModuleType("paho.mqtt")
    _client = types.ModuleType("paho.mqtt.client")
    _client.Client = object
    _client.CallbackAPIVersion = types.SimpleNamespace(VERSION1=1, VERSION2=2)
    _client.MQTTv311 = 4
    _mqtt.client = _client; _paho.mqtt = _mqtt
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _mqtt
    sys.modules["paho.mqtt.client"] = _client

from cc_mqtt_monitor import publisher                       # noqa: E402


class _Broker(object):
    client_id_prefix = "cc-rms-monitor"


class _Cfg(object):
    topic_prefix = "stations"
    broker = _Broker()
    def __init__(self, host): self.host_name = host


class _Client(object):
    def __init__(self): self.published = []
    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))


class _Pub(publisher.Publisher):
    """Bypass __init__/connect: we are unit-testing the rename cleanup alone."""
    def __init__(self, host, path):
        self.config = _Cfg(host)
        self.client = _Client()
        os.environ["CC_HOSTNAME_FILE"] = path


def _tmp():
    d = tempfile.mkdtemp()
    return os.path.join(d, "last_host_name")


def test_first_run_records_name_and_clears_nothing():
    p = _Pub("WSU_Lind", _tmp())
    p.clear_renamed_host()
    assert p.client.published == []                       # nothing to clear yet
    with open(os.environ["CC_HOSTNAME_FILE"]) as fh:
        assert fh.read().strip() == "WSU_Lind"


def test_rename_tombstones_the_old_topics():
    path = _tmp()
    _Pub("ProCamSFF14", path).clear_renamed_host()        # first boot under the old name
    p = _Pub("WSU_Lind", path)                            # ...then renamed
    p.clear_renamed_host()
    topics = {t for t, _, _ in p.client.published}
    assert topics == {"stations/ProCamSFF14/health", "stations/ProCamSFF14/status"}
    for _t, payload, retain in p.client.published:
        assert payload == b"", "tombstone must be an EMPTY payload"
        assert retain is True, "tombstone must be RETAINED to replace the retained record"


def test_unchanged_name_clears_nothing():
    path = _tmp()
    _Pub("WSU_Lind", path).clear_renamed_host()
    p = _Pub("WSU_Lind", path)
    p.clear_renamed_host()
    assert p.client.published == []


def test_rename_is_recorded_so_it_only_fires_once():
    path = _tmp()
    _Pub("old", path).clear_renamed_host()
    _Pub("new", path).clear_renamed_host()
    p = _Pub("new", path)
    p.clear_renamed_host()
    assert p.client.published == []                       # already tombstoned last time


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
