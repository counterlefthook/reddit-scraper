"""Shared FastAPI dependencies and app configuration loading."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request

from thread_miner import db, load_config, load_env

BASE_DIR = Path(__file__).resolve().parent.parent  # the thread-miner/ checkout


def app_config() -> dict:
    """Config path is overridable (TM_CONFIG) so tests can point at a temp dir."""
    return load_config(os.environ.get("TM_CONFIG", str(BASE_DIR / "config.yaml")))


def app_env() -> dict:
    return load_env(BASE_DIR / ".env")


def get_conn(request: Request):
    """Per-request SQLite connection (sqlite3 connections are per-thread)."""
    conn = db.connect(request.app.state.cfg["paths"]["db"])
    try:
        yield conn
    finally:
        conn.close()
