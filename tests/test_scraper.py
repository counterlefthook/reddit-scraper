import json
from pathlib import Path
from unittest.mock import patch

import pytest

from reddit_scraper.scraper import RedditScraper, ScrapeError, normalize_url

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------- normalize_url

@pytest.mark.parametrize("raw,expected", [
    (
        "https://www.reddit.com/r/AskReddit/comments/1abcde/some_title/",
        "https://www.reddit.com/r/AskReddit/comments/1abcde",
    ),
    (
        "https://old.reddit.com/r/AskReddit/comments/1abcde/some_title/?utm_source=share",
        "https://www.reddit.com/r/AskReddit/comments/1abcde",
    ),
    (
        # comment permalink collapses to the thread
        "https://www.reddit.com/r/AskReddit/comments/1abcde/title/kx9yz/",
        "https://www.reddit.com/r/AskReddit/comments/1abcde",
    ),
    ("https://redd.it/1abcde", "https://www.reddit.com/comments/1abcde"),
    ("redd.it/1abcde", "https://www.reddit.com/comments/1abcde"),
    (
        "www.reddit.com/comments/1abcde",
        "https://www.reddit.com/comments/1abcde",
    ),
    (
        "  'https://www.reddit.com/r/x/comments/1abcde/t/'  ",
        "https://www.reddit.com/r/x/comments/1abcde",
    ),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_share_link_kept():
    assert normalize_url("https://www.reddit.com/r/foo/s/AbCd123") == \
        "https://www.reddit.com/r/foo/s/AbCd123"


@pytest.mark.parametrize("bad", ["", "https://example.com/thread/1", "https://reddit.com/r/foo/"])
def test_normalize_rejects_bad_urls(bad):
    with pytest.raises(ScrapeError):
        normalize_url(bad)


# --------------------------------------------------------------- thread parsing

def make_scraper(**kwargs):
    return RedditScraper(delay=0, **kwargs)


def test_scrape_thread_parses_post_and_comments():
    scraper = make_scraper(expand_more=False)
    with patch.object(scraper, "_get", return_value=load("thread.json")):
        result = scraper.scrape_thread("https://www.reddit.com/r/AskReddit/comments/1abcde/x/")

    assert result.ok
    post = result.post
    assert post.title == "What is the best productivity tip you know?"
    assert post.subreddit == "AskReddit"
    assert post.score == 4213
    assert post.permalink.startswith("https://www.reddit.com/r/AskReddit/")

    assert [c.id for c in result.comments] == ["c1", "c2", "c3"]
    c1, c2, c3 = result.comments
    assert c1.depth == 0 and c1.parent_id == ""
    assert c2.depth == 1 and c2.parent_id == "c1" and c2.is_submitter
    assert c3.author == "[deleted]"


def test_scrape_thread_expands_more_comments():
    scraper = make_scraper(expand_more=True)
    thread, more = load("thread.json"), load("morechildren.json")

    def fake_get(url, params=None):
        return more if "morechildren" in url else thread

    with patch.object(scraper, "_get", side_effect=fake_get):
        result = scraper.scrape_thread("https://www.reddit.com/comments/1abcde")

    ids = [c.id for c in result.comments]
    assert ids == ["c1", "c2", "c3", "c4", "c5"]
    by_id = {c.id: c for c in result.comments}
    assert by_id["c4"].depth == 1  # child of c1
    assert by_id["c5"].depth == 2  # child of c4


def test_scrape_thread_records_error_instead_of_raising():
    scraper = make_scraper()
    with patch.object(scraper, "_get", side_effect=ScrapeError("thread not found (404)")):
        result = scraper.scrape_thread("https://www.reddit.com/comments/deadbeef")
    assert not result.ok
    assert "404" in result.error


def test_scrape_all_continues_after_failures():
    scraper = make_scraper(expand_more=False)
    thread = load("thread.json")
    calls = iter([ScrapeError("boom"), thread])

    def fake_get(url, params=None):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    with patch.object(scraper, "_get", side_effect=fake_get):
        results = scraper.scrape_all([
            "https://www.reddit.com/comments/aaaaaa",
            "https://www.reddit.com/comments/1abcde",
        ])
    assert [r.ok for r in results] == [False, True]
    assert len(results[1].comments) == 3
