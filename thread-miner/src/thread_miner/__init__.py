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
    """Read secrets from the process env, falling back to a local .env file."""
    values: dict[str, str] = {}
    env_file = Path(env_path)
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    for key in _ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values
