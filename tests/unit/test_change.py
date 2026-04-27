"""Tests for the pre/post change validator."""

from __future__ import annotations

from netauto.validators.change import (
    ChangeValidator,
    ChangeWindow,
    HealthSnapshot,
    _parse_bgp,
    _parse_interfaces,
    _parse_isis,
    _parse_rib,
)

# ----------------------------------------------------- parser tests -----------


def test_parse_bgp_summary_cisco():
    output = """
BGP router identifier 10.0.0.1, local AS number 65000
Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
10.1.1.1        4 65001  100      99      1234     0    0 1d04h    Established
10.1.1.2        4 65002   50      48       100     0    0 00:00:30 Idle
"""
    parsed = _parse_bgp(output)
    assert parsed["10.1.1.1"] == "Established"
    assert parsed["10.1.1.2"] == "Idle"


def test_parse_isis_neighbors():
    output = """
System Id      Type Interface           IP Address      State Holdtime Circuit Id
core-01        L2   GigabitEthernet0/1  10.1.1.2        Up    27       0x01
core-02        L2   GigabitEthernet0/2  10.1.1.6        Init  29       0x01
core-03        L2   GigabitEthernet0/3  10.1.1.10       Down  0        0x01
"""
    parsed = _parse_isis(output)
    assert parsed["core-01"] == "Up"
    assert parsed["core-02"] == "Init"
    assert parsed["core-03"] == "Down"


def test_parse_interfaces_brief():
    output = """
Interface            IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0   10.0.0.1        YES NVRAM  up                    up
GigabitEthernet0/1   unassigned      YES NVRAM  administratively down down
GigabitEthernet0/2   10.0.0.5        YES NVRAM  down                  down
"""
    parsed = _parse_interfaces(output)
    assert parsed["GigabitEthernet0/0"] == "up/up"
    assert parsed["GigabitEthernet0/1"] == "admin-down"
    assert parsed["GigabitEthernet0/2"] == "down/down"


def test_parse_rib_with_total_line():
    output = "IP routing table summary\nTotal number of routes: 4521\n"
    assert _parse_rib(output) == 4521


def test_parse_rib_fallback_counts_route_codes():
    output = (
        "B    10.1.0.0/16 [200/0] via 10.0.0.1\n"
        "O    10.2.0.0/16 [110/2] via 10.0.0.2\n"
        "S    0.0.0.0/0 [1/0] via 10.0.0.254\n"
    )
    assert _parse_rib(output) == 3


# ----------------------------------------------------- compare() tests --------


def _snap(**kwargs) -> HealthSnapshot:
    base = {
        "hostname": "r1",
        "bgp_neighbors": {},
        "isis_adjacencies": {},
        "interfaces": {},
        "rib_size": 1000,
    }
    base.update(kwargs)
    return HealthSnapshot(**base)


def test_compare_passes_when_nothing_changed():
    before = _snap(
        bgp_neighbors={"10.1.1.1": "Established"},
        interfaces={"Eth1": "up/up"},
    )
    after = _snap(
        bgp_neighbors={"10.1.1.1": "Established"},
        interfaces={"Eth1": "up/up"},
    )
    v = ChangeValidator()
    result = v.compare(before, after, ChangeWindow(name="t"))
    assert result.passed
    assert result.issues == []


def test_compare_flags_bgp_regression():
    before = _snap(bgp_neighbors={"10.1.1.1": "Established"})
    after = _snap(bgp_neighbors={"10.1.1.1": "Idle"})
    result = ChangeValidator().compare(before, after, ChangeWindow(name="t"))
    assert not result.passed
    assert any("10.1.1.1" in i for i in result.issues)


def test_compare_flags_disappeared_bgp_peer():
    before = _snap(bgp_neighbors={"10.1.1.1": "Established"})
    after = _snap(bgp_neighbors={})
    result = ChangeValidator().compare(before, after, ChangeWindow(name="t"))
    assert not result.passed
    assert any("disappeared" in i for i in result.issues)


def test_compare_ignores_bgp_peer_that_was_already_down():
    before = _snap(bgp_neighbors={"10.1.1.1": "Idle"})
    after = _snap(bgp_neighbors={"10.1.1.1": "Idle"})
    result = ChangeValidator().compare(before, after, ChangeWindow(name="t"))
    assert result.passed


def test_compare_flags_isis_adjacency_loss():
    before = _snap(isis_adjacencies={"core-01": "Up"})
    after = _snap(isis_adjacencies={"core-01": "Down"})
    result = ChangeValidator().compare(before, after, ChangeWindow(name="t"))
    assert not result.passed


def test_compare_flags_interface_regression():
    before = _snap(interfaces={"Eth1": "up/up", "Eth2": "up/up"})
    after = _snap(interfaces={"Eth1": "up/up", "Eth2": "down/down"})
    result = ChangeValidator().compare(before, after, ChangeWindow(name="t"))
    assert not result.passed
    assert any("Eth2" in i for i in result.issues)


def test_compare_respects_allow_down():
    before = _snap(interfaces={"Eth1": "up/up"})
    after = _snap(interfaces={"Eth1": "down/down"})
    window = ChangeWindow(name="planned-eth1-cutover", allow_down=["Eth1"])
    result = ChangeValidator().compare(before, after, window)
    assert result.passed


def test_compare_rib_shrink_within_tolerance():
    before = _snap(rib_size=10000)
    after = _snap(rib_size=9950)  # 0.5% shrink
    window = ChangeWindow(name="t", rib_shrink_tolerance_pct=1.0)
    result = ChangeValidator().compare(before, after, window)
    assert result.passed


def test_compare_rib_shrink_beyond_tolerance():
    before = _snap(rib_size=10000)
    after = _snap(rib_size=8500)  # 15% shrink
    window = ChangeWindow(name="t", rib_shrink_tolerance_pct=1.0)
    result = ChangeValidator().compare(before, after, window)
    assert not result.passed
    assert any("RIB shrunk" in i for i in result.issues)


def test_snapshot_to_dict_round_trip():
    s = _snap(
        bgp_neighbors={"10.1.1.1": "Established"},
        interfaces={"Eth1": "up/up"},
    )
    d = s.to_dict()
    assert d["bgp_neighbors"]["10.1.1.1"] == "Established"
    assert d["rib_size"] == 1000
