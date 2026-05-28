"""Telegram bot for collecting daily content ideas.

Auth: only TELEGRAM_ALLOWED_USER_ID can interact. Anyone else is silently ignored.
Uses long-polling so no public webhook / domain / port binding needed.
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db

log = logging.getLogger(__name__)


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == config.TELEGRAM_ALLOWED_USER_ID)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "Social Autopilot online.\n\n"
        "Send any message — I'll queue it as a post idea.\n\n"
        "Commands:\n"
        "/list — show queued ideas\n"
        "/recent — show last posts I made\n"
        "/skip <id> — drop an idea from the queue\n"
        "/status — system status\n"
        "/post_now — fire a post cycle right now (manual override)"
    )


async def on_idea(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    idea_id = db.add_idea(text, source="telegram")
    await update.message.reply_text(
        f"✓ queued (idea #{idea_id}). Going out on the next scheduled slot."
    )


async def cmd_list(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    rows = db.list_queued(limit=15)
    if not rows:
        await update.message.reply_text("Queue is empty. Viral discovery will drive the next post.")
        return
    lines = [f"#{r['id']} — {r['text'][:80]}" for r in rows]
    await update.message.reply_text("Queued ideas:\n\n" + "\n".join(lines))


async def cmd_recent(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    rows = db.recent_posts(limit=6)
    if not rows:
        await update.message.reply_text("No posts logged yet.")
        return
    lines = []
    for r in rows:
        snippet = r["text"][:120].replace("\n", " ")
        lines.append(f"[{r['platform']}] {r['status']} — {snippet}")
    await update.message.reply_text("Recent posts:\n\n" + "\n\n".join(lines))


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /skip <idea_id>")
        return
    try:
        idea_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("idea_id must be a number")
        return
    db.skip_idea(idea_id)
    await update.message.reply_text(f"Skipped idea #{idea_id}.")


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    queued = len(db.list_queued(limit=100))
    recent = len(db.recent_posts(limit=100))
    msg = (
        f"queued ideas: {queued}\n"
        f"recent posts logged: {recent}\n"
        f"schedule: {', '.join(config.POST_TIMES)} {config.TIMEZONE}\n"
        f"apify viral discovery: {'on' if config.APIFY_ENABLED else 'off'}"
    )
    await update.message.reply_text(msg)


async def cmd_post_now(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual override: fire a single post cycle right now.

    Uses the same logic the scheduler uses — pulls next queued idea if any,
    otherwise falls back to the niche-inspired prompt. Runs synchronously in
    a thread executor so it doesn't block the bot's event loop.
    """
    if not _authorized(update):
        return

    # Imported lazily so a circular import (scheduler -> telegram_bot via main)
    # never bites us at startup.
    from .scheduler import run_post_cycle

    queued = len(db.list_queued(limit=1))
    if queued == 0:
        await update.message.reply_text(
            "⚠ No queued ideas — will use the niche-fallback prompt (Claude may "
            "invent a generic case study). Reply with a real idea first if you "
            "want it grounded in something specific, then resend /post_now.\n\n"
            "Firing now anyway… (5-30 sec)"
        )
    else:
        await update.message.reply_text("🚀 firing post cycle (5-30 sec)…")

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_post_cycle)
        await update.message.reply_text("✓ done. Use /recent to see what posted.")
    except Exception as e:
        log.exception("post_now failed")
        await update.message.reply_text(f"✗ failed: {str(e)[:400]}")


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("post_now", cmd_post_now))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_idea))
    return app
