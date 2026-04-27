"""Tests for the Jinja2-based provisioner."""

from __future__ import annotations

import pytest

from netauto.inventory.models import Device, Platform
from netauto.provision import Provisioner


@pytest.fixture
def template_dir(tmp_path):
    (tmp_path / "vlan.j2").write_text(
        "vlan {{ vlan_id }}\n"
        " name {{ vlan_name }}\n"
    )
    (tmp_path / "platform_aware.j2").write_text(
        "{% if device.platform == 'cisco_ios' %}\n"
        "vlan {{ vlan_id }}\n"
        "{% else %}\n"
        "set vlans v{{ vlan_id }} vlan-id {{ vlan_id }}\n"
        "{% endif %}\n"
    )
    (tmp_path / "broken.j2").write_text("{{ undefined_var }}\n")
    return tmp_path


def test_dry_run_renders_without_session(template_dir):
    p = Provisioner(template_dir)
    d = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
    summary = p.apply("vlan.j2", [d], dry_run=True, extra_vars={"vlan_id": 100, "vlan_name": "PROD"})
    assert len(summary.results) == 1
    res = summary.results[0]
    assert res.success
    assert "vlan 100" in res.rendered
    assert "name PROD" in res.rendered


def test_dry_run_uses_per_device_platform(template_dir):
    p = Provisioner(template_dir)
    cisco = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
    junos = Device(hostname="j1", mgmt_ip="10.0.0.2", platform=Platform.JUNIPER_JUNOS)

    summary = p.apply(
        "platform_aware.j2", [cisco, junos], dry_run=True, extra_vars={"vlan_id": 200},
    )
    by_host = {r.hostname: r for r in summary.results}
    assert "vlan 200" in by_host["r1"].rendered
    assert "set vlans v200" in by_host["j1"].rendered


def test_strict_undefined_fails_loudly(template_dir):
    p = Provisioner(template_dir)
    d = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
    summary = p.apply("broken.j2", [d], dry_run=True)
    assert not summary.results[0].success
    assert "render:" in summary.results[0].error


def test_apply_pushes_via_session(template_dir, fake_factory):
    from tests.conftest import FakeConnection

    registry, factory = fake_factory
    p = Provisioner(template_dir, factory=factory, max_workers=1)
    d = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
    # Pre-register a fake conn with empty running config
    registry["r1"] = FakeConnection(hostname="r1", running="!\nhostname r1\n!\n")

    summary = p.apply(
        "vlan.j2", [d], dry_run=False, extra_vars={"vlan_id": 100, "vlan_name": "PROD"},
    )
    assert summary.results[0].success
    # Verify the fake actually received the push
    pushed = registry["r1"].pushed
    assert len(pushed) >= 1
    assert any("vlan 100" in line for line in pushed[0])


def test_apply_rolls_back_on_verification_failure(template_dir, fake_factory):
    """If post-push config doesn't include the rendered lines, attempt rollback."""
    from tests.conftest import FakeConnection

    class StubbornConnection(FakeConnection):
        def send_config(self, commands):
            # Don't add anything to running config — simulate silent rejection
            self.pushed.append(list(commands))
            return "ignored"

    registry: dict = {}

    def factory(device):
        if device.hostname not in registry:
            registry[device.hostname] = StubbornConnection(
                hostname=device.hostname, running="hostname r1\n"
            )
        return registry[device.hostname]

    p = Provisioner(template_dir, factory=factory)
    d = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
    summary = p.apply(
        "vlan.j2", [d], dry_run=False, extra_vars={"vlan_id": 100, "vlan_name": "PROD"},
    )
    assert not summary.results[0].success
    assert "verification failed" in summary.results[0].error
    assert summary.results[0].rolled_back


def test_session_error_recorded(template_dir):
    from netauto.drivers import SessionError

    def factory(device):
        raise SessionError("connection refused")

    p = Provisioner(template_dir, factory=factory)
    d = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
    summary = p.apply("vlan.j2", [d], dry_run=False, extra_vars={"vlan_id": 1, "vlan_name": "x"})
    assert not summary.results[0].success
    assert "connection refused" in summary.results[0].error
