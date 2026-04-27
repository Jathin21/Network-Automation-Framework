"""Cross-cutting utilities: logging, diff."""

from netauto.utils.diff import unified_config_diff
from netauto.utils.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger", "unified_config_diff"]
