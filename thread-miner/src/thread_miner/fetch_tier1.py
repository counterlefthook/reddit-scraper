"""M2: Tier 1 fetcher. PRAW against oauth.reddit.com, read-only, token-bucket limited."""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from pathlib import Path

import praw
import prawcore
from tenacity import (
    retry, retry_if_exception_type, stop_after_attempt, wait_exponential,
)

from . import db

logger = logging.getLogger(__name__)


class FetchForbidden(Exception):
    """HTTP 403: routes to Tier 2 (never retried here)."""


class FetchNotFound(Exception):
    """HTTP 404: post removed or bad id."""


class FetchPrivate(Exception):
    """Private or quarantined subreddit."""


class TokenBucket:
    """Client-side rate limiter. Wraps every PRAW-triggering call."""

    def __init__(self, qpm: int) -> None:
        self.capacity = float(qpm)
        self.tokens = float(qpm)
        self.rate = qpm / 60.0  # tokens per second
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                time.sleep((1 - self.tokens) / self.rate)


def make_reddit(env: dict[str, str]) -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=env["REDDIT_CLIENT_ID"],
        client_secret=env["REDDIT_CLIENT_SECRET"],
        user_agent=env["REDDIT_USER_AGENT"],
    )
    if reddit.read_only is not True:
        raise SystemExit(
            "Refusing to start: PRAW client is not in read-only mode. Thread Miner "
            "must never hold write credentials; remove username/password from the config."
        )
    return reddit


def _author_name(thing) -> str | None:
    author = getattr(thing, "author", None)
    return getattr(author, "name", None) if author else None


def _walk_forest(forest, out: list) -> None:
    """Depth-first walk preserving Reddit's display order."""
    for comment in forest:
        out.append(comment)
        _walk_forest(comment.replies, out)


class Tier1Fetcher:
    def __init__(self, reddit: praw.Reddit, cfg: dict) -> None:
        self.reddit = reddit
        self.cfg = cfg
        self.limiter = TokenBucket(cfg["thresholds"]["CLIENT_QPM"])
        self.max_more = cfg["thresholds"]["MAX_MORE_CALLS"]
        retries = cfg["thresholds"]["FETCH_RETRIES"]
        # tenacity on 5xx/429 only; 403/404 route instead (spec section 9).
        self._fetch_with_retry = retry(
            retry=retry_if_exception_type(
                (prawcore.exceptions.ServerError, prawcore.exceptions.TooManyRequests)
            ),
            stop=stop_after_attempt(retries),
            wait=wait_exponential(multiplier=2, min=2, max=8),
            reraise=True,
        )(self._fetch_once)

    def probe(self) -> bool:
        """Batch-start health probe: r/announcements hot listing, limit 1."""
        self.limiter.acquire()
        try:
            next(iter(self.reddit.subreddit("announcements").hot(limit=1)), None)
            return True
        except (prawcore.exceptions.Forbidden, prawcore.exceptions.OAuthException,
                prawcore.exceptions.ResponseException) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403) or isinstance(exc, prawcore.exceptions.Forbidden):
                return False
            raise

    def fetch(self, post_id: str) -> tuple[dict, list[dict], bool]:
        """Fetch one thread. Returns (raw_thread, comment_rows, truncated)."""
        try:
            return self._fetch_with_retry(post_id)
        except prawcore.exceptions.Forbidden as exc:
            raise FetchForbidden(str(exc)) from exc
        except prawcore.exceptions.NotFound as exc:
            raise FetchNotFound(str(exc)) from exc

    def _fetch_once(self, post_id: str) -> tuple[dict, list[dict], bool]:
        self.limiter.acquire()
        submission = self.reddit.submission(id=post_id)
        try:
            _ = submission.title  # force the fetch
        except prawcore.exceptions.Forbidden as exc:
            # Distinguish private/quarantined from generic 403 where possible.
            reason = str(exc).lower()
            if "private" in reason or "quarantine" in reason:
                raise FetchPrivate(str(exc)) from exc
            raise

        self.limiter.acquire()
        remaining = submission.comments.replace_more(limit=self.max_more)
        truncated = len(remaining) > 0

        flat: list = []
        _walk_forest(submission.comments, flat)

        raw = {
            "tier": 1,
            "post": {
                "id": submission.id,
                "fullname": submission.fullname,
                "subreddit": submission.subreddit.display_name,
                "title": submission.title,
                "selftext": submission.selftext,
                "author": _author_name(submission),
                "score": submission.score,
                "upvote_ratio": submission.upvote_ratio,
                "num_comments": submission.num_comments,
                "created_utc": submission.created_utc,
                "permalink": submission.permalink,
                "link_flair_text": submission.link_flair_text,
                "over_18": submission.over_18,
            },
            "comments": [
                {
                    "fullname": c.fullname,
                    "parent_fullname": c.parent_id,
                    "author": _author_name(c),
                    "body": c.body,
                    "score": c.score,
                    "created_utc": c.created_utc,
                    "is_submitter": bool(getattr(c, "is_submitter", False)),
                    "stickied": bool(getattr(c, "stickied", False)),
                    "distinguished": getattr(c, "distinguished", None),
                }
                for c in flat
            ],
        }
        return raw, build_comment_rows(raw), truncated


