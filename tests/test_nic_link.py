"""A NIC that negotiated below what its link partner OFFERED must raise nic_link_slow.

A gigabit switch port advertising 1000baseT while the link came up at 100 Mb/s
(or 10, or half-duplex) is the signature of a damaged cable / bad crimp / flaky
switch port, visible in sysfs + ethtool long before the error counters show
packet loss. But 100 Mb/s is RIGHT when the far end -- a camera on a direct
cable, a Fast-Ethernet switch -- offers no more, so the default "auto" rule
judges each link against the partner's advertisement and needs no topology
knowledge. A numeric threshold is a hard floor instead.

Runs under pytest, or standalone: `python tests/test_nic_link.py`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health, oslevel              # noqa: E402
from cc_mqtt_monitor.config import Thresholds            # noqa: E402

T = Thresholds()           # nic_link_speed_min_mbps == "auto"


def _host(links):
    return {"nic_link": links, "mem_available_mb": 10000}


def _link(speed, duplex="full", oper="up", partner=None, supported=1000, partner_autoneg=True):
    """A wired link as the collector publishes it. `partner` is what the far end
    advertised (None = not readable); expected = min(supported, partner)."""
    link = {"speed_mbps": speed, "duplex": duplex, "operstate": oper}
    if speed is not None:
        link.update({"partner_max_mbps": partner, "supported_max_mbps": supported,
                     "partner_autoneg": partner_autoneg,
                     "expected_mbps": (min(partner, supported)
                                       if partner and supported else None)})
    return link


# --- the auto (partner-based) rule -------------------------------------------

def test_gigabit_link_on_gigabit_partner_is_ok():
    status, problems = health.evaluate_host(_host({"eno1": _link(1000, partner=1000)}), T)
    assert status == health.OK and not problems


def test_100_on_a_gigabit_switch_is_degraded_and_names_the_partner():
    status, problems = health.evaluate_host(_host({"eno1": _link(100, partner=1000)}), T)
    assert status == health.DEGRADED
    assert len(problems) == 1
    assert "eno1 at 100 Mb/s" in problems[0]
    assert "partner advertises 1000" in problems[0]


def test_camera_on_a_direct_cable_negotiates_100_and_is_quiet():
    # A 100 Mb camera (or a Fast-Ethernet switch) offers 100baseT at most.
    status, problems = health.evaluate_host(_host({"eth0": _link(100, partner=100)}), T)
    assert status == health.OK and not problems


def test_fast_ethernet_host_on_a_gigabit_switch_is_quiet():
    # A Pi 3 (100 Mb NIC) on a gigabit switch: expected = min(100, 1000) = 100.
    status, problems = health.evaluate_host(
        _host({"eth0": _link(100, partner=1000, supported=100)}), T)
    assert status == health.OK and not problems


def test_10_on_a_100_partner_still_fires():
    _, problems = health.evaluate_host(_host({"eth0": _link(10, partner=100)}), T)
    assert "eth0 at 10 Mb/s" in problems[0] and "advertises 100" in problems[0]


def test_unreadable_partner_advertisement_is_silent_not_guessed():
    # No ethtool / driver says "Not reported": auto has nothing to judge against.
    status, problems = health.evaluate_host(_host({"eno1": _link(100, partner=None)}), T)
    assert status == health.OK and not problems


def test_half_duplex_always_fires_even_at_the_partner_speed():
    _, problems = health.evaluate_host(_host({"eno1": _link(100, duplex="half", partner=100)}), T)
    assert len(problems) == 1 and "half-duplex" in problems[0]
    # A partner that does NOT auto-negotiate is the forced-port duplex mismatch.
    _, problems = health.evaluate_host(
        _host({"eno1": _link(100, duplex="half", partner=100, partner_autoneg=False)}), T)
    assert "forced port" in problems[0]


def test_one_bad_link_on_a_two_nic_box_still_fires():
    _, problems = health.evaluate_host(
        _host({"eno1": _link(1000, partner=1000), "enp2s0": _link(10, partner=1000)}), T)
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


# --- the numeric floor / off ---------------------------------------------------

def test_numeric_floor_ignores_the_partner_and_zero_disables():
    floor = Thresholds(nic_link_speed_min_mbps=1000)
    # Hard floor: fires even when the partner only offers 100 ...
    _, problems = health.evaluate_host(_host({"eth0": _link(100, partner=100)}), floor)
    assert len(problems) == 1 and "expected >= 1000" in problems[0]
    # ... and when the advertisement is unreadable.
    assert health.evaluate_host(_host({"eth0": _link(100, partner=None)}), floor)[0] == health.DEGRADED
    pi3 = Thresholds(nic_link_speed_min_mbps=100)
    assert health.evaluate_host(_host({"eth0": _link(100, partner=None)}), pi3) == (health.OK, [])
    assert health.evaluate_host(_host({"eth0": _link(10, partner=100)}), pi3)[0] == health.DEGRADED
    for off in (0, "0", None, "off"):
        assert health.evaluate_host(_host({"eno1": _link(10, partner=1000, duplex="half")}),
                                    Thresholds(nic_link_speed_min_mbps=off)) == (health.OK, [])


def test_rule_parsing():
    r = health._link_speed_rule
    assert r("auto") == "auto" and r(" AUTO ") == "auto"
    assert r(1000) == 1000 and r("100") == 100
    assert r(0) is None and r("0") is None and r(None) is None and r("off") is None
    assert r("garbage") == "auto"      # unparseable => the safe rule, not a crash


def test_disabled_check_is_skipped():
    status, problems = health.evaluate_host(_host({"eno1": _link(100, partner=1000)}), T,
                                            disabled={"nic_link_slow"})
    assert status == health.OK and not problems


# --- ethtool parsing ------------------------------------------------------------

ETHTOOL_GIGABIT = """\
netlink error: Operation not permitted
Settings for eno1:
\tSupported ports: [ TP ]
\tSupported link modes:   10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\t                        1000baseT/Full
\tSupported pause frame use: Symmetric Receive-only
\tSupports auto-negotiation: Yes
\tSupported FEC modes: Not reported
\tAdvertised link modes:  10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\t                        1000baseT/Full
\tAdvertised pause frame use: Symmetric Receive-only
\tAdvertised auto-negotiation: Yes
\tAdvertised FEC modes: Not reported
\tLink partner advertised link modes:  10baseT/Half 10baseT/Full
\t                                     100baseT/Half 100baseT/Full
\t                                     1000baseT/Full
\tLink partner advertised pause frame use: Symmetric
\tLink partner advertised auto-negotiation: Yes
\tLink partner advertised FEC modes: Not reported
\tSpeed: 1000Mb/s
\tDuplex: Full
\tAuto-negotiation: on
\tPort: Twisted Pair
\tPHYAD: 1
\tTransceiver: internal
\tMDI-X: off (auto)
        Current message level: 0x00000007 (7)
                               drv probe link
