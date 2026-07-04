"""M4: routes each post to Tier 1 (primary) or Tier 2 (break-glass fallback)."""

from __future__ import annotations

import logging
import time

from . import db
from .fetch_tier1 import (
    FetchForbidden, FetchNotFound, FetchPrivate, Tier1Fetcher,
    build_comment_rows, make_reddit, persist_thread,
)

logger = logging.getLogger(__name__)

TIER1_403_RETRY_SLEEP = 30.0


def _fetch_via_tier2(conn, cfg, post_fullname: str, permalink: str | None) -> bool:
    from .fetch_tier2 import SelectorDrift, Tier2Error, fetch_thread_tier2

    post_id = post_fullname[3:]
    permalink = permalink or f"/comments/{post_id}/"
    try:
        raw, truncated = fetch_thread_tier2(permalink, post_id, cfg)
        rows = build_comment_rows(raw)
        persist_thread(conn, raw, rows, truncated, tier=2,
                       raw_dir=cfg["paths"]["raw_dir"])
        db.log_event(conn, post_fullname, 2, "fetch_ok", f"{len(rows)} comments")
        if truncated:
            db.log_event(conn, post_fullname, 2, "truncated", "tier2 nav/depth cap")
        return True
    except SelectorDrift as exc:
        # Markup changed: fail loudly, store nothing partial.
        db.log_event(conn, post_fullname, 2, "selector_drift", str(exc))
        return False
    except Tier2Error as exc:
        db.log_event(conn, post_fullname, 2, "error", str(exc))
        return False
    except Exception as exc:
        logger.exception("tier2 failure for %s", post_fullname)
        db.log_event(conn, post_fullname, 2, "error", str(exc))
        return False


def fetch_batch(conn, cfg, env, batch_id: str, force: bool = False,
                tier2_only: bool = False) -> dict:
    """Fetch every unique post in a batch. Returns a summary dict."""
    thresholds = cfg["thresholds"]
    fullnames = db.batch_fullnames_to_fetch(
        conn, batch_id, thresholds["REFETCH_TTL_DAYS"], force
    )
    summary = {"to_fetch": len(fullnames), "fetched": 0, "failed": 0, "tier2": 0}
    if not fullnames:
        return summary

    tier1: Tier1Fetcher | None = None
    tier1_up = False
    if not tier2_only:
        tier1 = Tier1Fetcher(make_reddit(env), cfg)
        # Batch-start probe: r/announcements hot, limit 1.
        if tier1.probe():
            tier1_up = True
            db.log_event(conn, None, 1, "probe_ok")
        else:
            db.log_event(conn, None, 1, "probe_403")
            print(
                "WARNING: Reddit OAuth is blocked from this network (probe returned "
                "401/403). All posts will route to Tier 2 (old.reddit HTML)."
            )

    for fullname in fullnames:
        post_id = fullname[3:]
        fetched = False

        if tier1_up and tier1 is not None:
            fetched = _try_tier1(conn, cfg, tier1, fullname, post_id)
            if fetched is None:  # hard failure (404/private): row already marked
                db.set_row_status_by_fullname(conn, batch_id, fullname, "fetch_failed")
                summary["failed"] += 1
                conn.commit()
                continue

        if not fetched:
            existing = db.get_post(conn, fullname)
            permalink = existing["permalink"] if existing else None
            if _fetch_via_tier2(conn, cfg, fullname, permalink):
                fetched = True
                summary["tier2"] += 1

        status = "fetched" if fetched else "fetch_failed"
        db.set_row_status_by_fullname(conn, batch_id, fullname, status)
        summary["fetched" if fetched else "failed"] += 1
        conn.commit()
    return summary


def _try_tier1(conn, cfg, tier1: Tier1Fetcher, fullname: str, post_id: str):
    """Returns True on success, False to route to Tier 2, None on hard failure."""
    for attempt in (1, 2):
        try:
            raw, rows, truncated = tier1.fetch(post_id)
            persist_thread(conn, raw, rows, truncated, tier=1,
                           raw_dir=cfg["paths"]["raw_dir"])
            db.log_event(conn, fullname, 1, "fetch_ok", f"{len(rows)} comments")
            if truncated:
                db.log_event(conn, fullname, 1, "truncated", "replace_more remainder")
            return True
        except FetchForbidden as exc:
            db.log_event(conn, fullname, 1, "http_403", str(exc))
            if attempt == 1:
                logger.info("403 on %s; retrying once after %.0fs", fullname,
                            TIER1_403_RETRY_SLEEP)
                time.sleep(TIER1_403_RETRY_SLEEP)
                continue
            return False  # second 403 routes to Tier 2
        except FetchNotFound as exc:
            db.log_event(conn, fullname, 1, "http_404", str(exc))
            return None
        except FetchPrivate as exc:
            db.log_event(conn, fullname, 1, "private_sub", str(exc))
            return None
        except Exception as exc:
            logger.exception("tier1 error for %s", fullname)
            db.log_event(conn, fullname, 1, "error", str(exc))
            return None
    return False
