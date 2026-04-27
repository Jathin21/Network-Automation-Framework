"""Configuration backup collector.

Pulls ``running-config`` from each target device and writes it to a structured
directory tree::

    backups/
      2024-09-15T03-00-00Z/
        site=hyd-dc-1/
          edge-router-1.cfg
          edge-router-1.meta.json
        site=blr-dc-2/
          ...
        manifest.json

The manifest captures hashes and metadata so a subsequent run can detect drift
without re-reading every config off disk.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from netauto.drivers import ConnectionFactory, SessionError, open_session, session_metadata
from netauto.inventory.models import Device
from netauto.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class BackupResult:
    """Outcome of one device's backup attempt."""

    hostname: str
    success: bool
    path: Path | None = None
    bytes_written: int = 0
    sha256: str = ""
    error: str = ""
    duration_s: float = 0.0


@dataclass(slots=True)
class _Run:
    """Aggregate state for one backup run."""

    timestamp: str
    root: Path
    results: list[BackupResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[BackupResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[BackupResult]:
        return [r for r in self.results if not r.success]


class ConfigBackup:
    """Parallel config backup runner.

    The runner is intentionally stateless across runs: each invocation produces
    a new timestamped directory. Idempotency and drift detection are handled by
    the auditor, not the backup itself.
    """

    def __init__(
        self,
        output_dir: str | Path = "configs/backups",
        *,
        max_workers: int = 16,
        factory: ConnectionFactory | None = None,
    ) -> None:
        self._root = Path(output_dir)
        self._max_workers = max_workers
        self._factory = factory

    def run(self, devices: Iterable[Device]) -> _Run:
        """Backup every device in the iterable. Returns an aggregate result."""
        devices = list(devices)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        run_dir = self._root / ts
        run_dir.mkdir(parents=True, exist_ok=True)
        log.info("backup_start", devices=len(devices), output=str(run_dir))

        results: list[BackupResult] = []
        with cf.ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_host = {
                pool.submit(self._backup_one, d, run_dir): d.hostname for d in devices
            }
            for fut in cf.as_completed(future_to_host):
                results.append(fut.result())

        run = _Run(timestamp=ts, root=run_dir, results=results)
        self._write_manifest(run, devices)
        log.info(
            "backup_complete",
            ok=len(run.succeeded),
            failed=len(run.failed),
            output=str(run_dir),
        )
        return run

    # ------------------------------------------------------------------ helpers

    def _backup_one(self, device: Device, run_dir: Path) -> BackupResult:
        bound = log.bind(host=device.hostname)
        started = datetime.now(timezone.utc)
        try:
            with open_session(device, factory=self._factory) as conn:
                config = conn.get_running_config()
        except SessionError as exc:
            return BackupResult(
                hostname=device.hostname,
                success=False,
                error=str(exc),
                duration_s=(datetime.now(timezone.utc) - started).total_seconds(),
            )

        site_dir = run_dir / f"site={device.site}"
        site_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = site_dir / f"{device.hostname}.cfg"
        cfg_path.write_text(config)
        digest = hashlib.sha256(config.encode("utf-8")).hexdigest()

        meta = session_metadata(device) | {"sha256": digest, "size": len(config)}
        (site_dir / f"{device.hostname}.meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True)
        )

        bound.info("backup_ok", bytes=len(config), sha256=digest[:12])
        return BackupResult(
            hostname=device.hostname,
            success=True,
            path=cfg_path,
            bytes_written=len(config),
            sha256=digest,
            duration_s=(datetime.now(timezone.utc) - started).total_seconds(),
        )

    @staticmethod
    def _write_manifest(run: _Run, devices: list[Device]) -> None:
        host_to_device = {d.hostname: d for d in devices}
        manifest = {
            "timestamp": run.timestamp,
            "device_count": len(devices),
            "succeeded": len(run.succeeded),
            "failed": len(run.failed),
            "entries": [
                {
                    "hostname": r.hostname,
                    "site": host_to_device[r.hostname].site
                    if r.hostname in host_to_device
                    else "unknown",
                    "platform": host_to_device[r.hostname].platform.value
                    if r.hostname in host_to_device
                    else "unknown",
                    "success": r.success,
                    "sha256": r.sha256,
                    "bytes": r.bytes_written,
                    "duration_s": round(r.duration_s, 3),
                    "error": r.error,
                }
                for r in sorted(run.results, key=lambda x: x.hostname)
            ],
        }
        (run.root / "manifest.json").write_text(json.dumps(manifest, indent=2))
