"""Claude-powered post generator.

Builds a system prompt from brand config (cached for ~5 min cost savings),
then asks for two variants per idea: LinkedIn long-form + X short-form.
Returns clean text — CTA URL is appended by the caller with per-post UTMs.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse, urlunparse

import anthropic

from .. import config, db

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


@dataclass
class GeneratedPost:
    platform: str        # 'linkedin' | 'x'
    text: str            # final text including CTA line
    cta_url: str         # the URL with UTMs (also embedded in text)


def _build_system_prompt() -> str:
    b = config.BRAND_CONFIG
    g = b["generation"]
    voice_bullets = "\n".join(f"- {v}" for v in b["voice"])
    pain_bullets = "\n".join(f"- {p}" for p in b["audience"]["pain_points"])
    offers_bullets = "\n".join(f"- {o}" for o in b["brand"]["offers"])

    return f"""You are the content strategist for {b['brand']['name']} (owner: {b['brand']['owner']}).

ABOUT THE BUSINESS
{b['brand']['one_liner']}

OFFERS
{offers_bullets}

AUDIENCE
Primary: {b['audience']['primary']}
Pain points they feel daily:
{pain_bullets}

VOICE
{voice_bullets}

YOUR JOB
Given a content idea (or a viral post for inspiration), produce TWO post variants:

1. LinkedIn variant — ~{g['linkedin_target_chars']} characters, long-form, with line breaks,
   includes a story or concrete example, ends with a CTA line and {g['hashtags_linkedin']} hashtags.
2. X (Twitter) variant — body must be ≤{g['x_target_chars']} chars (the caller will append a URL
   that consumes ~25 more chars, so X's 280 limit is preserved). Punchy, single hook + payoff,
   ends with the spoken CTA but tighter, {g['hashtags_x']} hashtags max.

CRITICAL RULES
- Never use the phrase "game changer", "unlock", "leverage", "in today's fast-paced world", or em-dashes inside marketing-speak. No emojis unless the idea demands one (max 1).
- Always lead with a specific, concrete hook (a number, a contradiction, a tiny story). Never start with "In today's…" or "As an AI agency…".
- The CTA must be ACTION-oriented and reference the audience's pain. Example: "If you're losing leads after 6pm, I'll show you the exact agent setup we use — link below."
- Output JSON ONLY in this exact shape:
{{"linkedin": "<full linkedin post text without CTA URL>", "x": "<full x post text without CTA URL>"}}
- Do NOT include the CTA URL itself — the caller appends it. Just leave the CTA *line* with phrasing like "Link in comments" or "DM me 'AGENT'" or end with the spoken CTA — the URL will be added on its own line at the end.
"""


def _build_viral_context(platform: str, limit: int = 4) -> str:
    rows = db.recent_viral(platform=platform, limit=limit)
    if not rows:
        return ""
    samples = []
    for r in rows:
        snippet = r["text"][:600].replace("\n", " ")
        samples.append(f"- [{r['engagement']} engagement] {snippet}")
    return (
        f"\nRECENT VIRAL {platform.upper()} POSTS IN THIS NICHE (for hook/structure inspiration — "
        f"do NOT copy phrasing, just learn what's working):\n" + "\n".join(samples)
    )


def _build_creators_context(platform: str, limit: int = 5) -> str:
    """Posts from creators Vishal explicitly tracks. Stronger signal than
    keyword-scraped trending — these are voices he aligns with stylistically.
    """
    rows = db.recent_creator_posts(platform=platform, limit=limit)
    if not rows:
        return ""
    samples = []
    for r in rows:
        snippet = r["text"][:700].replace("\n", " ")
        author = r["source_creator"] or r["author"] or "creator"
        samples.append(f"- [@{author}, {r['engagement']} engagement] {snippet}")
    return (
        f"\nCREATORS VISHAL FOLLOWS ON {platform.upper()} (these are voices whose style/angle "
        f"resonates — study their HOOK PATTERNS, format choices, and topical lens, then REMIX "
        f"into our AI-automation niche. Do not copy phrasing or claim their stories as ours.):\n"
        + "\n".join(samples)
    )


def _append_utm(url: str, post_id_hint: str, platform: str) -> str:
    parsed = urlparse(url)
    extra = urlencode({
        "utm_source": platform,
        "utm_medium": "organic",
        "utm_campaign": "social_autopilot",
        "utm_content": post_id_hint,
    })
    new_query = f"{parsed.query}&{extra}" if parsed.query else extra
    return urlunparse(parsed._replace(query=new_query))


def _extract_json(raw: str) -> dict:
    """Claude usually returns clean JSON, but defensively strip fences."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?", "", s).rstrip("`").strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model output: {raw[:200]}")
    return json.loads(s[start : end + 1])


def generate(idea_text: str, *, post_id_hint: str) -> list[GeneratedPost]:
    """Generate LinkedIn + X variants for one idea."""
    system_prompt = _build_system_prompt()
    viral_li = _build_viral_context("linkedin")
    viral_x = _build_viral_context("x")
    creators_li = _build_creators_context("linkedin")
    creators_x = _build_creators_context("x")

    user_msg = (
        f"CONTENT IDEA:\n{idea_text}\n"
        f"{creators_li}\n{creators_x}\n"
        f"{viral_li}\n{viral_x}\n\n"
        "Produce the two variants now. Return JSON only."
    )

    resp = _client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = "".join(block.text for block in resp.content if hasattr(block, "text"))
    log.info(
        "Claude usage: input=%d, cache_read=%d, cache_create=%d, output=%d",
        resp.usage.input_tokens,
        getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        resp.usage.output_tokens,
    )
    data = _extract_json(raw)

    x_max_body = int(config.BRAND_CONFIG["generation"]["x_target_chars"])

    results: list[GeneratedPost] = []
    for platform_key in ("linkedin", "x"):
        body = (data.get(platform_key) or "").strip()
        if not body:
            continue
        if platform_key == "x" and len(body) > x_max_body:
            log.warning("X body %d chars > %d — truncating", len(body), x_max_body)
            body = body[: x_max_body - 1].rstrip() + "…"
        cta_url = _append_utm(config.CTA_URL, post_id_hint, platform_key)
        full_text = f"{body}\n\n{cta_url}"
        results.append(GeneratedPost(platform=platform_key, text=full_text, cta_url=cta_url))
    return results
