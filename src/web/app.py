"""FastAPI app — Vishal AI control panel.

Mounted as a worker process on Railway. Auth via Telegram magic link.
Templates: Jinja2. UI: HTMX via CDN. No build step.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import config

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_ROOT / "templates"
STATIC_DIR = WEB_ROOT / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app() -> FastAPI:
    app = FastAPI(title="Vishal AI", docs_url=None, redoc_url=None)

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.WEB_SESSION_SECRET,
        session_cookie="vishal_ai_session",
        max_age=60 * 60 * 24 * 30,  # 30 days
        same_site="lax",
        https_only=False,  # Railway terminates TLS; cookies still work
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Routes registered in routes.py (imported here to avoid circulars).
    from . import routes  # noqa: F401
    routes.register(app, templates)

    return app
