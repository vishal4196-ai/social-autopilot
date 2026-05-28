"""Pull recent high-engagement posts from LinkedIn + X via Apify actors.

We don't post these — we feed them into the prompt as hook/structure inspiration
so generated posts stay on-trend without copying.
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


def refresh() -> dict:
    """Run discovery for both platforms; persist results. Safe to call ad-hoc."""
    if not config.APIFY_ENABLED:
        log.info("Apify discovery disabled — skipping")
        return {"linkedin": 0, "x": 0, "skipped": True}

    li = fetch_linkedin_viral()
    x = fetch_x_viral()
    db.save_viral_samples(li + x)
    log.info("Viral discovery: %d LinkedIn, %d X", len(li), len(x))
    return {"linkedin": len(li), "x": len(x), "skipped": False}
