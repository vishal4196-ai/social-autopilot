"""Postsyncer API client.

Docs: https://docs.postsyncer.com
Endpoint: POST https://postsyncer.com/api/v1/posts
Auth:    Authorization: Bearer <API_KEY>
"""
from __future__ import annotations

import logging

import requests
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .. import config

log = logging.getLogger(__name__)

BASE = "https://postsyncer.com/api/v1"

# Per-tweet ceiling for X (Postsyncer threads). We use 270 to leave headroom
# for any URL t.co rendering and the (1/n) numbering if Claude adds it.
X_PART_MAX = 270
# Threads (Meta) hard cap is 500.
THREADS_PART_MAX = 480


class PostsyncerError(RuntimeError):
    pass


def _split_into_thread_parts(text: str, max_per: int) -> list[str]:
    """Split text into thread-sized parts at sentence boundaries.

    Returns [text] if it already fits in one part. Otherwise splits at the
    best sentence boundary (or word boundary as fallback), preserving
    paragraph breaks. Never cuts mid-word.
    """
    text = (text or "").strip()
    if len(text) <= max_per:
        return [text] if text else []

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_per:
            parts.append(remaining)
            break

        head = remaining[:max_per]
        # Best: a sentence terminator (and the trailing whitespace) reasonably late in the head
        best = -1
        for marker in ('. ', '! ', '? ', '.\n', '!\n', '?\n', '\n\n'):
            idx = head.rfind(marker)
            if idx > best:
                best = idx + 1   # keep the punctuation, drop the trailing ws
        if best > max_per // 2:
            cut = best
        else:
            # Fallback: last word boundary
            cut = head.rfind(' ')
            if cut < max_per // 3:
                cut = max_per  # hard cut, very rare

        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [p for p in parts if p]


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type(requests.RequestException),
)
def _post(path: str, body: dict) -> dict:
    r = requests.post(
        f"{BASE}{path}",
        headers={
            "Authorization": f"Bearer {config.POSTSYNCER_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    if r.status_code >= 400:
        log.error("Postsyncer %s -> %d: %s", path, r.status_code, r.text[:500])
        raise PostsyncerError(f"{r.status_code}: {r.text[:300]}")
    return r.json()


def _platform_to_account_id(platform: str) -> int:
    if platform == "linkedin":
        return config.POSTSYNCER_LINKEDIN_ACCOUNT_ID
    if platform == "x":
        return config.POSTSYNCER_X_ACCOUNT_ID
    if platform == "threads":
        return config.POSTSYNCER_THREADS_ACCOUNT_ID
    raise ValueError(f"Unknown platform: {platform}")


def publish(
    *,
    platform: str,
    text: str,
    schedule_for: dict | None = None,
    media_urls: list[str] | None = None,
) -> dict:
    """Schedule (or publish-now if schedule_for omitted) a single post.

    schedule_for shape: {"date": "2026-07-04", "time": "13:00", "timezone": "America/Toronto"}
    media_urls: optional list of public image URLs to attach.
    """
    account_id = _platform_to_account_id(platform)
    if not account_id:
        raise PostsyncerError(
            f"No Postsyncer account ID configured for platform={platform}. "
            f"Connect the account in the Postsyncer dashboard and set the env var."
        )

    # Split into thread parts for X and Threads. LinkedIn stays single-post.
    if platform == "x":
        parts = _split_into_thread_parts(text, max_per=X_PART_MAX)
    elif platform == "threads":
        parts = _split_into_thread_parts(text, max_per=THREADS_PART_MAX)
    else:
        parts = [text] if text else []

    if not parts:
        raise PostsyncerError(f"Empty content for platform={platform}")

    # Each part becomes one post in the thread. Media attaches to part #1 only.
    clean_media = [u for u in (media_urls or []) if u]
    content: list[dict] = []
    for i, part in enumerate(parts):
        item: dict = {"text": part}
        if i == 0 and clean_media:
            item["media"] = clean_media
        content.append(item)

    log.info(
        "Postsyncer publish: platform=%s parts=%d (chars=%s)",
        platform, len(parts), [len(p) for p in parts],
    )

    body: dict = {
        "workspace_id": config.POSTSYNCER_WORKSPACE_ID,
        "content": content,
        "accounts": [{"id": account_id}],
    }
    if schedule_for:
        body["schedule_type"] = "schedule"
        body["schedule_for"] = schedule_for
    else:
        # Postsyncer enum: publish_now | schedule | draft
        body["schedule_type"] = "publish_now"

    try:
        return _post("/posts", body)
    except RetryError as e:
        raise PostsyncerError(str(e)) from e
