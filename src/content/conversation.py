"""Conversational chat agent for Telegram.

Replaces the intent classifier with a real Claude conversation that has
tool use. Vishal talks to the bot like a colleague; the model decides when
to call tools (add an idea, list pipeline, run research, etc.) vs. just chat.

Architecture:
- One in-memory conversation history per Telegram user (single-user app).
- System prompt + tools are prompt-cached for cost.
- Tool handlers wrap the existing DB/orchestrator/publisher actions.
- Loop runs up to MAX_ROUNDS tool turns per user message before bailing out.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

import anthropic

from .. import config, db

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ── History per user (one user, but kept extensible) ──────────
_HISTORY: dict[int, list[dict]] = {}
_LAST_TURN_TS: dict[int, float] = {}
HISTORY_MAX_MESSAGES = 24      # rolling window
HISTORY_RESET_AFTER_S = 3600   # idle > 1h → fresh chat
MAX_TOOL_ROUNDS = 6


# ── System prompt ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are Vishal's content assistant for UpliftAI.co.

UpliftAI.co is an autopilot SEO + AI-search-growth engine for local service
businesses (landscaping, cleaning, HVAC, plumbing, contractors, food service).
It ships 30 AI-written SEO articles a month, auto-publishes them, and ranks
clients on both Google AND ChatGPT/Perplexity.

You manage Vishal's social content pipeline (LinkedIn + X + Threads):
  Ideation → Drafted → Approved/Scheduled → Published

Vishal texts you on Telegram. Talk like a sharp casual colleague — short,
warm, no fluff. NEVER sound like a CLI, customer-support bot, or marketing
copy.

═══════════════════════════════════════════════════════════════
HOW YOU WORK

When Vishal:
- Shares something substantive (a story, hot take, observation, client win,
  lesson, angle): call add_idea with HIS EXACT WORDS, then acknowledge.
- Asks for ideas or sounds stuck: offer to run research, OR riff with him
  first (suggest 2-3 angles in UpliftAI's lane) then add the best one.
- Asks what's going on / queue / pipeline: call list_pipeline.
- Wants to see recent posts: call recent_posts.
- Wants to track / drop a creator: call follow_creator / unfollow_creator.
- Pastes a LinkedIn or X URL of a post he likes: call remix_url with it,
  then IMMEDIATELY call draft_idea with the returned idea id (he shares
  links because he wants a draft, not just a bookmark). Tell him it's
  waiting for his approval on the Today page.
- Wants an existing idea written up ("draft #14", "write that one up"):
  call draft_idea with the id.
- Says "post now" / "fire it" / "publish": call post_now.
- Asks a question about the system or content strategy: ANSWER DIRECTLY
  without tools, like a real conversation.

═══════════════════════════════════════════════════════════════
TONE

- Short messages. Cut adverbs. No "Certainly!" or "I'd be happy to help!".
- Talk to him like a teammate who already knows the project.
- Use light emoji ONLY when it adds info (✓ done, 🚀 fired, 📅 scheduled).
- Never apologize unless something genuinely broke.
- When you add an idea, just confirm briefly. Don't recap the whole idea
  back at him.

═══════════════════════════════════════════════════════════════
BRAND LANE (so you can riff intelligently)

Topics you can brainstorm in: AI-written SEO, GEO (generative engine
optimization), ranking on ChatGPT/Perplexity, local SEO for service
businesses, programmatic content, the death+rebirth of SEO via AI,
operator economics (cost-per-ranked-article, cost-per-lead-from-organic).

OUT OF LANE — don't suggest these: AI voice agents, n8n automations,
generic agency advice, generic ChatGPT prompt lists, anything not tied
to local-service-business SEO/AI-search.

═══════════════════════════════════════════════════════════════
ALWAYS

- Use the user's own words when calling add_idea (don't paraphrase).
- When uncertain whether something is an idea or a question, ASK briefly.
- Be present. Pick up context from the last few turns."""


# ── Tool schemas ──────────────────────────────────────────────

