"""Username + password auth for the Vishal AI web app.

Single-user app: credentials live in env vars (WEB_USERNAME, WEB_PASSWORD).
Comparison uses secrets.compare_digest to dodge timing leaks even though
the practical risk is near-zero at this scale.

Session lives in a signed cookie (Starlette SessionMiddleware), 30-day TTL.
"""
from __future__ import annotations

import secrets

from fastapi import Request

from .. import config


def verify_password(username: str, password: str) -> bool:
    return (
        secrets.compare_digest((username or "").strip(), config.WEB_USERNAME)
        and secrets.compare_digest((password or ""), config.WEB_PASSWORD)
    )


def is_authed(request: Request) -> bool:
    return bool(request.session.get("authed"))


def set_authed(request: Request) -> None:
    request.session["authed"] = True


def clear_session(request: Request) -> None:
    request.session.clear()
