"""Entry point: Telegram bot (long-polling) + APScheduler + FastAPI web app
in one asyncio loop.

Telegram uses long-polling so no inbound port is needed for it. The web app
binds to 0.0.0.0:$PORT so Railway can route the *.up.railway.app domain to it.
"""
import asyncio
import logging
import signal

import uvicorn

from . import config, db, scheduler, telegram_bot
from .web.app import create_app
from .web import auth as web_auth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("autopilot")


async def amain() -> None:
    db.init()
    log.info("DB ready at %s", config.DB_PATH)

    sched = scheduler.build_scheduler()
    sched.start()
    log.info("Scheduler started")

    # Telegram bot (long-polling — no port needed)
    tg_app = telegram_bot.build_app()
    log.info("Telegram bot starting")
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    # So the web layer can DM magic-link logins to Vishal
    web_auth.register_telegram_app(tg_app)

    # Web app (uvicorn) — binds the public port
    web_app = create_app()
    uconfig = uvicorn.Config(
        app=web_app,
        host="0.0.0.0",
        port=config.PORT,
        log_level="info",
        # Behind Railway's edge proxy — we need request.url to reflect the
        # public HTTPS host so login links and OAuth-like flows resolve right.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(uconfig)
    log.info("Web app on 0.0.0.0:%d (open the Railway domain)", config.PORT)

    # On Linux, install graceful shutdown handlers. Windows just relies on KeyboardInterrupt.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, server.handle_exit, sig, None)
        except NotImplementedError:
            pass

    try:
        await server.serve()
    finally:
        log.info("stopping…")
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            log.exception("telegram shutdown errored")
        sched.shutdown(wait=False)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
