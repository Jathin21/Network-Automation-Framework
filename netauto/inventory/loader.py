"""YAML inventory loader with environment-variable expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from netauto.inventory.models import Inventory


class InventoryError(Exception):
    """Raised when an inventory file cannot be loaded or validated."""


_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:default}`` in strings."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_inventory(path: str | Path) -> Inventory:
    """Load and validate an inventory from a YAML file."""
    p = Path(path)
    if not p.is_file():
        raise InventoryError(f"inventory file not found: {p}")

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise InventoryError(f"YAML parse error in {p}: {exc}") from exc

    if raw is None:
        raise InventoryError(f"inventory file is empty: {p}")
    if not isinstance(raw, dict):
        raise InventoryError(f"inventory root must be a mapping, got {type(raw).__name__}")

    expanded = _expand_env(raw)
    try:
        return Inventory.model_validate(expanded)
    except ValidationError as exc:
        raise InventoryError(f"inventory validation failed for {p}:\n{exc}") from exc
