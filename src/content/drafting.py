"""Shared draft pipeline: idea → text variants + branded image → 'drafted' phase.

Used by both the web route (/ideation/{id}/draft) and the chat agent's
draft_idea tool, so drafting behaves identically from every door.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .. import config, db
from . import generator, image_gen

log = logging.getLogger(__name__)


def draft_idea(idea_id: int, base_url: str | None = None) -> dict:
    """Generate LinkedIn + X + Threads variants and a branded image for an
    idea, save them, and move the idea to phase='drafted'.

    base_url: public origin used to build the image URL Postsyncer will
    fetch. Falls back to config.PUBLIC_BASE_URL.

    Returns the drafts dict. Raises ValueError if the idea doesn't exist,
    or whatever generator.generate raises on failure.
    """
    idea = db.get_idea(idea_id)
    if not idea:
        raise ValueError(f"Idea #{idea_id} not found")

    post_id_hint = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + f"_idea{idea_id}"
    variants = generator.generate(idea["text"], post_id_hint=post_id_hint)
    drafts = {v.platform: v.text for v in variants}

    try:
        meta = json.loads(idea["meta"]) if idea["meta"] else {}
    except (ValueError, TypeError):
        meta = {}

    try:
        overline, headline, subline = image_gen.auto_headline(
            idea_text=idea["text"],
            linkedin_text=drafts.get("linkedin", ""),
            meta=meta,
        )
        img = image_gen.generate_post_image(
            headline=headline, subline=subline, overline=overline,
            topic_hint=idea["text"][:200],
        )
        base = (base_url or config.PUBLIC_BASE_URL).rstrip("/")
        drafts["image_url"] = f"{base}/images/{img.filename}"
    except Exception:
        log.exception("image gen failed for idea %d — continuing without image", idea_id)
        drafts["image_url"] = ""

    db.save_drafts(idea_id, drafts)
    return drafts
