"""Tests for unified_config_diff."""

from __future__ import annotations

from netauto.utils.diff import unified_config_diff


def test_identical_configs_return_empty():
    cfg = "interface Eth1\n description prod\n!"
    assert unified_config_diff(cfg, cfg) == ""


def test_noise_lines_ignored():
    before = (
        "Building configuration...\n"
        "! Last configuration change at 2024-09-01\n"
        "interface Eth1\n description prod\n"
    )
    after = (
        "Building configuration...\n"
        "! Last configuration change at 2024-09-15\n"  # only timestamp differs
        "interface Eth1\n description prod\n"
    )
    assert unified_config_diff(before, after) == ""


def test_real_change_produces_diff():
    before = "interface Eth1\n description prod\n no shutdown"
    after = "interface Eth1\n description PROD-2\n no shutdown"
    diff = unified_config_diff(before, after, fromfile="r1.before", tofile="r1.after")
    assert "description prod" in diff
    assert "description PROD-2" in diff
    assert "r1.before" in diff


def test_diff_shows_added_lines():
    before = "interface Eth1\n description prod"
    after = "interface Eth1\n description prod\n service-policy QOS"
    diff = unified_config_diff(before, after)
    assert "+ service-policy QOS" in diff or "+service-policy QOS" in diff
