"""M6: cross-thread rollup, grouped by tag. One rollup per run."""

from __future__ import annotations

import json
import logging

from . import db, metrics
from .analysis import LLMClient
from .prompts import ROLLUP_PROMPT
from .schemas import Rollup, ThreadSynthesis

logger = logging.getLogger(__name__)


def group_posts_by_tag(conn, batch_id: str, posts) -> dict[str, list]:
    groups: dict[str, list] = {}
    for post in posts:
        tags = db.tags_for_post(conn, batch_id, post["post_fullname"]) or ["untagged"]
        for tag in tags:
            groups.setdefault(tag, []).append(post)
    return groups


def build_rollup_user_message(conn, cfg, run_id: str, batch_id: str, posts) -> str:
    syntheses = db.syntheses_for_run(conn, run_id)
    groups = group_posts_by_tag(conn, batch_id, posts)

    sections = []
    for tag in sorted(groups):
        tag_posts = groups[tag]
        per_thread = []
        thread_parts = []
        for post in tag_posts:
            fn = post["post_fullname"]
            labeled = [(r["sentiment"], r["score"] or 0)
                       for r in db.labels_for_post(conn, run_id, fn)]
            per_thread.append({
                "num_comments_fetched": post["num_comments_fetched"],
                "sentiment_index": metrics.weighted_sentiment_index(labeled)
                if labeled else None,
            })
            payload = syntheses.get(fn)
            if payload:
                synth = ThreadSynthesis.model_validate_json(payload)
                thread_parts.append(
                    f"### {fn}: {post['title']}\n"
                    f"summary: {synth.summary}\n"
                    f"themes: {', '.join(t.name for t in synth.themes)}\n"
                    "top questions: "
                    + " | ".join(q.canonical_question for q in synth.question_clusters[:5])
                    + "\nvocabulary: " + ", ".join(synth.audience_vocabulary[:15])
                )
        stats = metrics.tag_engagement(per_thread)
        sections.append(
            f"## TAG: {tag}\n"
            "PIPELINE STATS (ground truth, reference qualitatively only): "
            + json.dumps(stats) + "\n\n" + "\n\n".join(thread_parts)
        )
    return "\n\n".join(sections)


def run_rollup(conn, cfg, env, run_id: str) -> None:
    run = db.get_run(conn, run_id)
    if run is None:
        raise SystemExit(f"unknown run id: {run_id}")
    posts = db.fetched_posts_for_batch(conn, run["batch_id"])
    llm = LLMClient(env["ANTHROPIC_API_KEY"], cfg)

    message = build_rollup_user_message(conn, cfg, run_id, run["batch_id"], posts)
    rollup = llm.validated_call(cfg["models"]["synthesis"], ROLLUP_PROMPT, message, Rollup)
    if rollup is None:
        logger.error("rollup failed validation twice; storing empty rollup")
        rollup = Rollup(executive_summary="Rollup generation failed.",
                        cross_thread_themes=[], content_opportunity_brief="")
    else:
        valid_posts = {p["post_fullname"] for p in posts}
        for theme in rollup.cross_thread_themes:
            theme.post_fullnames = [p for p in theme.post_fullnames if p in valid_posts]
    db.save_rollup(conn, run_id, rollup.model_dump_json())
    db.set_run_stage(conn, run_id, "rollup_done")
