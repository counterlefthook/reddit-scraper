"""Worker job execution and restart-recovery paths, with the pipeline mocked."""

import pytest

from thread_miner import PipelineError, db

import webapp.worker as worker


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


CFG = {"thresholds": {"SHARE_RESOLVE_TIMEOUT": 1.0},
       "paths": {"db": "unused", "raw_dir": "unused", "reports_dir": "unused",
                 "uploads_dir": "unused"}}
REDDIT_ENV = {"REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "s",
              "REDDIT_USER_AGENT": "ua", "ANTHROPIC_API_KEY": "k"}


def make_job(conn, job_id="job1", **updates):
    db.create_job(conn, job_id, "Chris", "urls.csv", "/tmp/urls.csv")
    if updates:
        if updates.get("batch_id") and db.get_batch(conn, updates["batch_id"]) is None:
            db.create_batch(conn, updates["batch_id"], "urls.csv", 1)
        db.update_job(conn, job_id, **updates)
    return db.get_job(conn, job_id)


def wire_happy_pipeline(monkeypatch, conn, calls):
    def fake_ingest(c, path, tag_col, share_resolve_timeout):
        calls.append("ingest")
        db.create_batch(c, "batch1", "urls.csv", 1)
        return "batch1"

    def fake_fetch(c, cfg, env, batch_id, **kwargs):
        calls.append(("fetch", kwargs))

    def fake_analysis(c, cfg, env, batch_id, resume_run_id=None,
                      on_run_created=None, **kwargs):
        calls.append(("analyze", resume_run_id))
        run_id = resume_run_id or "run1"
        if resume_run_id is None:
            db.create_run(c, run_id, batch_id)
            if on_run_created:
                on_run_created(run_id)
        db.set_run_stage(c, run_id, "synthesis_done")
        return run_id

    def fake_rollup(c, cfg, env, run_id):
        calls.append("rollup")
        db.set_run_stage(c, run_id, "rollup_done")

    def fake_report(c, cfg, run_id):
        calls.append("report")
        db.set_run_stage(c, run_id, "report_done")

    monkeypatch.setattr(worker, "ingest_file", fake_ingest)
    monkeypatch.setattr(worker, "fetch_batch", fake_fetch)
    monkeypatch.setattr(worker, "run_analysis", fake_analysis)
    monkeypatch.setattr(worker, "run_rollup", fake_rollup)
    monkeypatch.setattr(worker, "render_report", fake_report)


def test_process_job_happy_path(conn, monkeypatch):
    calls = []
    wire_happy_pipeline(monkeypatch, conn, calls)
    job = make_job(conn, status="running")

    worker.process_job(conn, CFG, REDDIT_ENV, job)

    job = db.get_job(conn, "job1")
    assert job["status"] == "done"
    assert job["batch_id"] == "batch1"
    assert job["run_id"] == "run1"
    assert calls[0] == "ingest"
    # with Reddit creds and TIER2_ENABLED unset, the worker forces tier1_only
    assert ("fetch", {"tier1_only": True}) in calls
    assert "rollup" in calls and "report" in calls


def test_pipeline_error_marks_job_failed_not_crashed(conn, monkeypatch):
    monkeypatch.setattr(worker, "ingest_file", lambda *a, **k: "batch1")
    monkeypatch.setattr(
        worker, "fetch_batch",
        lambda *a, **k: (_ for _ in ()).throw(PipelineError("reddit down")),
    )
    job = make_job(conn, status="running")

    worker.process_job(conn, CFG, REDDIT_ENV, job)  # must not raise

    job = db.get_job(conn, "job1")
    assert job["status"] == "failed"
    assert "reddit down" in job["error"]


def test_no_credentials_fails_with_clear_message(conn, monkeypatch):
    monkeypatch.delenv("TIER2_ENABLED", raising=False)
    monkeypatch.setattr(worker, "ingest_file", lambda *a, **k: "batch1")
    job = make_job(conn, status="running")

    worker.process_job(conn, CFG, {"ANTHROPIC_API_KEY": "k"}, job)

    job = db.get_job(conn, "job1")
    assert job["status"] == "failed"
    assert "no Reddit API credentials" in job["error"]


def test_tier2_enabled_without_reddit_creds_uses_tier2(conn, monkeypatch):
    monkeypatch.setenv("TIER2_ENABLED", "true")
    calls = []
    wire_happy_pipeline(monkeypatch, conn, calls)
    job = make_job(conn, status="running")

    worker.process_job(conn, CFG, {"ANTHROPIC_API_KEY": "k"}, job)

    assert ("fetch", {"tier2_only": True}) in calls
    assert db.get_job(conn, "job1")["status"] == "done"


def test_recovery_resumes_analysis_by_run_id(conn, monkeypatch):
    """Restart mid-analysis: job already has batch_id + run_id; the worker must
    resume the existing run (no re-ingest, no re-fetch) and finish it."""
    calls = []
    wire_happy_pipeline(monkeypatch, conn, calls)
    db.create_batch(conn, "batch1", "urls.csv", 1)
    db.create_run(conn, "run1", "batch1")
    db.set_run_stage(conn, "run1", "chunks_submitted")
    job = make_job(conn, status="running", batch_id="batch1", run_id="run1")

    worker.process_job(conn, CFG, REDDIT_ENV, job)

    assert "ingest" not in calls
    assert not any(isinstance(c, tuple) and c[0] == "fetch" for c in calls)
    assert ("analyze", "run1") in calls
    assert db.get_job(conn, "job1")["status"] == "done"


def test_recovery_refetches_when_only_batch_exists(conn, monkeypatch):
    """Restart mid-fetch: batch_id set, no run_id. Fetch re-runs (idempotent
    via TTL) and analysis starts fresh."""
    calls = []
    wire_happy_pipeline(monkeypatch, conn, calls)
    job = make_job(conn, status="running", batch_id="batch1")

    worker.process_job(conn, CFG, REDDIT_ENV, job)

    assert "ingest" not in calls
    assert ("fetch", {"tier1_only": True}) in calls
    assert ("analyze", None) in calls
    assert db.get_job(conn, "job1")["status"] == "done"


def test_claim_order_is_fifo(conn):
    make_job(conn, "job-a")
    make_job(conn, "job-b")
    first = db.claim_next_job(conn)
    second = db.claim_next_job(conn)
    assert first["job_id"] == "job-a"
    assert second["job_id"] == "job-b"
    assert first["status"] == "running"
    assert db.claim_next_job(conn) is None
