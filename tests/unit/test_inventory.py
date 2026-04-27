"""Tests for the inventory model and YAML loader."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from netauto.inventory import InventoryError, Platform, load_inventory
from netauto.inventory.models import Device, DeviceGroup, Inventory


class TestDevice:
    def test_minimal_device(self):
        d = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
        assert d.port == 22
        assert d.username == "netauto"
        assert d.tags == []

    def test_hostname_rejects_whitespace(self):
        with pytest.raises(ValidationError):
            Device(hostname="r 1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)

    def test_tags_normalized_to_lowercase(self):
        d = Device(
            hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS,
            tags=["  PROD ", "BGP", "", "   "],
        )
        assert d.tags == ["prod", "bgp"]

    def test_netmiko_params_includes_secret_only_when_set(self):
        d = Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
        params = d.netmiko_params()
        assert "secret" not in params
        d2 = Device(
            hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS, secret="cisco"
        )
        assert d2.netmiko_params()["secret"] == "cisco"

    def test_napalm_driver_mapping(self):
        assert Platform.CISCO_IOS.napalm_driver == "ios"
        assert Platform.CISCO_NXOS.napalm_driver == "nxos_ssh"
        assert Platform.JUNIPER_JUNOS.napalm_driver == "junos"
        assert Platform.ARISTA_EOS.napalm_driver == "eos"
        assert Platform.CISCO_XR.napalm_driver == "iosxr"

    def test_password_excluded_from_repr(self):
        d = Device(
            hostname="r1", mgmt_ip="10.0.0.1",
            platform=Platform.CISCO_IOS, password="hunter2", secret="enable",
        )
        text = repr(d)
        assert "hunter2" not in text
        assert "enable" not in text


class TestInventory:
    def test_duplicate_hostnames_rejected(self):
        with pytest.raises(ValidationError):
            Inventory(
                devices=[
                    Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS),
                    Device(hostname="r1", mgmt_ip="10.0.0.2", platform=Platform.CISCO_IOS),
                ]
            )

    def test_group_member_must_exist(self):
        with pytest.raises(ValidationError):
            Inventory(
                devices=[
                    Device(hostname="r1", mgmt_ip="10.0.0.1", platform=Platform.CISCO_IOS)
                ],
                groups=[DeviceGroup(name="g1", members=["r1", "ghost"])],
            )

    def test_get_raises_keyerror(self, sample_inventory):
        with pytest.raises(KeyError):
            sample_inventory.get("nope")

    def test_filter_by_site(self, sample_inventory):
        out = sample_inventory.filter(site="dc1")
        assert {d.hostname for d in out} == {"r1", "r2"}

    def test_filter_by_role_and_platform(self, sample_inventory):
        out = sample_inventory.filter(role="edge", platform=Platform.CISCO_IOS)
        assert [d.hostname for d in out] == ["r1"]

    def test_filter_by_tag_case_insensitive(self, sample_inventory):
        out = sample_inventory.filter(tag="PROD")
        assert {d.hostname for d in out} == {"r1", "r2"}

    def test_filter_by_unknown_group_raises(self, sample_inventory):
        with pytest.raises(KeyError):
            sample_inventory.filter(group="nonexistent")

    def test_filter_returns_empty_list_no_match(self, sample_inventory):
        assert sample_inventory.filter(role="impossible") == []


class TestLoader:
    def test_loads_valid_yaml(self, tmp_path):
        f = tmp_path / "inv.yml"
        f.write_text(
            """
            devices:
              - hostname: r1
                mgmt_ip: 10.0.0.1
                platform: cisco_ios
            """
        )
        inv = load_inventory(f)
        assert inv.devices[0].hostname == "r1"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(InventoryError, match="not found"):
            load_inventory(tmp_path / "missing.yml")

    def test_empty_file_raises(self, tmp_path):
        f = tmp_path / "empty.yml"
        f.write_text("")
        with pytest.raises(InventoryError, match="empty"):
            load_inventory(f)

    def test_non_mapping_root_raises(self, tmp_path):
        f = tmp_path / "list.yml"
        f.write_text("- 1\n- 2\n")
        with pytest.raises(InventoryError, match="must be a mapping"):
            load_inventory(f)

    def test_malformed_yaml_raises(self, tmp_path):
        f = tmp_path / "bad.yml"
        f.write_text("devices: [\nthis is not yaml")
        with pytest.raises(InventoryError, match="parse error"):
            load_inventory(f)

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_PASS", "secret123")
        f = tmp_path / "inv.yml"
        f.write_text(
            """
            devices:
              - hostname: r1
                mgmt_ip: 10.0.0.1
                platform: cisco_ios
                password: ${MY_PASS}
            """
        )
        inv = load_inventory(f)
        assert inv.devices[0].password == "secret123"

    def test_env_var_default_used_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UNSET_VAR", raising=False)
        f = tmp_path / "inv.yml"
        f.write_text(
            """
            devices:
              - hostname: r1
                mgmt_ip: 10.0.0.1
                platform: cisco_ios
                username: ${UNSET_VAR:fallback-user}
            """
        )
        inv = load_inventory(f)
        assert inv.devices[0].username == "fallback-user"

    def test_validation_error_wrapped(self, tmp_path):
        f = tmp_path / "inv.yml"
        f.write_text(
            """
            devices:
              - hostname: r1
                mgmt_ip: 10.0.0.1
                platform: bogus_platform
            """
        )
        with pytest.raises(InventoryError, match="validation failed"):
            load_inventory(f)