TOOLS = [
    {
        "name": "add_idea",
        "description": (
            "Add a new content idea to the Ideation column. Use Vishal's "
            "exact words. The idea will be available for him to draft + "
            "approve from the web app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The idea text, in Vishal's words."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_pipeline",
        "description": "Show what's currently in the content pipeline by phase.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recent_posts",
        "description": "Show the last N posts that went out (or are scheduled).",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 5}},
        },
    },
    {
        "name": "status",
        "description": "System health snapshot: counts per phase, schedule, integrations.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "skip_idea",
        "description": "Drop / kill an idea by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {"idea_id": {"type": "integer"}},
            "required": ["idea_id"],
        },
    },
    {
        "name": "follow_creator",
        "description": "Start tracking a creator's posts as remix inspiration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["linkedin", "x"]},
                "handle": {"type": "string", "description": "Username without @"},
            },
            "required": ["platform", "handle"],
        },
    },
    {
        "name": "unfollow_creator",
        "description": "Stop tracking a creator.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["linkedin", "x"]},
                "handle": {"type": "string"},
            },
            "required": ["platform", "handle"],
        },
    },
    {
        "name": "list_creators",
        "description": "List all currently tracked creators.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_signal",
        "description": "Trigger an Apify scrape of viral keywords + tracked creators now.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_research",
        "description": (
            "Run the research agent: scout the niche, generate 5-6 fresh "
            "scored post ideas, drop them into Ideation. Takes ~30-60 sec."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remix_url",
        "description": (
            "Fetch a LinkedIn or X post by URL and queue a remix idea "
            "(adapt its hook/structure to UpliftAI's niche)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "note": {"type": "string", "description": "Optional: Vishal's note about why he liked it"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "post_now",
        "description": (
            "Bypass approval gate and immediately publish the top approved "
            "idea. Use only when Vishal explicitly says 'post now', 'fire it', "
            "or similar."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "draft_idea",
        "description": (
            "Draft an idea NOW: generates the LinkedIn + X + Threads variants "
            "AND a branded image, moves it to 'awaiting approval' on the Today "
            "page. Takes ~20 sec. Use after remix_url or add_idea when Vishal "
            "wants it written up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"idea_id": {"type": "integer"}},
            "required": ["idea_id"],
        },
    },
]


# ── Tool handlers — sync, return strings (Claude reads them) ──

def _tool_add_idea(text: str) -> str:
    idea_id = db.add_idea(text.strip(), source="telegram", phase="ideation")
    return f"OK — added as idea #{idea_id} in Ideation."


def _tool_list_pipeline() -> str:
    ideation = db.list_by_phase("ideation", limit=50)
    drafted = db.list_by_phase("drafted", limit=50)
    approved = db.list_by_phase("approved", limit=50) + db.list_by_phase("scheduled", limit=50)
    parts = []
    parts.append(f"Ideation: {len(ideation)} ({sum(1 for i in ideation if i['source']=='research_agent')} from AI, {sum(1 for i in ideation if i['source']!='research_agent')} from user)")
    parts.append(f"Drafted (awaiting approval): {len(drafted)}")
    parts.append(f"Approved + scheduled in Postsyncer: {len(approved)}")
    if drafted:
        parts.append("\nDrafts awaiting approval:")
        for r in drafted[:5]:
            parts.append(f"  #{r['id']} — {r['text'][:90]}")
    if approved:
        parts.append("\nApproved / scheduled:")
        for r in approved[:5]:
            parts.append(f"  #{r['id']} — {r['text'][:90]}")
    if ideation:
        parts.append("\nIdeation (top 8):")
        for r in ideation[:8]:
            score = f" [{r['score']}]" if r['score'] else ""
            parts.append(f"  #{r['id']}{score} — {r['text'][:90]}")
    return "\n".join(parts)


def _tool_recent_posts(limit: int = 5) -> str:
    rows = db.recent_posts(limit=limit)
    if not rows:
        return "No posts logged yet."
    lines = []
    for r in rows:
        snippet = (r['text'] or '')[:120].replace('\n', ' ')
        lines.append(f"[{r['platform']}·{r['status']}·{r['created_at'][:10]}] {snippet}")
    return "\n".join(lines)


def _tool_status() -> str:
    ideation = len(db.list_by_phase("ideation", limit=999))
    drafted = len(db.list_by_phase("drafted", limit=999))
    approved = len(db.list_by_phase("approved", limit=999)) + len(db.list_by_phase("scheduled", limit=999))
    recent = len(db.recent_posts(limit=999))
    return (
        f"Pipeline: {ideation} ideation, {drafted} drafted, {approved} approved/scheduled, {recent} posts logged. "
        f"Schedule: {', '.join(config.POST_TIMES)} {config.TIMEZONE}. "
        f"Daily ideation: {config.IDEATION_TIME} ({config.IDEATION_COUNT} ideas). "
        f"Apify: {'on' if config.APIFY_ENABLED else 'off'}."
    )


def _tool_skip_idea(idea_id: int) -> str:
    db.skip_idea(idea_id)
    return f"Idea #{idea_id} skipped."


def _tool_follow_creator(platform: str, handle: str) -> str:
    handle = handle.lstrip("@").lower()
    _, was_new = db.add_creator(platform=platform.lower(), handle=handle)
    if not was_new:
        return f"Already tracking @{handle} on {platform}."
    extra = " (Apify off — won't scrape until enabled.)" if not config.APIFY_ENABLED else ""
    return f"Now following @{handle} on {platform}.{extra}"


def _tool_unfollow_creator(platform: str, handle: str) -> str:
    handle = handle.lstrip("@").lower()
    removed = db.remove_creator(platform=platform.lower(), handle=handle)
    return f"Stopped tracking @{handle}." if removed else f"Wasn't tracking @{handle}."


def _tool_list_creators() -> str:
    rows = db.list_creators()
    if not rows:
        return "Not tracking any creators yet."
    by_p: dict[str, list[str]] = {}
    for r in rows:
        by_p.setdefault(r['platform'], []).append(f"@{r['handle']}")
    return "; ".join(f"{p}: {', '.join(hs)}" for p, hs in sorted(by_p.items()))


def _tool_refresh_signal() -> str:
    if not config.APIFY_ENABLED:
        return "Apify is disabled — set APIFY_ENABLED=true in env."
    try:
        from . import viral_discovery
        result = viral_discovery.refresh()
        return f"Refresh done: {result['linkedin_trending']} LI trending, {result['x_trending']} X trending, {result['creator_posts']} creator posts."
    except Exception as e:
        log.exception("refresh failed")
        return f"Refresh errored: {e}"


def _tool_run_research() -> str:
    try:
        from ..agents import orchestrator
        s = orchestrator.run_research_pipeline(refresh_signal=True)
        top_titles = "; ".join(f"[{i['score']}] {i['hook'][:60]}" for i in s.get('top_ideas', [])[:3])
        return f"Research done. Brief: {s['brief_summary'][:200]}. Queued {s['ideas_queued']} ideas. Top: {top_titles}"
    except Exception as e:
        log.exception("research failed")
        return f"Research errored: {e}"


def _tool_remix_url(url: str, note: str | None = None) -> str:
    if not config.APIFY_ENABLED:
        return "Apify is off — can't fetch URL. Vishal should paste the text directly."
    try:
        from . import url_fetch
        post = url_fetch.fetch_single_post(url)
        if not post:
            return "Couldn't read that URL. Vishal could paste the text instead."
        framing = [
            "REMIX SOURCE POST — adapt hook style, format, angle to UpliftAI's niche (AI SEO + GEO for local service businesses).",
            f"Source: @{post['author']} on {post['platform']} ({post['engagement']} eng)",
            f"URL: {url}",
        ]
        if note:
            framing.append(f"Note: {note}")
        framing.append(f'\nOriginal:\n"""\n{post["text"]}\n"""')
        idea_id = db.add_idea("\n".join(framing), source="url_remix", phase="ideation")
        return f"Remix added as idea #{idea_id}. Author: @{post['author']}, snippet: {post['text'][:140]}"
    except Exception as e:
        log.exception("remix failed")
        return f"Remix errored: {e}"


def _tool_draft_idea(idea_id: int) -> str:
    try:
        from . import drafting
        drafts = drafting.draft_idea(idea_id)
        parts = []
        for k in ("linkedin", "x", "threads"):
            if drafts.get(k):
                parts.append(f"{k} {len(drafts[k])} chars")
        img = "with branded image" if drafts.get("image_url") else "no image"
        return (
            f"Drafted idea #{idea_id} ({', '.join(parts)}; {img}). "
            f"It's now awaiting approval on the Today page."
        )
    except Exception as e:
        log.exception("draft_idea tool failed")
        return f"Draft failed: {e}"


def _tool_post_now() -> str:
    approved = len(db.list_by_phase("approved", limit=1))
    if approved == 0:
        return "Nothing approved to publish. Vishal needs to approve a draft first."
    try:
        from ..scheduler import run_post_cycle
        run_post_cycle()
        return "✓ Top approved idea fired."
    except Exception as e:
        log.exception("post_now failed")
        return f"Failed: {e}"


_DISPATCH: dict[str, Callable[..., str]] = {
    "add_idea": _tool_add_idea,
    "list_pipeline": _tool_list_pipeline,
    "recent_posts": _tool_recent_posts,
    "status": _tool_status,
    "skip_idea": _tool_skip_idea,
    "follow_creator": _tool_follow_creator,
    "unfollow_creator": _tool_unfollow_creator,
    "list_creators": _tool_list_creators,
    "refresh_signal": _tool_refresh_signal,
    "run_research": _tool_run_research,
    "remix_url": _tool_remix_url,
    "post_now": _tool_post_now,
    "draft_idea": _tool_draft_idea,
}


# ── History management ────────────────────────────────────────

def _get_history(user_id: int) -> list[dict]:
    now = time.time()
    last = _LAST_TURN_TS.get(user_id, 0)
    if now - last > HISTORY_RESET_AFTER_S:
        _HISTORY[user_id] = []
    _LAST_TURN_TS[user_id] = now
    return _HISTORY.setdefault(user_id, [])


def _trim_history(messages: list[dict]) -> list[dict]:
    """Keep history under MAX. Always preserve first user msg + tail."""
    if len(messages) <= HISTORY_MAX_MESSAGES:
        return messages
    return messages[-HISTORY_MAX_MESSAGES:]


def reset(user_id: int) -> None:
    _HISTORY.pop(user_id, None)
    _LAST_TURN_TS.pop(user_id, None)


# ── Main chat entry point ─────────────────────────────────────

def chat(user_id: int, text: str) -> str:
    """One conversational turn. Returns the bot's reply text."""
    history = _get_history(user_id)
    history.append({"role": "user", "content": text})

    for round_idx in range(MAX_TOOL_ROUNDS):
        try:
            resp = _client.messages.create(
                model=config.CONVERSATION_MODEL,
                max_tokens=1500,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
                messages=history,
            )
        except Exception as e:
            log.exception("chat: messages.create failed")
            return f"(Something errored on my end: {str(e)[:200]})"

        # Append the assistant turn (text + tool_use blocks) verbatim
        history.append({
            "role": "assistant",
            "content": [b.model_dump() for b in resp.content],
        })

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]

        if not tool_uses:
            # Final answer
            history[:] = _trim_history(history)
            reply = "\n".join(text_blocks).strip()
            return reply or "🙂"

        # Execute each tool, feed results back as a 'user' message of tool_results
        tool_results = []
        for tu in tool_uses:
            handler = _DISPATCH.get(tu.name)
            if not handler:
                result_str = f"Unknown tool: {tu.name}"
            else:
                try:
                    result_str = str(handler(**(tu.input or {})))
                except TypeError as e:
                    result_str = f"Bad arguments for {tu.name}: {e}"
                except Exception as e:
                    log.exception("tool %s failed", tu.name)
                    result_str = f"Tool {tu.name} errored: {e}"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_str,
            })
        history.append({"role": "user", "content": tool_results})

    log.warning("chat: hit MAX_TOOL_ROUNDS, bailing")
    return "(That took too many steps — try a simpler ask?)"
