"""Web UI: upload an Excel/CSV of Reddit URLs, watch progress, download results.

Run with:  python app.py   then open http://127.0.0.1:5000
"""

from __future__ import annotations

import logging
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from reddit_scraper.excel_io import read_urls, write_results
from reddit_scraper.scraper import RedditScraper

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload cap

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
WORK_DIR = Path(tempfile.gettempdir()) / "reddit-scraper-jobs"
WORK_DIR.mkdir(exist_ok=True)

ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".txt"}


def _run_job(job_id: str, urls: list[str], delay: float, expand_more: bool) -> None:
    def progress(done: int, total: int, result) -> None:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["done"] = done
            job["comments"] += len(result.comments)
            if not result.ok:
                job["failures"].append({"url": result.source_url, "error": result.error})

    try:
        scraper = RedditScraper(delay=delay, expand_more=expand_more)
        results = scraper.scrape_all(urls, progress=progress)
        out_path = WORK_DIR / f"reddit_results_{job_id}.xlsx"
        write_results(out_path, results)
        with JOBS_LOCK:
            JOBS[job_id].update(status="finished", file=str(out_path))
    except Exception as exc:  # surface crashes to the UI instead of hanging
        logging.exception("job %s crashed", job_id)
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", error=str(exc))


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/scrape")
def scrape():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(error="Please choose a file to upload."), 400
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return jsonify(error=f"Unsupported file type '{suffix}'. Use .xlsx, .csv, or .txt."), 400

    tmp = WORK_DIR / f"upload_{uuid.uuid4().hex}{suffix}"
    upload.save(tmp)
    try:
        urls = read_urls(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if not urls:
        return jsonify(error="No Reddit URLs found in that file."), 400

    try:
        delay = max(2.0, float(request.form.get("delay", 6.5)))
    except ValueError:
        delay = 6.5
    expand_more = request.form.get("expand_more", "on") == "on"

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "done": 0, "total": len(urls),
            "comments": 0, "failures": [], "file": None,
            "started": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_job, args=(job_id, urls, delay, expand_more), daemon=True
    ).start()
    return jsonify(job_id=job_id, total=len(urls))


@app.get("/status/<job_id>")
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify(error="unknown job"), 404
        return jsonify({k: v for k, v in job.items() if k != "file"} | {"job_id": job_id})


@app.get("/download/<job_id>")
def download(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or job.get("status") != "finished" or not job.get("file"):
        return jsonify(error="results not ready"), 404
    return send_file(
        job["file"],
        as_attachment=True,
        download_name=f"reddit_results_{datetime.now():%Y%m%d_%H%M}.xlsx",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
