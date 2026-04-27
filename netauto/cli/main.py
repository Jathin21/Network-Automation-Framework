"""Top-level click CLI exposing every netauto operation.

Usage examples::

    netauto inventory show -i inventory.yml
    netauto backup -i inventory.yml --site hyd-dc-1
    netauto audit -i inventory.yml -r policies/baseline.yml
    netauto provision -i inventory.yml -t vlan.j2 --vars vlan_id=200 --apply
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from netauto import __version__
from netauto.collectors.backup import ConfigBackup
from netauto.inventory import Platform, load_inventory
from netauto.provision import Provisioner
from netauto.utils.logging import configure_logging
from netauto.validators.compliance import ComplianceAuditor

console = Console()


def _filter_inventory(
    inv_path: str,
    site: str | None,
    role: str | None,
    platform: str | None,
    tag: str | None,
    group: str | None,
) -> list:
    inv = load_inventory(inv_path)
    p = Platform(platform) if platform else None
    return inv.filter(site=site, role=role, platform=p, tag=tag, group=group)


@click.group()
@click.version_option(__version__, prog_name="netauto")
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
@click.option("--json-logs", is_flag=True, help="Emit JSON-line logs (CI / centralized logging).")
def cli(log_level: str, json_logs: bool) -> None:
    """netauto - multi-vendor network automation framework."""
    configure_logging(level=log_level, json_output=json_logs)


# ----------------------------------------------------- inventory subcommand ---


@cli.group()
def inventory() -> None:
    """Inventory inspection commands."""


@inventory.command("show")
@click.option("-i", "--inventory-file", required=True, type=click.Path(exists=True))
@click.option("--site")
@click.option("--role")
@click.option("--platform", "platform_filter")
@click.option("--tag")
@click.option("--group")
def inventory_show(
    inventory_file: str,
    site: str | None,
    role: str | None,
    platform_filter: str | None,
    tag: str | None,
    group: str | None,
) -> None:
    """Print devices matching the supplied filters."""
    devices = _filter_inventory(inventory_file, site, role, platform_filter, tag, group)
    table = Table(title=f"Inventory ({len(devices)} devices)")
    for col in ("hostname", "mgmt_ip", "platform", "site", "role", "tags"):
        table.add_column(col)
    for d in devices:
        table.add_row(
            d.hostname, d.mgmt_ip, d.platform.value, d.site, d.role, ",".join(d.tags)
        )
    console.print(table)


# ----------------------------------------------------- backup subcommand ------


@cli.command("backup")
@click.option("-i", "--inventory-file", required=True, type=click.Path(exists=True))
@click.option("--site")
@click.option("--role")
@click.option("--platform", "platform_filter")
@click.option("--tag")
@click.option("--group")
@click.option("-o", "--output-dir", default="configs/backups", show_default=True)
@click.option("--max-workers", default=16, show_default=True, type=int)
def backup(
    inventory_file: str,
    site: str | None,
    role: str | None,
    platform_filter: str | None,
    tag: str | None,
    group: str | None,
    output_dir: str,
    max_workers: int,
) -> None:
    """Pull running-config from each matched device."""
    devices = _filter_inventory(inventory_file, site, role, platform_filter, tag, group)
    if not devices:
        click.echo("no devices matched filters", err=True)
        sys.exit(2)

    runner = ConfigBackup(output_dir=output_dir, max_workers=max_workers)
    run = runner.run(devices)

    table = Table(title=f"Backup run {run.timestamp}")
    table.add_column("hostname")
    table.add_column("status")
    table.add_column("bytes", justify="right")
    table.add_column("sha256")
    table.add_column("error")
    for r in sorted(run.results, key=lambda x: x.hostname):
        table.add_row(
            r.hostname,
            "[green]ok[/green]" if r.success else "[red]fail[/red]",
            str(r.bytes_written),
            r.sha256[:12],
            r.error,
        )
    console.print(table)
    sys.exit(0 if not run.failed else 1)


# ----------------------------------------------------- audit subcommand -------


@cli.command("audit")
@click.option("-i", "--inventory-file", required=True, type=click.Path(exists=True))
@click.option("-r", "--rules-file", required=True, type=click.Path(exists=True))
@click.option("-c", "--configs-dir", default="configs/backups", show_default=True,
              help="Directory containing the latest backup run.")
@click.option("--run-timestamp",
              help="Specific backup run subdir to audit. Defaults to most recent.")
@click.option("--json", "json_out", is_flag=True, help="Emit JSON report on stdout.")
def audit(
    inventory_file: str,
    rules_file: str,
    configs_dir: str,
    run_timestamp: str | None,
    json_out: bool,
) -> None:
    """Audit the latest backup against a rule set."""
    inv = load_inventory(inventory_file)
    auditor = ComplianceAuditor.from_yaml(rules_file)

    root = Path(configs_dir)
    if run_timestamp:
        run_root = root / run_timestamp
    else:
        # Pick the most recent timestamped subdir
        candidates = [p for p in root.iterdir() if p.is_dir()]
        if not candidates:
            click.echo(f"no backup runs found under {root}", err=True)
            sys.exit(2)
        run_root = max(candidates, key=lambda p: p.name)

    pairs = []
    for d in inv.devices:
        cfg = run_root / f"site={d.site}" / f"{d.hostname}.cfg"
        if cfg.is_file():
            pairs.append((d, cfg.read_text()))
    report = auditor.audit(pairs)

    if json_out:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        table = Table(title=f"Compliance ({report.total_devices} devices, "
                            f"{report.total_failures} failures)")
        for col in ("hostname", "rule", "severity", "status", "findings"):
            table.add_column(col)
        for host, results in sorted(report.by_device.items()):
            for r in results:
                if not r.applicable:
                    continue
                table.add_row(
                    host,
                    r.rule_id,
                    r.severity.value,
                    "[green]pass[/green]" if r.passed else "[red]fail[/red]",
                    "; ".join(r.findings),
                )
        console.print(table)
    sys.exit(0 if report.is_clean() else 1)


# ----------------------------------------------------- provision subcommand ---


@cli.command("provision")
@click.option("-i", "--inventory-file", required=True, type=click.Path(exists=True))
@click.option("-t", "--template", required=True, help="Template name relative to --template-dir.")
@click.option("-d", "--template-dir", default="configs/templates", show_default=True)
@click.option("--site")
@click.option("--role")
@click.option("--platform", "platform_filter")
@click.option("--tag")
@click.option("--group")
@click.option("--vars", "vars_kv", multiple=True, help="key=value (repeatable).")
@click.option("--apply", "apply_changes", is_flag=True,
              help="Push to devices. Default is dry-run (render only).")
def provision(
    inventory_file: str,
    template: str,
    template_dir: str,
    site: str | None,
    role: str | None,
    platform_filter: str | None,
    tag: str | None,
    group: str | None,
    vars_kv: tuple[str, ...],
    apply_changes: bool,
) -> None:
    """Render (and optionally push) a Jinja2 template across devices."""
    devices = _filter_inventory(inventory_file, site, role, platform_filter, tag, group)
    if not devices:
        click.echo("no devices matched filters", err=True)
        sys.exit(2)

    extra: dict[str, Any] = {}
    for kv in vars_kv:
        if "=" not in kv:
            click.echo(f"invalid --vars: {kv} (expected key=value)", err=True)
            sys.exit(2)
        k, v = kv.split("=", 1)
        extra[k] = v

    p = Provisioner(template_dir=template_dir)
    summary = p.apply(template, devices, dry_run=not apply_changes, extra_vars=extra)

    for r in summary.results:
        if r.success:
            click.echo(f"--- {r.hostname} ---")
            click.echo(r.rendered if not apply_changes else "applied")
        else:
            click.echo(f"!!! {r.hostname}: {r.error}", err=True)
    sys.exit(0 if not summary.failed else 1)


if __name__ == "__main__":
    cli()
