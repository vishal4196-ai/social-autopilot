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
        "📝 Ideas\n"
        "• \"We shipped a GHL automation that cut response time from 4h to 12s\""
        " → queues as a post idea\n"
        "• \"post now\" or \"publish it\" → fires a real LinkedIn + X post\n"
        "• \"what's queued?\" → show queue\n"
        "• \"what did you post?\" → recent posts\n"
        "• \"skip 3\" → drop idea #3\n\n"
        "🎯 Creators (remix inspiration)\n"
        "• \"follow justin welsh on linkedin\" → track him\n"
        "• \"track @greg_isenberg on x\" → track him\n"
        "• \"who do you follow?\" → list tracked\n"
        "• \"unfollow @greg_isenberg\" → stop tracking\n\n"
        "⚙ \"status\" → system check"
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


async def do_follow(platform: str, handle: str, send) -> None:
    _, was_new = db.add_creator(platform=platform, handle=handle)
    if was_new:
        await send(
            f"✓ following @{handle} on {platform}. Their recent posts will be "
            "scraped on the next discovery refresh and fed into the generator "
            "as remix inspiration."
        )
    else:
        await send(f"Already following @{handle} on {platform}.")
    if not config.APIFY_ENABLED:
        await send(
            "⚠ Heads-up: Apify is off, so I can't actually scrape their posts yet. "
            "Set APIFY_ENABLED=true and APIFY_TOKEN in Railway to activate scraping."
        )


async def do_unfollow(platform: str, handle: str, send) -> None:
    removed = db.remove_creator(platform=platform, handle=handle)
    if removed:
        await send(f"Stopped following @{handle} on {platform}.")
    else:
        await send(f"Wasn't tracking @{handle} on {platform} anyway.")


async def do_list_creators(send) -> None:
    rows = db.list_creators()
    if not rows:
        await send(
            "Not tracking anyone yet. Try: \"follow justin welsh on linkedin\""
        )
        return
    by_platform: dict[str, list[str]] = {}
    for r in rows:
        last = r["last_scraped_at"]
        last_str = f" (last scraped {last[:10]})" if last else " (never scraped)"
        by_platform.setdefault(r["platform"], []).append(f"@{r['handle']}{last_str}")
    lines = []
    for platform in sorted(by_platform.keys()):
        lines.append(f"\n{platform.upper()}:")
        for h in by_platform[platform]:
            lines.append(f"  • {h}")
    await send("Tracked creators:" + "\n".join(lines))


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
    elif intent.name == "follow" and intent.platform and intent.handle:
        await do_follow(intent.platform, intent.handle, send)
    elif intent.name == "unfollow" and intent.platform and intent.handle:
        await do_unfollow(intent.platform, intent.handle, send)
    elif intent.name == "creators":
        await do_list_creators(send)
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
