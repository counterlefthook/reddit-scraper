"""FastAPI app: login, CSV upload, job progress, report viewing/downloads."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from thread_miner import db

from .auth import (
    COOKIE_NAME, MAX_AGE_SECONDS, LoginRequired, check_password,
    current_user, login_redirect, require_login, session_cookie_value,
)
from .deps import app_config, app_env, get_conn
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xlsm"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# analysis_runs.stage -> coarse overall percentage
STAGE_PCT = {
    "created": 62, "chunks_submitted": 68, "chunks_done": 78,
    "synthesis_submitted": 84, "synthesis_done": 90,
    "rollup_done": 95, "report_done": 100, "failed": 100,
}
PHASE_LABEL = {
    "ingest": "Reading your spreadsheet",
    "fetch": "Downloading Reddit threads",
    "analyze": "Analyzing comments with Claude",
    "rollup": "Summarizing across threads",
    "report": "Building the report",
}


def create_app() -> FastAPI:
    app = FastAPI(title="Thread Miner")
    app.state.cfg = app_config()
    app.state.env = app_env()

    here = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(here / "templates"))
    app.mount("/static", StaticFiles(directory=str(here / "static")), name="static")

    @app.on_event("startup")
    def start_worker() -> None:
        if os.environ.get("TM_DISABLE_WORKER") == "1":
            logger.info("worker disabled (TM_DISABLE_WORKER=1)")
            return
        app.state.worker = Worker(app.state.cfg, app.state.env)
        app.state.worker.start()

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not logged in"}, status_code=401)
        return login_redirect()

    # ------------------------------------------------------------------ auth

    @app.get("/login")
    def login_page(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"error": None, "password_set": bool(os.environ.get("APP_PASSWORD"))}
        )

    @app.post("/login")
    def login_submit(request: Request, name: str = Form(...), password: str = Form(...)):
        name = name.strip()[:60]
        if not name or not check_password(password):
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Wrong password (or missing name).",
                 "password_set": bool(os.environ.get("APP_PASSWORD"))},
                status_code=401,
            )
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE_NAME, session_cookie_value(name), max_age=MAX_AGE_SECONDS,
            httponly=True, samesite="lax",
            secure=os.environ.get("TM_INSECURE_COOKIE") != "1",
        )
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ------------------------------------------------------------- dashboard

    @app.get("/")
    def index(request: Request, user: str = Depends(require_login),
              conn=Depends(get_conn)):
        jobs = [dict(j) for j in db.list_jobs(conn)]
        return templates.TemplateResponse(
            request, "index.html", {"user": user, "jobs": jobs}
        )

    @app.post("/jobs")
    async def create_job(request: Request, user: str = Depends(require_login),
                         conn=Depends(get_conn),
                         file: UploadFile = File(...),
                         tag_col: str = Form("tag")):
        filename = Path(file.filename or "upload").name
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            return templates.TemplateResponse(
                request, "index.html",
                {"user": user, "jobs": [dict(j) for j in db.list_jobs(conn)],
                 "upload_error": f"Unsupported file type '{suffix}'. "
                                 "Upload a .csv or .xlsx with a 'url' column."},
                status_code=400,
            )
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            return templates.TemplateResponse(
                request, "index.html",
                {"user": user, "jobs": [dict(j) for j in db.list_jobs(conn)],
                 "upload_error": "File is too large (10 MB max)."},
                status_code=400,
            )
        job_id = uuid.uuid4().hex[:12]
        upload_dir = Path(request.app.state.cfg["paths"]["uploads_dir"]) / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / filename
        upload_path.write_bytes(content)
        db.create_job(conn, job_id, user, filename, str(upload_path),
                      tag_col=(tag_col.strip() or "tag"))
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    # ------------------------------------------------------------------ jobs

    def _job_or_none(conn, job_id: str):
        return db.get_job(conn, job_id)

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: str,
                 user: str = Depends(require_login), conn=Depends(get_conn)):
        job = _job_or_none(conn, job_id)
        if job is None:
            return templates.TemplateResponse(
                request, "index.html",
                {"user": user, "jobs": [dict(j) for j in db.list_jobs(conn)],
                 "upload_error": "That run does not exist."},
                status_code=404,
            )
        return templates.TemplateResponse(
            request, "job.html", {"user": user, "job": dict(job)}
        )

    @app.get("/api/jobs/{job_id}/progress")
    def job_progress(request: Request, job_id: str,
                     user: str = Depends(require_login), conn=Depends(get_conn)):
        job = _job_or_none(conn, job_id)
        if job is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)

        pct = 0
        detail = "Waiting in the queue"
        events: list[dict] = []
        if job["status"] in ("running", "done", "failed"):
            phase = job["phase"] or "ingest"
            detail = PHASE_LABEL.get(phase, phase)
            if phase == "ingest":
                pct = 5
            elif phase == "fetch" and job["batch_id"]:
                counts = db.batch_row_status_counts(conn, job["batch_id"])
                total = sum(counts.get(s, 0) for s in
                            ("normalized", "fetched", "fetch_failed"))
                done = counts.get("fetched", 0) + counts.get("fetch_failed", 0)
                pct = 10 + int(50 * done / total) if total else 10
                detail = f"Downloading Reddit threads ({done} of {total})"
            elif job["run_id"]:
                run = db.get_run(conn, job["run_id"])
                pct = STAGE_PCT.get(run["stage"], 62) if run else 62
            else:
                pct = 60
            if job["batch_id"]:
                events = [dict(e) for e in
                          db.recent_fetch_events(conn, job["batch_id"], limit=6)]
        if job["status"] == "done":
            pct, detail = 100, "Done"
        if job["status"] == "failed":
            detail = "Failed"
        return {
            "job_id": job_id, "status": job["status"], "phase": job["phase"],
            "pct": pct, "detail": detail, "error": job["error"],
            "events": events, "report_ready": job["status"] == "done",
        }

    # ---------------------------------------------------------------- report

    def _report_dir(request: Request, conn, job_id: str) -> Path | None:
        job = _job_or_none(conn, job_id)
        if job is None or job["status"] != "done" or not job["run_id"]:
            return None
        # run_id comes from our own DB (a uuid we minted), never from the URL.
        return Path(request.app.state.cfg["paths"]["reports_dir"]) / job["run_id"]

    @app.get("/jobs/{job_id}/report")
    def report_inline(request: Request, job_id: str,
                      user: str = Depends(require_login), conn=Depends(get_conn)):
        report_dir = _report_dir(request, conn, job_id)
        if report_dir is None or not (report_dir / "report.html").exists():
            return JSONResponse({"error": "report not ready"}, status_code=404)
        return FileResponse(
            report_dir / "report.html", media_type="text/html",
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy":
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; img-src data:",
            },
        )

    @app.get("/jobs/{job_id}/report.html")
    def report_download(request: Request, job_id: str,
                        user: str = Depends(require_login), conn=Depends(get_conn)):
        report_dir = _report_dir(request, conn, job_id)
        if report_dir is None or not (report_dir / "report.html").exists():
            return JSONResponse({"error": "report not ready"}, status_code=404)
        return FileResponse(report_dir / "report.html", media_type="text/html",
                            filename=f"thread-miner-report-{job_id}.html")

    @app.get("/jobs/{job_id}/question_bank.csv")
    def question_bank_download(request: Request, job_id: str,
                               user: str = Depends(require_login),
                               conn=Depends(get_conn)):
        report_dir = _report_dir(request, conn, job_id)
        if report_dir is None or not (report_dir / "question_bank.csv").exists():
            return JSONResponse({"error": "not ready"}, status_code=404)
        return FileResponse(report_dir / "question_bank.csv", media_type="text/csv",
                            filename=f"question_bank-{job_id}.csv")

    return app


app = create_app()
