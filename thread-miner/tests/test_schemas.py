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
