"""Bulk provisioning via Jinja2 templates.

The provisioner renders a Jinja2 template per device using the device's
inventory metadata (plus extra variables), then pushes the rendered config in
either dry-run mode (render only, no device contact) or apply mode (render +
push via Netmiko).

Each device push is wrapped in a transaction:
  1. Snapshot running-config
  2. Apply candidate config
  3. Verify candidate is committed (re-read running-config)
  4. If verification fails, replay the snapshot (best-effort rollback)

Rollback isn't perfect — vendor-specific config-replace would be required for
true atomicity — but it catches the common "mistyped command silently dropped"
case.
"""

from __future__ import annotations

import concurrent.futures as cf
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template

from netauto.drivers import ConnectionFactory, SessionError, open_session
from netauto.inventory.models import Device
from netauto.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ProvisionResult:
    """Outcome of one provisioning operation."""

    hostname: str
    success: bool
    rendered: str = ""
    output: str = ""
    error: str = ""
    rolled_back: bool = False


@dataclass(slots=True)
class _RunSummary:
    results: list[ProvisionResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[ProvisionResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[ProvisionResult]:
        return [r for r in self.results if not r.success]


class Provisioner:
    """Render and push templated configurations across many devices."""

    def __init__(
        self,
        template_dir: str | Path,
        *,
        max_workers: int = 8,
        factory: ConnectionFactory | None = None,
    ) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            undefined=StrictUndefined,  # missing variables = fail loudly, not silently
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._max_workers = max_workers
        self._factory = factory

    def render(self, template_name: str, device: Device, **vars_: Any) -> str:
        """Render a template for one device. Returns the rendered config string."""
        tmpl: Template = self._env.get_template(template_name)
        ctx = {
            "device": device.model_dump(exclude={"password", "secret"}),
            **vars_,
        }
        return tmpl.render(**ctx)

    def apply(
        self,
        template_name: str,
        devices: Iterable[Device],
        *,
        dry_run: bool = True,
        extra_vars: dict[str, Any] | None = None,
    ) -> _RunSummary:
        """Render (and optionally push) a template across devices."""
        devices = list(devices)
        extra_vars = extra_vars or {}
        log.info(
            "provision_start",
            template=template_name,
            devices=len(devices),
            dry_run=dry_run,
        )

        if dry_run:
            results = [self._render_only(template_name, d, extra_vars) for d in devices]
            return _RunSummary(results=results)

        results: list[ProvisionResult] = []
        with cf.ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [
                pool.submit(self._apply_one, template_name, d, extra_vars) for d in devices
            ]
            for fut in cf.as_completed(futures):
                results.append(fut.result())
        return _RunSummary(results=results)

    # ------------------------------------------------------------------ helpers

    def _render_only(
        self,
        template_name: str,
        device: Device,
        extra_vars: dict[str, Any],
    ) -> ProvisionResult:
        try:
            rendered = self.render(template_name, device, **extra_vars)
            return ProvisionResult(hostname=device.hostname, success=True, rendered=rendered)
        except Exception as exc:  # noqa: BLE001 - any render error is reported back
            return ProvisionResult(
                hostname=device.hostname, success=False, error=f"render: {exc}"
            )

    def _apply_one(
        self,
        template_name: str,
        device: Device,
        extra_vars: dict[str, Any],
    ) -> ProvisionResult:
        bound = log.bind(host=device.hostname)
        try:
            rendered = self.render(template_name, device, **extra_vars)
        except Exception as exc:  # noqa: BLE001
            return ProvisionResult(hostname=device.hostname, success=False, error=f"render: {exc}")

        commands = [ln for ln in rendered.splitlines() if ln.strip()]

        try:
            with open_session(device, factory=self._factory) as conn:
                snapshot = conn.get_running_config()
                output = conn.send_config(commands)
                # Best-effort verification: re-read config and ensure every non-blank
                # rendered line appears somewhere in the post-config.
                post = conn.get_running_config()
                missing = [ln for ln in commands if ln not in post]
                if missing:
                    bound.warning("verify_failed", missing_count=len(missing))
                    # Attempt rollback by re-pushing the snapshot
                    rollback_cmds = [
                        ln for ln in snapshot.splitlines() if ln.strip() and not ln.startswith("!")
                    ]
                    try:
                        conn.send_config(rollback_cmds)
                    except Exception as exc:  # noqa: BLE001
                        bound.error("rollback_failed", error=str(exc))
                    return ProvisionResult(
                        hostname=device.hostname,
                        success=False,
                        rendered=rendered,
                        output=output,
                        error=f"verification failed: {len(missing)} line(s) not present post-push",
                        rolled_back=True,
                    )
        except SessionError as exc:
            return ProvisionResult(
                hostname=device.hostname, success=False, rendered=rendered, error=str(exc)
            )

        bound.info("provision_ok", lines=len(commands))
        return ProvisionResult(
            hostname=device.hostname, success=True, rendered=rendered, output=output
        )
