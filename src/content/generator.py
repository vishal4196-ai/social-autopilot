"""Claude-powered post generator.

The prompt gives Claude editorial autonomy: when to include the booking link,
when to skip hashtags, which post format to use. We don't append CTAs
mechanically anymore — Claude embeds a {{CTA}} token where it wants the URL
to appear (if at all), and the substitution happens here.

Inputs to the prompt:
- Brand voice, audience, offers (from config.yaml)
- "Creators Vishal follows" — strong remix signal from tracked accounts
- "Recent viral posts in niche" — broader trending signal (keyword-scraped)
- "Posts you wrote recently" — so Claude doesn't repeat formats back-to-back
- The user's idea (or remix-from-URL framing)
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

CTA_TOKEN = "{{CTA}}"


@dataclass
class GeneratedPost:
    platform: str        # 'linkedin' | 'x'
    text: str            # final text — CTA token (if any) already substituted
    cta_url: str         # the URL with UTMs (empty string if Claude chose no CTA)
    included_cta: bool


def _build_system_prompt() -> str:
    b = config.BRAND_CONFIG
    voice_bullets = "\n".join(f"- {v}" for v in b["voice"])
    pain_bullets = "\n".join(f"- {p}" for p in b["audience"]["pain_points"])
    offers_bullets = "\n".join(f"- {o}" for o in b["brand"]["offers"])

    return f"""You write social posts for {b['brand']['name']} (owner: {b['brand']['owner']}).
You're a thoughtful content creator, NOT a marketing department.

ABOUT THE BUSINESS
{b['brand']['one_liner']}

