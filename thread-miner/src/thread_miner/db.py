"""SQLite connection, migrations, and every SQL statement in the project."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS ingest_batches (
  batch_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_filename TEXT NOT NULL,
  row_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_rows (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL REFERENCES ingest_batches(batch_id),
  raw_url TEXT NOT NULL,
  tag TEXT,
  extra_cols TEXT,
  post_fullname TEXT,
  focus_comment_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','normalized','invalid_url',
                      'unresolved_share_link','fetched','fetch_failed'))
);

CREATE TABLE IF NOT EXISTS posts (
  post_fullname TEXT PRIMARY KEY,
  post_id TEXT NOT NULL,
  subreddit TEXT NOT NULL,
  title TEXT NOT NULL,
  selftext TEXT,
  author TEXT,
  score INTEGER,
  upvote_ratio REAL,
  num_comments_reported INTEGER,
  num_comments_fetched INTEGER,
  created_utc INTEGER,
  permalink TEXT,
  link_flair_text TEXT,
  over_18 INTEGER NOT NULL DEFAULT 0,
  fetch_tier INTEGER,
  fetch_truncated INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT NOT NULL,
  raw_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
  comment_fullname TEXT PRIMARY KEY,
  post_fullname TEXT NOT NULL REFERENCES posts(post_fullname),
  parent_fullname TEXT NOT NULL,
  depth INTEGER NOT NULL,
  author TEXT,
  body TEXT,
  score INTEGER,
  created_utc INTEGER,
  is_submitter INTEGER NOT NULL DEFAULT 0,
  stickied INTEGER NOT NULL DEFAULT 0,
  distinguished TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_fullname);

CREATE TABLE IF NOT EXISTS fetch_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_fullname TEXT,
  tier INTEGER,
  event TEXT NOT NULL,
  detail TEXT,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
  run_id TEXT PRIMARY KEY,
  batch_id TEXT REFERENCES ingest_batches(batch_id),
  anthropic_batch_id TEXT,
  stage TEXT NOT NULL DEFAULT 'created'
    CHECK (stage IN ('created','chunks_submitted','chunks_done',
                     'synthesis_submitted','synthesis_done',
                     'rollup_done','report_done','failed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comment_labels (
  comment_fullname TEXT PRIMARY KEY REFERENCES comments(comment_fullname),
  run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
  sentiment TEXT NOT NULL CHECK (sentiment IN ('positive','negative','neutral','mixed'))
);

CREATE TABLE IF NOT EXISTS extracted_items (
  item_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
  post_fullname TEXT NOT NULL,
  comment_fullname TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('question','pain_point','objection','vocabulary')),
  text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_synthesis (
  post_fullname TEXT NOT NULL,
  run_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (post_fullname, run_id)
);

CREATE TABLE IF NOT EXISTS rollups (
  run_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(DDL)
    return conn


# ------------------------------------------------------------------ ingestion

def create_batch(conn, batch_id: str, source_filename: str, row_count: int) -> None:
    conn.execute(
        "INSERT INTO ingest_batches (batch_id, created_at, source_filename, row_count) "
        "VALUES (?, ?, ?, ?)",
        (batch_id, now_iso(), source_filename, row_count),
    )


def add_ingest_row(conn, batch_id: str, raw_url: str, tag, extra_cols: dict,
                   post_fullname, focus_comment_id, status: str) -> None:
    conn.execute(
        "INSERT INTO ingest_rows (batch_id, raw_url, tag, extra_cols, post_fullname, "
        "focus_comment_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, raw_url, tag, json.dumps(extra_cols) if extra_cols else None,
         post_fullname, focus_comment_id, status),
    )


def rows_for_batch(conn, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ingest_rows WHERE batch_id = ? ORDER BY row_id", (batch_id,)
    ).fetchall()


def set_row_status_by_fullname(conn, batch_id: str, post_fullname: str, status: str) -> None:
    conn.execute(
        "UPDATE ingest_rows SET status = ? WHERE batch_id = ? AND post_fullname = ?",
        (status, batch_id, post_fullname),
    )


def batch_fullnames_to_fetch(conn, batch_id: str, ttl_days: int, force: bool) -> list[str]:
    """Unique normalized fullnames in a batch, minus fresh already-fetched posts."""
    rows = conn.execute(
        "SELECT DISTINCT post_fullname FROM ingest_rows "
        "WHERE batch_id = ? AND status = 'normalized' AND post_fullname IS NOT NULL "
        "ORDER BY post_fullname",
        (batch_id,),
    ).fetchall()
    fullnames = [r["post_fullname"] for r in rows]
    if force:
        return fullnames
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for fn in fullnames:
        hit = conn.execute(
            "SELECT 1 FROM posts WHERE post_fullname = ? AND fetched_at >= ?", (fn, cutoff)
        ).fetchone()
        if hit:
            set_row_status_by_fullname(conn, batch_id, fn, "fetched")
        else:
            out.append(fn)
    return out


# ------------------------------------------------------------------ fetch

def store_thread(conn, post: dict, comments: list[dict]) -> None:
    """Insert a post and its comments in one transaction (replacing any prior fetch)."""
    with conn:
        conn.execute("DELETE FROM comment_labels WHERE comment_fullname IN "
                     "(SELECT comment_fullname FROM comments WHERE post_fullname = ?)",
                     (post["post_fullname"],))
        conn.execute("DELETE FROM comments WHERE post_fullname = ?", (post["post_fullname"],))
        conn.execute(
            "INSERT OR REPLACE INTO posts (post_fullname, post_id, subreddit, title, selftext, "
            "author, score, upvote_ratio, num_comments_reported, num_comments_fetched, "
            "created_utc, permalink, link_flair_text, over_18, fetch_tier, fetch_truncated, "
            "fetched_at, raw_path) VALUES (:post_fullname, :post_id, :subreddit, :title, "
            ":selftext, :author, :score, :upvote_ratio, :num_comments_reported, "
            ":num_comments_fetched, :created_utc, :permalink, :link_flair_text, :over_18, "
            ":fetch_tier, :fetch_truncated, :fetched_at, :raw_path)",
            post,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO comments (comment_fullname, post_fullname, parent_fullname, "
            "depth, author, body, score, created_utc, is_submitter, stickied, distinguished) "
            "VALUES (:comment_fullname, :post_fullname, :parent_fullname, :depth, :author, "
            ":body, :score, :created_utc, :is_submitter, :stickied, :distinguished)",
            comments,
        )


def log_event(conn, post_fullname, tier, event: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO fetch_log (post_fullname, tier, event, detail, at) VALUES (?, ?, ?, ?, ?)",
        (post_fullname, tier, event, detail, now_iso()),
    )
    conn.commit()


def get_post(conn, post_fullname: str):
    return conn.execute(
        "SELECT * FROM posts WHERE post_fullname = ?", (post_fullname,)
    ).fetchone()


def comments_for_post(conn, post_fullname: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM comments WHERE post_fullname = ? ORDER BY rowid", (post_fullname,)
    ).fetchall()


def comment_ids_for_post(conn, post_fullname: str) -> set[str]:
    rows = conn.execute(
        "SELECT comment_fullname FROM comments WHERE post_fullname = ?", (post_fullname,)
    ).fetchall()
    return {r["comment_fullname"] for r in rows}


def fetched_posts_for_batch(conn, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT DISTINCT p.* FROM posts p JOIN ingest_rows r ON r.post_fullname = p.post_fullname "
        "WHERE r.batch_id = ? AND r.status = 'fetched' ORDER BY p.post_fullname",
        (batch_id,),
    ).fetchall()


def tags_for_post(conn, batch_id: str, post_fullname: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT tag FROM ingest_rows WHERE batch_id = ? AND post_fullname = ? "
        "AND tag IS NOT NULL AND tag != ''",
        (batch_id, post_fullname),
    ).fetchall()
    return sorted(r["tag"] for r in rows)


# ------------------------------------------------------------------ analysis

def create_run(conn, run_id: str, batch_id: str) -> None:
    conn.execute(
        "INSERT INTO analysis_runs (run_id, batch_id, stage, created_at, updated_at) "
        "VALUES (?, ?, 'created', ?, ?)",
        (run_id, batch_id, now_iso(), now_iso()),
    )
    conn.commit()


def get_run(conn, run_id: str):
    return conn.execute(
        "SELECT * FROM analysis_runs WHERE run_id = ?", (run_id,)
    ).fetchone()


def set_run_stage(conn, run_id: str, stage: str, anthropic_batch_id: str | None = None) -> None:
    if anthropic_batch_id is not None:
        conn.execute(
            "UPDATE analysis_runs SET stage = ?, anthropic_batch_id = ?, updated_at = ? "
            "WHERE run_id = ?",
            (stage, anthropic_batch_id, now_iso(), run_id),
        )
    else:
        conn.execute(
            "UPDATE analysis_runs SET stage = ?, updated_at = ? WHERE run_id = ?",
            (stage, now_iso(), run_id),
        )
    conn.commit()


def save_labels(conn, run_id: str, labels: list[tuple[str, str]]) -> None:
    """labels: (comment_fullname, sentiment)"""
    conn.executemany(
        "INSERT OR REPLACE INTO comment_labels (comment_fullname, run_id, sentiment) "
        "VALUES (?, ?, ?)",
        [(cid, run_id, s) for cid, s in labels],
    )
    conn.commit()


def save_items(conn, run_id: str, post_fullname: str,
               items: list[tuple[str | None, str, str]]) -> None:
    """items: (comment_fullname, kind, text)"""
    conn.executemany(
        "INSERT INTO extracted_items (run_id, post_fullname, comment_fullname, kind, text) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_id, post_fullname, cid, kind, text) for cid, kind, text in items],
    )
    conn.commit()


def labels_for_post(conn, run_id: str, post_fullname: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT l.comment_fullname, l.sentiment, c.score FROM comment_labels l "
        "JOIN comments c ON c.comment_fullname = l.comment_fullname "
        "WHERE l.run_id = ? AND c.post_fullname = ?",
        (run_id, post_fullname),
    ).fetchall()


def items_for_post(conn, run_id: str, post_fullname: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM extracted_items WHERE run_id = ? AND post_fullname = ? ORDER BY item_id",
        (run_id, post_fullname),
    ).fetchall()


def save_synthesis(conn, run_id: str, post_fullname: str, payload: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO thread_synthesis (post_fullname, run_id, payload) "
        "VALUES (?, ?, ?)",
        (post_fullname, run_id, payload),
    )
    conn.commit()


def syntheses_for_run(conn, run_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT post_fullname, payload FROM thread_synthesis WHERE run_id = ?", (run_id,)
    ).fetchall()
    return {r["post_fullname"]: r["payload"] for r in rows}


def save_rollup(conn, run_id: str, payload: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO rollups (run_id, payload) VALUES (?, ?)", (run_id, payload)
    )
    conn.commit()


def get_rollup(conn, run_id: str):
    row = conn.execute("SELECT payload FROM rollups WHERE run_id = ?", (run_id,)).fetchone()
    return row["payload"] if row else None


# ------------------------------------------------------------------ reporting

def fetch_log_summary(conn, fullnames: list[str]) -> list[sqlite3.Row]:
    if not fullnames:
        return []
    marks = ",".join("?" for _ in fullnames)
    return conn.execute(
        f"SELECT event, tier, COUNT(*) AS n FROM fetch_log "
        f"WHERE post_fullname IN ({marks}) GROUP BY event, tier ORDER BY n DESC",
        fullnames,
    ).fetchall()


def failed_rows_for_batch(conn, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT raw_url, status, post_fullname FROM ingest_rows "
        "WHERE batch_id = ? AND status IN ('invalid_url','unresolved_share_link','fetch_failed') "
        "ORDER BY row_id",
        (batch_id,),
    ).fetchall()


def get_batch(conn, batch_id: str):
    return conn.execute(
        "SELECT * FROM ingest_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
