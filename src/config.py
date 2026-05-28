import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val or ""


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Claude
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", required=True)
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-opus-4-7")

# Postsyncer (unified LinkedIn + X)
POSTSYNCER_API_KEY = _env("POSTSYNCER_API_KEY", required=True)
POSTSYNCER_WORKSPACE_ID = int(_env("POSTSYNCER_WORKSPACE_ID", "0", required=True))
POSTSYNCER_LINKEDIN_ACCOUNT_ID = int(_env("POSTSYNCER_LINKEDIN_ACCOUNT_ID", "0"))
POSTSYNCER_X_ACCOUNT_ID = int(_env("POSTSYNCER_X_ACCOUNT_ID", "0"))

# Telegram
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_ALLOWED_USER_ID = int(_env("TELEGRAM_ALLOWED_USER_ID", "0", required=True))

# GHL CTA
CTA_URL = _env("CTA_URL", required=True)
CTA_LABEL = _env("CTA_LABEL", "Book a free call")

# Apify
APIFY_ENABLED = _bool("APIFY_ENABLED", False)
APIFY_TOKEN = _env("APIFY_TOKEN")
APIFY_LINKEDIN_ACTOR = _env("APIFY_LINKEDIN_ACTOR", "scarletapi/linkedin-viral-posts-finder")
APIFY_X_ACTOR = _env("APIFY_X_ACTOR", "apidojo/twitter-scraper-lite")
# Per-creator scrapers (different actors, profile-based input):
APIFY_LINKEDIN_CREATOR_ACTOR = _env("APIFY_LINKEDIN_CREATOR_ACTOR", "apimaestro/linkedin-profile-posts")
APIFY_X_CREATOR_ACTOR = _env("APIFY_X_CREATOR_ACTOR", "apidojo/twitter-user-scraper")
# Single-post-by-URL scrapers (for the "paste a URL to remix" flow):
APIFY_LINKEDIN_POST_ACTOR = _env("APIFY_LINKEDIN_POST_ACTOR", "apimaestro/linkedin-post-detail")
APIFY_X_POST_ACTOR = _env("APIFY_X_POST_ACTOR", "apidojo/twitter-scraper-lite")

# Schedule
POST_TIMES = [t.strip() for t in _env("POST_TIMES", "09:00,13:00,18:00").split(",") if t.strip()]
TIMEZONE = _env("TIMEZONE", "America/Toronto")

# Web server
PORT = int(_env("PORT", "8080"))
WEB_SESSION_SECRET = _env(
    "WEB_SESSION_SECRET",
    "change-me-set-a-long-random-string-in-env",
)
WEB_USERNAME = _env("WEB_USERNAME", "vishal")
WEB_PASSWORD = _env("WEB_PASSWORD", "change-me-set-in-env")

# Storage
DB_PATH = _env("DB_PATH", str(ROOT / "autopilot.db"))

# Brand config from YAML
with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
    BRAND_CONFIG: dict = yaml.safe_load(f)