OFFERS (don't recite these — they inform the angle)
{offers_bullets}

AUDIENCE
Primary: {b['audience']['primary']}
Pain points they feel daily:
{pain_bullets}

VOICE
{voice_bullets}

═══════════════════════════════════════════════════════════════
YOUR JOB

Given a content idea (or a source post to remix), produce ONE post each for
LinkedIn and X. The two should share an angle but be format-tuned for each
platform. They are NOT just the same post truncated.

═══════════════════════════════════════════════════════════════
CALL-TO-ACTION POLICY (read this carefully)

Most of the best content on social media has NO direct CTA. It's insight,
story, or value that builds trust. Use the booking link sparingly.

DEFAULT: no booking link. Just end with the payoff or a question.

INCLUDE A BOOKING LINK only when:
  • Post is a case study with a concrete outcome a reader might want for themselves
  • Post is a deep-teaching breakdown of something Vishal can implement for them
  • Post naturally arrives at "if this resonates, talk to me" without forcing it

When you do include a link, embed the literal token {CTA_TOKEN} where you want
the URL placed (the system substitutes it for a tracked booking URL). Phrase
the surrounding text softly: "If this maps to your situation, {CTA_TOKEN}"
or "Same playbook is at {CTA_TOKEN} if you want it" — NOT "Book a free call!"

TARGET FREQUENCY: roughly 1 in 3 posts has a CTA. Two in three should not.
If the idea is a pure value bomb, a hot take, or a story without a clear "buy"
moment — NO {CTA_TOKEN}.

═══════════════════════════════════════════════════════════════
HASHTAGS POLICY

Use sparingly. Generic tags (#AI, #automation, #marketing, #leadership) are
spam-coded — skip them. A niche-specific one (e.g. #n8n, #GoHighLevel) can
help discoverability but only if it genuinely fits.

LinkedIn: 0-2 hashtags. Often zero is right.
X: 0-1 hashtags. Often zero is right.

═══════════════════════════════════════════════════════════════
POST VARIETY

Don't ship the same template twice. Look at "POSTS YOU WROTE RECENTLY" below
and choose a DIFFERENT format. Mix from:

  • Short banger (2-4 sharp lines, ends on a contradiction or punch)
  • Case study / story (a real or representative situation + outcome)
  • Framework / listicle (3 steps, 5 things, etc.)
  • Hot take / contrarian angle ("everyone says X. Wrong. Here's why.")
  • Question post (asks the audience something, invites discussion)
  • Behind-the-scenes (what we're building / how we work)
  • Lesson learned (what didn't work + what we figured out)

═══════════════════════════════════════════════════════════════
HARD RULES

- Never use: "game changer", "unlock", "leverage", "in today's fast-paced world",
  "synergy", "revolutionize", em-dashes inside marketing-speak.
- Lead with a SPECIFIC, concrete hook. Number, contradiction, story, or claim.
  Never start with "In today's..." or "As an AI agency...".
- One emoji max per post, only if it earns its keep. Default: zero.
- X length: body ≤{int(config.BRAND_CONFIG['generation']['x_target_chars'])} characters
  (the system reserves ~25 chars for the URL if you include {CTA_TOKEN}).
- LinkedIn length: aim for {int(config.BRAND_CONFIG['generation']['linkedin_target_chars'])} chars,
  with line breaks every 1-3 sentences for scannability.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT

Return JSON ONLY, no prose, no fences:
{{
  "linkedin": "<full linkedin post text>",
  "x": "<full x post text>"
}}

If you want the booking link in a post, put {CTA_TOKEN} where the URL should appear.
If a post should have no link, just omit the token entirely.
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
    rows = db.recent_creator_posts(platform=platform, limit=limit)
    if not rows:
        return ""
    samples = []
    for r in rows:
        snippet = r["text"][:700].replace("\n", " ")
        author = r["source_creator"] or r["author"] or "creator"
        samples.append(f"- [@{author}, {r['engagement']} engagement] {snippet}")
    return (
        f"\nCREATORS VISHAL FOLLOWS ON {platform.upper()} (voices whose style/angle "
        f"resonates — study their hook patterns, format choices, and topical lens, "
        f"then REMIX into our niche. Do not copy phrasing or claim their stories as ours):\n"
        + "\n".join(samples)
    )


def _build_recent_self_context(limit: int = 4) -> str:
    """Show Claude the last few posts WE wrote so it picks a different format."""
    rows = db.recent_posts(limit=limit * 2)  # *2 because we have LI + X per cycle
    if not rows:
        return ""
    # Dedupe by post text first ~120 chars (so the same idea on LI + X doesn't double-count)
    seen: set[str] = set()
    samples: list[str] = []
    for r in rows:
        key = r["text"][:120]
        if key in seen:
            continue
        seen.add(key)
        snippet = r["text"][:300].replace("\n", " ")
        samples.append(f"- [{r['platform']}] {snippet}")
        if len(samples) >= limit:
            break
    if not samples:
        return ""
    return (
        "\nPOSTS YOU WROTE RECENTLY (avoid repeating these formats / hooks / angles — "
        "pick a different shape):\n" + "\n".join(samples)
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
    creators_li = _build_creators_context("linkedin")
    creators_x = _build_creators_context("x")
    viral_li = _build_viral_context("linkedin")
    viral_x = _build_viral_context("x")
    recent_self = _build_recent_self_context()

    user_msg = (
        f"CONTENT IDEA:\n{idea_text}\n"
        f"{creators_li}\n{creators_x}\n"
        f"{viral_li}\n{viral_x}\n"
        f"{recent_self}\n\n"
        "Write the two variants now. Decide per-post whether to include {{CTA}}. "
        "Return JSON only."
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

        has_cta = CTA_TOKEN in body
        cta_url = _append_utm(config.CTA_URL, post_id_hint, platform_key) if has_cta else ""

        if has_cta:
            body = body.replace(CTA_TOKEN, cta_url)

        # X length safety net — only trim if no CTA URL inside (URL counts as
        # ~23 chars on X regardless of real length, but we leave room).
        if platform_key == "x":
            # For length check, count the URL as ~25 chars even though it's longer literally.
            effective_len = len(body) - (len(cta_url) - 25 if cta_url else 0)
            if effective_len > x_max_body + 25:
                log.warning("X body %d effective chars > limit — truncating", effective_len)
                # Trim from the body portion (not the URL). Simplistic but safe.
                if cta_url and cta_url in body:
                    before, _, after = body.partition(cta_url)
                    keep = x_max_body - len(after) - 30
                    if keep > 50:
                        before = before[:keep].rstrip() + "… "
                    body = before + cta_url + after
                else:
                    body = body[: x_max_body - 1].rstrip() + "…"

        results.append(GeneratedPost(
            platform=platform_key,
            text=body,
            cta_url=cta_url,
            included_cta=has_cta,
        ))

    log.info(
        "Generated %d posts (CTA included: %s)",
        len(results),
        [r.included_cta for r in results],
    )
    return results
