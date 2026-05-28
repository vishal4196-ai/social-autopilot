"""Telegram bot — natural-language first.

Send anything in plain English. A Claude-Haiku router classifies your intent
and routes to the right action. Slash commands still work as a fallback.
"""
import asyncio
import logging

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
        "Hey — just talk to me normally. Examples:\n\n"
        "• \"We shipped a GHL automation that cut response time from 4h to 12s\""
        " → I queue it as a post idea\n"
        "• \"post now\" or \"publish it\" → I fire a real LinkedIn + X post\n"
        "• \"what's queued?\" → show your queue\n"
        "• \"what did you post?\" → show recent posts\n"
        "• \"skip 3\" → drop idea #3\n"
        "• \"status\" → system check\n\n"
        "Slash commands still work too: /post_now, /list, /recent, /skip, /status"
    )


# ── Action handlers — caller passes `send` so we don't bind to a specific update ──

async def do_queue_idea(text: str, send) -> None:
    idea_id = db.add_idea(text, source="telegram")
    await send(f"✓ queued (idea #{idea_id}). Going out on the next scheduled slot.")


async def do_list(send) -> None:
    rows = db.list_queued(limit=15)
    if not rows:
        await send("Queue is empty. Send me a thought and I'll queue it.")
        return
    lines = [f"#{r['id']} — {r['text'][:80]}" for r in rows]
    await send("Queued ideas:\n\n" + "\n".join(lines))


async def do_recent(send) -> None:
    rows = db.recent_posts(limit=6)
    if not rows:
        await send("Nothing posted yet.")
        return
    out = []
    for r in rows:
        snippet = r["text"][:120].replace("\n", " ")
        out.append(f"[{r['platform']}] {r['status']} — {snippet}")
    await send("Recent posts:\n\n" + "\n\n".join(out))


async def do_skip(idea_id: int, send) -> None:
    db.skip_idea(idea_id)
    await send(f"Skipped idea #{idea_id}.")


async def do_status(send) -> None:
    queued = len(db.list_queued(limit=100))
    recent = len(db.recent_posts(limit=100))
    await send(
        f"queued ideas: {queued}\n"
        f"recent posts logged: {recent}\n"
        f"schedule: {', '.join(config.POST_TIMES)} {config.TIMEZONE}\n"
        f"apify viral discovery: {'on' if config.APIFY_ENABLED else 'off'}"
    )


async def do_post_now(send) -> None:
    # Lazy import to avoid any chance of a circular import via main.py.
    from .scheduler import run_post_cycle

    queued = len(db.list_queued(limit=1))
    if queued == 0:
        await send(
            "⚠ Queue is empty — Claude will write from a generic niche fallback "
            "(may invent a case study). Send a real idea first if you want it "
            "grounded, then say \"post now\" again.\n\nFiring anyway… (5-30 sec)"
        )
    else:
        await send("🚀 firing post cycle (5-30 sec)…")

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_post_cycle)
        await send("✓ done. Say \"recent\" to see what posted.")
    except Exception as e:
        log.exception("post_now failed")
        await send(f"✗ failed: {str(e)[:400]}")


# ── Single message handler — natural language OR slash commands route through here ──

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
    elif intent.name == "help":
        await send(_help_text())
    elif intent.name == "small_talk":
        await send("👍")
    else:
        # Default: queue as a content idea.
        await do_queue_idea(text, send)


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    # One handler to rule them all — slash commands and natural language both land here.
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    return app
