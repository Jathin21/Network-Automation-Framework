# Architecture

This document explains the design choices behind netauto and the trade-offs
that aren't obvious from reading code.

## Design principles

1. **Fail fast at load time, not at 3am.** Inventory and policy files are
   strictly validated by pydantic. A typo in a regex or an unknown platform
   becomes a CLI error, not a confused device session two hours into a
   maintenance window.

2. **Driver-agnostic core.** Every operation that talks to a device goes
   through the `DeviceConnection` Protocol. Production code uses Netmiko
   under the hood, but tests inject `FakeConnection` objects with scripted
   responses. No mock libraries, no network in unit tests.

3. **Dry-run by default.** `provision` renders to stdout unless `--apply`
   is passed. `audit` reads from on-disk backups, never live devices.
   `backup` is the only operation that contacts devices without an explicit
   action flag, and it's read-only.

4. **Structured outputs everywhere.** Backup manifests, audit reports, and
   change-validation results all have a `to_dict()` method that's
   JSON-serializable so downstream systems (ServiceNow, Datadog, PagerDuty)
   can ingest without scraping logs.

5. **Concurrency at the framework level.** Per-device operations run in a
   `ThreadPoolExecutor` capped at 16 workers by default. AAA servers and
   small device control planes don't appreciate 200 simultaneous SSH
   sessions; the cap is configurable via `--max-workers`.

## Why both Netmiko and NAPALM

NAPALM is the "right answer" when its getters are well-supported on your
target platform — it returns structured data, no parsing. But coverage gaps
are real (NAPALM's `iosxr` driver, for instance, has historically lagged on
newer XR features), and some operators run versions older than NAPALM
supports.

The framework uses Netmiko (CLI scrape) as the universal substrate and
treats NAPALM's structured getters as an optimization where available.
That trade-off keeps the framework working against the long tail of vendor
software versions that production fleets actually run.

## Why a Protocol instead of an ABC

```python
class DeviceConnection(Protocol):
    def send_command(self, command: str) -> str: ...
    def send_config(self, commands: list[str]) -> str: ...
```

Two reasons:

1. **Real Netmiko / NAPALM connections satisfy this Protocol structurally.**
   Nobody has to inherit from a netauto base class — if your custom driver
   has these methods, it works.

2. **Test fakes are trivial.** `tests/conftest.FakeConnection` is ~30 lines
   and does not import Netmiko. CI doesn't even need Netmiko's transitive
   dependency tree to run unit tests.

## Concurrency model

```
backup / audit / provision (apply)
  │
  ▼
ThreadPoolExecutor(max_workers=16)
  │
  ├─► open_session(device1) ─► retry(3, exp backoff) ─► fake|netmiko
  ├─► open_session(device2) ─► retry(3, exp backoff) ─► fake|netmiko
  └─► open_session(device3) ─► retry(3, exp backoff) ─► fake|netmiko
```

We use threads, not asyncio. Netmiko is sync and runs the SSH session in
its own thread; using async wouldn't actually parallelize the I/O without
adopting `asyncssh` directly. Threads are also conceptually simpler to
reason about during a postmortem, which is when this code matters most.

## Compliance rule engine

Rules are deliberately *simple*: a list of regexes that must match plus a
list that must not match. Two reasons we didn't go further:

1. **A more powerful engine (e.g. Batfish, NetCfgBu) is the right tool for
   network-wide reasoning** — reachability, ACL conflict analysis, route
   leak detection. netauto is for the per-device, per-rule "is the SSH
   server v2-only?" class of question.

2. **Network engineers can read regex.** A YAML rule file is debuggable by
   the people who maintain it without learning a DSL.

## Change validation: why parse text?

The natural objection: NAPALM has `get_bgp_neighbors_detail()`. Why parse
`show ip bgp summary` by hand?

- **NAPALM coverage is uneven across platforms.** Junos returns rich data;
  IOS often returns less.
- **The parsers we ship match what's in front of operators today.** When an
  RCA quotes the BGP summary table, that table is what we want our diff to
  reference.
- **Plain-text snapshots are diff-able and pasteable.** They land in MOPs
  and tickets unmodified.

The parsers are intentionally narrow: each one extracts the few fields the
validator actually compares (peer state, adjacency state, line/proto, RIB
size). Vendor-format drift in fields we don't care about doesn't break
validation.

## What netauto explicitly does *not* do

- **Topology discovery.** Inventory is supplied, not discovered. Discovery
  is a separate concern with its own tooling (LLDP harvesting, IPAM
  integration).
- **NetBox / IPAM integration.** Out of scope. The inventory loader is
  small enough that an IPAM-backed adapter is straightforward to add.
- **State management.** netauto is stateless across runs. Each backup is a
  full snapshot, each audit reads from on-disk backups. There is no central
  database.
- **Streaming telemetry.** netauto runs on demand. For continuous
  monitoring, ship the same configs into your existing Prometheus / Datadog
  / SNMP stack.
