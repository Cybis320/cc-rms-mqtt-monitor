"""Two hosts that share a hostname must NOT share an MQTT client id.

MQTT ids are exclusive: when a second client connects with an id already in use, the
broker kicks the incumbent. Four machines that kept the default `raspberrypi` hostname
therefore evicted each other at ~5 reconnects/second -- forever, since every eviction is
immediately followed by the loser reconnecting and evicting the winner. Each round trip
also re-fires the Last-Will and rewrites the retained status topic.

The id must additionally be STABLE across restarts (a new id every boot would leave the
broker holding sessions for ids nothing answers to) and must not come from
/etc/machine-id, which clones of a common image share.

Runs under pytest, or standalone: `python tests/test_client_id_unique.py`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# publisher imports paho only to reach the broker; this test never does.
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


def _fresh_suffix_file():
    os.environ["CC_CLIENT_ID_FILE"] = os.path.join(tempfile.mkdtemp(), "client_suffix")
    return os.environ["CC_CLIENT_ID_FILE"]


def test_same_hostname_different_machines_get_different_ids():
    """The raspberrypi case: identical hostname, two installs, ids must differ."""
    _fresh_suffix_file()
    a = publisher._instance_suffix()
    _fresh_suffix_file()                       # a second machine = its own state dir
    b = publisher._instance_suffix()
    assert a != b, "two installs sharing a hostname would evict each other forever"


def test_suffix_is_stable_across_restarts():
    _fresh_suffix_file()
    first = publisher._instance_suffix()
    assert publisher._instance_suffix() == first
    assert publisher._instance_suffix() == first


def test_suffix_is_persisted_and_reused_from_disk():
    path = _fresh_suffix_file()
    got = publisher._instance_suffix()
    with open(path) as fh:
        assert fh.read().strip() == got

    with open(path, "w") as fh:                # a pre-existing install keeps its id
        fh.write("abc123\n")
    assert publisher._instance_suffix() == "abc123"


def test_unwritable_location_still_yields_a_unique_id():
    """Read-only /var must not fall back to a shared/blank id -- that is the bug."""
    os.environ["CC_CLIENT_ID_FILE"] = "/proc/cc-cannot-write-here/client_suffix"
    a = publisher._instance_suffix()
    b = publisher._instance_suffix()
    assert a and b and a != b, "per-run ids are still unique, which is what matters"


def test_suffix_is_not_derived_from_machine_id():
    """Cloned images share /etc/machine-id, so it cannot be the uniqueness source."""
    try:
        with open("/etc/machine-id") as fh:
            mid = fh.read().strip()
    except (IOError, OSError):
        return                                  # nothing to collide with here
    _fresh_suffix_file()
    assert publisher._instance_suffix() not in (mid, mid[:6], mid[-6:])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
