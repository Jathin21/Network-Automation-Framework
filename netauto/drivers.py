"""Driver abstraction over Netmiko and NAPALM.

The framework uses Netmiko for screen-scraping CLI commands (the lowest common
denominator across vendors and software versions) and NAPALM where its
vendor-agnostic getters are well-supported. A thin :class:`DeviceSession`
wraps both so callers don't need to know which one is in use.

This indirection is also what makes the framework testable: tests inject a
fake session via ``DeviceSession.from_factory`` rather than monkey-patching
Netmiko internals.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from netauto.inventory.models import Device
from netauto.utils.logging import get_logger

log = get_logger(__name__)


class SessionError(Exception):
    """Raised when a device session fails irrecoverably."""


class DeviceConnection(Protocol):
    """Protocol every driver session must satisfy.

    Defined as a Protocol so that real Netmiko / NAPALM connections plus test
    fakes are both structurally valid without inheritance.
    """

    def send_command(self, command: str) -> str: ...
    def send_config(self, commands: list[str]) -> str: ...
    def get_running_config(self) -> str: ...
    def disconnect(self) -> None: ...


# Type for a factory that yields a connected session for a device. Used to inject
# fakes during testing.
ConnectionFactory = Callable[[Device], DeviceConnection]


class _NetmikoSession:
    """Concrete Netmiko-backed connection.

    Imported lazily so that unit tests don't need Netmiko installed.
    """

    def __init__(self, device: Device) -> None:
        from netmiko import ConnectHandler  # noqa: PLC0415  - lazy import

        self._device = device
        self._conn = ConnectHandler(**device.netmiko_params())

    def send_command(self, command: str) -> str:
        return str(self._conn.send_command(command))

    def send_config(self, commands: list[str]) -> str:
        return str(self._conn.send_config_set(commands))

    def get_running_config(self) -> str:
        # Vendor-specific dispatch
        cmd = {
            "cisco_ios": "show running-config",
            "cisco_xr": "show running-config",
            "cisco_nxos": "show running-config",
            "juniper_junos": "show configuration | display set",
            "arista_eos": "show running-config",
        }.get(self._device.platform.value, "show running-config")
        return self.send_command(cmd)

    def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.disconnect()


def default_factory(device: Device) -> DeviceConnection:
    """Default production factory: builds a real Netmiko session."""
    return _NetmikoSession(device)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(OSError),
)
def _connect_with_retry(factory: ConnectionFactory, device: Device) -> DeviceConnection:
    """Retry transient connection errors up to 3 times with backoff."""
    return factory(device)


@contextlib.contextmanager
def open_session(
    device: Device,
    factory: ConnectionFactory | None = None,
) -> Iterator[DeviceConnection]:
    """Context-managed session that always disconnects, even on error.

    Args:
        device: Target device.
        factory: Optional override (used by tests). Defaults to real Netmiko.

    Yields:
        A connected :class:`DeviceConnection`.

    Raises:
        SessionError: If the session cannot be established after retries.
    """
    factory = factory or default_factory
    bound = log.bind(host=device.hostname, platform=device.platform.value)
    bound.debug("opening session")

    try:
        conn = _connect_with_retry(factory, device)
    except RetryError as exc:
        raise SessionError(f"failed to connect to {device.hostname} after retries") from exc
    except Exception as exc:
        raise SessionError(f"failed to connect to {device.hostname}: {exc}") from exc

    try:
        yield conn
    finally:
        bound.debug("closing session")
        conn.disconnect()


def session_metadata(device: Device) -> dict[str, Any]:
    """Return a small dict suitable for tagging audit / backup artifacts."""
    return {
        "hostname": device.hostname,
        "mgmt_ip": device.mgmt_ip,
        "platform": device.platform.value,
        "site": device.site,
        "role": device.role,
    }
