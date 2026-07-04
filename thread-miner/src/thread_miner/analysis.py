"""M5: LLM analysis. Chunk extraction (haiku) then thread synthesis (sonnet).

All calls run at temperature 0 and validate against pydantic schemas. LLMs
extract and interpret only; every number comes from metrics.py. Validation
failures retry once with the error appended; a second failure marks the item
failed and the pipeline continues.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import anthropic
from pydantic import ValidationError

from . import db, metrics
from .prompts import CHUNK_EXTRACTION_PROMPT, THREAD_SYNTHESIS_PROMPT
from .schemas import ChunkExtraction, ThreadSynthesis
from .serialize import serialize_thread

logger = logging.getLogger(__name__)

MAX_TOKENS = 8192


def parse_json_object(text: str) -> dict:
    """Parse a JSON object, tolerating stray markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


class LLMClient:
    def __init__(self, api_key: str, cfg: dict) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)
        self.cfg = cfg
        self.schema_retry = cfg["thresholds"]["SCHEMA_RETRY"]
        self.poll_seconds = cfg["thresholds"]["BATCH_POLL_SECONDS"]

    def _call(self, model: str, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=model, max_tokens=MAX_TOKENS, temperature=0,
            system=system, messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text

    def validated_call(self, model: str, system: str, user: str, schema_cls):
        """Sync call with SCHEMA_RETRY validation retries. None if all fail."""
        attempts = 1 + self.schema_retry
        message = user
        for attempt in range(attempts):
            try:
                text = self._call(model, system, message)
                return schema_cls.model_validate(parse_json_object(text))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("schema validation failed (attempt %d): %s", attempt + 1, exc)
                message = (
                    f"{user}\n\nYour previous response failed validation with this "
                    f"error, fix it and respond with only the corrected JSON object:\n{exc}"
                )
        return None

    def validate_or_retry(self, text: str, model: str, system: str, user: str, schema_cls):
        """Validate batch-result text; on failure fall back to one sync retry."""
        try:
            return schema_cls.model_validate(parse_json_object(text))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("batch result failed validation, retrying sync: %s", exc)
            retry_user = (
                f"{user}\n\nYour previous response failed validation with this "
                f"error, fix it and respond with only the corrected JSON object:\n{exc}"
            )
            try:
                fixed = self._call(model, system, retry_user)
                return schema_cls.model_validate(parse_json_object(fixed))
            except (ValidationError, ValueError, json.JSONDecodeError,
                    anthropic.AnthropicError) as exc2:
                logger.error("item failed after retry: %s", exc2)
                return None

    def submit_batch(self, requests: list[tuple[str, str, str, str]]) -> str:
        """requests: (custom_id, model, system, user). Returns anthropic batch id."""
        batch = self.client.messages.batches.create(
            requests=[
                {
                    "custom_id": custom_id,
                    "params": {
                        "model": model, "max_tokens": MAX_TOKENS, "temperature": 0,
                        "system": system,
                        "messages": [{"role": "user", "content": user}],
                    },
                }
                for custom_id, model, system, user in requests
            ]
        )
        return batch.id

    def poll_batch(self, batch_id: str) -> dict[str, str]:
        """Block until the batch ends; return custom_id -> response text (succeeded only)."""
        while True:
            batch = self.client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                break
            logger.info("batch %s: %s, waiting %ds", batch_id,
                        batch.processing_status, self.poll_seconds)
            time.sleep(self.poll_seconds)
        out: dict[str, str] = {}
        for entry in self.client.messages.batches.results(batch_id):
            if entry.result.type == "succeeded":
                out[entry.custom_id] = entry.result.message.content[0].text
            else:
                logger.error("batch item %s: %s", entry.custom_id, entry.result.type)
        return out


# --------------------------------------------------------------- request prep

def build_chunk_requests(conn, cfg, posts) -> dict[str, tuple[str, str]]:
    """custom_id -> (post_fullname, chunk_text). Deterministic, so a resumed
    run rebuilds the same mapping without persisting request bodies."""
    model = cfg["models"]["extraction"]
    out: dict[str, tuple[str, str]] = {}
    for post in posts:
        comments = db.comments_for_post(conn, post["post_fullname"])
        if not comments:
            continue
        for i, chunk in enumerate(serialize_thread(post, comments, cfg)):
            out[f"chunk|{post['post_fullname']}|{i}"] = (post["post_fullname"], chunk)
    return out


def build_synthesis_user_message(conn, cfg, run_id: str, post) -> str:
    fullname = post["post_fullname"]
    comments = db.comments_for_post(conn, fullname)
    labeled = [(r["sentiment"], r["score"] or 0)
               for r in db.labels_for_post(conn, run_id, fullname)]
    stats = metrics.stats_block(post, comments, labeled)

    top = sorted(comments, key=lambda c: c["score"] or 0, reverse=True)[:10]
    top_lines = [
        f"[{c['comment_fullname']}|s={c['score']}|{c['author'] or '[deleted]'}] "
        + " ".join((c["body"] or "").split())[:1200]
        for c in top
    ]

    items = db.items_for_post(conn, run_id, fullname)
    deduped: dict[str, list[str]] = {"question": [], "pain_point": [], "objection": [],
                                     "vocabulary": []}
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item["kind"], item["text"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        prefix = f"({item['comment_fullname']}) " if item["comment_fullname"] else ""
        deduped[item["kind"]].append(prefix + item["text"])

    sections = [
        stats,
        "TOP COMMENTS (verbatim):\n" + "\n".join(top_lines),
        "RAW EXTRACTED QUESTIONS:\n" + "\n".join(deduped["question"] or ["(none)"]),
        "RAW PAIN POINTS:\n" + "\n".join(deduped["pain_point"] or ["(none)"]),
        "RAW OBJECTIONS:\n" + "\n".join(deduped["objection"] or ["(none)"]),
        "RAW VOCABULARY:\n" + ", ".join(deduped["vocabulary"] or ["(none)"]),
    ]
    return "\n\n".join(sections)


# --------------------------------------------------------------- result stores

def store_chunk_result(conn, run_id: str, post_fullname: str,
                       extraction: ChunkExtraction) -> None:
    valid_ids = db.comment_ids_for_post(conn, post_fullname)

    labels = []
    for label in extraction.labels:
        if label.comment_id in valid_ids:
            labels.append((label.comment_id, label.sentiment))
        else:
            logger.warning("dropping invented comment id in labels: %s", label.comment_id)
    db.save_labels(conn, run_id, labels)

    items: list[tuple[str | None, str, str]] = []
    for kind, entries in (("question", extraction.questions),
                          ("pain_point", extraction.pain_points),
                          ("objection", extraction.objections)):
        for item in entries:
            if item.comment_id in valid_ids:
                items.append((item.comment_id, kind, item.text))
            else:
                logger.warning("dropping invented comment id in %s: %s", kind, item.comment_id)
    items.extend((None, "vocabulary", term) for term in extraction.vocabulary)
    db.save_items(conn, run_id, post_fullname, items)


def _filter_ids(ids: list[str], valid: set[str]) -> list[str]:
    kept = [i for i in ids if i in valid]
    if len(kept) < len(ids):
        logger.warning("dropped %d invented comment ids from synthesis", len(ids) - len(kept))
    return kept


def sanitize_synthesis(synth: ThreadSynthesis, valid_ids: set[str]) -> ThreadSynthesis:
    """Never render fabricated citations: drop ids not stored for this post."""
    for theme in synth.themes:
        theme.supporting_comment_ids = _filter_ids(theme.supporting_comment_ids, valid_ids)
    for cluster in synth.question_clusters:
        cluster.member_comment_ids = _filter_ids(cluster.member_comment_ids, valid_ids)
    for angle in synth.content_angles:
        angle.supporting_comment_ids = _filter_ids(angle.supporting_comment_ids, valid_ids)
    synth.contrarian_takes = [t for t in synth.contrarian_takes if t.comment_id in valid_ids]
    return synth


# --------------------------------------------------------------------- driver

def run_analysis(conn, cfg, env, batch_id: str, resume_run_id: str | None = None,
                 no_batch: bool = False) -> str:
    llm = LLMClient(env["ANTHROPIC_API_KEY"], cfg)
    extraction_model = cfg["models"]["extraction"]
    synthesis_model = cfg["models"]["synthesis"]

    if resume_run_id:
        run = db.get_run(conn, resume_run_id)
        if run is None:
            raise SystemExit(f"unknown run id: {resume_run_id}")
        run_id, batch_id = run["run_id"], run["batch_id"]
    else:
        run_id = str(uuid.uuid4())
        db.create_run(conn, run_id, batch_id)
        run = db.get_run(conn, run_id)

    posts = db.fetched_posts_for_batch(conn, batch_id)
    if not posts:
        raise SystemExit("no fetched posts in this batch; run `tm fetch` first")
    use_batch = len(posts) > cfg["thresholds"]["SYNC_THRESHOLD"] and not no_batch
    stage = run["stage"]

    chunk_map = build_chunk_requests(conn, cfg, posts)

    if stage == "created":
        if use_batch:
            reqs = [(cid, extraction_model, CHUNK_EXTRACTION_PROMPT, chunk)
                    for cid, (_, chunk) in sorted(chunk_map.items())]
            anthropic_batch = llm.submit_batch(reqs)
            db.set_run_stage(conn, run_id, "chunks_submitted", anthropic_batch)
            stage = "chunks_submitted"
        else:
            for cid, (post_fullname, chunk) in sorted(chunk_map.items()):
                extraction = llm.validated_call(
                    extraction_model, CHUNK_EXTRACTION_PROMPT, chunk, ChunkExtraction
                )
                if extraction:
                    store_chunk_result(conn, run_id, post_fullname, extraction)
            db.set_run_stage(conn, run_id, "chunks_done")
            stage = "chunks_done"

    if stage == "chunks_submitted":
        results = llm.poll_batch(run["anthropic_batch_id"])
        for cid, text in sorted(results.items()):
            post_fullname, chunk = chunk_map[cid]
            extraction = llm.validate_or_retry(
                text, extraction_model, CHUNK_EXTRACTION_PROMPT, chunk, ChunkExtraction
            )
            if extraction:
                store_chunk_result(conn, run_id, post_fullname, extraction)
        db.set_run_stage(conn, run_id, "chunks_done")
        stage = "chunks_done"

    if stage == "chunks_done":
        synth_msgs = {
            p["post_fullname"]: build_synthesis_user_message(conn, cfg, run_id, p)
            for p in posts
        }
        if use_batch:
            reqs = [(f"synth|{fn}", synthesis_model, THREAD_SYNTHESIS_PROMPT, msg)
                    for fn, msg in sorted(synth_msgs.items())]
            anthropic_batch = llm.submit_batch(reqs)
            db.set_run_stage(conn, run_id, "synthesis_submitted", anthropic_batch)
            stage = "synthesis_submitted"
        else:
            for fn, msg in sorted(synth_msgs.items()):
                synth = llm.validated_call(
                    synthesis_model, THREAD_SYNTHESIS_PROMPT, msg, ThreadSynthesis
                )
                if synth:
                    synth = sanitize_synthesis(synth, db.comment_ids_for_post(conn, fn))
                    db.save_synthesis(conn, run_id, fn, synth.model_dump_json())
            db.set_run_stage(conn, run_id, "synthesis_done")
            stage = "synthesis_done"

    if stage == "synthesis_submitted":
        run = db.get_run(conn, run_id)
        results = llm.poll_batch(run["anthropic_batch_id"])
        for cid, text in sorted(results.items()):
            fn = cid.split("|", 1)[1]
            msg = build_synthesis_user_message(conn, cfg, run_id, db.get_post(conn, fn))
            synth = llm.validate_or_retry(
                text, synthesis_model, THREAD_SYNTHESIS_PROMPT, msg, ThreadSynthesis
            )
            if synth:
                synth = sanitize_synthesis(synth, db.comment_ids_for_post(conn, fn))
                db.save_synthesis(conn, run_id, fn, synth.model_dump_json())
        db.set_run_stage(conn, run_id, "synthesis_done")

    return run_id
