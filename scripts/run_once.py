"""Manual test runner — fires a single post cycle right now.

Usage:
    python -m scripts.run_once

Use this to:
- Verify Claude generation works
- Verify Postsyncer credentials work
- Smoke-test before letting the scheduler run unattended
"""
import logging

from src import db, scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

if __name__ == "__main__":
    db.init()
    scheduler.run_post_cycle()
