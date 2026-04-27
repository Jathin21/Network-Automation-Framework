"""Compliance auditor.

Rules are declarative: each rule states what *must* be present and/or what *must
not* be present in a device's running configuration. Rules are loaded from YAML
so non-coders can extend the policy library without touching Python.

YAML rule example::

    - id: SEC-001
      title: SSH must be version 2 only
      severity: high
      platforms: [cisco_ios, cisco_nxos]
      must_match:
        - "^ip ssh version 2$"
      must_not_match:
        - "^ip ssh version 1"
        - "^transport input telnet"

Each rule produces a Pass / Fail / NotApplicable result per device. The aggregate
report is JSON-serializable for ingestion into ServiceNow / Datadog.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from netauto.inventory.models import Device, Platform
from netauto.utils.logging import get_logger

log = get_logger(__name__)


class Severity(str, Enum):
    """Severity used by reporting / alerting downstream."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ComplianceRule(BaseModel):
    """A single declarative compliance rule."""

    id: str = Field(..., min_length=1)
    title: str
    severity: Severity = Severity.MEDIUM
    platforms: list[Platform] = Field(
        default_factory=list,
        description="If empty, rule applies to all platforms.",
    )
    must_match: list[str] = Field(default_factory=list)
    must_not_match: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("must_match", "must_not_match")
    @classmethod
    def _compile_regexes(cls, patterns: list[str]) -> list[str]:
        # Validate at load time, but keep strings for serialization.
        for p in patterns:
            try:
                re.compile(p, re.MULTILINE)
            except re.error as exc:
                raise ValueError(f"invalid regex {p!r}: {exc}") from exc
        return patterns

    def applies_to(self, device: Device) -> bool:
        """Whether this rule should run against ``device``."""
        if not self.platforms:
            return True
        return device.platform in self.platforms


@dataclass(slots=True)
class _RuleResult:
    """One rule's outcome against one device."""

    rule_id: str
    title: str
    severity: Severity
    passed: bool
    applicable: bool
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": self.severity.value,
            "passed": self.passed,
            "applicable": self.applicable,
            "findings": self.findings,
        }


@dataclass(slots=True)
class ComplianceReport:
    """Aggregate results across all devices and rules."""

    by_device: dict[str, list[_RuleResult]] = field(default_factory=dict)

    @property
    def total_devices(self) -> int:
        return len(self.by_device)

    @property
    def total_failures(self) -> int:
        return sum(
            1 for results in self.by_device.values() for r in results if r.applicable and not r.passed
        )

    @property
    def critical_failures(self) -> int:
        return sum(
            1
            for results in self.by_device.values()
            for r in results
            if r.applicable and not r.passed and r.severity == Severity.CRITICAL
        )

    def is_clean(self) -> bool:
        """True iff every applicable rule passed on every device."""
        return self.total_failures == 0

    def to_dict(self) -> dict:
        return {
            "summary": {
                "devices": self.total_devices,
                "failures": self.total_failures,
                "critical_failures": self.critical_failures,
                "clean": self.is_clean(),
            },
            "devices": {
                host: [r.to_dict() for r in results] for host, results in self.by_device.items()
            },
        }


class ComplianceAuditor:
    """Run a set of :class:`ComplianceRule` against device configs."""

    def __init__(self, rules: list[ComplianceRule]) -> None:
        if not rules:
            raise ValueError("at least one rule is required")
        self._rules = rules
        # Pre-compile regexes once for performance on large fleets
        self._compiled: dict[str, tuple[list[re.Pattern[str]], list[re.Pattern[str]]]] = {
            r.id: (
                [re.compile(p, re.MULTILINE) for p in r.must_match],
                [re.compile(p, re.MULTILINE) for p in r.must_not_match],
            )
            for r in rules
        }

    @classmethod
    def from_yaml(cls, path: str | Path) -> ComplianceAuditor:
        """Load rules from a YAML file (a list of rule mappings)."""
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, list):
            raise ValueError(f"{path}: top-level YAML must be a list of rules")
        rules = [ComplianceRule.model_validate(r) for r in raw]
        return cls(rules)

    def audit_one(self, device: Device, config: str) -> list[_RuleResult]:
        """Audit a single device's configuration against all rules."""
        results: list[_RuleResult] = []
        for rule in self._rules:
            if not rule.applies_to(device):
                results.append(
                    _RuleResult(
                        rule_id=rule.id,
                        title=rule.title,
                        severity=rule.severity,
                        passed=True,
                        applicable=False,
                    )
                )
                continue
            findings: list[str] = []
            must_match, must_not_match = self._compiled[rule.id]
            for pat in must_match:
                if not pat.search(config):
                    findings.append(f"required pattern not found: {pat.pattern}")
            for pat in must_not_match:
                if pat.search(config):
                    findings.append(f"forbidden pattern present: {pat.pattern}")
            results.append(
                _RuleResult(
                    rule_id=rule.id,
                    title=rule.title,
                    severity=rule.severity,
                    passed=not findings,
                    applicable=True,
                    findings=findings,
                )
            )
        return results

    def audit(self, items: Iterable[tuple[Device, str]]) -> ComplianceReport:
        """Audit many ``(device, config)`` pairs and return an aggregate report."""
        report = ComplianceReport()
        for device, config in items:
            results = self.audit_one(device, config)
            report.by_device[device.hostname] = results
            failed = [r for r in results if r.applicable and not r.passed]
            log.info(
                "audit_device",
                host=device.hostname,
                failures=len(failed),
                rules=len(results),
            )
        return report
