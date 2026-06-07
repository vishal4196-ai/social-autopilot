"""Telegram bot — conversational agent backed by Claude with tool use.

Every text message goes through `conversation.chat()`. The agent decides
what to do (call a tool, just answer, ask clarifying question). Slash
commands kept as a deterministic escape hatch for power users.
"""
import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db
from .content import conversation

log = logging.getLogger(__name__)


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == config.TELEGRAM_ALLOWED_USER_ID)


# ── Slash command fast-path (deterministic, no Claude call) ──

async def cmd_reset(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    conversation.reset(update.effective_user.id)
    await update.message.reply_text("Fresh chat. What's up?")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "Just text me normally — I'm a real conversation, not commands.\n\n"
        "Drop ideas. Ask what's in the pipeline. Say 'give me ideas' when you're "
        "stuck. Paste a LinkedIn or X URL to remix it. Say 'post now' to fire "
        "the top approved one.\n\n"
        "If you want to clear our chat memory: /reset"
    )


# ── Main message handler — conversational ──

async def on_message(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    # Show typing while Claude thinks (renews automatically every ~5s in client)
    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(
            None,
            lambda: conversation.chat(update.effective_user.id, text),
        )
    except Exception as e:
        log.exception("chat threw")
        reply = f"(Something errored on my end: {str(e)[:200]})"

    # Telegram caps a single message at 4096 chars
    if len(reply) > 4000:
        reply = reply[:3990] + "\n…(truncated)"
    await update.message.reply_text(reply)


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    # Slash commands first so they take precedence
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    # Everything else: conversational agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app
