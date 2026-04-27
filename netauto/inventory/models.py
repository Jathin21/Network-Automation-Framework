"""Pydantic models for device inventory."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class Platform(str, Enum):
    """Supported network device platforms.

    Values are aligned with Netmiko's ``device_type`` strings so they can be
    passed through directly without translation.
    """

    CISCO_IOS = "cisco_ios"
    CISCO_XR = "cisco_xr"
    CISCO_NXOS = "cisco_nxos"
    JUNIPER_JUNOS = "juniper_junos"
    ARISTA_EOS = "arista_eos"

    @property
    def napalm_driver(self) -> str:
        return {
            Platform.CISCO_IOS: "ios",
            Platform.CISCO_XR: "iosxr",
            Platform.CISCO_NXOS: "nxos_ssh",
            Platform.JUNIPER_JUNOS: "junos",
            Platform.ARISTA_EOS: "eos",
        }[self]


class Device(BaseModel):
    """A single network device.

    The model is deliberately strict: missing hostnames or unknown platforms fail
    fast at load time rather than during a 3am cutover.
    """

    hostname: str = Field(..., min_length=1)
    mgmt_ip: str = Field(...)
    platform: Platform
    username: str = Field(default="netauto")
    password: str = Field(default="", repr=False)
    port: int = Field(default=22, ge=1, le=65535)
    secret: str = Field(default="", repr=False)
    site: str = Field(default="default")
    role: str = Field(default="unknown")
    tags: list[str] = Field(default_factory=list)

    @field_validator("hostname")
    @classmethod
    def _hostname_no_whitespace(cls, v: str) -> str:
        if any(c.isspace() for c in v):
            raise ValueError("hostname must not contain whitespace")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_lowercased(cls, v: list[str]) -> list[str]:
        return [t.strip().lower() for t in v if t.strip()]

    def netmiko_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "device_type": self.platform.value,
            "host": self.mgmt_ip,
            "username": self.username,
            "password": self.password,
            "port": self.port,
        }
        if self.secret:
            params["secret"] = self.secret
        return params

    def napalm_params(self) -> dict[str, Any]:
        return {
            "driver": self.platform.napalm_driver,
            "hostname": self.mgmt_ip,
            "username": self.username,
            "password": self.password,
            "optional_args": {"port": self.port, "secret": self.secret},
        }


class DeviceGroup(BaseModel):
    name: str
    members: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)


class Inventory(BaseModel):
    devices: list[Device]
    groups: list[DeviceGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _hostnames_unique(self) -> Inventory:
        seen: set[str] = set()
        for d in self.devices:
            if d.hostname in seen:
                raise ValueError(f"duplicate hostname in inventory: {d.hostname}")
            seen.add(d.hostname)
        return self

    @model_validator(mode="after")
    def _group_members_resolve(self) -> Inventory:
        hostnames = {d.hostname for d in self.devices}
        for g in self.groups:
            missing = set(g.members) - hostnames
            if missing:
                raise ValueError(
                    f"group '{g.name}' references unknown hosts: {sorted(missing)}"
                )
        return self

    def get(self, hostname: str) -> Device:
        for d in self.devices:
            if d.hostname == hostname:
                return d
        raise KeyError(hostname)

    def filter(
        self,
        *,
        platform: Platform | None = None,
        site: str | None = None,
        role: str | None = None,
        tag: str | None = None,
        group: str | None = None,
    ) -> list[Device]:
        members: set[str] | None = None
        if group is not None:
            for g in self.groups:
                if g.name == group:
                    members = set(g.members)
                    break
            else:
                raise KeyError(f"unknown group: {group}")

        out: list[Device] = []
        for d in self.devices:
            if platform is not None and d.platform != platform:
                continue
            if site is not None and d.site != site:
                continue
            if role is not None and d.role != role:
                continue
            if tag is not None and tag.lower() not in d.tags:
                continue
            if members is not None and d.hostname not in members:
                continue
            out.append(d)
        return out
