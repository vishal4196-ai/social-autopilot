"""Pull recent posts from LinkedIn + X via Apify actors.

TWO modes:
1. Keyword search — generic niche trends ("AI automation agency", "n8n", etc.)
2. Per-creator — recent posts from accounts the owner explicitly follows.

Generator treats them as separate signals: trends = "what's working broadly",
creators = "voices Vishal wants to remix into our niche".
"""
import logging
import time

import requests

from .. import config, db

log = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"


def _run_actor_sync(actor: str, payload: dict, timeout: int = 180) -> list[dict]:
    """Start an actor and block until results land. Returns dataset items."""
    actor_path = actor.replace("/", "~")
    url = f"{APIFY_BASE}/acts/{actor_path}/run-sync-get-dataset-items"
    params = {"token": config.APIFY_TOKEN, "timeout": timeout}
    r = requests.post(url, params=params, json=payload, timeout=timeout + 10)
    r.raise_for_status()
    return r.json()


# ── Mode 1: keyword-based trending ────────────────────────

def fetch_linkedin_viral() -> list[dict]:
    keywords = config.BRAND_CONFIG["viral_discovery"]["linkedin_keywords"]
    max_samples = config.BRAND_CONFIG["viral_discovery"]["max_samples_per_run"]
    min_engagement = config.BRAND_CONFIG["viral_discovery"]["min_engagement"]

    samples: list[dict] = []
    for kw in keywords:
        try:
            items = _run_actor_sync(
                config.APIFY_LINKEDIN_ACTOR,
                {"keywords": kw, "limit": max_samples, "sortBy": "engagement"},
            )
        except Exception as e:
            log.warning("LinkedIn actor failed for %r: %s", kw, e)
            continue

        for it in items:
            text = (it.get("text") or it.get("content") or "").strip()
            if not text:
                continue
            likes = int(it.get("likes") or it.get("numLikes") or 0)
            comments = int(it.get("comments") or it.get("numComments") or 0)
            engagement = likes + comments
            if engagement < min_engagement:
                continue
            samples.append({
                "platform": "linkedin",
                "author": (it.get("author") or it.get("authorName") or "")[:200],
                "text": text[:4000],
                "engagement": engagement,
                "url": it.get("url") or it.get("postUrl") or "",
            })
        time.sleep(1)
    return samples


def fetch_x_viral() -> list[dict]:
    keywords = config.BRAND_CONFIG["viral_discovery"]["x_keywords"]
    max_samples = config.BRAND_CONFIG["viral_discovery"]["max_samples_per_run"]
    min_engagement = config.BRAND_CONFIG["viral_discovery"]["min_engagement"]

    samples: list[dict] = []
    for kw in keywords:
        try:
            items = _run_actor_sync(
                config.APIFY_X_ACTOR,
                {"searchTerms": [kw], "maxItems": max_samples, "sort": "Top"},
            )
        except Exception as e:
            log.warning("X actor failed for %r: %s", kw, e)
            continue

        for it in items:
            text = (it.get("text") or it.get("full_text") or "").strip()
            if not text:
                continue
            likes = int(it.get("likeCount") or it.get("favorite_count") or 0)
            replies = int(it.get("replyCount") or it.get("reply_count") or 0)
            reposts = int(it.get("retweetCount") or it.get("retweet_count") or 0)
            engagement = likes + replies + reposts
            if engagement < min_engagement:
                continue
            samples.append({
                "platform": "x",
                "author": (it.get("author", {}).get("userName")
                           or it.get("user", {}).get("screen_name")
                           or "")[:200],
                "text": text[:4000],
                "engagement": engagement,
                "url": it.get("url") or it.get("twitterUrl") or "",
            })
        time.sleep(1)
    return samples


# ── Mode 2: per-creator scraping ──────────────────────────

