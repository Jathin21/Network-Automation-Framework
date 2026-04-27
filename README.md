# netauto — Network Automation Framework

[![CI](https://github.com/Jathin21/Network-Automation-Framework/actions/workflows/ci.yml/badge.svg)](https://github.com/Jathin21/Network-Automation-Framework/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Coverage](https://img.shields.io/badge/coverage-93%25-brightgreen)]()

Multi-vendor network automation for **config backup**, **compliance auditing**,
**bulk provisioning**, and **pre/post-change validation** across Cisco IOS / IOS-XR /
NX-OS, Juniper Junos, and Arista EOS.

Built to replace the manual workflows that dominate large-fleet operations:
weekly snapshots taken by hand, ACL changes typed into 200 switches one at a
time, and 3am MOPs where nobody remembers what the BGP table looked like
*before*.

---

## What it does

| Capability             | Module                            | CLI                |
| ---------------------- | --------------------------------- | ------------------ |
| Parallel config backup | `netauto.collectors.backup`       | `netauto backup`   |
| Declarative compliance | `netauto.validators.compliance`   | `netauto audit`    |
| Pre/post change checks | `netauto.validators.change`       | (library)          |
| Templated provisioning | `netauto.provision`               | `netauto provision`|
| Inventory + filtering  | `netauto.inventory`               | `netauto inventory show` |

Built on **Netmiko** (CLI scrape, lowest common denominator across vendor/version)
and **NAPALM** (vendor-agnostic getters where well-supported), with **Ansible**
playbooks for the cases where idempotent vendor modules are the better tool.

## Why it exists

In a previous role I owned BGP/ISIS/MPLS deployment and support for ~50
production sites. Three patterns ate the team's time:

1. **Weekly config snapshots** taken by hand, then archived inconsistently.
2. **ACL / VLAN changes** that touched 200+ switches and ate 4 hours per site.
3. **Change-window verification** — comparing `show ip bgp summary` and
   `show ip route summary` outputs by eyeball, before and after.

This framework is the consolidation of the scripts that solved those three.
After it landed, weekly automation effort dropped from ~18 engineer-hours to
under 2; per-site VLAN provisioning dropped from ~4 hours to ~12 minutes; and
change-window verification time dropped roughly two-thirds.

## Install

```bash
git clone https://github.com/Jathin21/Network-Automation-Framework
cd network-automation-framework
pip install -e ".[dev]"
```

Python 3.10+ is required.

## Quickstart

### 1. Define your inventory

```yaml
# inventory.yml
devices:
  - hostname: hyd-edge-01
    mgmt_ip: 10.10.1.11
    platform: cisco_ios
    site: hyd-dc-1
    role: edge
    tags: [bgp, isis, mpls, prod]
    username: ${NETAUTO_USER:netauto}
    password: ${NETAUTO_PASS:}
```

`${VAR:default}` is expanded from the environment, so passwords stay out of git.

```bash
export NETAUTO_USER=netauto
export NETAUTO_PASS=$(vault read -field=password netauto/prod)
```

### 2. Backup

```bash
netauto backup -i inventory.yml --site hyd-dc-1
```

Produces a timestamped tree:

```
configs/backups/2024-09-15T03-00-00Z/
├── manifest.json                    # SHA-256 + size + duration per device
├── site=hyd-dc-1/
│   ├── hyd-edge-01.cfg
│   ├── hyd-edge-01.meta.json
│   └── ...
```

### 3. Audit against a policy

```bash
netauto audit -i inventory.yml -r policies/baseline.yml
```

Policies are declarative YAML:

```yaml
- id: SEC-004
  title: SNMPv1/v2c communities forbidden
  severity: critical
  must_not_match:
    - "^snmp-server community .* (RO|RW)$"
```

Returns a non-zero exit code when any rule fails — wire into CI to gate config
changes.

### 4. Provision in bulk

Dry-run first (default — no device contact):

```bash
netauto provision -i inventory.yml -t vlan.j2 \
  --vars vlan_id=200 --vars vlan_name=PROD_DB \
  --site hyd-dc-1
```

When rendered output looks right, push:

```bash
netauto provision -i inventory.yml -t vlan.j2 \
  --vars vlan_id=200 --vars vlan_name=PROD_DB \
  --site hyd-dc-1 --apply
```

Each push: snapshot → apply → verify → rollback on verification failure.

### 5. Pre/post change validation (library)

```python
from netauto.inventory import load_inventory
from netauto.validators.change import ChangeValidator, ChangeWindow

inv = load_inventory("inventory.yml")
validator = ChangeValidator()

before = validator.snapshot(inv.get("hyd-edge-01"))
# ... maintenance happens ...
after = validator.snapshot(inv.get("hyd-edge-01"))

window = ChangeWindow(name="CR-12345", allow_down=["GigabitEthernet0/2"])
result = validator.compare(before, after, window)

if not result.passed:
    for issue in result.issues:
        print(f"REGRESSION: {issue}")
    raise SystemExit(1)
```

Detects:
- BGP neighbors that left the Established state
- ISIS adjacencies that fell from Up
- Interfaces that went from `up/up` to anything else (with an explicit allow-list)
- RIB shrinkage beyond a configurable percentage tolerance

## Ansible

For idempotent vendor-module operations:

```bash
cd ansible
ansible-playbook -i inventory/hosts.yml playbooks/provision_vlan.yml \
  -e "vlan_id=200 vlan_name=PROD_DB trunk_intfs=Ethernet1,Ethernet2"
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                          CLI (click + rich)                       │
└──────┬─────────────┬──────────────┬─────────────┬─────────────────┘
       │             │              │             │
   inventory     collectors     validators    provision
   (pydantic)    (backup)       (compliance,  (jinja2 +
       │                         change)       transactional push)
       │                                            │
       └────────────────────┬───────────────────────┘
                            │
                       drivers (Protocol)
                            │
            ┌───────────────┴────────────────┐
        Netmiko (real)                  FakeConnection (tests)
```

The `DeviceConnection` Protocol is what makes the framework testable: every
operation accepts an optional `factory` that yields connections, so unit tests
inject in-memory fakes and never touch a network.

## Testing

```bash
pytest tests/unit                              # unit tests, no devices
pytest -m integration                          # requires NETAUTO_LAB_INVENTORY
pytest --cov=netauto --cov-report=term         # with coverage
```

Current coverage: **93%** across 65 tests.

## Development

```bash
pip install -e ".[dev]"
ruff check netauto tests
mypy netauto
pytest tests/unit
```

CI (GitHub Actions) runs lint + tests on Python 3.10 / 3.11 / 3.12 and syntax-
checks every Ansible playbook on every push.

## Project layout

```
netauto/
├── inventory/          # pydantic models + YAML loader (env-var expansion)
├── collectors/         # backup runner (parallel, manifest, sha256)
├── validators/
│   ├── compliance.py   # declarative rule engine
│   └── change.py       # pre/post snapshot diff (BGP, ISIS, intf, RIB)
├── provision.py        # Jinja2 + transactional push w/ rollback
├── drivers.py          # Protocol + Netmiko impl + retry
├── utils/              # logging, diff
└── cli/                # click subcommands
ansible/                # playbooks/ + roles/ for idempotent vendor modules
policies/baseline.yml   # default compliance ruleset
inventory.yml           # sample multi-site, multi-vendor inventory
tests/                  # unit/ (65 tests) + integration/ (opt-in)
```

## License

MIT — see [LICENSE](LICENSE).
