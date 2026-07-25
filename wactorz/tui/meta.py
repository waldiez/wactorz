"""Static metadata for the TUI (kept tiny to avoid import cycles)."""

from __future__ import annotations

try:
    from .._version import __version__ as VERSION
except ImportError:  # pragma: no cover - version file is always present in-tree
    VERSION = "0.0.0"

SUBTITLE = "actor-model multi-agent runtime"

__all__ = ["SUBTITLE", "VERSION"]
