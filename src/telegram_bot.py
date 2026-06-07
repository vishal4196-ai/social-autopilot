"""Telegram bot — natural-language first, conversational tone.

Send anything in plain English. A Claude-Haiku router classifies the intent
and routes to the right action. Replies aim to feel like a colleague, not a
CLI. Slash commands still work as fallback.
"""
import asyncio
import logging
import random

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db
from .content import intent as intent_router

log = logging.getLogger(__name__)


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == config.TELEGRAM_ALLOWED_USER_ID)


def _help_text() -> str:
    return (
        "Hey 👋 just text me normally. Examples:\n\n"
        "📝 Drop an idea\n"
        "   Anything substantive becomes a post idea in Today's Ideation column.\n"
        "   You draft + approve when ready — no surprise auto-posts.\n"
        "   e.g. \"A landscaping client paid an agency $4,200/mo for 4 blog posts. We did 30.\"\n\n"
        "💡 Need inspiration\n"
        "   \"give me ideas\" · \"I'm stuck\" · \"what should I post about\"\n"
        "   I'll run the research agent and drop 5 fresh ones in Ideation.\n\n"
        "👀 Check on things\n"
        "   \"what's brewing\"        — show your ideas + drafts\n"
        "   \"what did you post\"     — recent posts\n"
        "   \"status\"                — quick health check\n\n"
        "🎯 Sources\n"
        "   \"follow justin welsh on linkedin\"   — track a creator\n"
        "   \"who do you follow\"                 — list tracked\n"
        "   \"refresh\"                            — scrape creators now\n\n"
        "🔗 Paste any LinkedIn or X URL → I'll read it and queue a remix.\n\n"
        "⚡ Fast lane: \"post now\" bypasses the approval gate."
    )


# Friendlier reply varieties so the bot doesn't sound robotic on repeats.
_IDEA_ACKS = [
    "Got it 👍 added to your ideas.",
    "Locked in. That's now in Ideation.",
    "Nice — saved as an idea.",
    "Captured. Open Today when you're ready to draft it.",
    "Done, that's in the Ideation column.",
]
_SMALL_TALK = ["👍", "anytime", "🙌", "got it", "sure thing", "🫡"]


# ── Action handlers — caller passes `send` so we don't bind to a specific update ──

async def do_queue_idea(text: str, send) -> None:
    """Telegram message → Ideation phase (review lane), matches web flow."""
    idea_id = db.add_idea(text, source="telegram", phase="ideation")
    pre = random.choice(_IDEA_ACKS)
    await send(
        f"{pre}\n"
        f"→ idea #{idea_id} in /today's Ideation. Draft + approve when ready, "
        f"or say \"post now\" to push the top approved one immediately."
    )


async def do_list(send) -> None:
    """Show what's in flight: ideas + drafts + approved."""
    ideation = db.list_by_phase("ideation", limit=15)
    drafted = db.list_by_phase("drafted", limit=15)
    approved = db.list_by_phase("approved", limit=15) + db.list_by_phase("scheduled", limit=15)
    if not (ideation or drafted or approved):
        await send("Pipeline's empty. Text me an idea or say \"give me ideas\" for some inspiration.")
        return
    out = []
    if drafted:
        out.append(f"✎ {len(drafted)} drafts waiting for your approval:")
        for r in drafted[:5]:
            out.append(f"   #{r['id']} {r['text'][:70]}")
        out.append("")
    if approved:
        out.append(f"📅 {len(approved)} approved · scheduled to publish:")
        for r in approved[:5]:
            out.append(f"   #{r['id']} {r['text'][:70]}")
        out.append("")
    if ideation:
        out.append(f"💡 {len(ideation)} ideas in Ideation:")
        for r in ideation[:8]:
            src = "AI" if r["source"] == "research_agent" else "you"
            out.append(f"   #{r['id']} [{src}] {r['text'][:70]}")
    await send("\n".join(out).strip())


async def do_recent(send) -> None:
    rows = db.recent_posts(limit=6)
    if not rows:
        await send("Nothing posted yet — once a post lands, it'll show here.")
        return
    out = ["Last few posts:\n"]
    for r in rows:
        snippet = (r["text"] or "")[:120].replace("\n", " ")
        out.append(f"[{r['platform']}·{r['status']}] {snippet}")
    await send("\n\n".join(out))


