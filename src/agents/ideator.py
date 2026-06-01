"""Ideator Agent (Strategist).

Takes a research brief and produces concrete, scored post ideas tailored to
Vishal's niche. Top-scoring ideas get queued (source='research_agent') with
their rationale stored in meta, so the web UI can show WHY each idea is good.
"""
from __future__ import annotations

import json
import logging
import re

import anthropic

from .. import config, db

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _system_prompt() -> str:
    b = config.BRAND_CONFIG
    pains = "\n".join(f"- {p}" for p in b["audience"]["pain_points"])
    offers = "\n".join(f"- {o}" for o in b["brand"]["offers"])
    positioning = b["brand"].get("positioning", b["brand"].get("one_liner", ""))
    return f"""You are the CONTENT STRATEGIST for {b['brand']['name']} (owner: {b['brand']['owner']}).

THE BRAND SPINE (every idea must live here)
{positioning}

These ideas are for Vishal's personal LinkedIn + X. Every post needs to
implicitly say "this person ships AI automation systems for service
businesses." Generic agency advice or generic AI takes don't qualify — those
could come from anyone. We're building a profile that positions Vishal as
THE go-to operator for AI automation in service businesses.

AUDIENCE: {b['audience']['primary']}
Pains:
{pains}
OFFERS (the wedge — angles live in this orbit, but don't recite them):
{offers}

RULES FOR EACH IDEA
- CONCRETE: ready-to-write, with a specific hook. Not "post about AI
  automation" but "Break down the exact 3-step n8n flow that replaced a
  client's $3k/mo VA, with the hour-by-hour breakdown."
- ANCHORED to UpliftAI's lane (voice agents, lead-routing, GHL/n8n
  automations, AI for service businesses). A generic "5 ChatGPT prompts"
  list is OFF-brand — reject it.
- Tied to a specific audience pain.
- Score 1-10 for engagement potential (honest; reserve 9-10 for bangers).
- Format: story | framework | hot_take | case_study | question | bts | lesson
- platform_fit: "linkedin" | "x" | "both"
- Don't repeat recent ideas/posts (shown below).

Return JSON ONLY:
{{
  "ideas": [
    {{
      "hook": "the actual opening line or sharp angle",
      "format": "one of the formats above",
      "pain_point": "which audience pain this hits",
      "why": "one sentence on why this earns engagement AND reinforces the UpliftAI positioning",
      "score": 8.5,
      "platform_fit": "both"
    }}
  ]
}}
"""


def _brief_to_text(brief: dict) -> str:
    def lst(key, label):
        items = brief.get(key, []) or []
        return f"{label}:\n" + "\n".join(f"- {i}" for i in items) if items else ""
    blocks = [
        f"SUMMARY: {brief.get('summary', '')}",
        lst("themes", "HOT THEMES"),
        lst("hooks_working", "HOOKS THAT ARE WORKING"),
        lst("content_gaps", "CONTENT GAPS TO OWN"),
        lst("audience_questions", "AUDIENCE QUESTIONS"),
    ]
    return "\n\n".join(b for b in blocks if b)


def _extract_json(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?", "", s).rstrip("`").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in ideator output: {raw[:200]}")
    return json.loads(s[start : end + 1])


def _idea_to_prompt(idea: dict) -> str:
    """Turn a structured idea into the rich idea_text the Writer agent reads."""
    return (
        f"{idea.get('hook', '').strip()}\n\n"
        f"Format: {idea.get('format', 'story')}\n"
        f"Angle: hits the pain '{idea.get('pain_point', '')}'. {idea.get('why', '')}\n"
        f"Best fit: {idea.get('platform_fit', 'both')}"
    )


def run_ideation(brief: dict) -> list[dict]:
    """Generate ideas from a brief, queue the top N. Returns all ideas (scored)."""
    recent = db.recent_idea_texts(limit=12)
    recent_block = (
        "\nRECENT IDEAS/POSTS (do not repeat these):\n"
        + "\n".join(f"- {t[:160]}" for t in recent)
        if recent else ""
    )

    user = (
        "RESEARCH BRIEF:\n"
        f"{_brief_to_text(brief)}\n"
        f"{recent_block}\n\n"
        f"Generate {config.IDEAS_PER_RUN} scored post ideas. Return JSON only."
    )

    resp = _client.messages.create(
        model=config.RESEARCH_MODEL,
        max_tokens=3000,
        system=_system_prompt(),
        messages=[{"role": "user", "content": user}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    ideas = _extract_json(raw).get("ideas", [])

    # Sort by score desc, queue the top N.
    ideas_sorted = sorted(ideas, key=lambda i: float(i.get("score", 0)), reverse=True)
    queued = 0
    for idea in ideas_sorted[: config.IDEAS_TO_QUEUE]:
        db.add_idea(
            text=_idea_to_prompt(idea),
            source="research_agent",
            score=float(idea.get("score", 0)),
            meta={
                "hook": idea.get("hook", ""),
                "format": idea.get("format", ""),
                "pain_point": idea.get("pain_point", ""),
                "why": idea.get("why", ""),
                "platform_fit": idea.get("platform_fit", ""),
            },
        )
        queued += 1

    log.info("Ideator produced %d ideas, queued top %d", len(ideas), queued)
    return ideas_sorted
