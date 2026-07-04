"""M1: spreadsheet ingestion. CSV/XLSX with a required url column, optional tag column."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from . import db
from .normalize import NORMALIZED, normalize_url, resolve_share_link


class IngestError(Exception):
    pass


def _read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str)
    if suffix in (".xlsx", ".xlsm"):
        return pd.read_excel(path, dtype=str)
    raise IngestError(f"unsupported file type '{suffix}': expected .csv or .xlsx")


def ingest_file(
    conn,
    path: str | Path,
    tag_col: str = "tag",
    share_resolve_timeout: float = 10.0,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Ingest a spreadsheet of Reddit URLs. Returns the new batch_id."""
    path = Path(path)
    if not path.exists():
        raise IngestError(f"file not found: {path}")
    df = _read_table(path)
    df.columns = [str(c).strip() for c in df.columns]
    if "url" not in df.columns:
        raise IngestError("input file must have a 'url' column")

    resolver = share_resolver or (
        lambda url: resolve_share_link(url, timeout=share_resolve_timeout)
    )

    batch_id = str(uuid.uuid4())
    db.create_batch(conn, batch_id, path.name, len(df))

    for _, row in df.iterrows():
        raw_url = str(row["url"]) if pd.notna(row["url"]) else ""
        tag = None
        if tag_col in df.columns and pd.notna(row.get(tag_col)):
            tag = str(row[tag_col]).strip() or None
        extra = {
            col: str(row[col])
            for col in df.columns
            if col not in ("url", tag_col) and pd.notna(row[col])
        }
        result = normalize_url(raw_url, share_resolver=resolver)
        db.add_ingest_row(
            conn, batch_id, raw_url, tag, extra,
            result.post_fullname if result.status == NORMALIZED else None,
            result.focus_comment_id,
            result.status,
        )
    conn.commit()
    return batch_id


def batch_summary(conn, batch_id: str) -> dict:
    rows = db.rows_for_batch(conn, batch_id)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    unique = {r["post_fullname"] for r in rows if r["post_fullname"]}
    return {"rows": len(rows), "by_status": by_status, "unique_posts": len(unique)}
