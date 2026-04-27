"""Inventory loading and device models."""

from netauto.inventory.loader import InventoryError, load_inventory
from netauto.inventory.models import Device, DeviceGroup, Inventory, Platform

__all__ = ["Device", "DeviceGroup", "Inventory", "InventoryError", "Platform", "load_inventory"]
