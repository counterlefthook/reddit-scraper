"""Background job worker: one daemon thread, one pipeline job at a time.

Queue state lives in the SQLite `jobs` table, so a container restart loses
nothing: the startup recovery scan resumes interrupted jobs from whatever
stage they reached (fetch is idempotent via the refetch TTL; analysis resumes
via the persisted run stage and Anthropic batch id).
"""

from __future__ import annotations

import logging
import os
import threading
import time

from thread_miner import PipelineError, db
from thread_miner.analysis import run_analysis
from thread_miner.ingest import IngestError, ingest_file
from thread_miner.report import render_report
from thread_miner.rollup import run_rollup
from thread_miner.router import fetch_batch

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
REDDIT_KEYS = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")


def _fetch(conn, cfg, env, batch_id: str) -> None:
    """Pick the fetch transport from available credentials + environment.

    Hosted (Tier 1 only): Reddit blocks datacenter HTML scraping, and the
    container has no Chromium, so the Tier 2 fallback is disabled unless
    TIER2_ENABLED=true (a local/homelab deployment on a residential IP).
    """
    have_reddit = all(env.get(k) for k in REDDIT_KEYS)
    tier2_enabled = os.environ.get("TIER2_ENABLED", "false").lower() == "true"
    if have_reddit:
        fetch_batch(conn, cfg, env, batch_id, tier1_only=not tier2_enabled)
    elif tier2_enabled:
        fetch_batch(conn, cfg, env, batch_id, tier2_only=True)
    else:
        raise PipelineError(
            "no Reddit API credentials configured and TIER2_ENABLED is false; "
            "set REDDIT_CLIENT_ID/SECRET/USER_AGENT (or TIER2_ENABLED=true on a "
            "residential-IP deployment)"
        )


def process_job(conn, cfg, env, job) -> None:
    """Run one job through the pipeline, resuming from persisted state."""
    job_id = job["job_id"]
    batch_id = job["batch_id"]
    run_id = job["run_id"]
    try:
        if not batch_id:
            db.update_job(conn, job_id, phase="ingest")
            batch_id = ingest_file(
                conn, job["upload_path"], tag_col=job["tag_col"] or "tag",
                share_resolve_timeout=cfg["thresholds"]["SHARE_RESOLVE_TIMEOUT"],
            )
            db.update_job(conn, job_id, batch_id=batch_id)

        if not run_id:
            db.update_job(conn, job_id, phase="fetch")
            _fetch(conn, cfg, env, batch_id)
            db.update_job(conn, job_id, phase="analyze")
            run_id = run_analysis(
                conn, cfg, env, batch_id,
                on_run_created=lambda rid: db.update_job(conn, job_id, run_id=rid),
            )
        else:
            db.update_job(conn, job_id, phase="analyze")
            run_id = run_analysis(conn, cfg, env, batch_id, resume_run_id=run_id)

        stage = db.get_run(conn, run_id)["stage"]
        if stage == "synthesis_done":
            db.update_job(conn, job_id, phase="rollup")
            run_rollup(conn, cfg, env, run_id)
            stage = "rollup_done"
        if stage != "report_done":
            db.update_job(conn, job_id, phase="report")
            render_report(conn, cfg, run_id)

        db.update_job(conn, job_id, status="done", phase="report",
                      finished_at=db.now_iso(), error=None)
        logger.info("job %s finished (run %s)", job_id, run_id)
    except (PipelineError, IngestError) as exc:
        logger.warning("job %s failed: %s", job_id, exc)
        db.update_job(conn, job_id, status="failed", error=str(exc),
                      finished_at=db.now_iso())
    except Exception as exc:  # a crashed job must never kill the worker loop
        logger.exception("job %s crashed", job_id)
        db.update_job(conn, job_id, status="failed",
                      error=f"unexpected error: {exc}", finished_at=db.now_iso())


class Worker(threading.Thread):
    def __init__(self, cfg: dict, env: dict) -> None:
        super().__init__(name="tm-worker", daemon=True)
        self.cfg = cfg
        self.env = env
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # sqlite3 connections are per-thread: the worker owns this one.
        conn = db.connect(self.cfg["paths"]["db"])
        try:
            self.recover(conn)
            while not self._stop.is_set():
                job = db.claim_next_job(conn)
                if job is None:
                    self._stop.wait(POLL_SECONDS)
                    continue
                logger.info("job %s claimed (%s)", job["job_id"],
                            job["source_filename"])
                process_job(conn, self.cfg, self.env, job)
        finally:
            conn.close()

    def recover(self, conn) -> None:
        """Resume jobs the previous process left mid-flight."""
        for job in db.running_jobs(conn):
            logger.info("recovering interrupted job %s (batch=%s run=%s)",
                        job["job_id"], job["batch_id"], job["run_id"])
            process_job(conn, self.cfg, self.env, job)
