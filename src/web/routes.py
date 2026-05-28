"""All HTTP routes for the Vishal AI web app."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config, db
from . import auth

log = logging.getLogger(__name__)


# ── Auth helpers ──────────────────────────────────────────

def _require_auth(request: Request) -> RedirectResponse | None:
    if not auth.is_authed(request):
        return RedirectResponse(url="/login", status_code=303)
    return None


# ── Helpers ───────────────────────────────────────────────

def _next_scheduled_slot() -> str:
    """Returns a human-readable next post time, e.g. 'today 13:00' or 'tomorrow 09:00'."""
    if not config.POST_TIMES:
        return "no slots configured"
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    today = now.date()
    candidates = []
    for slot in config.POST_TIMES:
        hh, mm = slot.split(":")
        for d in (today, today + timedelta(days=1)):
            dt = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=tz)
            if dt > now:
                candidates.append(dt)
    if not candidates:
        return "—"
    nxt = min(candidates)
    label = "today" if nxt.date() == today else "tomorrow"
    return f"{label} {nxt.strftime('%H:%M')} {config.TIMEZONE}"


def _truncate(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


# ── The route registrar ───────────────────────────────────

def register(app: FastAPI, templates: Jinja2Templates) -> None:

    # ─── Auth: login form + handler + logout ────────────────────

    @app.get("/login", response_class=HTMLResponse)
    async def get_login(request: Request, msg: str = ""):
        if auth.is_authed(request):
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"msg": msg, "msg_ok": False}
        )

    @app.post("/login", response_class=HTMLResponse)
    async def post_login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        if auth.verify_password(username, password):
            auth.set_authed(request)
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"msg": "Wrong username or password.", "msg_ok": False},
        )

    @app.get("/logout")
    async def get_logout(request: Request):
        auth.clear_session(request)
        return RedirectResponse(url="/login?msg=Logged+out", status_code=303)

    # ─── Health (public) ──────────────────────────────────

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "service": "vishal-ai-web"}

    # ─── Dashboard ────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if r := _require_auth(request):
            return r
        ideas = db.list_queued(limit=100)
        posts = db.recent_posts(limit=5)
        creators_count = len(db.list_creators())
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "page": "dashboard",
                "queued_count": len(ideas),
                "posts_count": len(db.recent_posts(limit=1000)),
                "creators_count": creators_count,
                "recent_posts": posts,
                "next_slot": _next_scheduled_slot(),
                "apify_on": config.APIFY_ENABLED,
                "post_times": config.POST_TIMES,
                "timezone": config.TIMEZONE,
                "_t": _truncate,
            },
        )

    # ─── Queue ────────────────────────────────────────────

    @app.get("/queue", response_class=HTMLResponse)
    async def queue_get(request: Request):
        if r := _require_auth(request):
            return r
        ideas = db.list_queued(limit=200)
        return templates.TemplateResponse(
            request,
            "queue.html",
            {"page": "queue", "ideas": ideas, "_t": _truncate},
        )

    @app.post("/queue/add")
    async def queue_add(request: Request, text: str = Form(...)):
        if r := _require_auth(request):
            return r
        text = (text or "").strip()
        if text:
            db.add_idea(text, source="web")
        return RedirectResponse(url="/queue", status_code=303)

    @app.post("/queue/{idea_id}/skip")
    async def queue_skip(request: Request, idea_id: int):
        if r := _require_auth(request):
            return r
        db.skip_idea(idea_id)
        return RedirectResponse(url="/queue", status_code=303)

    # ─── Posts history ────────────────────────────────────

    @app.get("/posts", response_class=HTMLResponse)
    async def posts_get(request: Request):
        if r := _require_auth(request):
            return r
        posts = db.recent_posts(limit=50)
        return templates.TemplateResponse(
            request,
            "posts.html",
            {"page": "posts", "posts": posts, "_t": _truncate},
        )

    # ─── Creators ─────────────────────────────────────────

    @app.get("/creators", response_class=HTMLResponse)
    async def creators_get(request: Request):
        if r := _require_auth(request):
            return r
        creators = db.list_creators()
        return templates.TemplateResponse(
            request,
            "creators.html",
            {"page": "creators", "creators": creators},
        )

    @app.post("/creators/add")
    async def creators_add(
        request: Request,
        platform: str = Form(...),
        handle: str = Form(...),
    ):
        if r := _require_auth(request):
            return r
        platform = platform.strip().lower()
        handle = handle.strip().lstrip("@").lower()
        if platform in {"linkedin", "x"} and handle:
            db.add_creator(platform=platform, handle=handle)
        return RedirectResponse(url="/creators", status_code=303)

    @app.post("/creators/{creator_id}/remove")
    async def creators_remove(request: Request, creator_id: int):
        if r := _require_auth(request):
            return r
        # Look up to get platform+handle, then remove via that pair.
        rows = [c for c in db.list_creators() if c["id"] == creator_id]
        if rows:
            db.remove_creator(rows[0]["platform"], rows[0]["handle"])
        return RedirectResponse(url="/creators", status_code=303)

    # ─── Compose: draft → preview → publish/queue/regenerate ──

    @app.get("/compose", response_class=HTMLResponse)
    async def compose_get(request: Request):
        if r := _require_auth(request):
            return r
        return templates.TemplateResponse(
            request,
            "compose.html",
            {"page": "compose", "draft": None, "idea_text": ""},
        )

    @app.post("/compose/draft", response_class=HTMLResponse)
    async def compose_draft(request: Request, idea: str = Form(...)):
        if r := _require_auth(request):
            return r
        idea = (idea or "").strip()
        if not idea:
            return RedirectResponse(url="/compose", status_code=303)

        from ..content import generator
        post_id_hint = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_web"
        loop = asyncio.get_running_loop()
        try:
            variants = await loop.run_in_executor(
                None, lambda: generator.generate(idea, post_id_hint=post_id_hint)
            )
        except Exception as e:
            log.exception("compose draft failed")
            return templates.TemplateResponse(
                request,
                "compose.html",
                {
                    "page": "compose",
                    "draft": None,
                    "idea_text": idea,
                    "error": str(e)[:300],
                },
            )

        draft = {v.platform: v for v in variants}
        return templates.TemplateResponse(
            request,
            "compose.html",
            {
                "page": "compose",
                "draft": draft,
                "idea_text": idea,
            },
        )

    @app.post("/compose/publish")
    async def compose_publish(
        request: Request,
        idea: str = Form(...),
        linkedin_text: str = Form(""),
        x_text: str = Form(""),
        mode: str = Form("schedule"),  # 'now' or 'schedule'
    ):
        if r := _require_auth(request):
            return r

        from ..publishers import postsyncer
        idea_id = db.add_idea(idea, source="web") if idea.strip() else None

        # Compute next slot for scheduling
        schedule_for = None
        if mode == "schedule":
            schedule_for = _next_slot_payload()

        results = []
        for platform_key, text in (("linkedin", linkedin_text), ("x", x_text)):
            text = (text or "").strip()
            if not text:
                continue
            try:
                resp = postsyncer.publish(
                    platform=platform_key, text=text, schedule_for=schedule_for
                )
                ps_id = str(resp.get("data", {}).get("id") or resp.get("id") or "")
                db.log_post(
                    idea_id=idea_id,
                    platform=platform_key,
                    text=text,
                    cta_url="",  # already inline if present
                    status="scheduled" if schedule_for else "published",
                    postsyncer_post_id=ps_id,
                )
                results.append((platform_key, True, None))
            except Exception as e:
                log.exception("publish failed for %s", platform_key)
                db.log_post(
                    idea_id=idea_id, platform=platform_key, text=text,
                    cta_url="", status="failed", error=str(e)[:300],
                )
                results.append((platform_key, False, str(e)[:200]))

        if idea_id and any(ok for _, ok, _ in results):
            db.mark_idea_used(idea_id)

        return templates.TemplateResponse(
            request,
            "compose.html",
            {
                "page": "compose",
                "draft": None,
                "idea_text": "",
                "published": results,
                "mode_used": mode,
                "scheduled_for_label": (
                    _next_scheduled_slot() if mode == "schedule" else "now"
                ),
            },
        )

    @app.post("/compose/queue")
    async def compose_queue(request: Request, idea: str = Form(...)):
        if r := _require_auth(request):
            return r
        idea = (idea or "").strip()
        if idea:
            db.add_idea(idea, source="web")
        return RedirectResponse(url="/queue", status_code=303)

    # ─── Manual triggers (post_now, refresh) ──────────────

    @app.post("/actions/post_now")
    async def actions_post_now(request: Request):
        if r := _require_auth(request):
            return r
        from .. import scheduler
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, scheduler.run_post_cycle)
        except Exception as e:
            log.exception("post_now failed")
        return RedirectResponse(url="/posts", status_code=303)

    @app.post("/actions/refresh")
    async def actions_refresh(request: Request):
        if r := _require_auth(request):
            return r
        from ..content import viral_discovery
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, viral_discovery.refresh)
        except Exception as e:
            log.exception("refresh failed")
        return RedirectResponse(url="/creators", status_code=303)

    # ─── Settings ─────────────────────────────────────────

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_get(request: Request):
        if r := _require_auth(request):
            return r
        env_summary = {
            "Anthropic model": config.CLAUDE_MODEL,
            "Postsyncer workspace": str(config.POSTSYNCER_WORKSPACE_ID),
            "LinkedIn acct id": str(config.POSTSYNCER_LINKEDIN_ACCOUNT_ID),
            "X acct id": str(config.POSTSYNCER_X_ACCOUNT_ID),
            "Telegram allowed user": str(config.TELEGRAM_ALLOWED_USER_ID),
            "Apify enabled": "yes" if config.APIFY_ENABLED else "no",
            "CTA URL": config.CTA_URL,
            "Post times": ", ".join(config.POST_TIMES) + f" {config.TIMEZONE}",
            "DB path": config.DB_PATH,
        }
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "page": "settings",
                "env": env_summary,
                "brand": config.BRAND_CONFIG,
            },
        )


def _next_slot_payload() -> dict:
    """Build a Postsyncer schedule_for payload for the next configured slot."""
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    today = now.date()
    candidates = []
    for slot in config.POST_TIMES:
        hh, mm = slot.split(":")
        for d in (today, today + timedelta(days=1)):
            dt = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=tz)
            if dt > now:
                candidates.append(dt)
    nxt = min(candidates) if candidates else now + timedelta(hours=1)
    return {
        "date": nxt.strftime("%Y-%m-%d"),
        "time": nxt.strftime("%H:%M"),
        "timezone": config.TIMEZONE,
    }
