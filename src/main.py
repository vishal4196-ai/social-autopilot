"""Entry point: Telegram bot (long-polling) + APScheduler in one asyncio loop.

No web server, no inbound ports — works as a Railway worker.
"""
import asyncio
import logging
import signal

from . import config, db, scheduler, telegram_bot

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

    app = telegram_bot.build_app()
    log.info("Telegram bot starting (long-polling)")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    stop_event = asyncio.Event()

    def _shutdown(*_a) -> None:
        log.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows doesn't support SIGTERM via add_signal_handler;
            # KeyboardInterrupt still works.
            pass

    try:
        await stop_event.wait()
    finally:
        log.info("stopping…")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        sched.shutdown(wait=False)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
