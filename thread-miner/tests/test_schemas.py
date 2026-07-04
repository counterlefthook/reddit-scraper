"""Schema validation behavior: the determinism line's enforcement point."""

import pytest
from pydantic import ValidationError

from thread_miner.analysis import parse_json_object, sanitize_synthesis
from thread_miner.schemas import (
    ChunkExtraction, CommentLabel, Rollup, ThreadSynthesis,
)


def test_chunk_extraction_valid():
    payload = {
        "labels": [{"comment_id": "t1_aaa", "sentiment": "positive"}],
        "questions": [{"comment_id": "t1_aaa", "text": "how do I start?"}],
        "pain_points": [],
        "objections": [],
        "vocabulary": ["boilerplate", "vibe check"],
    }
    parsed = ChunkExtraction.model_validate(payload)
    assert parsed.labels[0].sentiment == "positive"
    assert parsed.vocabulary == ["boilerplate", "vibe check"]


def test_sentiment_must_be_one_of_four():
    with pytest.raises(ValidationError):
        CommentLabel.model_validate({"comment_id": "t1_a", "sentiment": "angry"})


def test_extracted_item_text_capped_at_500():
    with pytest.raises(ValidationError):
        ChunkExtraction.model_validate({
            "labels": [], "questions": [{"comment_id": "t1_a", "text": "x" * 501}],
            "pain_points": [], "objections": [], "vocabulary": [],
        })


def test_vocabulary_capped_at_30():
    with pytest.raises(ValidationError):
        ChunkExtraction.model_validate({
            "labels": [], "questions": [], "pain_points": [], "objections": [],
            "vocabulary": [f"term{i}" for i in range(31)],
        })


def test_thread_synthesis_roundtrip():
    payload = {
        "summary": "People want simpler deployment options.",
        "themes": [{"name": "Deployment friction", "description": "Setup is hard.",
                    "supporting_comment_ids": ["t1_a", "t1_b"]}],
        "question_clusters": [{"canonical_question": "What host should I use?",
                               "member_comment_ids": ["t1_a"]}],
        "contrarian_takes": [{"comment_id": "t1_b", "text": "Self-hosting is easier."}],
        "audience_vocabulary": ["bare metal", "PaaS"],
        "content_angles": [{"title": "Hosting guide", "rationale": "Many asked.",
                            "supporting_comment_ids": ["t1_a"]}],
    }
    synth = ThreadSynthesis.model_validate(payload)
    assert ThreadSynthesis.model_validate_json(synth.model_dump_json()) == synth


def test_sanitize_synthesis_drops_invented_ids():
    synth = ThreadSynthesis.model_validate({
        "summary": "s",
        "themes": [{"name": "n", "description": "d",
                    "supporting_comment_ids": ["t1_real", "t1_fake"]}],
        "question_clusters": [{"canonical_question": "q?",
                               "member_comment_ids": ["t1_fake"]}],
        "contrarian_takes": [{"comment_id": "t1_fake", "text": "t"}],
        "audience_vocabulary": [],
        "content_angles": [{"title": "t", "rationale": "r",
                            "supporting_comment_ids": ["t1_real"]}],
    })
    clean = sanitize_synthesis(synth, valid_ids={"t1_real"})
    assert clean.themes[0].supporting_comment_ids == ["t1_real"]
    assert clean.question_clusters[0].member_comment_ids == []
    assert clean.contrarian_takes == []
    assert clean.content_angles[0].supporting_comment_ids == ["t1_real"]


def test_rollup_schema():
    rollup = Rollup.model_validate({
        "executive_summary": "Users need onboarding content.",
        "cross_thread_themes": [{"name": "Onboarding", "description": "d",
                                 "post_fullnames": ["t3_a", "t3_b"]}],
        "content_opportunity_brief": "Start with a setup guide.",
    })
    assert rollup.cross_thread_themes[0].post_fullnames == ["t3_a", "t3_b"]


def test_parse_json_object_tolerates_fences():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here you go: {"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("no json here")


# ------------------------------------------------- schema-in-prompt regression

def test_system_with_schema_embeds_field_names():
    from thread_miner.analysis import system_with_schema
    from thread_miner.prompts import CHUNK_EXTRACTION_PROMPT

    system = system_with_schema(CHUNK_EXTRACTION_PROMPT, ChunkExtraction)
    # The model can only comply if it can see the exact field names.
    for field in ("labels", "questions", "pain_points", "objections",
                  "vocabulary", "comment_id", "sentiment", "text"):
        assert f'"{field}"' in system


def test_validated_call_retries_with_schema(monkeypatch):
    """First response uses invented keys (the observed live failure); the retry,
    which now carries the schema and the validation error, succeeds."""
    import json as _json
    from unittest.mock import patch

    from thread_miner.analysis import LLMClient

    cfg = {"thresholds": {"SCHEMA_RETRY": 1, "BATCH_POLL_SECONDS": 1}}
    client = LLMClient(api_key="test-key", cfg=cfg)

    bad = _json.dumps({"comments": [{"comment_id": "t1_a", "question": "x?"}],
                       "vocabulary": []})
    good = _json.dumps({"labels": [{"comment_id": "t1_a", "sentiment": "neutral"}],
                        "questions": [{"comment_id": "t1_a", "text": "x?"}],
                        "pain_points": [], "objections": [], "vocabulary": []})
    calls = []

    def fake_call(model, system, user):
        calls.append((system, user))
        return bad if len(calls) == 1 else good

    with patch.object(client, "_call", side_effect=fake_call):
        result = client.validated_call("m", "SYSTEM", "USER", ChunkExtraction)

    assert result is not None
    assert result.questions[0].text == "x?"
    assert len(calls) == 2
    assert '"labels"' in calls[0][0]          # schema present from the first call
    assert "failed validation" in calls[1][1]  # retry carries the error
