"""M3: Tier 2 fetcher. Break-glass fallback only: old.reddit HTML via Playwright.

Serializes to the same raw JSON shape as Tier 1 so downstream code has one
input format. Never the default; the router activates it on repeated 403s.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from .normalize import BROWSER_UA

logger = logging.getLogger(__name__)


class SelectorDrift(Exception):
    """old.reddit markup changed: fail loudly rather than store bad data."""


class Tier2Error(Exception):
    pass


def _parse_score(thing) -> int:
    node = thing.select_one(".score.unvoted")
    if node:
        title = node.get("title")
        if title and title.lstrip("-").isdigit():
            return int(title)
        text = node.get_text(" ", strip=True)
        m = re.match(r"(-?\d+)\s*point", text)
        if m:
            return int(m.group(1))
    return 0


def _parse_created(thing) -> float:
    node = thing.select_one("time[datetime]")
    if node and node.get("datetime"):
        try:
            dt = datetime.fromisoformat(node["datetime"].replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).timestamp()
        except ValueError:
            pass
    return 0.0


def _comment_depth(thing) -> int:
    """Depth from DOM nesting of .child containers."""
    depth = 0
    node = thing.parent
    while node is not None:
        classes = node.get("class") or [] if hasattr(node, "get") else []
        if "child" in classes:
            depth += 1
        node = node.parent
    return max(0, depth - 1)  # the listing sits inside one wrapper level


def parse_thread_html(html: str, post_id: str) -> dict:
    """Parse one old.reddit comment page into partial raw-thread structure."""
    soup = BeautifulSoup(html, "html.parser")

    post_thing = soup.select_one(f'div.thing[data-fullname="t3_{post_id}"]') or \
        soup.select_one("div.thing.link")
    title_node = soup.select_one("a.title")
    if post_thing is None or title_node is None:
        raise SelectorDrift("post node or title not found on page")

    num_reported = 0
    comments_link = soup.select_one("a.comments")
    if comments_link:
        m = re.search(r"(\d[\d,]*)", comments_link.get_text())
        if m:
            num_reported = int(m.group(1).replace(",", ""))

    selftext_node = post_thing.select_one(".usertext-body .md")
    author = post_thing.get("data-author") or None
    score_attr = post_thing.get("data-score")

    subreddit = post_thing.get("data-subreddit") or ""
    permalink = post_thing.get("data-permalink") or f"/comments/{post_id}/"

    post = {
        "id": post_id,
        "fullname": f"t3_{post_id}",
        "subreddit": subreddit,
        "title": title_node.get_text(strip=True),
        "selftext": selftext_node.get_text("\n", strip=True) if selftext_node else "",
        "author": author,
        "score": int(score_attr) if score_attr and score_attr.lstrip("-").isdigit() else 0,
        "upvote_ratio": None,
        "num_comments": num_reported,
        "created_utc": _parse_created(post_thing),
        "permalink": permalink,
        "link_flair_text": None,
        "over_18": bool(post_thing.get("data-nsfw") == "true"),
    }

    comments = []
    for thing in soup.select("div.thing.comment"):
        fullname = thing.get("data-fullname")
        if not fullname:
            continue
        body_node = thing.select_one(".usertext-body .md")
        comments.append({
            "fullname": fullname,
            "parent_fullname": "",  # filled from depth chain below
            "author": thing.get("data-author") or None,
            "body": body_node.get_text("\n", strip=True) if body_node else "",
            "score": _parse_score(thing),
            "created_utc": _parse_created(thing),
            "is_submitter": "submitter" in (thing.select_one(".author") or {}).get("class", []),
            "stickied": "stickied" in (thing.get("class") or []),
            "distinguished": None,
            "_depth": _comment_depth(thing),
        })

    # Reconstruct parent chains from document order + depth.
    stack: list[tuple[int, str]] = []
    for c in comments:
        depth = c.pop("_depth")
        while stack and stack[-1][0] >= depth:
            stack.pop()
        c["parent_fullname"] = stack[-1][1] if stack else f"t3_{post_id}"
        stack.append((depth, c["fullname"]))

    more_links = []
    for a in soup.select("span.morecomments a"):
        href = a.get("href")
        if href:
            more_links.append(href)
    for a in soup.select("a"):
        if "continue this thread" in a.get_text(strip=True).lower() and a.get("href"):
            more_links.append(a["href"])

    return {"post": post, "comments": comments, "more_links": more_links}


def fetch_thread_tier2(permalink: str, post_id: str, cfg: dict) -> tuple[dict, bool]:
    """Navigate old.reddit with Playwright, expanding more/continue links BFS.

    Returns (raw_thread_dict_in_tier1_shape, truncated). Raises SelectorDrift
    when the fetched comment count is under 50% of Reddit's reported count.
    """
    from playwright.sync_api import sync_playwright  # lazy: Phase 3 dependency

    t = cfg["thresholds"]
    delay_lo, delay_hi = t["TIER2_NAV_DELAY"]
    max_nav = t["TIER2_MAX_NAV"]
    depth_cap = t["TIER2_DEPTH_CAP"]

    seen_comments: dict[str, dict] = {}
    post: dict | None = None
    truncated = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=BROWSER_UA, viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()

        queue: list[tuple[str, int]] = [(f"https://old.reddit.com{permalink}?limit=500", 0)]
        nav_count = 0
        visited: set[str] = set()

        while queue:
            if nav_count >= max_nav:
                truncated = True
                break
            url, nav_depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            if nav_count > 0:
                time.sleep(random.uniform(delay_lo, delay_hi))
            nav_count += 1
            logger.info("tier2 nav %d/%d: %s", nav_count, max_nav, url)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            parsed = parse_thread_html(page.content(), post_id)

            if post is None:
                post = parsed["post"]
            for c in parsed["comments"]:
                seen_comments.setdefault(c["fullname"], c)
            if nav_depth >= depth_cap:
                truncated = truncated or bool(parsed["more_links"])
                continue
            for href in parsed["more_links"]:
                full = href if href.startswith("http") else f"https://old.reddit.com{href}"
                if full not in visited:
                    queue.append((full, nav_depth + 1))

        if queue:
            truncated = True
        browser.close()

    if post is None:
        raise Tier2Error("no page parsed")

    comments = list(seen_comments.values())
    reported = post["num_comments"] or 0
    # Selector drift guard: markup change shows up as a huge undercount.
    if reported > 0 and not truncated and len(comments) < 0.5 * reported:
        raise SelectorDrift(
            f"fetched {len(comments)} of {reported} reported comments"
        )

    return {"tier": 2, "post": post, "comments": comments}, truncated