def compute_depths(comments: list[dict]) -> dict[str, int]:
    """Depth by walking parent_id chains; a t3 parent means depth 0."""
    parents = {c["fullname"]: c["parent_fullname"] for c in comments}
    depths: dict[str, int] = {}

    def depth_of(fullname: str) -> int:
        if fullname in depths:
            return depths[fullname]
        parent = parents.get(fullname, "t3_")
        d = 0 if parent.startswith("t3_") else depth_of(parent) + 1
        depths[fullname] = d
        return d

    for c in comments:
        depth_of(c["fullname"])
    return depths


def build_comment_rows(raw: dict) -> list[dict]:
    depths = compute_depths(raw["comments"])
    post_fullname = raw["post"]["fullname"]
    return [
        {
            "comment_fullname": c["fullname"],
            "post_fullname": post_fullname,
            "parent_fullname": c["parent_fullname"],
            "depth": depths[c["fullname"]],
            # [deleted]/[removed] comments keep their row; author becomes NULL.
            "author": c["author"],
            "body": c["body"],
            "score": c["score"],
            "created_utc": int(c["created_utc"] or 0),
            "is_submitter": int(bool(c["is_submitter"])),
            "stickied": int(bool(c["stickied"])),
            "distinguished": c["distinguished"],
        }
        for c in raw["comments"]
    ]


def persist_thread(conn, raw: dict, comment_rows: list[dict], truncated: bool,
                   tier: int, raw_dir: str | Path) -> dict:
    """Write raw gz first, then insert rows in one transaction. Returns the post row."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = raw["post"]
    raw_path = raw_dir / f"{p['id']}.json.gz"
    with gzip.open(raw_path, "wt", encoding="utf-8") as f:
        json.dump(raw, f)

    post_row = {
        "post_fullname": p["fullname"],
        "post_id": p["id"],
        "subreddit": p["subreddit"],
        "title": p["title"],
        "selftext": p["selftext"],
        "author": p["author"],
        "score": p["score"],
        "upvote_ratio": p["upvote_ratio"],
        "num_comments_reported": p["num_comments"],
        "num_comments_fetched": len(comment_rows),
        "created_utc": int(p["created_utc"] or 0),
        "permalink": p["permalink"],
        "link_flair_text": p["link_flair_text"],
        "over_18": int(bool(p["over_18"])),
        "fetch_tier": tier,
        "fetch_truncated": int(truncated),
        "fetched_at": db.now_iso(),
        "raw_path": str(raw_path),
    }
    db.store_thread(conn, post_row, comment_rows)
    return post_row
