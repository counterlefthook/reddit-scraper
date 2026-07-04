"""Fetch and parse Reddit threads via Reddit's public JSON API.

No API key is required: every Reddit thread is available as JSON by
appending ``.json`` to its URL. Reddit rate-limits anonymous clients,
so requests are spaced out and retried with backoff on 429/5xx.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "reddit-thread-scraper/0.1 (research tool; contact: local user)"

# Matches /comments/<id> style permalinks and redd.it short links.
_COMMENTS_RE = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)
_SHORTLINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?redd\.it/([a-z0-9]+)", re.IGNORECASE)


class ScrapeError(Exception):
    """Raised when a thread cannot be fetched or parsed."""


@dataclass
class Post:
    id: str
    url: str
    subreddit: str
    title: str
    author: str
    score: int
    upvote_ratio: Optional[float]
    num_comments: int
    created_utc: float
    selftext: str
    link_flair: str
    is_video: bool
    over_18: bool
    permalink: str


@dataclass
class Comment:
    id: str
    post_id: str
    parent_id: str  # "" for top-level comments
    depth: int
    author: str
    body: str
    score: int
    created_utc: float
    is_submitter: bool
    stickied: bool
    permalink: str


@dataclass
class ThreadResult:
    source_url: str
    post: Optional[Post] = None
    comments: list[Comment] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.post is not None


def normalize_url(url: str) -> str:
    """Return a canonical ``https://www.reddit.com/comments/<id>`` JSON-able URL.

    Accepts full permalinks, old.reddit/np.reddit variants, redd.it short
    links, and URLs with query strings or trailing junk copied from Excel.
    """
    url = str(url).strip().strip('"').strip("'")
    if not url:
        raise ScrapeError("empty URL")

    m = _SHORTLINK_RE.match(url)
    if m:
        return f"https://www.reddit.com/comments/{m.group(1)}"

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parts = urlsplit(url)
    host = parts.netloc.lower()
    if not host.endswith("reddit.com") and host != "redd.it":
        raise ScrapeError(f"not a Reddit URL: {url}")

    path = parts.path
    m = _COMMENTS_RE.search(path)
    if m:
        # Keep only up to the post id; drop comment-permalink suffixes so we
        # always fetch the full thread.
        path = path[: m.end()]
        return urlunsplit(("https", "www.reddit.com", path, "", ""))

    # Share links (reddit.com/r/x/s/abc) redirect to the real thread; keep
    # them as-is and let requests follow the redirect.
    if "/s/" in path:
        return urlunsplit(("https", host, path, "", ""))

    raise ScrapeError(f"could not find a post id in URL: {url}")


class RedditScraper:
    """Downloads Reddit threads and flattens their comment trees.

    Parameters
    ----------
    delay:
        Minimum seconds between HTTP requests (Reddit throttles anonymous
        clients at roughly 10 requests/minute; the default stays under that).
    expand_more:
        Whether to fetch comments hidden behind "load more comments" stubs.
    max_more_requests:
        Cap on extra "load more" requests per thread, so one huge thread
        can't stall the whole run.
    """

    MORE_CHILDREN_URL = "https://www.reddit.com/api/morechildren.json"
    BATCH = 100  # max ids per morechildren call

    def __init__(
        self,
        delay: float = 6.5,
        expand_more: bool = True,
        max_more_requests: int = 20,
        timeout: float = 30.0,
        max_retries: int = 4,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.delay = delay
        self.expand_more = expand_more
        self.max_more_requests = max_more_requests
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self._last_request_at = 0.0

    # ------------------------------------------------------------------ http

    def _throttle(self) -> None:
        wait = self._last_request_at + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, params: Optional[dict] = None) -> dict | list:
        """GET with throttling and retry/backoff on 429 and 5xx."""
        backoff = 5.0
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self._last_request_at = time.monotonic()
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    pause = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                    logger.warning("HTTP %s from %s; retrying in %.0fs", resp.status_code, url, pause)
                    time.sleep(pause)
                    backoff *= 2
                    continue
                if resp.status_code == 403:
                    raise ScrapeError(
                        "Reddit returned 403 (blocked). The post may be private/quarantined, "
                        "or Reddit is blocking this network. Try a longer --delay."
                    )
                if resp.status_code == 404:
                    raise ScrapeError("thread not found (404) - deleted post or bad URL")
                raise ScrapeError(f"HTTP {resp.status_code} from Reddit")
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                if attempt < self.max_retries:
                    logger.warning("network error (%s); retrying in %.0fs", exc, backoff)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
        raise ScrapeError(f"network error after {self.max_retries} retries: {last_error}")

    # ----------------------------------------------------------------- parse

    @staticmethod
    def _parse_post(data: dict, source_url: str) -> Post:
        return Post(
            id=data.get("id", ""),
            url=source_url,
            subreddit=data.get("subreddit", ""),
            title=data.get("title", ""),
            author=data.get("author") or "[deleted]",
            score=int(data.get("score") or 0),
            upvote_ratio=data.get("upvote_ratio"),
            num_comments=int(data.get("num_comments") or 0),
            created_utc=float(data.get("created_utc") or 0),
            selftext=data.get("selftext") or "",
            link_flair=data.get("link_flair_text") or "",
            is_video=bool(data.get("is_video")),
            over_18=bool(data.get("over_18")),
            permalink="https://www.reddit.com" + (data.get("permalink") or ""),
        )

    @staticmethod
    def _parse_comment(data: dict, post_id: str, depth: int) -> Comment:
        parent = data.get("parent_id") or ""
        if parent.startswith("t3_"):
            parent = ""  # top-level: parent is the post itself
        elif parent.startswith("t1_"):
            parent = parent[3:]
        return Comment(
            id=data.get("id", ""),
            post_id=post_id,
            parent_id=parent,
            depth=depth,
            author=data.get("author") or "[deleted]",
            body=data.get("body") or "",
            score=int(data.get("score") or 0),
            created_utc=float(data.get("created_utc") or 0),
            is_submitter=bool(data.get("is_submitter")),
            stickied=bool(data.get("stickied")),
            permalink="https://www.reddit.com" + (data.get("permalink") or ""),
        )

    def _walk_comments(
        self, children: Iterable[dict], post_id: str, depth: int,
        out: list[Comment], more_ids: list[str],
    ) -> None:
        for child in children:
            kind = child.get("kind")
            data = child.get("data", {})
            if kind == "t1":
                out.append(self._parse_comment(data, post_id, depth))
                replies = data.get("replies")
                if isinstance(replies, dict):
                    self._walk_comments(
                        replies.get("data", {}).get("children", []),
                        post_id, depth + 1, out, more_ids,
                    )
            elif kind == "more":
                more_ids.extend(data.get("children") or [])

    def _expand_more(self, link_id: str, more_ids: list[str], post_id: str,
                     depth_by_id: dict[str, int]) -> list[Comment]:
        """Fetch comments hidden behind "load more comments" stubs."""
        out: list[Comment] = []
        pending = list(dict.fromkeys(more_ids))  # dedupe, keep order
        requests_made = 0
        while pending and requests_made < self.max_more_requests:
            batch, pending = pending[: self.BATCH], pending[self.BATCH:]
            requests_made += 1
            try:
                payload = self._get(
                    self.MORE_CHILDREN_URL,
                    params={
                        "api_type": "json",
                        "link_id": f"t3_{link_id}",
                        "children": ",".join(batch),
                        "raw_json": 1,
                    },
                )
            except ScrapeError as exc:
                logger.warning("could not expand more-comments batch: %s", exc)
                continue
            things = (
                payload.get("json", {}).get("data", {}).get("things", [])
                if isinstance(payload, dict) else []
            )
            for thing in things:
                data = thing.get("data", {})
                if thing.get("kind") == "more":
                    pending.extend(data.get("children") or [])
                    continue
                if thing.get("kind") != "t1":
                    continue
                parent = data.get("parent_id") or ""
                if parent.startswith("t1_"):
                    depth = depth_by_id.get(parent[3:], 0) + 1
                else:
                    depth = 0
                comment = self._parse_comment(data, post_id, depth)
                depth_by_id[comment.id] = depth
                out.append(comment)
        if pending:
            logger.info(
                "thread %s: stopped after %d more-comment requests; %d stubs left",
                link_id, requests_made, len(pending),
            )
        return out

    # ------------------------------------------------------------------- api

    def scrape_thread(self, url: str) -> ThreadResult:
        """Scrape a single thread. Never raises; errors land in ``result.error``."""
        result = ThreadResult(source_url=url)
        try:
            clean = normalize_url(url)
            payload = self._get(clean + ".json", params={"limit": 500, "raw_json": 1})
            if not isinstance(payload, list) or len(payload) < 2:
                raise ScrapeError("unexpected response shape from Reddit")

            post_children = payload[0].get("data", {}).get("children", [])
            if not post_children:
                raise ScrapeError("no post data in response")
            post = self._parse_post(post_children[0].get("data", {}), url)
            result.post = post

            comments: list[Comment] = []
            more_ids: list[str] = []
            self._walk_comments(
                payload[1].get("data", {}).get("children", []),
                post.id, 0, comments, more_ids,
            )

            if self.expand_more and more_ids:
                depth_by_id = {c.id: c.depth for c in comments}
                comments.extend(self._expand_more(post.id, more_ids, post.id, depth_by_id))

            result.comments = comments
        except ScrapeError as exc:
            result.error = str(exc)
        except Exception as exc:  # defensive: one bad thread must not kill the run
            logger.exception("unexpected error scraping %s", url)
            result.error = f"unexpected error: {exc}"
        return result

    def scrape_all(
        self,
        urls: Iterable[str],
        progress: Optional[Callable[[int, int, ThreadResult], None]] = None,
    ) -> list[ThreadResult]:
        """Scrape every URL, calling ``progress(done, total, result)`` after each."""
        urls = list(urls)
        results: list[ThreadResult] = []
        for i, url in enumerate(urls, start=1):
            res = self.scrape_thread(url)
            results.append(res)
            if res.ok:
                logger.info("[%d/%d] %s - %d comments", i, len(urls), url, len(res.comments))
            else:
                logger.warning("[%d/%d] %s - FAILED: %s", i, len(urls), url, res.error)
            if progress:
                progress(i, len(urls), res)
        return results
