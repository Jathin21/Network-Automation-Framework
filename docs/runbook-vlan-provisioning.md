# Runbook: VLAN Provisioning at Scale

**Audience:** NOC / deployment engineers
**Estimated time:** 12 minutes per site (was 4 hours pre-automation)
**Risk level:** Low (dry-run + per-device verify + rollback on failure)

This runbook covers adding a new VLAN to every access switch at a site,
including trunk membership.

## Prerequisites

- `inventory.yml` covers the target site
- TACACS credentials in environment: `NETAUTO_USER`, `NETAUTO_PASS`
- Change ticket open (CR-XXXXX)

## Step 1: Pre-change snapshot

Capture a full health snapshot of every device that will be touched. This
is the "before" half of the change-window comparison.

```bash
mkdir -p /tmp/cr-12345
netauto backup -i inventory.yml --site hyd-dc-1 -o /tmp/cr-12345/before
```

Verify all devices succeeded:

```bash
jq '.failed' /tmp/cr-12345/before/*/manifest.json
# Expect: 0
```

If any device failed, **stop here**. Investigate (likely a credentials or
reachability issue) before continuing.

## Step 2: Dry-run the provisioning

```bash
netauto provision \
    -i inventory.yml \
    --site hyd-dc-1 --role access \
    -t vlan.j2 \
    --vars vlan_id=247 \
    --vars vlan_name=VOICE_PROD \
    --vars 'trunk_intfs=Ethernet1,Ethernet2,Ethernet3'
```

This renders the template per device and prints the resulting config to
stdout — no device contact. Skim it. Confirm:

- Each device's output is sensible for that platform.
- No `! unsupported platform: ...` warnings (means a vendor in the inventory
  has no template branch).
- VLAN ID and name are correct.

## Step 3: Apply

```bash
netauto provision \
    -i inventory.yml \
    --site hyd-dc-1 --role access \
    -t vlan.j2 \
    --vars vlan_id=247 \
    --vars vlan_name=VOICE_PROD \
    --vars 'trunk_intfs=Ethernet1,Ethernet2,Ethernet3' \
    --apply
```

For each device the framework will:

1. Snapshot its running-config.
2. Push the rendered candidate.
3. Re-read the running-config and verify every pushed line is present.
4. **If verification fails**: best-effort rollback by replaying the
   pre-change snapshot. The result is reported as failed with
   `rolled_back=True`.

## Step 4: Post-change snapshot + diff

```bash
netauto backup -i inventory.yml --site hyd-dc-1 -o /tmp/cr-12345/after
```

Compare to the pre-change snapshot. The auditor's noise filter strips
timestamps, so any non-empty diff represents a real change:

```bash
for host in $(ls /tmp/cr-12345/before/*/site=hyd-dc-1/ | grep .cfg); do
    diff /tmp/cr-12345/before/*/site=hyd-dc-1/$host \
         /tmp/cr-12345/after/*/site=hyd-dc-1/$host
done
```

You should see `vlan 247` and the trunk membership lines added on every
target device. Anything else is suspicious — investigate before closing the
ticket.

## Step 5: Compliance check

```bash
netauto audit -i inventory.yml -r policies/baseline.yml -c /tmp/cr-12345/after
```

Exit code 0 means every applicable rule passed. Non-zero means the change
introduced (or exposed) a compliance regression. Don't close the ticket on
non-zero — open a follow-up.

## Rollback

If anything goes wrong post-apply, the per-device pre-change snapshots
under `/tmp/cr-12345/before/.../*.cfg` are full running-configs and can be
re-applied by hand or via:

```bash
# Per device
netauto provision -i inventory.yml --tag <host> -t restore.j2 \
    --vars "config_path=/tmp/cr-12345/before/.../host.cfg" --apply
```

(Note: full-config replace is best done with vendor-native config-replace
where available — `configure replace` on IOS-XE, `commit confirmed` on
Junos. The script-based push is a fallback.)

## When to escalate

- Verification failure on >5% of devices in any single batch
- Any rollback reporting a `rollback_failed` log line
- BGP / ISIS adjacency loss reported by a follow-on `ChangeValidator.compare`
- Any unexplained line in the post-change diff

Page the on-call and reference this runbook + the change ticket.
