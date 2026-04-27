"""Integration tests against a real lab.

These are skipped unless ``NETAUTO_LAB_INVENTORY`` points at a YAML inventory
of reachable lab devices. Run them locally with::

    NETAUTO_LAB_INVENTORY=lab/inventory.yml pytest -m integration

Production CI does NOT run these — they require live equipment.
"""

from __future__ import annotations

import os

import pytest

from netauto.collectors.backup import ConfigBackup
from netauto.inventory import load_inventory

pytestmark = pytest.mark.integration


@pytest.fixture
def lab_inventory():
    path = os.environ.get("NETAUTO_LAB_INVENTORY")
    if not path:
        pytest.skip("NETAUTO_LAB_INVENTORY not set; skipping integration tests")
    return load_inventory(path)


def test_backup_against_lab(tmp_path, lab_inventory):
    runner = ConfigBackup(output_dir=tmp_path, max_workers=4)
    run = runner.run(lab_inventory.devices)
    assert run.results, "no results — inventory empty?"
    failures = [r for r in run.results if not r.success]
    assert not failures, f"backup failed for: {[r.hostname for r in failures]}"


def test_audit_against_lab(tmp_path, lab_inventory):
    """Smoke-test: backup + audit returns a well-formed report."""
    from pathlib import Path

    from netauto.validators.compliance import ComplianceAuditor

    rules_path = Path(__file__).parent.parent.parent / "policies" / "baseline.yml"
    auditor = ComplianceAuditor.from_yaml(rules_path)

    runner = ConfigBackup(output_dir=tmp_path, max_workers=4)
    run = runner.run(lab_inventory.devices)

    pairs = []
    for d in lab_inventory.devices:
        cfg_path = run.root / f"site={d.site}" / f"{d.hostname}.cfg"
        if cfg_path.is_file():
            pairs.append((d, cfg_path.read_text()))

    report = auditor.audit(pairs)
    assert report.total_devices == len(pairs)
