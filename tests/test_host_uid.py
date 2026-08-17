"""host_uid must follow the CONFIGURED host_name, not the machine hostname.

load_config builds Config() first -- so host_name falls back to socket.gethostname() --
and only then applies the YAML, calling __post_init__ a second time. A host_uid computed
on the first pass and preserved on the second freezes to the machine hostname and ignores
the operator's configured name. Seen live: the host renamed to `WSU_Lind` published as
`ProCamSFF14-0a3fe4`, resurrecting the name it had been renamed away from and putting a
private machine name into a public topic.

Runs under pytest, or standalone: `python tests/test_host_uid.py`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import config as cfgmod                # noqa: E402


def _fresh_suffix_file():
    os.environ["CC_CLIENT_ID_FILE"] = os.path.join(tempfile.mkdtemp(), "client_suffix")


def test_uid_follows_a_later_host_name_assignment():
    """Exactly load_config's order: construct, then apply YAML, then re-run post-init."""
    _fresh_suffix_file()
    c = cfgmod.Config()                       # host_name := machine hostname
    machine_uid = c.host_uid
    c.host_name = "WSU_Lind"                  # ...then the YAML value lands
    c.__post_init__()
    assert c.host_uid.startswith("WSU_Lind-"), c.host_uid
    assert c.host_uid != machine_uid, "uid must not stay frozen to the machine hostname"


def test_uid_is_hostname_plus_suffix():
    _fresh_suffix_file()
    c = cfgmod.Config(host_name="raspberrypi")
    suffix = cfgmod._instance_suffix()
    assert c.host_uid == "raspberrypi-%s" % suffix


def test_uid_is_stable_across_repeated_post_init():
    """Re-running post-init (config hot-reload) must not churn the identity."""
    _fresh_suffix_file()
    c = cfgmod.Config(host_name="host-a")
    first = c.host_uid
    c.__post_init__(); c.__post_init__()
    assert c.host_uid == first


def test_two_installs_sharing_a_host_name_differ():
    """Identity is MAC-derived, so a second box means a different MAC, not just a
    different state dir."""
    import uuid as real_uuid

    def uid_for(mac):
        cfgmod.uuid = type("u", (), {"getnode": staticmethod(lambda: mac)})
        _fresh_suffix_file()
        try:
            return cfgmod.Config(host_name="raspberrypi").host_uid
        finally:
            cfgmod.uuid = real_uuid

    a, b = uid_for(0x001122334455), uid_for(0x66778899aabb)
    assert a != b and a.startswith("raspberrypi-") and b.startswith("raspberrypi-")


def test_load_config_applies_yaml_host_name_to_the_uid():
    """End-to-end through the real loader, which is where the ordering bug lived."""
    if cfgmod.yaml is None:
        return                                 # PyYAML absent: the unit cases still cover it
    _fresh_suffix_file()
    path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    with open(path, "w") as fh:
        fh.write("host_name: WSU_Lind\n")
    c = cfgmod.load_config(path)
    assert c.host_name == "WSU_Lind"
    assert c.host_uid.startswith("WSU_Lind-"), c.host_uid


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
