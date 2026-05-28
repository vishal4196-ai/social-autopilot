"""Magic-link auth via Telegram.

Flow:
1. /login → page with a "Send me a link in Telegram" button
2. POST /login/request → issues a one-time token, DMs the link to Vishal
3. Vishal taps the link in Telegram → GET /login/verify?token=… → token validated,
   single-use, sets session cookie → redirect to dashboard.

In-memory token store with 10-minute TTL. Survives the bot's lifetime but not
process restarts — that's fine, Vishal just requests a fresh link if Railway
redeploys mid-session.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import RedirectResponse

log = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 600  # 10 min

# token -> expires_at_ts
_TOKENS: dict[str, float] = {}


def _prune() -> None:
    now = time.time()
    expired = [t for t, exp in _TOKENS.items() if exp < now]
    for t in expired:
        _TOKENS.pop(t, None)


def issue_token() -> str:
    _prune()
    token = secrets.token_urlsafe(32)
    _TOKENS[token] = time.time() + TOKEN_TTL_SECONDS
    return token


def consume_token(token: str) -> bool:
    """Single-use: returns True only on first valid check."""
    _prune()
    exp = _TOKENS.pop(token, None)
    if not exp:
        return False
    return time.time() <= exp


def is_authed(request: Request) -> bool:
    return bool(request.session.get("authed"))


def set_authed(request: Request) -> None:
    request.session["authed"] = True
    request.session["authed_at"] = int(time.time())


def clear_session(request: Request) -> None:
    request.session.clear()


# ── Send link via Telegram ────────────────────────────────────
# We import the global telegram Application lazily so this module stays
# loadable even if main.py hasn't initialised the bot yet.

_tg_app = None  # set by main.py at startup


def register_telegram_app(app) -> None:
    global _tg_app
    _tg_app = app


async def send_login_link(base_url: str) -> tuple[bool, str]:
    """Send a magic link to the configured Telegram user. Returns (ok, message)."""
    from .. import config

    if _tg_app is None:
        return False, "Telegram bot not running"

    token = issue_token()
    url = f"{base_url.rstrip('/')}/login/verify?token={token}"
    msg = (
        "🔑 Vishal AI login link (valid 10 min, single-use):\n\n"
        f"{url}\n\n"
        "If you didn't request this, ignore — the link expires automatically."
    )
    try:
        await _tg_app.bot.send_message(
            chat_id=config.TELEGRAM_ALLOWED_USER_ID, text=msg
        )
        return True, "Link sent — check Telegram."
    except Exception as e:
        log.exception("send_login_link failed")
        return False, f"Failed to send: {e}"