async def do_skip(idea_id: int, send) -> None:
    db.skip_idea(idea_id)
    await send(f"Killed idea #{idea_id}. Won't see it again.")


async def do_status(send) -> None:
    ideation = len(db.list_by_phase("ideation", limit=999))
    drafted = len(db.list_by_phase("drafted", limit=999))
    approved = len(db.list_by_phase("approved", limit=999)) + len(db.list_by_phase("scheduled", limit=999))
    recent = len(db.recent_posts(limit=999))
    await send(
        "All good.\n\n"
        f"💡 Ideas in pipeline: {ideation}\n"
        f"✎ Drafts awaiting you: {drafted}\n"
        f"📅 Approved/scheduled: {approved}\n"
        f"📤 Posts logged: {recent}\n\n"
        f"Schedule: {', '.join(config.POST_TIMES)} {config.TIMEZONE}\n"
        f"Daily ideation: {config.IDEATION_TIME} ({config.IDEATION_COUNT} ideas)\n"
        f"Apify discovery: {'on' if config.APIFY_ENABLED else 'off'}"
    )


async def do_follow(platform: str, handle: str, send) -> None:
    _, was_new = db.add_creator(platform=platform, handle=handle)
    if was_new:
        await send(
            f"✓ Now following @{handle} on {platform}. "
            f"I'll pull their recent posts on the next discovery refresh and "
            f"weigh their style when I write."
        )
    else:
        await send(f"Already tracking @{handle} on {platform} — no change.")
    if not config.APIFY_ENABLED:
        await send("Heads-up though: Apify is off, so I can't actually scrape posts yet.")


async def do_unfollow(platform: str, handle: str, send) -> None:
    removed = db.remove_creator(platform=platform, handle=handle)
    if removed:
        await send(f"Done — stopped tracking @{handle} on {platform}.")
    else:
        await send(f"Wasn't following @{handle} on {platform} anyway.")


async def do_list_creators(send) -> None:
    rows = db.list_creators()
    if not rows:
        await send("Not tracking anyone yet. Try \"follow justin welsh on linkedin\".")
        return
    by_platform: dict[str, list[str]] = {}
    for r in rows:
        last = r["last_scraped_at"]
        last_str = f" (last scraped {last[:10]})" if last else " (never scraped)"
        by_platform.setdefault(r["platform"], []).append(f"@{r['handle']}{last_str}")
    lines = ["You're tracking:"]
    for platform in sorted(by_platform.keys()):
        lines.append(f"\n{platform.upper()}")
        for h in by_platform[platform]:
            lines.append(f"  • {h}")
    await send("\n".join(lines))


async def do_remix_url(url: str, extra_context: str | None, send) -> None:
    """Pasted URL → fetch + queue a remix idea (lands in Ideation)."""
    if not config.APIFY_ENABLED:
        await send(
            "Apify's off — can't fetch URLs. Paste the post text instead "
            "and I'll remix that."
        )
        return

    from .content import url_fetch
    await send(f"🔍 reading the post… ({url[:60]})")

    loop = asyncio.get_running_loop()
    try:
        post = await loop.run_in_executor(None, url_fetch.fetch_single_post, url)
    except Exception as e:
        log.exception("url_fetch threw")
        await send(f"✗ couldn't fetch — {str(e)[:200]}")
        return

    if not post:
        await send(
            "✗ couldn't read that one. Paste the post text directly and "
            "I'll remix it from there."
        )
        return

    snippet = (post["text"] or "")[:200].replace("\n", " ")
    framing = [
        "REMIX SOURCE POST — adapt its hook style, format, and angle to UpliftAI's lane "
        "(AI-powered SEO + GEO for local service businesses).",
        f"Source: @{post['author']} on {post['platform']} ({post['engagement']} eng)",
        f"URL: {url}",
    ]
    if extra_context:
        framing.append(f"Vishal's note: {extra_context}")
    framing.append("")
    framing.append("Original:")
    framing.append(f'"""\n{post["text"]}\n"""')
    framing.append("")
    framing.append(
        "Write our version. Don't copy phrasing or claim their story as ours. "
        "Take what worked — hook, structure, energy — and apply it to local "
        "service businesses (landscaping, cleaning, contractors, HVAC, etc.)."
    )

    idea_id = db.add_idea("\n".join(framing), source="url_remix", phase="ideation")
    await send(
        f"✓ Got it — remix from @{post['author']} added as idea #{idea_id} in Ideation.\n"
        f"Preview: \"{snippet}…\"\n\n"
        f"Open Today to draft + approve, or say \"post now\" for the fast lane."
    )


