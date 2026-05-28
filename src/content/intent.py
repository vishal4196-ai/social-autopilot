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

SYSTEM_PROMPT = """You are the intent router for a personal content-scheduling bot.
The owner (Vishal) sends you Telegram messages — figure out what they want.

INTENTS:
- post_now: They want to PUBLISH a post immediately. Examples: "post now", "publish it", "go ahead and post", "fire it", "do it now", "post please", "send it", "publish"
- list: They want to see queued ideas. Examples: "what's queued", "show ideas", "list queue", "what's in the queue", "show me the queue"
- recent: They want to see what was posted recently. Examples: "what did you post", "show recent posts", "what went out today", "show the last ones", "recent"
- status: Health/system check. Examples: "status", "are you alive", "how are you", "system check"
- skip: Remove a queued idea. MUST include a number. Examples: "skip 3", "delete idea 2", "remove 5", "drop number 4", "scrap idea 7"
- help: Show help. Examples: "help", "what can you do", "commands", "how does this work"
- idea: A NEW content idea to queue. This is the DEFAULT — anything that sounds like a topic, story, lesson, client win, hot take, observation, or note about their business or audience.
- small_talk: Pure conversational with no action needed. Examples: "thanks", "cool", "ok", "got it", "nice", "👍"

OUTPUT JSON ONLY:
{"intent": "<one of above>", "skip_id": <integer if intent=skip else null>}

DECISION RULES (apply in order):
1. If unsure between "idea" and a command → prefer "idea". Better to over-queue than to skip a real thought.
2. If the message is a short reactive phrase (1-3 words) and doesn't match a command → "small_talk".
3. "skip" requires a number. If they say "skip" without a number → "help" (we'll prompt them).
"""


@dataclass
class Intent:
    name: str                # idea | post_now | list | recent | status | skip | help | small_talk
    skip_id: int | None = None


_VALID = {"idea", "post_now", "list", "recent", "status", "skip", "help", "small_talk"}


def classify(text: str) -> Intent:
    """Classify a free-form message into a bot action.

    Returns Intent("idea") as the safe default on any failure — the message
    becomes a queued content idea, which is the original behaviour.
    """
    text = (text or "").strip()
    if not text:
        return Intent("idea")

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
        if cmd in {"help", "start"}:
            return Intent("help")
        if cmd == "skip":
            if len(parts) > 1 and parts[1].isdigit():
                return Intent("skip", int(parts[1]))
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
        if name == "skip":
            if not isinstance(skip_id, int):
                return Intent("help")
            return Intent("skip", skip_id)
        return Intent(name)
    except Exception as e:
        log.warning("router failed (%s) — defaulting to 'idea'", e)
        return Intent("idea")
