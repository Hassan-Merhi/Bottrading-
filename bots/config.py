"""Load config.yaml into a plain dict."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_path(p: str | Path) -> Path:
    """Relative paths in the config are relative to the project root."""
    p = Path(p)
    return p if p.is_absolute() else PROJECT_DIR / p
