"""Pre/post-change validator.

Captures a structured "health snapshot" of a device before a maintenance window
and again after, then diffs the two. The diff is human-readable enough to paste
into an MOP / RFE follow-up and machine-readable enough to gate automation.

A snapshot includes:
  * BGP neighbor states (peer -> Established | Idle | etc.)
  * ISIS adjacency states (neighbor -> Up | Init | Down)
  * Interface line/protocol status (interface -> up/up | down/down | admin-down)
  * Routing table size (counts only — full RIB is too noisy and too large)

The validator compares each section. A change-window is considered *successful*
when:
  * No interface that was up/up has gone down (excluding interfaces explicitly
    listed in the change window's ``allow_down`` field).
  * No BGP / ISIS adjacency that was Established / Up has flapped down.
  * Total RIB size has not shrunk by more than ``rib_shrink_tolerance_pct``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from netauto.drivers import ConnectionFactory, open_session
from netauto.inventory.models import Device, Platform
from netauto.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class HealthSnapshot:
    """Structured device health at a single point in time."""

    hostname: str
    bgp_neighbors: dict[str, str] = field(default_factory=dict)
    isis_adjacencies: dict[str, str] = field(default_factory=dict)
    interfaces: dict[str, str] = field(default_factory=dict)
    rib_size: int = 0

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "bgp_neighbors": dict(self.bgp_neighbors),
            "isis_adjacencies": dict(self.isis_adjacencies),
            "interfaces": dict(self.interfaces),
            "rib_size": self.rib_size,
        }


@dataclass(slots=True)
class ChangeWindow:
    """Configuration for the validator on a particular change."""

    name: str
    allow_down: list[str] = field(default_factory=list)
    rib_shrink_tolerance_pct: float = 1.0  # default: 1% RIB shrinkage tolerated


@dataclass(slots=True)
class ValidationResult:
    hostname: str
    passed: bool
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------- parsing -----------


# BGP summary line: <peer> <V> <AS> <MsgRcvd> <MsgSent> <TblVer> <InQ> <OutQ> <Up/Down> <State/PfxRcd>
# So after the IP we expect 7 numeric columns, then a non-numeric uptime, then state.
_BGP_LINE = re.compile(
    r"^\s*(?P<peer>\d+\.\d+\.\d+\.\d+)"
    r"\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+"  # V, AS, 5 counters
    r"\s+\S+\s+(?P<state>\S+)\s*$",                 # uptime, state
    re.MULTILINE,
)
# ISIS neighbor line: <sys-id> <type> <interface> <ip> <state> ...
_ISIS_LINE = re.compile(
    r"^\s*(?P<sys>\S+)\s+\S+\s+\S+\s+\S+\s+(?P<state>Up|Init|Down|Failed)\b",
    re.MULTILINE | re.IGNORECASE,
)
_INTF_LINE = re.compile(
    r"^(?P<intf>\S+)\s+(?:\S+\s+)?(?:YES|NO|unassigned|\d+\.\d+\.\d+\.\d+)?.*?"
    r"(?P<line>up|down|administratively down)\s+(?P<proto>up|down)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_RIB_COUNT = re.compile(r"^\s*Total\s+number\s+of\s+(?:routes|prefixes):\s*(\d+)", re.MULTILINE)


def _parse_bgp(output: str) -> dict[str, str]:
    return {m.group("peer"): m.group("state") for m in _BGP_LINE.finditer(output)}


def _parse_isis(output: str) -> dict[str, str]:
    return {m.group("sys"): m.group("state").capitalize() for m in _ISIS_LINE.finditer(output)}


def _parse_interfaces(output: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _INTF_LINE.finditer(output):
        line = m.group("line").lower()
        proto = m.group("proto").lower()
        status = "admin-down" if "administratively" in line else f"{line}/{proto}"
        out[m.group("intf")] = status
    return out


def _parse_rib(output: str) -> int:
    m = _RIB_COUNT.search(output)
    if m:
        return int(m.group(1))
    # Fallback: count "code" lines that begin with a routing protocol letter.
    return sum(1 for ln in output.splitlines() if re.match(r"^[BOSCDLR\*]\s", ln))


# ---------------------------------------------------------- validator ---------


class ChangeValidator:
    """Run pre / post snapshots and compare them."""

    # Per-platform commands. Where vendors disagree we use the closest equivalent.
    _COMMANDS = {
        "bgp": {
            Platform.CISCO_IOS: "show ip bgp summary",
            Platform.CISCO_NXOS: "show ip bgp summary",
            Platform.CISCO_XR: "show bgp summary",
            Platform.JUNIPER_JUNOS: "show bgp summary",
            Platform.ARISTA_EOS: "show ip bgp summary",
        },
        "isis": {
            Platform.CISCO_IOS: "show isis neighbors",
            Platform.CISCO_NXOS: "show isis adjacency",
            Platform.CISCO_XR: "show isis adjacency",
            Platform.JUNIPER_JUNOS: "show isis adjacency",
            Platform.ARISTA_EOS: "show isis neighbors",
        },
        "interfaces": {
            Platform.CISCO_IOS: "show ip interface brief",
            Platform.CISCO_NXOS: "show interface brief",
            Platform.CISCO_XR: "show ipv4 interface brief",
            Platform.JUNIPER_JUNOS: "show interfaces terse",
            Platform.ARISTA_EOS: "show ip interface brief",
        },
        "rib": {
            Platform.CISCO_IOS: "show ip route summary",
            Platform.CISCO_NXOS: "show ip route summary",
            Platform.CISCO_XR: "show route summary",
            Platform.JUNIPER_JUNOS: "show route summary",
            Platform.ARISTA_EOS: "show ip route summary",
        },
    }

    def __init__(self, factory: ConnectionFactory | None = None) -> None:
        self._factory = factory

    def snapshot(self, device: Device) -> HealthSnapshot:
        """Capture a single device's health snapshot."""
        with open_session(device, factory=self._factory) as conn:
            bgp_out = conn.send_command(self._COMMANDS["bgp"][device.platform])
            isis_out = conn.send_command(self._COMMANDS["isis"][device.platform])
            intf_out = conn.send_command(self._COMMANDS["interfaces"][device.platform])
            rib_out = conn.send_command(self._COMMANDS["rib"][device.platform])

        return HealthSnapshot(
            hostname=device.hostname,
            bgp_neighbors=_parse_bgp(bgp_out),
            isis_adjacencies=_parse_isis(isis_out),
            interfaces=_parse_interfaces(intf_out),
            rib_size=_parse_rib(rib_out),
        )

    def snapshot_many(self, devices: Iterable[Device]) -> dict[str, HealthSnapshot]:
        """Snapshot a batch of devices, keyed by hostname."""
        return {d.hostname: self.snapshot(d) for d in devices}

    def compare(
        self,
        before: HealthSnapshot,
        after: HealthSnapshot,
        window: ChangeWindow,
    ) -> ValidationResult:
        """Compare pre/post snapshots and produce a pass/fail with reasons."""
        issues: list[str] = []
        allow_down = set(window.allow_down)

        # BGP: any neighbor that was Established but isn't anymore is a regression.
        for peer, prev_state in before.bgp_neighbors.items():
            if prev_state.lower() != "established":
                continue
            curr_state = after.bgp_neighbors.get(peer)
            if curr_state is None:
                issues.append(f"BGP peer {peer} disappeared (was Established)")
            elif curr_state.lower() != "established":
                issues.append(f"BGP peer {peer} {prev_state} -> {curr_state}")

        # ISIS: any adjacency that was Up but isn't anymore is a regression.
        for sys_id, prev_state in before.isis_adjacencies.items():
            if prev_state.lower() != "up":
                continue
            curr_state = after.isis_adjacencies.get(sys_id)
            if curr_state is None:
                issues.append(f"ISIS adjacency to {sys_id} disappeared (was Up)")
            elif curr_state.lower() != "up":
                issues.append(f"ISIS adjacency to {sys_id} {prev_state} -> {curr_state}")

        # Interfaces: anything that was up/up and is now anything else is a regression,
        # unless explicitly allowed by the change window.
        for intf, prev in before.interfaces.items():
            if prev != "up/up" or intf in allow_down:
                continue
            curr = after.interfaces.get(intf, "missing")
            if curr != "up/up":
                issues.append(f"interface {intf} {prev} -> {curr}")

        # RIB shrinkage tolerance: relative shrinkage beyond threshold is a regression.
        if before.rib_size > 0:
            shrink_pct = (before.rib_size - after.rib_size) / before.rib_size * 100
            if shrink_pct > window.rib_shrink_tolerance_pct:
                issues.append(
                    f"RIB shrunk {shrink_pct:.2f}% "
                    f"(before={before.rib_size}, after={after.rib_size}, "
                    f"tolerance={window.rib_shrink_tolerance_pct}%)"
                )

        log.info(
            "change_compare",
            host=before.hostname,
            window=window.name,
            issues=len(issues),
        )
        return ValidationResult(
            hostname=before.hostname,
            passed=not issues,
            issues=issues,
        )
