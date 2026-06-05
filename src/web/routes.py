"""All HTTP routes for the Vishal AI web app."""
from __future__ import annotations

import asyncio
import calendar as cal_mod
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config, db
from . import auth

log = logging.getLogger(__name__)

DAY_HEADERS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _post_calendar_date(p, tz: ZoneInfo) -> datetime:
    """Best date for placing a post on the calendar: scheduled_for if set
    (local tz), else created_at (stored as naive UTC)."""
    raw = p["scheduled_for"]
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(tz)
        except (ValueError, TypeError):
            pass
    try:
        dt = datetime.fromisoformat(p["created_at"]).replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(tz)
    except (ValueError, TypeError):
        return datetime.now(tz)


def _time_label(dt: datetime) -> str:
    """Cross-platform 12-hour label (avoids %-I which breaks on Windows)."""
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour12}:{dt.minute:02d} {ampm}"


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


def _nav_counts() -> dict:
    """Sidebar phase badges. 'approved' here = anything waiting to publish,
    which includes both phase='scheduled' (sent to Postsyncer, awaiting fire)
    and phase='approved' (Postsyncer schedule failed, cron will retry)."""
    return {
        "ideation": len(db.list_by_phase("ideation", limit=999)),
        "researching": len(db.list_by_phase("researching", limit=999)),
        "drafted": len(db.list_by_phase("drafted", limit=999)),
        "approved": (
            len(db.list_by_phase("approved", limit=999))
            + len(db.list_by_phase("scheduled", limit=999))
        ),
    }


