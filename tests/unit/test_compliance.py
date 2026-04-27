"""Tests for the compliance auditor."""

from __future__ import annotations

import pytest

from netauto.inventory.models import Device, Platform
from netauto.validators.compliance import ComplianceAuditor, ComplianceRule, Severity


@pytest.fixture
def basic_rules() -> list[ComplianceRule]:
    return [
        ComplianceRule(
            id="R1",
            title="SSH v2 required",
            severity=Severity.HIGH,
            must_match=[r"^ip ssh version 2$"],
            must_not_match=[r"^transport input telnet"],
        ),
        ComplianceRule(
            id="R2",
            title="No SNMPv2c communities",
            severity=Severity.CRITICAL,
            must_not_match=[r"^snmp-server community .* (RO|RW)$"],
        ),
        ComplianceRule(
            id="R3",
            title="Junos-only rule",
            severity=Severity.LOW,
            platforms=[Platform.JUNIPER_JUNOS],
            must_match=[r"^set system services ssh$"],
        ),
    ]


@pytest.fixture
def cisco_device() -> Device:
    return Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)


@pytest.fixture
def junos_device() -> Device:
    return Device(hostname="j1", mgmt_ip="10.0.0.2", platform=Platform.JUNIPER_JUNOS)


class TestRuleValidation:
    def test_invalid_regex_rejected(self):
        with pytest.raises(ValueError):
            ComplianceRule(id="bad", title="t", must_match=["[unbalanced"])

    def test_severity_default_is_medium(self):
        r = ComplianceRule(id="r", title="t")
        assert r.severity == Severity.MEDIUM

    def test_applies_to_all_platforms_when_unspecified(self, cisco_device, junos_device):
        r = ComplianceRule(id="r", title="t")
        assert r.applies_to(cisco_device)
        assert r.applies_to(junos_device)

    def test_applies_only_to_listed_platforms(self, cisco_device, junos_device):
        r = ComplianceRule(id="r", title="t", platforms=[Platform.JUNIPER_JUNOS])
        assert not r.applies_to(cisco_device)
        assert r.applies_to(junos_device)


class TestAuditor:
    def test_empty_rules_rejected(self):
        with pytest.raises(ValueError):
            ComplianceAuditor([])

    def test_compliant_config_passes(self, basic_rules, cisco_device):
        auditor = ComplianceAuditor(basic_rules)
        config = "ip ssh version 2\nbanner motd ^Welcome^\nntp server 1.1.1.1\n"
        results = auditor.audit_one(cisco_device, config)
        # Only R1 and R2 are applicable to cisco_ios
        applicable = [r for r in results if r.applicable]
        assert len(applicable) == 2
        assert all(r.passed for r in applicable)

    def test_missing_required_pattern_fails(self, basic_rules, cisco_device):
        auditor = ComplianceAuditor(basic_rules)
        config = "no ip ssh\n"
        results = auditor.audit_one(cisco_device, config)
        r1 = next(r for r in results if r.rule_id == "R1")
        assert not r1.passed
        assert any("required pattern not found" in f for f in r1.findings)

    def test_forbidden_pattern_fails(self, basic_rules, cisco_device):
        auditor = ComplianceAuditor(basic_rules)
        config = "ip ssh version 2\nsnmp-server community public RO\n"
        results = auditor.audit_one(cisco_device, config)
        r2 = next(r for r in results if r.rule_id == "R2")
        assert not r2.passed
        assert any("forbidden pattern present" in f for f in r2.findings)

    def test_platform_filter_marks_inapplicable(self, basic_rules, cisco_device):
        auditor = ComplianceAuditor(basic_rules)
        results = auditor.audit_one(cisco_device, "")
        r3 = next(r for r in results if r.rule_id == "R3")
        assert not r3.applicable
        assert r3.passed  # inapplicable rules don't count as failures

    def test_aggregate_report_counts(self, basic_rules):
        auditor = ComplianceAuditor(basic_rules)
        d1 = Device(hostname="r1", mgmt_ip="1.1.1.1", platform=Platform.CISCO_IOS)
        d2 = Device(hostname="r2", mgmt_ip="2.2.2.2", platform=Platform.CISCO_IOS)
        bad = "snmp-server community public RO\n"
        good = "ip ssh version 2\n"
        report = auditor.audit([(d1, bad), (d2, good)])
        assert report.total_devices == 2
        assert report.total_failures == 2  # r1 fails R1 and R2
        assert report.critical_failures == 1
        assert not report.is_clean()

    def test_clean_report(self, basic_rules):
        auditor = ComplianceAuditor(basic_rules)
        d = Device(hostname="r1", mgmt_ip="1.1.1.1", platform=Platform.CISCO_IOS)
        good = "ip ssh version 2\n"
        report = auditor.audit([(d, good)])
        assert report.is_clean()
        assert report.total_failures == 0

    def test_to_dict_serializable(self, basic_rules, cisco_device):
        import json
        auditor = ComplianceAuditor(basic_rules)
        report = auditor.audit([(cisco_device, "ip ssh version 2\n")])
        # Round-trip through JSON to confirm serializability
        json.dumps(report.to_dict())


class TestYamlLoader:
    def test_from_yaml(self, tmp_path):
        f = tmp_path / "rules.yml"
        f.write_text(
            """
            - id: R-A
              title: Test rule
              severity: high
              must_match:
                - "^line-x$"
            """
        )
        auditor = ComplianceAuditor.from_yaml(f)
        assert len(auditor._rules) == 1
        assert auditor._rules[0].id == "R-A"

    def test_from_yaml_rejects_non_list(self, tmp_path):
        f = tmp_path / "rules.yml"
        f.write_text("not_a_list: true\n")
        with pytest.raises(ValueError, match="must be a list"):
            ComplianceAuditor.from_yaml(f)
