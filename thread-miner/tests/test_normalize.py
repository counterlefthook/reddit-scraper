"""Acceptance test A1 plus unit coverage for the section 7 normalization rules."""

from pathlib import Path

import pytest

from thread_miner import db
from thread_miner.ingest import ingest_file
from thread_miner.normalize import (
    INVALID, NORMALIZED, UNRESOLVED_SHARE, normalize_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "urls_mixed.csv"


def failing_resolver(url):
    """Offline share-link resolver: behaves like a network failure."""
    return None


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


# ------------------------------------------------------------- rule coverage

@pytest.mark.parametrize("url,fullname", [
    ("https://www.reddit.com/r/python/comments/1abc01/some_title/", "t3_1abc01"),
    ("https://old.reddit.com/r/python/comments/1abc02/title_two/", "t3_1abc02"),
    ("https://np.reddit.com/r/learnpython/comments/1abc03/x/", "t3_1abc03"),
    ("https://m.reddit.com/r/flask/comments/1abc04/y/", "t3_1abc04"),
    ("https://new.reddit.com/r/a/comments/1abc09/z/", "t3_1abc09"),
    ("https://sh.reddit.com/r/a/comments/1abc0a/z/", "t3_1abc0a"),
    ("https://redd.it/1abc05", "t3_1abc05"),
    ("https://www.reddit.com/comments/1abc0b/", "t3_1abc0b"),
    # Rule 1: query params and fragments stripped
    ("https://www.reddit.com/r/python/comments/1abc07/t/?utm_source=share#frag", "t3_1abc07"),
    # Rule 5: ids lowercased
    ("https://redd.it/1ABC05", "t3_1abc05"),
])
def test_normalize_valid_urls(url, fullname):
    result = normalize_url(url, share_resolver=failing_resolver)
    assert result.status == NORMALIZED
    assert result.post_fullname == fullname


def test_comment_permalink_sets_focus_comment_id():
    result = normalize_url(
        "https://www.reddit.com/r/python/comments/1abc06/title_six/mno1234/",
        share_resolver=failing_resolver,
    )
    assert result.status == NORMALIZED
    assert result.post_fullname == "t3_1abc06"
    assert result.focus_comment_id == "mno1234"


def test_share_link_resolves_and_renormalizes():
    resolver = lambda url: "https://www.reddit.com/r/python/comments/1abc0c/resolved/?share_id=x"
    result = normalize_url("https://www.reddit.com/r/python/s/AbCdEfGh12",
                           share_resolver=resolver)
    assert result.status == NORMALIZED
    assert result.post_fullname == "t3_1abc0c"


def test_share_link_unresolvable_marks_status():
    result = normalize_url("https://www.reddit.com/r/python/s/AbCdEfGh12",
                           share_resolver=failing_resolver)
    assert result.status == UNRESOLVED_SHARE
    assert result.post_fullname is None


@pytest.mark.parametrize("url", [
    "https://example.com/not/a/reddit/link",
    "https://www.reddit.com/r/python/",       # no post id
    "https://www.reddit.com/user/someone/",
    "",
    "not a url at all",
])
def test_invalid_urls(url):
    assert normalize_url(url, share_resolver=failing_resolver).status == INVALID


# --------------------------------------------------------- acceptance test A1

def test_a1_fixture_batch(conn):
    batch_id = ingest_file(conn, FIXTURE, tag_col="topic",
                           share_resolver=failing_resolver)
    rows = db.rows_for_batch(conn, batch_id)
    assert len(rows) == 12

    # Exactly 9 unique valid fullnames (share link is unresolved offline).
    fullnames = {r["post_fullname"] for r in rows if r["post_fullname"]}
    assert len(fullnames) == 9

    # The duplicate pair collapses to one fullname across two rows.
    dup_rows = [r for r in rows if r["post_fullname"] == "t3_1abc08"]
    assert len(dup_rows) == 2

    # Invalid and share-link rows carry correct statuses.
    by_url = {r["raw_url"]: r for r in rows}
    assert by_url["https://example.com/not/a/reddit/link"]["status"] == "invalid_url"
    assert by_url["https://www.reddit.com/r/python/s/AbCdEfGh12"]["status"] == \
        "unresolved_share_link"

    # The 404-id row normalizes fine; its failure is a fetch-stage concern.
    assert by_url["https://www.reddit.com/r/python/comments/404dead/removed_post/"][
        "status"] == "normalized"

    # Comment-permalink row records focus_comment_id.
    focus_row = by_url["https://www.reddit.com/r/python/comments/1abc06/title_six/mno1234/"]
    assert focus_row["focus_comment_id"] == "mno1234"

    # Tags come from the --tag-col column.
    assert by_url["https://redd.it/1abc05"]["tag"] == "learning"

    # Dedup at fetch time: 9 unique fullnames to fetch.
    to_fetch = db.batch_fullnames_to_fetch(conn, batch_id, ttl_days=14, force=False)
    assert len(to_fetch) == 9
