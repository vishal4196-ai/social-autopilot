"""The brain. Wakes up on schedule, picks an idea (or pulls viral inspiration),
generates LinkedIn + X variants via Claude, and publishes via Postsyncer.
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from . import config, db
from .content import generator, viral_discovery
from .publishers import postsyncer

log = logging.getLogger(__name__)


def _fallback_idea_text() -> str:
    """Used when the queue is empty and Apify is disabled or returned nothing."""
    b = config.BRAND_CONFIG
    offer = b["brand"]["offers"][0]
    pain = b["audience"]["pain_points"][0]
    return (
        f"Write a post explaining how {offer.lower()} eliminates this pain: {pain}. "
        f"Use a real-sounding mini case study (you can invent numbers, but stay grounded — "
        f"e.g., 'a coaching client closed 14 hrs/wk back')."
    )


def run_post_cycle() -> None:
    """One end-to-end run: pick approved idea → publish its saved drafts.

    In the new phase model, ideas are only picked when phase='approved' —
    meaning the user has explicitly OK'd them. We publish exactly what they
    approved (no regeneration), to preserve their edits.

    If the approved idea has no saved drafts (legacy data), fall back to
    generating on the fly.
    """
    import json as _json
    log.info("─── post cycle start ───")

    idea_row = db.next_queued_idea()
    if not idea_row:
        log.info("No approved ideas — skipping cycle. Approve some in /publisher.")
        return

    idea_id = idea_row["id"]
    idea_text = idea_row["text"]
    log.info("Picking approved idea #%d", idea_id)

    # Use saved drafts if present (preserves user edits); else regenerate.
    variants_payload: list[tuple[str, str, str]] = []  # (platform, text, cta_url)
    saved_drafts: dict = {}
    if idea_row["drafts"]:
        try:
            saved_drafts = _json.loads(idea_row["drafts"]) or {}
        except (ValueError, TypeError):
            saved_drafts = {}

    image_url = (saved_drafts.get("image_url") or "").strip()

    if saved_drafts.get("linkedin") or saved_drafts.get("x") or saved_drafts.get("threads"):
        for platform_key in ("linkedin", "x", "threads"):
            text = (saved_drafts.get(platform_key) or "").strip()
            if text:
                variants_payload.append((platform_key, text, ""))
        log.info("Using user-approved drafts (no regeneration)")
    else:
        log.info("No saved drafts — regenerating from idea text")
        post_id_hint = datetime.utcnow().strftime("%Y%m%d_%H%M")
        try:
            generated = generator.generate(idea_text, post_id_hint=post_id_hint)
        except Exception as e:
            log.exception("Generation failed: %s", e)
            return
        variants_payload = [(v.platform, v.text, v.cta_url) for v in generated]

    if not variants_payload:
        log.warning("No variants to publish")
        return

    any_success = False
    for platform_key, text, cta_url in variants_payload:
        try:
            resp = postsyncer.publish(
                platform=platform_key, text=text,
                media_urls=[image_url] if image_url else None,
            )
            ps_id = str(resp.get("data", {}).get("id") or resp.get("id") or "")
            db.log_post(
                idea_id=idea_id, platform=platform_key, text=text,
                cta_url=cta_url, status="scheduled", postsyncer_post_id=ps_id,
            )
            log.info("Published to %s (postsyncer_id=%s)", platform_key, ps_id)
            any_success = True
        except Exception as e:
            log.exception("Publish failed for %s: %s", platform_key, e)
            db.log_post(
                idea_id=idea_id, platform=platform_key, text=text,
                cta_url=cta_url, status="failed", error=str(e)[:500],
            )

    if any_success:
        db.mark_idea_used(idea_id)

    log.info("─── post cycle done ───")


def run_viral_refresh() -> None:
    """Run viral discovery once a day. Cheap to skip if disabled."""
    try:
        result = viral_discovery.refresh()
        log.info("Viral refresh: %s", result)
    except Exception as e:
        log.exception("Viral refresh failed: %s", e)


def run_weekly_ideation() -> None:
    """Sunday-morning batch: scout the niche + drop N fresh ideas into Ideation.

    Sends a Telegram nudge when done so Vishal knows the brief is waiting.
    """
    import requests as _requests

    try:
        from .agents import orchestrator
        summary = orchestrator.run_research_pipeline(
            refresh_signal=False,  # daily viral_refresh handles the signal
            queue_count=config.WEEKLY_IDEATION_COUNT,
        )
        log.info("Weekly ideation: %s ideas queued", summary["ideas_queued"])
    except Exception as e:
        log.exception("Weekly ideation pipeline failed: %s", e)
        return

    # Telegram nudge so you know the brief is ready.
    if not config.NOTIFY_TELEGRAM_AFTER_IDEATION:
        return
    try:
        n = summary["ideas_queued"]
        top = summary.get("top_ideas") or []
        teaser = "\n".join(
            f"  [{i.get('score')}] {i.get('hook','')[:100]}" for i in top[:3]
        )
        text = (
            f"🗓 Sunday Brief\n\n"
            f"{n} fresh ideas in Ideation. Top three:\n\n"
            f"{teaser}\n\n"
            f"Open the app to draft + approve."
        )
        _requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_ALLOWED_USER_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning("Telegram nudge failed: %s", e)


def build_scheduler() -> AsyncIOScheduler:
    tz = ZoneInfo(config.TIMEZONE)
    sched = AsyncIOScheduler(timezone=tz)

    # 3 post cycles per day
    for slot in config.POST_TIMES:
        hh, mm = slot.split(":")
        sched.add_job(
            run_post_cycle,
            CronTrigger(hour=int(hh), minute=int(mm), timezone=tz),
            id=f"post_{slot}",
            replace_existing=True,
            misfire_grace_time=600,
        )
        log.info("Scheduled post cycle at %s %s", slot, config.TIMEZONE)

    # Viral discovery once a day, 30 min before first post slot
    if config.APIFY_ENABLED and config.POST_TIMES:
        first_hh, first_mm = config.POST_TIMES[0].split(":")
        refresh_hh = (int(first_hh) - 1) % 24
        sched.add_job(
            run_viral_refresh,
            CronTrigger(hour=refresh_hh, minute=int(first_mm), timezone=tz),
            id="viral_refresh",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        log.info("Scheduled viral refresh at %02d:%s %s", refresh_hh, first_mm, config.TIMEZONE)

    # Weekly ideation cadence — Sunday morning by default. One bigger drop per week
    # is cleaner than seven small daily drops.
    if config.WEEKLY_IDEATION_DAY and config.WEEKLY_IDEATION_TIME:
        w_hh, w_mm = config.WEEKLY_IDEATION_TIME.split(":")
        sched.add_job(
            run_weekly_ideation,
            CronTrigger(
                day_of_week=config.WEEKLY_IDEATION_DAY,
                hour=int(w_hh), minute=int(w_mm), timezone=tz,
            ),
            id="weekly_ideation",
            replace_existing=True,
            misfire_grace_time=3600 * 6,
        )
        log.info(
            "Scheduled weekly ideation: %s at %s %s (%d ideas/run)",
            config.WEEKLY_IDEATION_DAY, config.WEEKLY_IDEATION_TIME,
            config.TIMEZONE, config.WEEKLY_IDEATION_COUNT,
        )

    return sched