def _ctx(extra: dict | None = None) -> dict:
    """Common context: page + nav_counts. Routes spread their own dict over this."""
    base = {"nav_counts": _nav_counts()}
    if extra:
        base.update(extra)
    return base


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

    # ─── Today (the daily ritual: see + draft + approve) ─

    @app.get("/", response_class=HTMLResponse)
    @app.get("/today", response_class=HTMLResponse)
    async def today_get(request: Request, err: str = ""):
        if r := _require_auth(request):
            return r
        import json as _json
        nav = _nav_counts()
        # Fresh ideas (ideation phase), AI first then user
        ideation_all = db.list_by_phase("ideation", limit=200)
        ai_fresh = [i for i in ideation_all if i["source"] == "research_agent"]
        my_fresh = [i for i in ideation_all if i["source"] != "research_agent"]
        # Drafted (awaiting approval), parse drafts JSON
        drafted_rows = db.list_by_phase("drafted", limit=200)
        drafted = []
        for row in drafted_rows:
            try:
                d = _json.loads(row["drafts"]) if row["drafts"] else {}
            except (ValueError, TypeError):
                d = {}
            try:
                meta = _json.loads(row["meta"]) if row["meta"] else {}
            except (ValueError, TypeError):
                meta = {}
            drafted.append({"row": row, "drafts": d, "meta": meta})
        # Next scheduled slot summary
        return templates.TemplateResponse(
            request, "today.html",
            _ctx({
                "page": "today",
                "nav_counts": nav,
                "ai_fresh": ai_fresh,
                "my_fresh": my_fresh,
                "drafted": drafted,
                "approved_count": nav["approved"],
                "next_slot": _next_scheduled_slot(),
                "post_times": config.POST_TIMES,
                "timezone": config.TIMEZONE,
                "_t": _truncate,
                "err": err,
            }),
        )

    # Old overview URL redirects to /today
    @app.get("/overview")
    async def overview_redirect(request: Request):
        return RedirectResponse(url="/today", status_code=303)

    # /ideation deprecated — everything happens on /today now
    @app.get("/ideation")
    async def ideation_redirect(request: Request):
        return RedirectResponse(url="/today", status_code=303)

    @app.post("/ideation/add")
    async def ideation_add(request: Request, text: str = Form(...)):
        if r := _require_auth(request):
            return r
        text = (text or "").strip()
        if text:
            db.add_idea(text, source="web", phase="ideation")
        return RedirectResponse(url="/today", status_code=303)

    @app.post("/ideation/{idea_id}/draft")
    async def ideation_draft(request: Request, idea_id: int):
        """Generate text variants AND a branded image in one click.

        Headline is auto-extracted (uses ideator's hook from meta if present,
        else extracts the punchy line from the LinkedIn variant). The user
        reviews the result on /today and just approves — zero typing required.
        """
        if r := _require_auth(request):
            return r
        import json as _json
        idea = db.get_idea(idea_id)
        if not idea:
            return RedirectResponse(url="/today", status_code=303)

        from ..content import generator, image_gen
        loop = asyncio.get_running_loop()
        post_id_hint = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + f"_idea{idea_id}"

        # 1. Generate text variants
        try:
            variants = await loop.run_in_executor(
                None, lambda: generator.generate(idea["text"], post_id_hint=post_id_hint)
            )
            drafts = {v.platform: v.text for v in variants}
        except Exception:
            log.exception("text draft failed for idea %d", idea_id)
            return RedirectResponse(url="/today", status_code=303)

        # 2. Auto-extract headline + generate the branded image
        try:
            meta = _json.loads(idea["meta"]) if idea["meta"] else {}
        except (ValueError, TypeError):
            meta = {}
        try:
            overline, headline, subline = image_gen.auto_headline(
                idea_text=idea["text"],
                linkedin_text=drafts.get("linkedin", ""),
                meta=meta,
            )
            img = await loop.run_in_executor(
                None,
                lambda: image_gen.generate_post_image(
                    headline=headline, subline=subline, overline=overline,
                    topic_hint=idea["text"][:200],
                ),
            )
            # Build public URL the schedulers / Postsyncer will use
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
            drafts["image_url"] = f"{scheme}://{host}/images/{img.filename}"
        except Exception:
            log.exception("image gen failed for idea %d — continuing without image", idea_id)
            drafts["image_url"] = ""

        db.save_drafts(idea_id, drafts)
        return RedirectResponse(url="/today#draft-" + str(idea_id), status_code=303)

    @app.post("/ideation/{idea_id}/skip")
    async def ideation_skip(request: Request, idea_id: int):
        if r := _require_auth(request):
            return r
        db.skip_idea(idea_id)
        return RedirectResponse(url="/today", status_code=303)

    # /publisher deprecated — folded into /today
    @app.get("/publisher")
    async def publisher_redirect(request: Request):
        return RedirectResponse(url="/today", status_code=303)

    @app.post("/publisher/{idea_id}/approve")
    async def publisher_approve(
        request: Request,
        idea_id: int,
        linkedin_text: str = Form(""),
        x_text: str = Form(""),
        threads_text: str = Form(""),
        image_url: str = Form(""),
    ):
        """Save edits → immediately schedule each platform in Postsyncer for
        the next available slot. Postsyncer becomes the system of record;
        Vishal sees the post in Postsyncer's calendar right away.

        Phase transitions:
          drafted → approved (transient: edits saved, about to schedule)
                  → scheduled (success on >=1 platform)
                  → approved   (all platforms failed — cron will retry)
        """
        if r := _require_auth(request):
            return r
        import json as _json
        from ..publishers import postsyncer

        # ── 1. Save edits + set phase to 'approved' transiently ──────
        drafts = {
            "linkedin": linkedin_text.strip(),
            "x": x_text.strip(),
            "threads": threads_text.strip(),
            "image_url": image_url.strip(),
        }
        with db.get_conn() as c:
            c.execute(
                "UPDATE ideas SET drafts=?, phase='approved', approved_at=? WHERE id=?",
                (_json.dumps(drafts), datetime.utcnow().isoformat(), idea_id),
            )

        # ── 2. Compute this idea's slot index ───────────────────────
        # Count other ideas already approved/scheduled (waiting to publish).
        # The new one's slot = next slot AFTER all of those.
        tz = ZoneInfo(config.TIMEZONE)
        now = datetime.now(tz)
        others = [
            i for i in (db.list_by_phase("approved") + db.list_by_phase("scheduled"))
            if i["id"] != idea_id
        ]
        slot_index = len(others)

        # Generate enough upcoming slots
        slots: list[datetime] = []
        day_cursor = now.date()
        for _ in range(60):  # up to 60 days
            for slot_str in config.POST_TIMES:
                hh, mm = slot_str.split(":")
                slot_dt = datetime(
                    day_cursor.year, day_cursor.month, day_cursor.day,
                    int(hh), int(mm), tzinfo=tz,
                )
                if slot_dt > now:
                    slots.append(slot_dt)
            if len(slots) > slot_index:
                break
            day_cursor = day_cursor + timedelta(days=1)

        if not slots or len(slots) <= slot_index:
            log.error("Could not compute upcoming slot for idea #%d", idea_id)
            return RedirectResponse(url="/today", status_code=303)

        my_slot = slots[slot_index]
        schedule_for = {
            "date": my_slot.strftime("%Y-%m-%d"),
            "time": my_slot.strftime("%H:%M"),
            "timezone": config.TIMEZONE,
        }
        schedule_iso = f"{schedule_for['date']}T{schedule_for['time']}:00"
        log.info(
            "Approving idea #%d → slot %d → %s %s %s",
            idea_id, slot_index, schedule_for["date"], schedule_for["time"], config.TIMEZONE,
        )

        # ── 3. Schedule each platform in Postsyncer ──────────────────
        loop = asyncio.get_running_loop()
        any_success = False
        media = [image_url.strip()] if image_url.strip() else None
        for platform_key, text in (
            ("linkedin", linkedin_text.strip()),
            ("x", x_text.strip()),
            ("threads", threads_text.strip()),
        ):
            if not text:
                continue
            try:
                resp = await loop.run_in_executor(
                    None, lambda p=platform_key, t=text: postsyncer.publish(
                        platform=p, text=t,
                        schedule_for=schedule_for, media_urls=media,
                    ),
                )
                ps_id = str(resp.get("data", {}).get("id") or resp.get("id") or "")
                db.log_post(
                    idea_id=idea_id, platform=platform_key, text=text,
                    cta_url="", status="scheduled",
                    postsyncer_post_id=ps_id, scheduled_for=schedule_iso,
                )
                any_success = True
                log.info("Scheduled %s for idea #%d → postsyncer_id=%s", platform_key, idea_id, ps_id)
            except Exception as e:
                log.exception("Postsyncer schedule failed: idea=%d platform=%s", idea_id, platform_key)
                db.log_post(
                    idea_id=idea_id, platform=platform_key, text=text,
                    cta_url="", status="failed", scheduled_for=schedule_iso,
                    error=str(e)[:500],
                )

        # ── 4. Phase update ──────────────────────────────────────────
        if any_success:
            db.set_phase(idea_id, "scheduled")
        # else: leave at 'approved' — the cron will retry at the next slot.

        return RedirectResponse(url="/today", status_code=303)

    @app.post("/publisher/{idea_id}/back")
    async def publisher_back(request: Request, idea_id: int):
        if r := _require_auth(request):
            return r
        db.set_phase(idea_id, "ideation")
        return RedirectResponse(url="/today", status_code=303)

    @app.post("/publisher/{idea_id}/skip")
    async def publisher_skip(request: Request, idea_id: int):
        if r := _require_auth(request):
            return r
        db.skip_idea(idea_id)
        return RedirectResponse(url="/today", status_code=303)

    # /posts kept as redirect to /launch for backwards compat
    @app.get("/posts")
    async def posts_redirect(request: Request, year: int | None = None, month: int | None = None, view: str = "calendar"):
        qs = []
        if year: qs.append(f"year={year}")
        if month: qs.append(f"month={month}")
        if view and view != "calendar": qs.append(f"view={view}")
        target = "/launch" + ("?" + "&".join(qs) if qs else "")
        return RedirectResponse(url=target, status_code=303)

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

    @app.get("/launch", response_class=HTMLResponse)
    async def launch_get(
        request: Request,
        year: int | None = None,
        month: int | None = None,
        view: str = "calendar",
    ):
        if r := _require_auth(request):
            return r

        tz = ZoneInfo(config.TIMEZONE)
        now_local = datetime.now(tz)
        posts = db.recent_posts(limit=1000)

        # List view: simple chronological feed
        if view == "list":
            return templates.TemplateResponse(
                request,
                "posts.html",
                _ctx({"page": "launch", "view": "list", "posts": posts, "_t": _truncate}),
            )

        # Calendar view
        y = year or now_local.year
        m = month or now_local.month

        buckets: dict[tuple[int, int, int], list] = defaultdict(list)
        # 1. Already-published / scheduled posts (one entry per platform)
        for p in posts:
            d = _post_calendar_date(p, tz)
            buckets[(d.year, d.month, d.day)].append({
                "time": _time_label(d),
                "sort": d,
                "platform": p["platform"],
                "snippet": _truncate(p["text"], 42),
                "status": p["status"],
            })

        # 2. approved-but-Postsyncer-failed ideas → placeholder slots
        # (Successfully scheduled ones already appear via posts table above.)
        approved = list(db.list_by_phase("approved", limit=200))
        # Oldest-approved first (matches scheduler pick order)
        approved.sort(key=lambda r: r["approved_at"] or r["created_at"])
        upcoming_slots: list[datetime] = []
        day_cursor = now_local.date()
        max_days = 60
        while len(upcoming_slots) < len(approved) and max_days > 0:
            for slot_str in config.POST_TIMES:
                hh, mm = slot_str.split(":")
                slot_dt = datetime(
                    day_cursor.year, day_cursor.month, day_cursor.day,
                    int(hh), int(mm), tzinfo=tz,
                )
                if slot_dt > now_local:
                    upcoming_slots.append(slot_dt)
            day_cursor = day_cursor + timedelta(days=1)
            max_days -= 1
        for idea, slot in zip(approved, upcoming_slots):
            # Show the LinkedIn variant snippet if drafts exist; else raw idea text.
            preview = idea["text"]
            if idea["drafts"]:
                try:
                    import json as _json
                    d = _json.loads(idea["drafts"]) or {}
                    preview = d.get("linkedin") or d.get("x") or d.get("threads") or preview
                except (ValueError, TypeError):
                    pass
            buckets[(slot.year, slot.month, slot.day)].append({
                "time": _time_label(slot),
                "sort": slot,
                "platform": "scheduled",   # template renders a calendar icon
                "snippet": _truncate(preview, 42),
                "status": "approved",
            })

        cal_obj = cal_mod.Calendar(firstweekday=6)  # Sunday-first
        weeks = []
        for week in cal_obj.monthdayscalendar(y, m):
            cells = []
            for day in week:
                if day == 0:
                    cells.append({"day": None, "posts": [], "is_today": False})
                else:
                    key = (y, m, day)
                    day_posts = sorted(buckets.get(key, []), key=lambda x: x["sort"])
                    cells.append({
                        "day": day,
                        "is_today": (
                            y == now_local.year and m == now_local.month and day == now_local.day
                        ),
                        "posts": day_posts,
                    })
            weeks.append(cells)

        prev_y, prev_m = (y - 1, 12) if m == 1 else (y, m - 1)
        next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)

        return templates.TemplateResponse(
            request,
            "posts.html",
            _ctx({
                "page": "launch",
                "view": "calendar",
                "weeks": weeks,
                "day_headers": DAY_HEADERS,
                "month_label": datetime(y, m, 1).strftime("%B %Y"),
                "prev_y": prev_y, "prev_m": prev_m,
                "next_y": next_y, "next_m": next_m,
                "timezone": config.TIMEZONE,
            }),
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

    # /compose deprecated — replaced by /today which auto-drafts text + image
    @app.get("/compose")
    async def compose_redirect(request: Request):
        return RedirectResponse(url="/today", status_code=303)

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
        threads_text: str = Form(""),
        image_url: str = Form(""),
        mode: str = Form("schedule"),  # 'now' or 'schedule'
    ):
        if r := _require_auth(request):
            return r

        from ..publishers import postsyncer
        idea_id = db.add_idea(idea, source="web") if idea.strip() else None

        # Compute next slot for scheduling
        schedule_for = None
        schedule_iso = None
        if mode == "schedule":
            schedule_for = _next_slot_payload()
            schedule_iso = f"{schedule_for['date']}T{schedule_for['time']}:00"

        results = []
        for platform_key, text in (
            ("linkedin", linkedin_text),
            ("x", x_text),
            ("threads", threads_text),
        ):
            text = (text or "").strip()
            if not text:
                continue
            try:
                resp = postsyncer.publish(
                    platform=platform_key, text=text, schedule_for=schedule_for,
                    media_urls=[image_url] if image_url.strip() else None,
                )
                ps_id = str(resp.get("data", {}).get("id") or resp.get("id") or "")
                db.log_post(
                    idea_id=idea_id,
                    platform=platform_key,
                    text=text,
                    cta_url="",  # already inline if present
                    status="scheduled" if schedule_for else "published",
                    postsyncer_post_id=ps_id,
                    scheduled_for=schedule_iso,
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

    # ─── Image generation + serving ───────────────────────

    @app.get("/images/{filename}")
    async def get_image(filename: str):
        """Serve a generated post image (Postsyncer fetches via this URL)."""
        from fastapi.responses import FileResponse
        from ..content import image_gen
        # Prevent path traversal
        if "/" in filename or ".." in filename:
            return RedirectResponse(url="/", status_code=303)
        path = image_gen.OUTPUT_DIR / filename
        if not path.exists():
            return RedirectResponse(url="/", status_code=303)
        return FileResponse(str(path), media_type="image/png")

    @app.post("/compose/image", response_class=HTMLResponse)
    async def compose_image(
        request: Request,
        headline: str = Form(...),
        subline: str = Form(""),
        overline: str = Form("CASE STUDY"),
        topic_hint: str = Form(""),
    ):
        if r := _require_auth(request):
            return r
        from ..content import image_gen
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: image_gen.generate_post_image(
                    headline=headline.strip(),
                    subline=subline.strip() or None,
                    overline=overline.strip() or "CASE STUDY",
                    topic_hint=topic_hint.strip(),
                ),
            )
        except Exception as e:
            log.exception("image gen failed")
            return HTMLResponse(
                f'<div class="banner warn">Image gen failed: {str(e)[:300]}</div>',
                status_code=500,
            )
        # Build public URL the browser (and Postsyncer) can fetch
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        public_url = f"{scheme}://{host}/images/{result.filename}"
        # Return an HTML fragment for HTMX to swap in
        return HTMLResponse(
            f'<div class="image-preview">'
            f'  <img src="/images/{result.filename}" alt="post image">'
            f'  <input type="hidden" name="image_url" value="{public_url}">'
            f'  <div class="muted small">Image generated. Click Publish to attach it.</div>'
            f'</div>'
        )

    # ─── Backfill: schedule already-approved ideas in Postsyncer ──

    @app.post("/actions/sync_to_postsyncer")
    async def sync_to_postsyncer(request: Request):
        """Schedule every phase='approved' idea (no posts row yet) in
        Postsyncer. Use this once after deploy to push pre-existing approvals
        into Postsyncer's calendar."""
        if r := _require_auth(request):
            return r
        import json as _json
        from ..publishers import postsyncer

        tz = ZoneInfo(config.TIMEZONE)
        now = datetime.now(tz)
        pending = sorted(
            db.list_by_phase("approved", limit=100),
            key=lambda r: r["approved_at"] or r["created_at"],
        )

        # Generate slot calendar once
        slots: list[datetime] = []
        day_cursor = now.date()
        for _ in range(60):
            for slot_str in config.POST_TIMES:
                hh, mm = slot_str.split(":")
                slot_dt = datetime(
                    day_cursor.year, day_cursor.month, day_cursor.day,
                    int(hh), int(mm), tzinfo=tz,
                )
                if slot_dt > now:
                    slots.append(slot_dt)
            if len(slots) >= len(pending):
                break
            day_cursor = day_cursor + timedelta(days=1)

        loop = asyncio.get_running_loop()
        results = []
        for idx, idea in enumerate(pending):
            if idx >= len(slots):
                break
            slot = slots[idx]
            schedule_for = {
                "date": slot.strftime("%Y-%m-%d"),
                "time": slot.strftime("%H:%M"),
                "timezone": config.TIMEZONE,
            }
            schedule_iso = f"{schedule_for['date']}T{schedule_for['time']}:00"
            try:
                drafts_d = _json.loads(idea["drafts"]) if idea["drafts"] else {}
            except (ValueError, TypeError):
                drafts_d = {}
            media = [drafts_d.get("image_url")] if drafts_d.get("image_url") else None
            any_ok = False
            for platform_key in ("linkedin", "x", "threads"):
                text = (drafts_d.get(platform_key) or "").strip()
                if not text:
                    continue
                try:
                    resp = await loop.run_in_executor(
                        None,
                        lambda p=platform_key, t=text: postsyncer.publish(
                            platform=p, text=t,
                            schedule_for=schedule_for, media_urls=media,
                        ),
                    )
                    ps_id = str(resp.get("data", {}).get("id") or resp.get("id") or "")
                    db.log_post(
                        idea_id=idea["id"], platform=platform_key, text=text,
                        cta_url="", status="scheduled",
                        postsyncer_post_id=ps_id, scheduled_for=schedule_iso,
                    )
                    any_ok = True
                except Exception as e:
                    log.exception("backfill schedule failed: idea=%d platform=%s", idea["id"], platform_key)
                    db.log_post(
                        idea_id=idea["id"], platform=platform_key, text=text,
                        cta_url="", status="failed", scheduled_for=schedule_iso,
                        error=str(e)[:500],
                    )
            if any_ok:
                db.set_phase(idea["id"], "scheduled")
                results.append((idea["id"], "scheduled", slot.isoformat()))
            else:
                results.append((idea["id"], "failed", slot.isoformat()))
        log.info("Backfill scheduled %d ideas: %s", len(results), results)
        return RedirectResponse(url="/launch", status_code=303)

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

    # ─── Research ─────────────────────────────────────────

    @app.get("/research", response_class=HTMLResponse)
    async def research_get(request: Request, err: str = ""):
        if r := _require_auth(request):
            return r
        import json as _json
        brief = db.latest_research_brief()
        # Agent-generated queued ideas, with parsed meta for display
        agent_ideas = []
        for row in db.list_queued(limit=200):
            if row["source"] != "research_agent":
                continue
            meta_obj = {}
            try:
                meta_obj = _json.loads(row["meta"]) if row["meta"] else {}
            except (ValueError, TypeError):
                pass
            agent_ideas.append({
                "id": row["id"],
                "text": row["text"],
                "score": row["score"],
                "meta_obj": meta_obj,
            })
        agent_ideas.sort(key=lambda i: (i["score"] or 0), reverse=True)
        return templates.TemplateResponse(
            request,
            "research.html",
            {
                "page": "research",
                "brief": brief,
                "agent_ideas": agent_ideas,
                "research_time": f"daily at {config.IDEATION_TIME}",
                "timezone": config.TIMEZONE,
                "err": err,
            },
        )

    @app.post("/research/run")
    async def research_run(request: Request):
        if r := _require_auth(request):
            return r
        from ..agents import orchestrator
        import traceback as _tb, urllib.parse as _up
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: orchestrator.run_research_pipeline(refresh_signal=True)
            )
        except Exception:
            log.exception("research run failed")
            tb = _tb.format_exc()[-1800:]
            return RedirectResponse(
                url=f"/today?err={_up.quote(tb)}", status_code=303,
            )
        return RedirectResponse(url="/today", status_code=303)

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
            "Threads acct id": str(config.POSTSYNCER_THREADS_ACCOUNT_ID),
            "Telegram allowed user": str(config.TELEGRAM_ALLOWED_USER_ID),
            "Apify enabled": "yes" if config.APIFY_ENABLED else "no",
            "CTA URL": config.CTA_URL,
            "Post times": ", ".join(config.POST_TIMES) + f" {config.TIMEZONE}",
            "Daily ideation": (
                f"every day at {config.IDEATION_TIME} {config.TIMEZONE} · "
                f"{config.IDEATION_COUNT} ideas"
            ),
            "Telegram nudge after ideation": "yes" if config.NOTIFY_TELEGRAM_AFTER_IDEATION else "no",
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
