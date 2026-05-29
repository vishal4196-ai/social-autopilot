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
    """One end-to-end run: idea → generate → publish. Called by APScheduler."""
    log.info("─── post cycle start ───")

    # 1. Pick idea source
    idea_row = db.next_queued_idea()
    if idea_row:
        idea_text = idea_row["text"]
        idea_id = idea_row["id"]
        log.info("Using queued idea #%d", idea_id)
    else:
        log.info("Queue empty — using viral-inspired fallback")
        idea_text = _fallback_idea_text()
        idea_id = None

    # 2. Generate variants
    post_id_hint = f"{datetime.utcnow().strftime('%Y%m%d_%H%M')}"
    try:
        variants = generator.generate(idea_text, post_id_hint=post_id_hint)
    except Exception as e:
        log.exception("Generation failed: %s", e)
        if idea_id:
            db.log_post(
                idea_id=idea_id,
                platform="linkedin",
                text=idea_text,
                cta_url=config.CTA_URL,
                status="failed",
                error=f"generation: {e}",
            )
        return

    if not variants:
        log.warning("Generator returned 0 variants")
        return

    # 3. Publish each variant (LinkedIn + X)
    any_success = False
    for v in variants:
        try:
            resp = postsyncer.publish(platform=v.platform, text=v.text)
            ps_id = str(resp.get("data", {}).get("id") or resp.get("id") or "")
            db.log_post(
                idea_id=idea_id,
                platform=v.platform,
                text=v.text,
                cta_url=v.cta_url,
                status="scheduled",
                postsyncer_post_id=ps_id,
            )
            log.info("Published to %s (postsyncer_id=%s)", v.platform, ps_id)
            any_success = True
        except Exception as e:
            log.exception("Publish failed for %s: %s", v.platform, e)
            db.log_post(
                idea_id=idea_id,
                platform=v.platform,
                text=v.text,
                cta_url=v.cta_url,
                status="failed",
                error=str(e)[:500],
            )

    # 4. Mark idea consumed (only if at least one platform succeeded)
    if idea_id and any_success:
        db.mark_idea_used(idea_id)

    log.info("─── post cycle done ───")


def run_viral_refresh() -> None:
    """Run viral discovery once a day. Cheap to skip if disabled."""
    try:
        result = viral_discovery.refresh()
        log.info("Viral refresh: %s", result)
    except Exception as e:
        log.exception("Viral refresh failed: %s", e)


def run_research() -> None:
    """Daily research pipeline: scout → ideate → queue killer ideas."""
    try:
        from .agents import orchestrator
        # Signal refresh happens inside the scheduled viral_refresh; don't double it.
        summary = orchestrator.run_research_pipeline(refresh_signal=False)
        log.info("Research pipeline: %s", summary)
    except Exception as e:
        log.exception("Research pipeline failed: %s", e)


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

    # Research pipeline once a day (scout → ideate → queue ideas)
    if config.RESEARCH_TIME:
        r_hh, r_mm = config.RESEARCH_TIME.split(":")
        sched.add_job(
            run_research,
            CronTrigger(hour=int(r_hh), minute=int(r_mm), timezone=tz),
            id="research_pipeline",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        log.info("Scheduled research pipeline at %s %s", config.RESEARCH_TIME, config.TIMEZONE)

    return sched
