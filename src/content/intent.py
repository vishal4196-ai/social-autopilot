"""Natural-language intent classifier for incoming Telegram messages.

Uses Claude Haiku (cheap + fast) so the owner can talk plainly instead of
remembering slash commands. Slash commands still work via the fast path.

Cost note: ~500 input + ~30 output tokens per call ≈ $0.0002. Negligible
at personal-bot volume (a few dozen messages a day).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import anthropic

from .. import config

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# Cheap fast model for routing. If unavailable at runtime the call errors out
# and we gracefully fall back to "idea" — bot keeps queuing as before.
_ROUTER_MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """You are the intent router for Vishal's content bot. He
texts you in plain English. Map each message to one of these intents.

INTENTS:
- post_now: Publish immediately, bypass the approval gate. Examples: "post now", "publish it now", "fire it", "do it now", "ship it", "send the top one", "go go go"
- list: Show ideas / drafts. Examples: "what's brewing", "show my ideas", "what's in the queue", "what do I have", "show me my drafts", "queue", "ideas"
- recent: What posted recently. Examples: "what did you post today", "what went out", "recent posts", "show last few", "what's live"
- status: Health / how's it going. Examples: "status", "are you alive", "how's everything", "system check", "all good?"
- skip: Remove an idea (MUST have a number). Examples: "skip 3", "delete idea 2", "kill 5", "scrap that idea 7", "remove number 4"
- follow: Track a creator (MUST have platform + handle). Examples: "follow justin welsh on linkedin", "track @greg_isenberg on x", "add @naval to twitter", "watch alex hormozi on linkedin"
- unfollow: Stop tracking (MUST have platform + handle). Examples: "unfollow @greg_isenberg", "stop tracking justin welsh on linkedin"
- creators: List tracked creators. Examples: "who do you follow", "show creators", "who am I tracking"
- refresh: Scrape creator posts now. Examples: "refresh creators", "scrape now", "pull fresh posts from creators"
- research: Generate fresh ideas now. Examples: "give me ideas", "I need inspiration", "what should I post about", "brainstorm with me", "I'm stuck", "drop some ideas", "find me angles"
- help: Show what the bot can do. Examples: "help", "what can you do", "how does this work", "commands"
- idea: ANYTHING that sounds like content — a story, hot take, observation, client win, lesson, angle worth exploring. This is the default for anything substantive.
- small_talk: Greetings or short reactions with no action. Examples: "thanks", "cool", "got it", "👍", "morning", "hey", "ok"

OUTPUT JSON ONLY:
{
  "intent": "<one of above>",
  "skip_id": <integer if intent=skip else null>,
  "platform": "<'linkedin' or 'x' if intent=follow or unfollow else null>",
  "handle": "<the @username without the @ symbol, lowercased, if intent=follow or unfollow else null>"
}

DECISION RULES (apply in order):
1. If unsure between "idea" and a command → prefer "idea". Better to over-queue than to skip a real thought.
2. If the message is a short reactive phrase (1-3 words) and doesn't match a command → "small_talk".
3. "skip" requires a number; "follow"/"unfollow" require BOTH platform and handle. If any required field is missing → "help".
4. For follow/unfollow: "twitter" = "x". "linkedin" includes any phrasing like "li" or "linked in". If the platform is genuinely unclear, default to "linkedin" (Vishal's primary).
5. Strip the @ from handles and lowercase them. If a handle has spaces ("justin welsh") keep it as-is (the user might be using display name — downstream will resolve).
"""


@dataclass
class Intent:
    name: str                # idea | post_now | list | recent | status | skip | follow | unfollow | creators | refresh | remix_url | help | small_talk
    skip_id: int | None = None
    platform: str | None = None        # for follow/unfollow
    handle: str | None = None          # for follow/unfollow
    url: str | None = None             # for remix_url
    extra_context: str | None = None   # surrounding text when URL has context


_VALID = {
    "idea", "post_now", "list", "recent", "status", "skip",
    "follow", "unfollow", "creators", "refresh", "remix_url",
    "research", "help", "small_talk",
}


def classify(text: str) -> Intent:
    """Classify a free-form message into a bot action.

    Returns Intent("idea") as the safe default on any failure — the message
    becomes a queued content idea, which is the original behaviour.
    """
    text = (text or "").strip()
    if not text:
        return Intent("idea")

    # ─── Fast path: social post URL → remix. ──────────────────────────
    # Cheaper + more reliable than asking the LLM to extract URLs.
    from .url_fetch import detect_url  # local import: keeps router cheap if no URL
    url = detect_url(text)
    if url:
        # Everything around the URL is optional surrounding context.
        extra = text.replace(url, "").strip().strip(".:!?,-").strip() or None
        return Intent("remix_url", url=url, extra_context=extra)

    # ─── Fast path: slash commands skip the LLM entirely. ─────────────
    if text.startswith("/"):
        parts = text[1:].split()
        if not parts:
            return Intent("help")
        cmd = parts[0].lower()
        if cmd in {"post_now", "postnow"}:
            return Intent("post_now")
        if cmd == "list":
            return Intent("list")
        if cmd == "recent":
            return Intent("recent")
        if cmd == "status":
            return Intent("status")
        if cmd in {"creators", "following"}:
            return Intent("creators")
        if cmd in {"refresh", "scrape"}:
            return Intent("refresh")
        if cmd in {"research", "ideas", "brainstorm"}:
            return Intent("research")
        if cmd in {"help", "start"}:
            return Intent("help")
        if cmd == "skip":
            if len(parts) > 1 and parts[1].isdigit():
                return Intent("skip", int(parts[1]))
            return Intent("help")
        if cmd in {"follow", "unfollow"}:
            # /follow <linkedin|x> <handle>
            if len(parts) >= 3 and parts[1].lower() in {"linkedin", "x", "twitter"}:
                platform = "x" if parts[1].lower() in {"x", "twitter"} else "linkedin"
                handle = parts[2].lstrip("@").lower()
                return Intent(cmd, platform=platform, handle=handle)
            return Intent("help")
        return Intent("help")

    # ─── Natural language: ask the router model. ──────────────────────
    try:
        resp = _client.messages.create(
            model=_ROUTER_MODEL,
            max_tokens=80,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            log.warning("router returned no JSON: %r", raw)
            return Intent("idea")
        data = json.loads(m.group(0))
        name = str(data.get("intent") or "idea").lower()
        if name not in _VALID:
            name = "idea"
        skip_id = data.get("skip_id")
        platform = data.get("platform")
        handle = data.get("handle")

        if name == "skip":
            if not isinstance(skip_id, int):
                return Intent("help")
            return Intent("skip", skip_id=skip_id)

        if name in {"follow", "unfollow"}:
            if not platform or not handle:
                return Intent("help")
            platform = str(platform).lower()
            if platform in {"twitter", "tw"}:
                platform = "x"
            if platform not in {"linkedin", "x"}:
                return Intent("help")
            handle = str(handle).strip().lstrip("@").lower()
            if not handle:
                return Intent("help")
            return Intent(name, platform=platform, handle=handle)

        return Intent(name)
    except Exception as e:
        log.warning("router failed (%s) — defaulting to 'idea'", e)
        return Intent("idea")
