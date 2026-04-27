"""Shared pytest fixtures and a fake DeviceConnection for offline tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from netauto.drivers import DeviceConnection
from netauto.inventory.models import Device, Inventory, Platform


class FakeConnection:
    """In-memory DeviceConnection used to test orchestration without devices.

    The fixture-scoped store lets tests script "show foo" -> output mappings and
    spy on what config has been pushed.
    """

    def __init__(self, *, hostname: str, running: str = "", responses: dict[str, str] | None = None):
        self.hostname = hostname
        self._running = running
        self._responses = responses or {}
        self.pushed: list[list[str]] = []
        self.disconnected = False

    def send_command(self, command: str) -> str:
        if command in self._responses:
            return self._responses[command]
        # Common defaults so tests don't need to mock every getter
        if "running-config" in command or "configuration | display set" in command:
            return self._running
        return ""

    def send_config(self, commands: list[str]) -> str:
        self.pushed.append(list(commands))
        # Pretend the lines went into the running config so verify passes
        self._running += "\n" + "\n".join(commands)
        return f"applied {len(commands)} lines"

    def get_running_config(self) -> str:
        return self._running

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def fake_factory() -> tuple[dict[str, FakeConnection], callable]:
    """Returns (registry, factory). Tests pre-populate registry by hostname."""
    registry: dict[str, FakeConnection] = {}

    def factory(device: Device) -> DeviceConnection:
        if device.hostname not in registry:
            registry[device.hostname] = FakeConnection(hostname=device.hostname)
        return registry[device.hostname]

    return registry, factory


@pytest.fixture
def sample_inventory() -> Inventory:
    return Inventory(
        devices=[
            Device(
                hostname="r1", mgmt_ip="10.0.0.1",
                platform=Platform.CISCO_IOS, site="dc1", role="edge",
                tags=["bgp", "prod"],
            ),
            Device(
                hostname="r2", mgmt_ip="10.0.0.2",
                platform=Platform.JUNIPER_JUNOS, site="dc1", role="core",
                tags=["isis", "prod"],
            ),
            Device(
                hostname="r3", mgmt_ip="10.0.0.3",
                platform=Platform.ARISTA_EOS, site="dc2", role="leaf",
                tags=["evpn"],
            ),
        ]
    )


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
