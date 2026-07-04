"""M9: Streamlit upload UI (Phase 4). Local only, no auth, no deployment scaffolding.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from thread_miner import load_config, load_env
from thread_miner import db as tmdb
from thread_miner.ingest import ingest_file, batch_summary

st.set_page_config(page_title="Thread Miner", page_icon="🧵", layout="wide")
st.title("🧵 Thread Miner")
st.caption(
    "Upload a spreadsheet of Reddit post URLs. Thread Miner fetches every thread, "
    "runs the analysis pipeline, and produces a research report."
)

cfg = load_config(Path(__file__).parent / "config.yaml")
env = load_env(Path(__file__).parent / ".env")

missing = [k for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
                       "REDDIT_USER_AGENT", "ANTHROPIC_API_KEY") if not env.get(k)]
if missing:
    st.error(f"Missing credentials in .env: {', '.join(missing)}. "
             "Copy .env.example to .env and fill it in.")
    st.stop()

uploaded = st.file_uploader("URL spreadsheet (.csv or .xlsx, must have a 'url' column)",
                            type=["csv", "xlsx"])
tag_col = st.text_input("Tag column name (optional grouping)", value="tag")

if uploaded is not None and st.button("Run", type="primary"):
    conn = tmdb.connect(cfg["paths"]["db"])
    suffix = Path(uploaded.name).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(uploaded.getvalue())
        tmp_path = tmp.name

    progress = st.progress(0, text="Ingesting")
    log_area = st.empty()

    batch_id = ingest_file(
        conn, tmp_path, tag_col=tag_col,
        share_resolve_timeout=cfg["thresholds"]["SHARE_RESOLVE_TIMEOUT"],
    )
    summary = batch_summary(conn, batch_id)
    log_area.write(f"Batch `{batch_id}`: {summary['unique_posts']} unique posts, "
                   f"statuses {summary['by_status']}")

    progress.progress(15, text="Fetching threads (watch fetch_log below)")
    from thread_miner.router import fetch_batch
    fetch_summary = fetch_batch(conn, cfg, env, batch_id)
    log_area.write(f"Fetch: {fetch_summary}")
    if not fetch_summary["fetched"]:
        st.error("No threads fetched. See statuses above.")
        st.stop()

    progress.progress(45, text="Analyzing with Claude (this can take a while)")
    from thread_miner.analysis import run_analysis
    from thread_miner.rollup import run_rollup
    run_id = run_analysis(conn, cfg, env, batch_id)
    stage = tmdb.get_run(conn, run_id)["stage"]
    log_area.write(f"Analysis run `{run_id}`: stage {stage}")

    progress.progress(80, text="Rolling up and rendering report")
    run_rollup(conn, cfg, env, run_id)
    from thread_miner.report import render_report
    report_path = render_report(conn, cfg, run_id)
    progress.progress(100, text="Done")

    html = report_path.read_text(encoding="utf-8")
    csv_bytes = (report_path.parent / "question_bank.csv").read_bytes()

    col1, col2 = st.columns(2)
    with col1:
        st.download_button("Download report.html", html,
                           file_name="report.html", mime="text/html")
    with col2:
        st.download_button("Download question_bank.csv", csv_bytes,
                           file_name="question_bank.csv", mime="text/csv")

    st.subheader("Report preview")
    components.html(html, height=900, scrolling=True)

    # Live-ish view of the fetch log for this batch.
    posts = tmdb.fetched_posts_for_batch(conn, batch_id)
    events = tmdb.fetch_log_summary(conn, [p["post_fullname"] for p in posts])
    if events:
        st.subheader("Fetch log summary")
        st.table([dict(e) for e in events])
