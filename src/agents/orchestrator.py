"""Orchestrator — runs the autonomous research → ideation pipeline.

Wired into the scheduler (daily) and exposed to Telegram/web as "research now".
Optionally refreshes social signal first so research has fresh material.
"""
from __future__ import annotations

import logging

from .. import config
from . import ideator, research

log = logging.getLogger(__name__)


def run_research_pipeline(
    refresh_signal: bool = True,
    queue_count: int | None = None,
) -> dict:
    """Full loop: (refresh) → research → ideate → queue top ideas.

    queue_count overrides config.IDEAS_TO_QUEUE for this run (used by the
    weekly cadence to drop a bigger batch).

    Returns a summary dict for logging / UI / Telegram replies.
    """
    log.info("─── research pipeline start ───")

    refresh_result = None
    if refresh_signal and config.APIFY_ENABLED:
        try:
            from ..content import viral_discovery
            refresh_result = viral_discovery.refresh()
        except Exception as e:
            log.warning("signal refresh failed (continuing): %s", e)

    brief = research.run_research()
    # Temporarily override the queue count for this run, then restore.
    original = config.IDEAS_TO_QUEUE
    if queue_count is not None:
        config.IDEAS_TO_QUEUE = queue_count
    try:
        ideas = ideator.run_ideation(brief)
    finally:
        config.IDEAS_TO_QUEUE = original

    effective_count = queue_count if queue_count is not None else original
    top = ideas[:effective_count]
    summary = {
        "brief_summary": brief.get("summary", ""),
        "themes": brief.get("themes", [])[:5],
        "ideas_generated": len(ideas),
        "ideas_queued": len(top),
        "top_ideas": [
            {"score": i.get("score"), "hook": i.get("hook", "")[:120]} for i in top
        ],
        "signal_refresh": refresh_result,
    }
    log.info("─── research pipeline done: %s ideas, %s queued ───",
             summary["ideas_generated"], summary["ideas_queued"])
    return summary
