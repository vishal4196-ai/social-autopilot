"""Research Agent (Scout).

Reads the current social signal (viral posts + tracked creators) and, when
enabled, web-searches what's trending in the niche RIGHT NOW. Synthesises it
all into a tactical research brief that the Ideator turns into post ideas.

Web search uses Anthropic's server-side tool — Anthropic runs the searches
and feeds results back to the model within a single request. If the tool
isn't available on the key/plan, we transparently fall back to DB-only signal.
"""
from __future__ import annotations

import json
import logging
import re

import anthropic

from .. import config, db

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


def _system_prompt() -> str:
    b = config.BRAND_CONFIG
    offers = "\n".join(f"- {o}" for o in b["brand"]["offers"])
    pains = "\n".join(f"- {p}" for p in b["audience"]["pain_points"])
    return f"""You are the RESEARCH AGENT for {b['brand']['name']} (owner: {b['brand']['owner']}).

MISSION
Understand what's working RIGHT NOW in this niche so we can ride the wave.

THE BUSINESS
{b['brand']['one_liner']}

OFFERS
{offers}

AUDIENCE: {b['audience']['primary']}
Their daily pains:
{pains}

WHAT YOU ANALYZE
- Viral posts in the niche (hooks/formats getting engagement)
- Posts from creators we admire
- Current trends & news (use web search if available)

OUTPUT — a tactical brief. Be SPECIFIC, never generic.
Bad:  "AI agents are trending."
Good: "Before/after posts quantifying time saved ('14 hrs/week back') are
       outperforming feature lists ~3:1. Hook with the number, not the tool."

Return JSON ONLY:
{{
  "summary": "2-3 sentences on the state of the niche this week",
  "themes": ["specific hot theme 1", "theme 2", "..."],
  "hooks_working": ["specific hook/format pattern 1", "..."],
  "content_gaps": ["underserved angle Vishal could own 1", "..."],
  "audience_questions": ["real question the audience is asking 1", "..."]
}}
"""


def _format_signal() -> str:
    parts: list[str] = []

    def _block(title: str, rows) -> None:
        if not rows:
            return
        parts.append(f"\n{title}:")
        for r in rows:
            who = r["source_creator"] or r["author"] or "?"
            snippet = (r["text"] or "")[:400].replace("\n", " ")
            parts.append(f"- [@{who}, {r['engagement']} eng] {snippet}")

    _block("VIRAL LINKEDIN POSTS (keyword-scraped)", db.recent_viral("linkedin", limit=6))
    _block("VIRAL X POSTS (keyword-scraped)", db.recent_viral("x", limit=6))
    _block("TRACKED-CREATOR LINKEDIN POSTS", db.recent_creator_posts("linkedin", limit=6))
    _block("TRACKED-CREATOR X POSTS", db.recent_creator_posts("x", limit=6))

    if not parts:
        return "(No social signal in the DB yet — rely on web search + your knowledge of the niche.)"
    return "\n".join(parts)


def _extract_json(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?", "", s).rstrip("`").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in research output: {raw[:200]}")
    return json.loads(s[start : end + 1])


def _call(system: str, user: str, use_web_search: bool) -> str:
    kwargs = dict(
        model=config.RESEARCH_MODEL,
        max_tokens=3000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if use_web_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]
    resp = _client.messages.create(**kwargs)
    # Join all text blocks (web-search responses interleave tool blocks).
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def run_research() -> dict:
    """Produce and persist a research brief. Returns the parsed brief."""
    system = _system_prompt()
    user = (
        "Here is the current social signal pulled from our database:\n"
        f"{_format_signal()}\n\n"
        "Research the current state of this niche"
        + (" (web-search for fresh trends/news this week)" if config.ENABLE_WEB_SEARCH else "")
        + ". Then return the research brief as JSON."
    )

    raw = ""
    if config.ENABLE_WEB_SEARCH:
        try:
            raw = _call(system, user, use_web_search=True)
        except Exception as e:
            log.warning("Research with web search failed (%s) — retrying without", e)
            raw = ""
    if not raw:
        raw = _call(system, user, use_web_search=False)

    parsed = _extract_json(raw)
    db.save_research_brief(parsed)
    log.info(
        "Research brief: %d themes, %d hooks, %d gaps, %d questions",
        len(parsed.get("themes", [])),
        len(parsed.get("hooks_working", [])),
        len(parsed.get("content_gaps", [])),
        len(parsed.get("audience_questions", [])),
    )
    return parsed
