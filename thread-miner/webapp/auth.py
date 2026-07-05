"""Shared-team-password auth with a signed session cookie.

One password for the whole team (APP_PASSWORD env var); each person types
their name at login so runs are attributed in history. The cookie is signed
with SECRET_KEY, so it can't be forged, and holds only the display name.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "tm_session"
MAX_AGE_SECONDS = 30 * 24 * 3600

# Generated fallback keeps local tinkering painless; sessions just reset on
# restart. Production must set SECRET_KEY or logins won't survive deploys.
_FALLBACK_SECRET = secrets.token_hex(32)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(os.environ.get("SECRET_KEY") or _FALLBACK_SECRET)


def check_password(candidate: str) -> bool:
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        return False  # unset password means nobody can log in - fail closed
    return secrets.compare_digest(candidate.encode(), expected.encode())


def session_cookie_value(name: str) -> str:
    return _serializer().dumps({"name": name})


def current_user(request: Request) -> str | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=MAX_AGE_SECONDS)
        return data.get("name") or None
    except (BadSignature, SignatureExpired):
        return None


class LoginRequired(Exception):
    """Raised by the require_login dependency; handled with a redirect."""


def require_login(request: Request) -> str:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def login_redirect() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)