\tLink detected: yes
"""

# A 100 Mb camera plugged straight in: the partner offers 100baseT at most.
ETHTOOL_CAMERA = ETHTOOL_GIGABIT.replace(
    "\tLink partner advertised link modes:  10baseT/Half 10baseT/Full\n"
    "\t                                     100baseT/Half 100baseT/Full\n"
    "\t                                     1000baseT/Full\n",
    "\tLink partner advertised link modes:  10baseT/Half 10baseT/Full\n"
    "\t                                     100baseT/Half 100baseT/Full\n"
).replace("Speed: 1000Mb/s", "Speed: 100Mb/s")

# A driver (some USB adapters) that does not expose the partner's advertisement.
ETHTOOL_NO_PARTNER = ETHTOOL_GIGABIT.replace(
    "\tLink partner advertised link modes:  10baseT/Half 10baseT/Full\n"
    "\t                                     100baseT/Half 100baseT/Full\n"
    "\t                                     1000baseT/Full\n",
    "\tLink partner advertised link modes:  Not reported\n"
).replace("Link partner advertised auto-negotiation: Yes",
          "Link partner advertised auto-negotiation: No")


def test_parse_ethtool_reads_the_multiline_mode_lists():
    adv = oslevel.parse_ethtool(ETHTOOL_GIGABIT)
    assert adv == {"supported_max_mbps": 1000, "advertised_max_mbps": 1000,
                   "partner_max_mbps": 1000, "partner_autoneg": True, "autoneg": True}
    assert oslevel.parse_ethtool(ETHTOOL_CAMERA)["partner_max_mbps"] == 100
    no = oslevel.parse_ethtool(ETHTOOL_NO_PARTNER)
    assert no["partner_max_mbps"] is None and no["partner_autoneg"] is False
    assert no["supported_max_mbps"] == 1000
    assert oslevel.parse_ethtool("")["supported_max_mbps"] is None
    assert oslevel.parse_ethtool(None)["partner_max_mbps"] is None


def _fake_sysfs(tmp, ifaces):
    """Lay out /sys/class/net-style dirs; a None attr is left absent (EINVAL-like)."""
    for name, attrs in ifaces.items():
        d = os.path.join(tmp, name)
        os.makedirs(d)
        for attr, val in attrs.items():
            if val is not None:
                with open(os.path.join(d, attr), "w") as fh:
                    fh.write(val + "\n")


def test_read_nic_link_from_sysfs_and_ethtool():
    asked = []

    def fake_ethtool(iface):
        asked.append(iface)
        return {"eno1": ETHTOOL_GIGABIT, "eth1": ETHTOOL_CAMERA}.get(iface)

    with tempfile.TemporaryDirectory() as tmp:
        _fake_sysfs(tmp, {
            "eno1": {"speed": "100", "duplex": "full", "operstate": "up"},
            "eth1": {"speed": "100", "duplex": "full", "operstate": "up"},
            "wlp3s0": {"speed": None, "duplex": None, "operstate": "up"},
            "enp2s0": {"speed": "-1", "duplex": "unknown", "operstate": "down"},
            "lo": {"speed": None, "duplex": None, "operstate": "unknown"},
            "veth1a2b": {"speed": "10000", "duplex": "full", "operstate": "up"},
        })
        links = oslevel.read_nic_link(sysfs=tmp, ethtool=fake_ethtool)
        # A gigabit partner with the link at 100: expected 1000 => the fault.
        assert links["eno1"] == {"speed_mbps": 100, "duplex": "full", "operstate": "up",
                                 "expected_mbps": 1000, "partner_max_mbps": 1000,
                                 "supported_max_mbps": 1000, "partner_autoneg": True}
        # A 100 Mb camera on a direct cable: expected 100 => fine.
        assert links["eth1"]["expected_mbps"] == 100
        assert links["wlp3s0"]["speed_mbps"] is None and "expected_mbps" not in links["wlp3s0"]
        assert links["enp2s0"] == {"speed_mbps": None, "duplex": None, "operstate": "down"}
        assert "lo" not in links and "veth1a2b" not in links
        # Only links WITH a negotiated speed cost an ethtool call.
        assert sorted(asked) == ["eno1", "eth1"]
        # Scoped to the camera-facing set: only those names are read.
        scoped = oslevel.read_nic_link(interfaces={"eno1"}, sysfs=tmp, ethtool=fake_ethtool)
        assert list(scoped) == ["eno1"]
        # The collector output feeds the check directly: eno1 fires, eth1 does not.
        status, problems = health.evaluate_host(_host(links), T)
        assert status == health.DEGRADED and len(problems) == 1
        assert "eno1 at 100 Mb/s" in problems[0] and "eth1" not in problems[0]


def test_collect_summary_picks_the_slowest_wired_link_and_notes_unreadable(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _fake_sysfs(tmp, {
            "eno1": {"speed": "1000", "duplex": "full", "operstate": "up"},
            "enp2s0": {"speed": "100", "duplex": "half", "operstate": "up"},
            "wlan0": {"speed": None, "duplex": None, "operstate": "up"},
        })
        monkeypatch.setattr(oslevel, "_SYSFS_NET", tmp)
        monkeypatch.setattr(oslevel, "_run_ethtool",
                            lambda iface: {"eno1": ETHTOOL_GIGABIT}.get(iface))
        m = oslevel.collect_nic_link()
        assert m["nic_link_speed_mbps"] == 100
        assert m["nic_link_duplex"] == "half"
        assert set(m["nic_link"]) == {"eno1", "enp2s0", "wlan0"}
        assert m["nic_link"]["eno1"]["expected_mbps"] == 1000
        assert m["nic_link"]["enp2s0"]["expected_mbps"] is None
        assert "enp2s0" in m["nic_link_note"] and "eno1" not in m["nic_link_note"]
        # enp2s0's speed can't be judged (no advertisement) but half duplex still fires.
        status, problems = health.evaluate_host(m, T)
        assert status == health.DEGRADED and "half-duplex" in problems[0]


def test_no_ethtool_at_all_is_silent(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _fake_sysfs(tmp, {"eno1": {"speed": "100", "duplex": "full", "operstate": "up"}})
        monkeypatch.setattr(oslevel, "_SYSFS_NET", tmp)
        monkeypatch.setattr(oslevel, "_ETHTOOL_PATHS", ("/nonexistent/ethtool",))
        m = oslevel.collect_nic_link()
        assert m["nic_link"]["eno1"]["expected_mbps"] is None
        assert "ethtool missing" in m["nic_link_note"]
        assert health.evaluate_host(m, T) == (health.OK, [])


def test_unreadable_sysfs_yields_nulls(monkeypatch):
    monkeypatch.setattr(oslevel, "_SYSFS_NET", "/nonexistent/sys/class/net")
    m = oslevel.collect_nic_link()
    assert m == {"nic_link": None, "nic_link_speed_mbps": None, "nic_link_duplex": None}
    assert health.evaluate_host(m, T) == (health.OK, [])


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
