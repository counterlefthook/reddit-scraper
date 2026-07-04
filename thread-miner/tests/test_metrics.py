"""Acceptance test B2: lock the weighting math with fixed fixtures."""

import math

from thread_miner import metrics


def test_comment_weight():
    assert metrics.comment_weight(0) == 1.0
    assert metrics.comment_weight(9) == 2.0          # 1 + log10(10)
    assert metrics.comment_weight(99) == 3.0         # 1 + log10(100)
    assert metrics.comment_weight(-5) == 1.0         # negative scores clamp to 0
    assert metrics.comment_weight(None) == 1.0


def test_weighted_sentiment_index_hand_computed():
    labeled = [
        ("positive", 9),    # w=2, p=+1
        ("negative", 0),    # w=1, p=-1
        ("neutral", 99),    # w=3, p=0
        ("mixed", 9),       # w=2, p=0
    ]
    # (2 - 1 + 0 + 0) / (2 + 1 + 3 + 2) = 1/8 = 0.125
    assert metrics.weighted_sentiment_index(labeled) == 0.125


def test_weighted_sentiment_index_bounds_and_empty():
    assert metrics.weighted_sentiment_index([]) == 0.0
    assert metrics.weighted_sentiment_index([("positive", 100)]) == 1.0
    assert metrics.weighted_sentiment_index([("negative", 100)]) == -1.0


def test_weighted_sentiment_distribution():
    labeled = [("positive", 9), ("negative", 0), ("neutral", 99), ("mixed", 9)]
    dist = metrics.weighted_sentiment_distribution(labeled)
    assert dist == {"positive": 25.0, "negative": 12.5, "neutral": 37.5, "mixed": 25.0}
    assert sum(dist.values()) == 100.0


def test_question_rank_score():
    # 3 members, total score 99: 3 * (1 + log10(100)) = 9.0
    assert metrics.question_rank_score(3, 99) == 9.0
    assert metrics.question_rank_score(0, 50) == 0.0
    assert metrics.question_rank_score(2, 0) == 2.0
    assert metrics.question_rank_score(2, -10) == 2.0  # negative totals clamp
    # rounding to 2 decimals
    expected = round(5 * (1 + math.log10(1 + 42)), 2)
    assert metrics.question_rank_score(5, 42) == expected


def test_thread_engagement():
    post = {
        "score": 100, "upvote_ratio": 0.95, "num_comments_reported": 4,
        "num_comments_fetched": 3, "fetch_truncated": 0,
    }
    comments = [
        {"score": 10, "author": "a"},
        {"score": 2, "author": "b"},
        {"score": 5, "author": "a"},
    ]
    eng = metrics.thread_engagement(post, comments)
    assert eng["median_comment_score"] == 5
    assert eng["max_comment_score"] == 10
    assert eng["unique_authors"] == 2
    assert [c["score"] for c in eng["top_comments"]] == [10, 5, 2]


def test_tag_engagement():
    per_thread = [
        {"num_comments_fetched": 10, "sentiment_index": 0.5},
        {"num_comments_fetched": 30, "sentiment_index": -0.1},
        {"num_comments_fetched": 20, "sentiment_index": None},
    ]
    stats = metrics.tag_engagement(per_thread)
    assert stats["threads"] == 3
    assert stats["total_comments_fetched"] == 60
    assert stats["median_comments_per_thread"] == 20
    assert stats["median_sentiment_index"] == 0.2


def test_stats_block_contains_pipeline_numbers_only():
    post = {
        "score": 100, "upvote_ratio": 0.9, "num_comments_reported": 2,
        "num_comments_fetched": 2, "fetch_truncated": 1,
    }
    comments = [{"score": 9, "author": "a"}, {"score": 0, "author": "b"}]
    labeled = [("positive", 9), ("negative", 0)]
    block = metrics.stats_block(post, comments, labeled)
    assert "weighted sentiment index: 0.333" in block  # (2-1)/3
    assert "ground truth" in block
    assert "fetch truncated: True" in block
