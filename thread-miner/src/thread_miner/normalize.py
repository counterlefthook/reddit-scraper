"""M1: URL canonicalization. Rules from spec section 7, applied in order."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

ACCEPTED_HOSTS = {
    "reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com",
    "np.reddit.com", "m.reddit.com", "sh.reddit.com", "redd.it",
}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_BASE36 = re.compile(r"^[a-z0-9]+$")
_COMMENTS_PATH = re.compile(
    r"^/(?:r/(?P<sub>[^/]+)/)?comments/(?P<id>[A-Za-z0-9]+)"
    r"(?:/(?P<slug>[^/]+)(?:/(?P<comment>[A-Za-z0-9]+))?)?/?$"
)
_SHARE_PATH = re.compile(r"^/r/[^/]+/s/[A-Za-z0-9]+/?$")
_SHORTLINK_PATH = re.compile(r"^/(?P<id>[A-Za-z0-9]+)/?$")

# Statuses this module can assign (subset of the ingest_rows CHECK constraint).
NORMALIZED = "normalized"
INVALID = "invalid_url"
UNRESOLVED_SHARE = "unresolved_share_link"


@dataclass
class NormalizedURL:
    status: str
    post_fullname: Optional[str] = None  # t3_xxx
    focus_comment_id: Optional[str] = None

    @property
    def post_id(self) -> Optional[str]:
        return self.post_fullname[3:] if self.post_fullname else None


def resolve_share_link(url: str, timeout: float = 10.0) -> Optional[str]:
    """Resolve a /s/ mobile share link by following redirects. None on any failure."""
    try:
        resp = httpx.head(
            url, follow_redirects=True, timeout=timeout,
            headers={"User-Agent": BROWSER_UA},
        )
        if 200 <= resp.status_code < 400:
            return str(resp.url)
    except httpx.HTTPError:
        pass
    return None


def _fullname(raw_id: str) -> Optional[str]:
    post_id = raw_id.lower()
    if _BASE36.match(post_id):
        return f"t3_{post_id}"
    return None


def normalize_url(
    raw_url: str,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
    _resolving_share: bool = False,
) -> NormalizedURL:
    """Canonicalize one Reddit URL to a post fullname per the section 7 rules.

    share_resolver defaults to the live redirect resolver; tests inject a fake.
    """
    url = str(raw_url or "").strip()
    if not url:
        return NormalizedURL(INVALID)
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host not in ACCEPTED_HOSTS:
        return NormalizedURL(INVALID)

    # Rule 1: strip query parameters and fragments (implicit: we only use path).
    path = parts.path

    # Rule 2: redd.it/{id}
    if host == "redd.it":
        m = _SHORTLINK_PATH.match(path)
        if m:
            fullname = _fullname(m.group("id"))
            if fullname:
                return NormalizedURL(NORMALIZED, fullname)
        return NormalizedURL(INVALID)

    # Rule 3: /r/{sub}/comments/{id}[/{slug}[/{comment_id}]]
    m = _COMMENTS_PATH.match(path)
    if m:
        fullname = _fullname(m.group("id"))
        if not fullname:
            return NormalizedURL(INVALID)
        focus = m.group("comment")
        return NormalizedURL(NORMALIZED, fullname, focus.lower() if focus else None)

    # Rule 4: /r/{sub}/s/{token} mobile share link
    if _SHARE_PATH.match(path):
        if _resolving_share:  # a share link redirected to another share link
            return NormalizedURL(UNRESOLVED_SHARE)
        resolver = share_resolver or resolve_share_link
        resolved = resolver(f"https://{host}{path}")
        if not resolved:
            return NormalizedURL(UNRESOLVED_SHARE)
        result = normalize_url(resolved, share_resolver, _resolving_share=True)
        if result.status != NORMALIZED:
            return NormalizedURL(UNRESOLVED_SHARE)
        return result

    # Rule 6: nothing resolved to a post id.
    return NormalizedURL(INVALID)
