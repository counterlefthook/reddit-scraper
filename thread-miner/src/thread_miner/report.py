"""M7: report generator. Renders report.html (self-contained) + question_bank.csv.

Every number in the report is computed here or in metrics.py from SQL rows.
LLM prose is rendered as prose only.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, metrics
from .rollup import group_posts_by_tag
from .schemas import Rollup, ThreadSynthesis

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

CSV_COLUMNS = [
    "canonical_question", "rank_score", "member_count", "total_member_score",
    "tags", "post_fullnames", "permalinks", "member_comment_ids",
]


def comment_anchor(post_permalink: str, comment_fullname: str) -> str:
    return f"https://www.reddit.com{post_permalink}{comment_fullname.removeprefix('t1_')}/"


def build_question_bank(conn, run_id: str, batch_id: str, posts) -> list[dict]:
    """One row per question cluster, rank score from metrics.py, sorted descending."""
    syntheses = db.syntheses_for_run(conn, run_id)
    rows = []
    for post in posts:
        fn = post["post_fullname"]
        payload = syntheses.get(fn)
        if not payload:
            continue
        synth = ThreadSynthesis.model_validate_json(payload)
        scores = {c["comment_fullname"]: c["score"] or 0
                  for c in db.comments_for_post(conn, fn)}
        tags = db.tags_for_post(conn, batch_id, fn) or ["untagged"]
        for cluster in synth.question_clusters:
            members = cluster.member_comment_ids
            total = sum(scores.get(m, 0) for m in members)
            rows.append({
                "canonical_question": cluster.canonical_question,
                "rank_score": metrics.question_rank_score(len(members), total),
                "member_count": len(members),
                "total_member_score": total,
                "tags": ",".join(tags),
                "post_fullnames": fn,
                "post_title": post["title"],
                "permalinks": f"https://www.reddit.com{post['permalink']}",
                "member_comment_ids": ",".join(members),
                "member_links": [comment_anchor(post["permalink"], m) for m in members],
            })
    rows.sort(key=lambda r: r["rank_score"], reverse=True)
    return rows


def build_thread_cards(conn, run_id: str, batch_id: str, posts) -> list[dict]:
    syntheses = db.syntheses_for_run(conn, run_id)
    cards = []
    for post in posts:
        fn = post["post_fullname"]
        comments = db.comments_for_post(conn, fn)
        labeled = [(r["sentiment"], r["score"] or 0)
                   for r in db.labels_for_post(conn, run_id, fn)]
        synth = None
        payload = syntheses.get(fn)
        if payload:
            synth = ThreadSynthesis.model_validate_json(payload)
        cards.append({
            "post": dict(post),
            "url": f"https://www.reddit.com{post['permalink']}",
            "tags": db.tags_for_post(conn, batch_id, fn) or ["untagged"],
            "sentiment_index": metrics.weighted_sentiment_index(labeled) if labeled else None,
            "distribution": metrics.weighted_sentiment_distribution(labeled),
            "engagement": {k: v for k, v in
                           metrics.thread_engagement(post, comments).items()
                           if k != "top_comments"},
            "synthesis": synth,
            "anchor": lambda cid, p=post["permalink"]: comment_anchor(p, cid),
        })
    return cards


def render_report(conn, cfg, run_id: str) -> Path:
    run = db.get_run(conn, run_id)
    if run is None:
        raise SystemExit(f"unknown run id: {run_id}")
    batch_id = run["batch_id"]
    batch = db.get_batch(conn, batch_id)
    posts = db.fetched_posts_for_batch(conn, batch_id)

    rollup = None
    payload = db.get_rollup(conn, run_id)
    if payload:
        rollup = Rollup.model_validate_json(payload)

    question_bank = build_question_bank(conn, run_id, batch_id, posts)
    cards = build_thread_cards(conn, run_id, batch_id, posts)

    tier_counts: dict[int, int] = {}
    truncated = 0
    total_comments = 0
    for post in posts:
        tier_counts[post["fetch_tier"]] = tier_counts.get(post["fetch_tier"], 0) + 1
        truncated += post["fetch_truncated"]
        total_comments += post["num_comments_fetched"] or 0

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["comment_anchor"] = comment_anchor
    template = env.get_template("report.html.j2")
    html = template.render(
        run=dict(run), batch=dict(batch) if batch else {},
        posts=[dict(p) for p in posts],
        thread_count=len(posts), total_comments=total_comments,
        tier_counts=tier_counts, truncated_count=truncated,
        rollup=rollup, question_bank=question_bank, cards=cards,
        tag_groups={t: [p["post_fullname"] for p in g]
                    for t, g in group_posts_by_tag(conn, batch_id, posts).items()},
        fetch_log=[dict(r) for r in
                   db.fetch_log_summary(conn, [p["post_fullname"] for p in posts])],
        failed_rows=[dict(r) for r in db.failed_rows_for_batch(conn, batch_id)],
    )

    out_dir = Path(cfg["paths"]["reports_dir"]) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")

    csv_path = out_dir / "question_bank.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(question_bank)

    db.set_run_stage(conn, run_id, "report_done")
    return report_path
