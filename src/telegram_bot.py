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
        "• \"unfollow @greg_isenberg\" → stop tracking\n"
        "• \"refresh\" → scrape now (don't wait for morning cron)\n\n"
        "🔬 Research\n"
        "• \"research\" or \"find me ideas\" → research agent scouts the niche + queues fresh killer ideas\n\n"
        "🔗 Paste a post URL → I read it and queue a remix\n"
        "  Just send the LinkedIn or X URL (with optional note: \"this hook is fire — remix it\")\n\n"
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


async def do_remix_url(url: str, extra_context: str | None, send) -> None:
    """Vishal pasted a LinkedIn/X post URL → fetch it, queue a remix idea."""
    if not config.APIFY_ENABLED:
        await send(
            "Apify is off — I can't fetch the post. Paste the post text directly "
            "and I'll remix that, or enable APIFY_ENABLED in Railway."
        )
        return

    from .content import url_fetch
    await send(f"🔍 reading the post at {url[:60]}…")

    loop = asyncio.get_running_loop()
    try:
        post = await loop.run_in_executor(None, url_fetch.fetch_single_post, url)
    except Exception as e:
        log.exception("url_fetch threw")
        post = None
        await send(f"✗ fetch errored: {str(e)[:200]}")
        return

    if not post:
        await send(
            "✗ couldn't read that post (actor returned empty). Paste the post "
            "text here instead and I'll remix it."
        )
        return

    snippet = post["text"][:200].replace("\n", " ")
    framing_lines = [
        "REMIX SOURCE POST — adapt its hook style, format, and angle into our AI-automation niche.",
        f"Source: @{post['author']} on {post['platform']} ({post['engagement']} engagement)",
        f"URL: {url}",
    ]
    if extra_context:
        framing_lines.append(f"Vishal's note about it: {extra_context}")
    framing_lines.append("")
    framing_lines.append("Original post:")
    framing_lines.append(f'"""\n{post["text"]}\n"""')
    framing_lines.append("")
    framing_lines.append(
        "Write our version. Do NOT copy the source's wording or claim its story "
        "as ours. Take what made it work — the angle, the structure, the energy — "
        "and apply it to Vishal's audience (agency owners drowning in repetitive ops)."
    )
    remix_idea = "\n".join(framing_lines)

    idea_id = db.add_idea(remix_idea, source="url_remix")
    await send(
        f"✓ queued (idea #{idea_id}) — remix from @{post['author']}.\n"
        f"Preview: \"{snippet}…\"\n\n"
        f"Say \"post now\" to fire immediately, or it'll go on the next scheduled slot."
    )


async def do_refresh(send) -> None:
    """Trigger an Apify discovery run on demand (creators + viral keywords)."""
    if not config.APIFY_ENABLED:
        await send(
            "Apify is off — set APIFY_ENABLED=true and APIFY_TOKEN in Railway "
            "to enable scraping."
        )
        return
    from .content import viral_discovery
    await send("🔄 scraping creators + viral keywords (10-60 sec)…")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, viral_discovery.refresh)
        await send(
            f"✓ done.\n"
            f"  trending LI: {result['linkedin_trending']}\n"
            f"  trending X: {result['x_trending']}\n"
            f"  creator posts: {result['creator_posts']}"
        )
    except Exception as e:
        log.exception("refresh failed")
        await send(f"✗ refresh failed: {str(e)[:400]}")


async def do_research(send) -> None:
    """Run the research pipeline now and report the brief + queued ideas."""
    await send("🔬 research agent working — scouting the niche + drafting ideas (20-60 sec)…")
    from .agents import orchestrator
    loop = asyncio.get_running_loop()
    try:
        summary = await loop.run_in_executor(
            None, lambda: orchestrator.run_research_pipeline(refresh_signal=True)
        )
    except Exception as e:
        log.exception("research failed")
        await send(f"✗ research failed: {str(e)[:400]}")
        return

    lines = [f"🧠 {summary['brief_summary']}", ""]
    if summary.get("themes"):
        lines.append("Hot themes:")
        lines += [f"• {t}" for t in summary["themes"]]
        lines.append("")
    lines.append(f"Queued {summary['ideas_queued']} of {summary['ideas_generated']} ideas:")
    for i in summary.get("top_ideas", []):
        lines.append(f"  [{i['score']}] {i['hook']}")
    lines.append("")
    lines.append("Say \"post now\" to fire the top one, or review them in the web app.")
    await send("\n".join(lines))


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
    elif intent.name == "refresh":
        await do_refresh(send)
    elif intent.name == "research":
        await do_research(send)
    elif intent.name == "remix_url" and intent.url:
        await do_remix_url(intent.url, intent.extra_context, send)
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
