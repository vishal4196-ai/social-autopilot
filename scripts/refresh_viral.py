"""Manual viral discovery run.

Usage:
    python -m scripts.refresh_viral
"""
import logging

from src import db
from src.content import viral_discovery

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

if __name__ == "__main__":
    db.init()
    result = viral_discovery.refresh()
    print(result)
