"""Portable locations and path resolution for runtime state."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    """Return the per-user directory used for mutable display state."""

    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "UsageMaxDisplay"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "UsageMax Display"
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "usagemax-display"


def resolve_path(value: str | Path, base: Path) -> Path:
    """Expand a path and resolve relative values under ``base``."""

    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path