def _fetch_linkedin_creator(handle: str) -> list[dict]:
    """One creator → up to N recent posts."""
    max_posts = int(config.BRAND_CONFIG["viral_discovery"].get("max_posts_per_creator", 5))
    profile_url = f"https://www.linkedin.com/in/{handle}/"
    # Different actors accept different input shapes — pass several common keys
    # so swapping APIFY_LINKEDIN_CREATOR_ACTOR doesn't require code changes.
    payload = {
        "urls": [profile_url],
        "profileUrls": [profile_url],
        "usernames": [handle],
        "limit": max_posts,
        "postsPerProfile": max_posts,
    }
    try:
        items = _run_actor_sync(config.APIFY_LINKEDIN_CREATOR_ACTOR, payload)
    except Exception as e:
        log.warning("LinkedIn creator actor failed for %s: %s", handle, e)
        return []

    out = []
    for it in items[:max_posts]:
        text = (it.get("text") or it.get("content") or it.get("commentary") or "").strip()
        if not text:
            continue
        likes = int(it.get("likes") or it.get("numLikes") or it.get("reactionsCount") or 0)
        comments = int(it.get("comments") or it.get("numComments") or it.get("commentsCount") or 0)
        out.append({
            "platform": "linkedin",
            "author": handle,
            "text": text[:4000],
            "engagement": likes + comments,
            "url": it.get("url") or it.get("postUrl") or "",
            "source_creator": handle,
        })
    return out


def _fetch_x_creator(handle: str) -> list[dict]:
    max_posts = int(config.BRAND_CONFIG["viral_discovery"].get("max_posts_per_creator", 5))
    payload = {
        "usernames": [handle],
        "twitterHandles": [handle],
        "handles": [handle],
        "maxItems": max_posts,
        "tweetsDesired": max_posts,
    }
    try:
        items = _run_actor_sync(config.APIFY_X_CREATOR_ACTOR, payload)
    except Exception as e:
        log.warning("X creator actor failed for %s: %s", handle, e)
        return []

    out = []
    for it in items[:max_posts]:
        text = (it.get("text") or it.get("full_text") or "").strip()
        if not text:
            continue
        likes = int(it.get("likeCount") or it.get("favorite_count") or 0)
        replies = int(it.get("replyCount") or it.get("reply_count") or 0)
        reposts = int(it.get("retweetCount") or it.get("retweet_count") or 0)
        out.append({
            "platform": "x",
            "author": handle,
            "text": text[:4000],
            "engagement": likes + replies + reposts,
            "url": it.get("url") or it.get("twitterUrl") or "",
            "source_creator": handle,
        })
    return out


def fetch_all_creators() -> list[dict]:
    """Iterate over every tracked creator across platforms."""
    creators = db.list_creators()
    if not creators:
        return []
    samples: list[dict] = []
    for c in creators:
        platform = c["platform"]
        handle = c["handle"]
        if platform == "linkedin":
            rows = _fetch_linkedin_creator(handle)
        elif platform == "x":
            rows = _fetch_x_creator(handle)
        else:
            log.warning("Unknown platform %r for creator %s", platform, handle)
            continue
        if rows:
            samples.extend(rows)
            db.mark_creator_scraped(c["id"])
        time.sleep(1)  # be polite to Apify
    return samples


# ── Entry point used by scheduler ─────────────────────────

def refresh() -> dict:
    """Run both modes; persist results. Safe to call ad-hoc."""
    if not config.APIFY_ENABLED:
        log.info("Apify disabled — skipping discovery")
        return {"linkedin_trending": 0, "x_trending": 0, "creator_posts": 0, "skipped": True}

    li = fetch_linkedin_viral()
    x = fetch_x_viral()
    creators = fetch_all_creators()
    db.save_viral_samples(li + x + creators)
    summary = {
        "linkedin_trending": len(li),
        "x_trending": len(x),
        "creator_posts": len(creators),
        "skipped": False,
    }
    log.info("Discovery: %s", summary)
    return summary
