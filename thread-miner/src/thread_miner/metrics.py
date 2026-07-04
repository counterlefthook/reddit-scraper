"""ALL math lives here. Every number rendered anywhere in a report comes from
this module or straight SQL. LLMs never compute; this is the determinism line."""

from __future__ import annotations

import statistics
from math import log10

POLARITY = {"positive": 1.0, "negative": -1.0, "neutral": 0.0, "mixed": 0.0}
SENTIMENTS = ("positive", "negative", "neutral", "mixed")


def comment_weight(score) -> float:
    """w = 1 + log10(1 + max(score, 0))"""
    s = score if score is not None else 0
    return 1.0 + log10(1.0 + max(s, 0))


def weighted_sentiment_index(labeled: list[tuple[str, int]]) -> float:
    """labeled: (sentiment, comment_score). Returns sum(w*p)/sum(w) in [-1, 1], 3 dp."""
    if not labeled:
        return 0.0
    num = sum(comment_weight(score) * POLARITY[sent] for sent, score in labeled)
    den = sum(comment_weight(score) for _, score in labeled)
    return round(num / den, 3) if den else 0.0


def weighted_sentiment_distribution(labeled: list[tuple[str, int]]) -> dict[str, float]:
    """Per label: sum(w in label) / sum(w all), as percentages (1 dp)."""
    total = sum(comment_weight(score) for _, score in labeled)
    dist = {}
    for label in SENTIMENTS:
        w = sum(comment_weight(score) for sent, score in labeled if sent == label)
        dist[label] = round(100.0 * w / total, 1) if total else 0.0
    return dist


def question_rank_score(n_members: int, total_member_score) -> float:
    """n_members * (1 + log10(1 + sum(scores of member comments))), 2 dp."""
    total = total_member_score if total_member_score is not None else 0
    return round(n_members * (1.0 + log10(1.0 + max(total, 0))), 2)


def thread_engagement(post_row, comment_rows) -> dict:
    """Deterministic engagement stats for one thread."""
    scores = [c["score"] or 0 for c in comment_rows]
    top = sorted(comment_rows, key=lambda c: c["score"] or 0, reverse=True)[:10]
    return {
        "post_score": post_row["score"],
        "upvote_ratio": post_row["upvote_ratio"],
        "num_comments_reported": post_row["num_comments_reported"],
        "num_comments_fetched": post_row["num_comments_fetched"],
        "median_comment_score": statistics.median(scores) if scores else 0,
        "max_comment_score": max(scores) if scores else 0,
        "unique_authors": len({c["author"] for c in comment_rows if c["author"]}),
        "top_comments": top,
        "fetch_truncated": bool(post_row["fetch_truncated"]),
    }


def tag_engagement(per_thread: list[dict]) -> dict:
    """Aggregate stats for a group of threads (one tag)."""
    fetched = [t["num_comments_fetched"] or 0 for t in per_thread]
    indexes = [t["sentiment_index"] for t in per_thread if t.get("sentiment_index") is not None]
    return {
        "threads": len(per_thread),
        "total_comments_fetched": sum(fetched),
        "median_comments_per_thread": statistics.median(fetched) if fetched else 0,
        "median_sentiment_index": round(statistics.median(indexes), 3) if indexes else None,
    }


def stats_block(post_row, comment_rows, labeled: list[tuple[str, int]]) -> str:
    """Pipeline-computed ground-truth block handed to the synthesis prompt."""
    eng = thread_engagement(post_row, comment_rows)
    idx = weighted_sentiment_index(labeled)
    dist = weighted_sentiment_distribution(labeled)
    lines = [
        "PIPELINE STATS (ground truth, do not restate numbers in prose):",
        f"- comments fetched: {eng['num_comments_fetched']} "
        f"(Reddit reported {eng['num_comments_reported']})",
        f"- post score: {eng['post_score']}, upvote ratio: {eng['upvote_ratio']}",
        f"- weighted sentiment index: {idx} (range -1 to 1)",
        "- weighted sentiment distribution: "
        + ", ".join(f"{k} {v}%" for k, v in dist.items()),
        f"- labeled comments: {len(labeled)}",
        f"- median comment score: {eng['median_comment_score']}, "
        f"max: {eng['max_comment_score']}, unique authors: {eng['unique_authors']}",
        f"- fetch truncated: {eng['fetch_truncated']}",
    ]
    return "\n".join(lines)
