"""Thread Miner: Reddit discussion intelligence pipeline."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

__version__ = "1.0.0"

_ENV_KEYS = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT", "ANTHROPIC_API_KEY")


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """Load config.yaml. Paths are resolved relative to the config file's directory."""
    path = Path(path)
    with open(path) as f:
        cfg = yaml.safe_load(f)
    base = path.resolve().parent
    cfg["paths"] = {k: str((base / v)) for k, v in cfg["paths"].items()}
    return cfg


def load_env(env_path: str | Path = ".env") -> dict[str, str]:
    """Read secrets: the project's .env file wins over process env vars.

    A machine may carry stale keys in its environment (set long ago by some
    other tool); for a local project the .env sitting next to the code is
    what the user intends, so it takes precedence.
    """
    values: dict[str, str] = {}
    for key in _ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    env_file = Path(env_path)
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if val:
                    values[key.strip()] = val
    return values