async def do_refresh(send) -> None:
    if not config.APIFY_ENABLED:
        await send("Apify's off — flip APIFY_ENABLED=true in Railway and I'll be able to scrape.")
        return
    from .content import viral_discovery
    await send("Scraping creators + viral keywords… give me 10-60 sec.")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, viral_discovery.refresh)
        await send(
            f"✓ Pulled fresh signal:\n"
            f"   LinkedIn trending posts: {result['linkedin_trending']}\n"
            f"   X trending posts: {result['x_trending']}\n"
            f"   Tracked-creator posts: {result['creator_posts']}"
        )
    except Exception as e:
        log.exception("refresh failed")
        await send(f"✗ refresh hit an error: {str(e)[:300]}")


async def do_research(send) -> None:
    await send("On it — scouting the niche, drafting ideas (~30-60 sec)…")
    from .agents import orchestrator
    loop = asyncio.get_running_loop()
    try:
        summary = await loop.run_in_executor(
            None, lambda: orchestrator.run_research_pipeline(refresh_signal=True)
        )
    except Exception as e:
        log.exception("research failed")
        await send(f"✗ research errored: {str(e)[:300]}")
        return

    lines = [f"🧠 {summary['brief_summary']}", ""]
    if summary.get("themes"):
        lines.append("Hot themes right now:")
        lines += [f"  • {t}" for t in summary["themes"]]
        lines.append("")
    lines.append(f"Dropped {summary['ideas_queued']} new ideas in Ideation:")
    for i in summary.get("top_ideas", []):
        lines.append(f"  [{i['score']}] {i['hook']}")
    lines.append("")
    lines.append("Open Today to draft the ones you like.")
    await send("\n".join(lines))


async def do_post_now(send) -> None:
    from .scheduler import run_post_cycle
    approved = len(db.list_by_phase("approved", limit=1))
    if approved == 0:
        await send(
            "Nothing approved to push. Approve a draft in /today first, then "
            "I can fire it. Or say \"give me ideas\" to start a new one."
        )
        return
    await send("🚀 Firing the top approved one now…")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_post_cycle)
        await send("✓ Done. Say \"what did you post\" to see what went out.")
    except Exception as e:
        log.exception("post_now failed")
        await send(f"✗ failed: {str(e)[:300]}")


# ── Single message handler ──

async def on_message(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    send = update.message.reply_text
    intent = intent_router.classify(text)
    log.info("intent=%s skip_id=%s for %r", intent.name, intent.skip_id, text[:80])

    if intent.name == "post_now":
        await do_post_now(send)
    elif intent.name == "list":
        await do_list(send)
    elif intent.name == "recent":
        await do_recent(send)
    elif intent.name == "status":
        await do_status(send)
    elif intent.name == "skip" and intent.skip_id is not None:
        await do_skip(intent.skip_id, send)
    elif intent.name == "follow" and intent.platform and intent.handle:
        await do_follow(intent.platform, intent.handle, send)
    elif intent.name == "unfollow" and intent.platform and intent.handle:
        await do_unfollow(intent.platform, intent.handle, send)
    elif intent.name == "creators":
        await do_list_creators(send)
    elif intent.name == "refresh":
        await do_refresh(send)
    elif intent.name == "research":
        await do_research(send)
    elif intent.name == "remix_url" and intent.url:
        await do_remix_url(intent.url, intent.extra_context, send)
    elif intent.name == "help":
        await send(_help_text())
    elif intent.name == "small_talk":
        await send(random.choice(_SMALL_TALK))
    else:
        # Default: treat as a content idea.
        await do_queue_idea(text, send)


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    return app
