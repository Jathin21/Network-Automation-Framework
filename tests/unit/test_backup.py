"""Tests for the parallel config-backup runner."""

from __future__ import annotations

import json

from netauto.collectors.backup import ConfigBackup
from netauto.inventory.models import Device, Platform


def _device(host: str, site: str = "dc1") -> Device:
    return Device(
        hostname=host, mgmt_ip=f"10.0.0.{abs(hash(host)) % 250 + 1}",
        platform=Platform.CISCO_IOS, site=site,
    )


def test_backup_writes_files_and_manifest(tmp_path, fake_factory):
    registry, factory = fake_factory
    devices = [_device("r1"), _device("r2")]
    for d in devices:
        registry[d.hostname] = type(registry).__class__  # placeholder

    # Pre-populate fake connections with running configs
    from tests.conftest import FakeConnection
    registry["r1"] = FakeConnection(hostname="r1", running="hostname r1\n!\nend")
    registry["r2"] = FakeConnection(hostname="r2", running="hostname r2\n!\nend")

    runner = ConfigBackup(output_dir=tmp_path, factory=factory, max_workers=2)
    run = runner.run(devices)

    assert len(run.results) == 2
    assert all(r.success for r in run.results)

    manifest_path = run.root / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["device_count"] == 2
    assert manifest["succeeded"] == 2
    assert manifest["failed"] == 0

    # Check sharded layout
    cfg_r1 = run.root / "site=dc1" / "r1.cfg"
    assert cfg_r1.is_file()
    assert "hostname r1" in cfg_r1.read_text()
    meta_r1 = run.root / "site=dc1" / "r1.meta.json"
    assert meta_r1.is_file()
    meta = json.loads(meta_r1.read_text())
    assert meta["hostname"] == "r1"
    assert "sha256" in meta


def test_backup_records_failure_for_unreachable(tmp_path, fake_factory):
    from netauto.drivers import SessionError

    devices = [_device("up"), _device("dead")]

    def factory(device):
        if device.hostname == "dead":
            raise SessionError(f"can't reach {device.hostname}")
        from tests.conftest import FakeConnection
        return FakeConnection(hostname=device.hostname, running=f"hostname {device.hostname}\n")

    runner = ConfigBackup(output_dir=tmp_path, factory=factory, max_workers=2)
    run = runner.run(devices)

    by_host = {r.hostname: r for r in run.results}
    assert by_host["up"].success is True
    assert by_host["dead"].success is False
    assert "can't reach" in by_host["dead"].error

    manifest = json.loads((run.root / "manifest.json").read_text())
    assert manifest["succeeded"] == 1
    assert manifest["failed"] == 1


def test_backup_directory_per_site(tmp_path):
    from tests.conftest import FakeConnection
    devices = [_device("r1", site="dc1"), _device("r2", site="dc2")]

    fakes = {d.hostname: FakeConnection(hostname=d.hostname, running=f"hostname {d.hostname}\n")
             for d in devices}

    def factory(device):
        return fakes[device.hostname]

    runner = ConfigBackup(output_dir=tmp_path, factory=factory)
    run = runner.run(devices)
    assert (run.root / "site=dc1" / "r1.cfg").is_file()
    assert (run.root / "site=dc2" / "r2.cfg").is_file()


def test_backup_sha256_stable(tmp_path):
    """Same config produces same digest across runs."""
    from tests.conftest import FakeConnection
    cfg = "hostname r1\nip ssh version 2\n"

    def factory(device):
        return FakeConnection(hostname=device.hostname, running=cfg)

    d = _device("r1")
    sha1 = ConfigBackup(output_dir=tmp_path / "a", factory=factory).run([d]).results[0].sha256
    sha2 = ConfigBackup(output_dir=tmp_path / "b", factory=factory).run([d]).results[0].sha256
    assert sha1 == sha2
    assert len(sha1) == 64
