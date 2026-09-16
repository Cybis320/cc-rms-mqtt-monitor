"""A gigabit NIC that has negotiated down to 100 Mb/s must raise nic_link_slow.

A link that auto-negotiates below its rated speed (100 or 10 on a gigabit port,
or half-duplex) is the signature of a damaged cable / bad crimp / flaky switch
port, visible in sysfs long before the error counters show packet loss.

Runs under pytest, or standalone: `python tests/test_nic_link.py`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health, oslevel              # noqa: E402
from cc_mqtt_monitor.config import Thresholds            # noqa: E402

T = Thresholds()


def _host(links):
    return {"nic_link": links, "mem_available_mb": 10000}


def _link(speed, duplex="full", oper="up"):
    return {"speed_mbps": speed, "duplex": duplex, "operstate": oper}


def test_gigabit_link_is_ok():
    status, problems = health.evaluate_host(_host({"eno1": _link(1000)}), T)
    assert status == health.OK and not problems


def test_link_at_100_is_degraded_and_names_the_interface():
    status, problems = health.evaluate_host(_host({"eno1": _link(100)}), T)
    assert status == health.DEGRADED
    assert len(problems) == 1
    assert "eno1 at 100 Mb/s" in problems[0]
    assert "1000" in problems[0]


def test_half_duplex_is_called_out():
    _, problems = health.evaluate_host(_host({"eno1": _link(100, duplex="half")}), T)
    assert "half-duplex" in problems[0]


def test_one_bad_link_on_a_two_nic_box_still_fires():
    _, problems = health.evaluate_host(
        _host({"eno1": _link(1000), "enp2s0": _link(10)}), T)
    assert len(problems) == 1
    assert "enp2s0 at 10 Mb/s" in problems[0]
    assert "eno1" not in problems[0]


def test_wifi_and_down_links_never_fire():
    # Wifi and a wired port with no carrier have no negotiated speed (sysfs EINVAL).
    status, problems = health.evaluate_host(
        _host({"wlan0": _link(None, duplex=None), "eno1": _link(None, duplex=None, oper="down")}), T)
    assert status == health.OK and not problems


def test_absent_field_never_fires():
    status, problems = health.evaluate_host({"mem_available_mb": 10000}, T)
    assert status == health.OK and not problems


def test_threshold_zero_disables_and_fast_ethernet_host_can_lower_it():
    off = Thresholds(nic_link_speed_min_mbps=0)
    assert health.evaluate_host(_host({"eno1": _link(10)}), off) == (health.OK, [])
    pi3 = Thresholds(nic_link_speed_min_mbps=100)
    assert health.evaluate_host(_host({"eth0": _link(100)}), pi3) == (health.OK, [])
    assert health.evaluate_host(_host({"eth0": _link(10)}), pi3)[0] == health.DEGRADED


def test_disabled_check_is_skipped():
    status, problems = health.evaluate_host(_host({"eno1": _link(100)}), T,
                                            disabled={"nic_link_slow"})
    assert status == health.OK and not problems


def _fake_sysfs(tmp, ifaces):
    """Lay out /sys/class/net-style dirs; a None attr is left absent (EINVAL-like)."""
    for name, attrs in ifaces.items():
        d = os.path.join(tmp, name)
        os.makedirs(d)
        for attr, val in attrs.items():
            if val is not None:
                with open(os.path.join(d, attr), "w") as fh:
                    fh.write(val + "\n")


def test_read_nic_link_from_sysfs():
    with tempfile.TemporaryDirectory() as tmp:
        _fake_sysfs(tmp, {
            "eno1": {"speed": "100", "duplex": "full", "operstate": "up"},
            "wlp3s0": {"speed": None, "duplex": None, "operstate": "up"},
            "enp2s0": {"speed": "-1", "duplex": "unknown", "operstate": "down"},
            "lo": {"speed": None, "duplex": None, "operstate": "unknown"},
            "veth1a2b": {"speed": "10000", "duplex": "full", "operstate": "up"},
        })
        links = oslevel.read_nic_link(sysfs=tmp)
        assert links["eno1"] == {"speed_mbps": 100, "duplex": "full", "operstate": "up"}
        assert links["wlp3s0"]["speed_mbps"] is None
        assert links["enp2s0"] == {"speed_mbps": None, "duplex": None, "operstate": "down"}
        assert "lo" not in links and "veth1a2b" not in links
        # Scoped to the camera-facing set: only those names are read.
        scoped = oslevel.read_nic_link(interfaces={"eno1"}, sysfs=tmp)
        assert list(scoped) == ["eno1"]


def test_collect_summary_picks_the_slowest_wired_link(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _fake_sysfs(tmp, {
            "eno1": {"speed": "1000", "duplex": "full", "operstate": "up"},
            "enp2s0": {"speed": "100", "duplex": "half", "operstate": "up"},
            "wlan0": {"speed": None, "duplex": None, "operstate": "up"},
        })
        monkeypatch.setattr(oslevel, "_SYSFS_NET", tmp)
        m = oslevel.collect_nic_link()
        assert m["nic_link_speed_mbps"] == 100
        assert m["nic_link_duplex"] == "half"
        assert set(m["nic_link"]) == {"eno1", "enp2s0", "wlan0"}
        # And the collector output feeds the check directly.
        assert health.evaluate_host(m, T)[0] == health.DEGRADED


def test_unreadable_sysfs_yields_nulls(monkeypatch):
    monkeypatch.setattr(oslevel, "_SYSFS_NET", "/nonexistent/sys/class/net")
    m = oslevel.collect_nic_link()
    assert m == {"nic_link": None, "nic_link_speed_mbps": None, "nic_link_duplex": None}
    assert health.evaluate_host(m, T) == (health.OK, [])


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
