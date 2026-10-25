"""Load config.yaml into a plain dict."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # When deployed, keep the database and backups on the persistent disk.
    state_dir = os.environ.get("BOTS_STATE_DIR")
    if state_dir:
        cfg["database_path"] = str(Path(state_dir) / "bots.db")
        cfg["worker"]["backups_dir"] = str(Path(state_dir) / "backups")
    return cfg


def resolve_path(p: str | Path) -> Path:
    """Relative paths in the config are relative to the project root."""
    p = Path(p)
    return p if p.is_absolute() else PROJECT_DIR / p
