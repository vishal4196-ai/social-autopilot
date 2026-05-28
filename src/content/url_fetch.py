"""Fetch a single LinkedIn / X post by URL via Apify.

Used by the "share a URL to remix" flow — Vishal pastes a post URL into
Telegram and the bot reads the original post so Claude can write our-niche
version inspired by its hook / structure / angle.

Different actors accept different input shapes. We pass several common
keys so swapping the actor in env doesn't require a code change.
"""
from __future__ import annotations

import logging
import re

import requests

from .. import config

log = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"

# Detects LinkedIn + X/Twitter post URLs. Matches whole URL on first hit.
SOCIAL_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:"
    r"linkedin\.com/(?:posts|feed/update|in/[^/\s]+/recent-activity)/[\w\-/?=&%.,_]+"
    r"|"
    r"(?:x|twitter)\.com/[^/\s]+/status/\d+\S*"
    r")",
    re.IGNORECASE,
)


def detect_url(text: str) -> str | None:
    """Return the first social post URL in `text`, or None."""
    if not text:
        return None
    m = SOCIAL_URL_RE.search(text)
    return m.group(0) if m else None


def _platform_for(url: str) -> str | None:
    u = url.lower()
    if "linkedin.com" in u:
        return "linkedin"
    if "x.com" in u or "twitter.com" in u:
        return "x"
    return None


def _run_actor_sync(actor: str, payload: dict, timeout: int = 120) -> list[dict]:
    actor_path = actor.replace("/", "~")
    url = f"{APIFY_BASE}/acts/{actor_path}/run-sync-get-dataset-items"
    params = {"token": config.APIFY_TOKEN, "timeout": timeout}
    r = requests.post(url, params=params, json=payload, timeout=timeout + 10)
    r.raise_for_status()
    return r.json()


def _extract_post_fields(it: dict, platform: str) -> dict | None:
    """Normalise the wildly-varying actor output shapes into one shape."""
    text = (
        it.get("text")
        or it.get("content")
        or it.get("full_text")
        or it.get("commentary")
        or ""
    ).strip()
    if not text:
        return None
    if platform == "linkedin":
        author = (
            it.get("authorName")
            or it.get("author", {}).get("name") if isinstance(it.get("author"), dict) else it.get("author")
            or it.get("authorUsername")
            or "unknown"
        )
        likes = int(it.get("likes") or it.get("numLikes") or it.get("reactionsCount") or 0)
        comments = int(it.get("comments") or it.get("numComments") or it.get("commentsCount") or 0)
        engagement = likes + comments
    else:
        author = (
            (it.get("author") or {}).get("userName") if isinstance(it.get("author"), dict) else None
        ) or (
            (it.get("user") or {}).get("screen_name") if isinstance(it.get("user"), dict) else None
        ) or it.get("authorUsername") or "unknown"
        likes = int(it.get("likeCount") or it.get("favorite_count") or 0)
        replies = int(it.get("replyCount") or it.get("reply_count") or 0)
        reposts = int(it.get("retweetCount") or it.get("retweet_count") or 0)
        engagement = likes + replies + reposts
    return {
        "platform": platform,
        "author": str(author)[:200].lstrip("@"),
        "text": text[:4000],
        "engagement": engagement,
    }


def fetch_single_post(url: str) -> dict | None:
    """Returns {platform, author, text, engagement} or None on failure."""
    if not config.APIFY_ENABLED:
        log.info("Apify disabled — can't fetch single post")
        return None
    platform = _platform_for(url)
    if not platform:
        return None

    # Different actors take different input keys — pass multiple variants.
    if platform == "linkedin":
        actor = config.APIFY_LINKEDIN_POST_ACTOR
        payload = {
            "urls": [url],
            "postUrls": [url],
            "postUrl": url,
            "limit": 1,
        }
    else:
        actor = config.APIFY_X_POST_ACTOR
        payload = {
            "urls": [url],
            "tweetUrls": [url],
            "startUrls": [{"url": url}],
            "maxItems": 1,
        }

    try:
        items = _run_actor_sync(actor, payload)
    except Exception as e:
        log.warning("single-post fetch failed for %s: %s", url, e)
        return None

    for it in items[:5]:  # some actors return adjacent posts too; first match wins
        normalised = _extract_post_fields(it, platform)
        if normalised:
            return normalised
    log.warning("actor returned no usable items for %s", url)
    return None
