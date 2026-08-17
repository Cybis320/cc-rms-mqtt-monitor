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
from cc_mqtt_monitor import config as cfgmod                # noqa: E402


def _fresh_suffix_file():
    os.environ["CC_CLIENT_ID_FILE"] = os.path.join(tempfile.mkdtemp(), "client_suffix")
    return os.environ["CC_CLIENT_ID_FILE"]


def _as_machine(mac):
    """Pretend to be a different physical box. Identity is MAC-derived now, so a second
    state dir alone no longer models a second machine -- the MAC has to differ too."""
    import uuid as _uuid
    cfgmod.uuid = type("u", (), {"getnode": staticmethod(lambda: mac)})
    _fresh_suffix_file()
    try:
        return publisher._instance_suffix()
    finally:
        cfgmod.uuid = _uuid


def test_same_hostname_different_machines_get_different_ids():
    """The raspberrypi case: identical hostname, two boxes, ids must differ."""
    a = _as_machine(0x001122334455)
    b = _as_machine(0x66778899aabb)
    assert a != b, "two installs sharing a hostname would evict each other forever"


def test_the_same_machine_always_derives_the_same_id():
    """The other half: a restart must NOT invent a new identity (28 orphans came of it)."""
    assert _as_machine(0x001122334455) == _as_machine(0x001122334455)


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


def test_unwritable_location_still_yields_a_STABLE_id():
    """Stability matters as much as uniqueness, and this case got it wrong first time.

    The service is unprivileged and /var/lib/cc-rms-monitor did not exist, so persisting
    always failed and a per-run random id was handed out. Unique, yes -- and every
    restart therefore published under a NEW host topic, leaving the old one retained:
    28 orphaned host records appeared within 26 minutes of the rollout. With nowhere to
    write, the id must still come out the same on every call."""
    os.environ["CC_CLIENT_ID_FILE"] = "/proc/cc-cannot-write-here/client_suffix"
    a = publisher._instance_suffix()
    b = publisher._instance_suffix()
    assert a and a == b, "an unpersistable id must still be stable across restarts"


def test_suffix_is_not_derived_from_machine_id():
    """Cloned images share /etc/machine-id, so it cannot be the uniqueness source."""
    try:
        with open("/etc/machine-id") as fh:
            mid = fh.read().strip()
    except (IOError, OSError):
        return                                  # nothing to collide with here
    _fresh_suffix_file()
    assert publisher._instance_suffix() not in (mid, mid[:6], mid[-6:])


def test_state_paths_falls_back_to_a_user_writable_dir():
    """The unprivileged service must have somewhere it can actually write."""
    os.environ.pop("CC_CLIENT_ID_FILE", None)
    paths = cfgmod.state_paths("client_suffix", "CC_CLIENT_ID_FILE")
    assert paths[0].startswith("/var/lib/"), paths
    assert len(paths) > 1 and paths[-1].startswith(os.path.expanduser("~")), paths


def test_env_override_wins_outright():
    os.environ["CC_CLIENT_ID_FILE"] = "/tmp/pinned-suffix"
    assert cfgmod.state_paths("client_suffix", "CC_CLIENT_ID_FILE") == ["/tmp/pinned-suffix"]


def test_machine_suffix_is_stable_and_not_random():
    """MAC-derived, so it survives restarts with no disk at all. None if untrustworthy."""
    got = cfgmod._machine_suffix()
    if got is None:
        return                                  # no stable MAC here; random path covers it
    assert got == cfgmod._machine_suffix()
    assert len(got) == 6


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
