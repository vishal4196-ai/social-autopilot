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


class PostsyncerError(RuntimeError):
    pass


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
    raise ValueError(f"Unknown platform: {platform}")


def publish(
    *,
    platform: str,
    text: str,
    schedule_for: dict | None = None,
) -> dict:
    """Schedule (or publish-now if schedule_for omitted) a single post.

    schedule_for shape: {"date": "2026-07-04", "time": "13:00", "timezone": "America/Toronto"}
    """
    account_id = _platform_to_account_id(platform)
    if not account_id:
        raise PostsyncerError(
            f"No Postsyncer account ID configured for platform={platform}. "
            f"Connect the account in the Postsyncer dashboard and set the env var."
        )

    body: dict = {
        "workspace_id": config.POSTSYNCER_WORKSPACE_ID,
        "content": [{"text": text}],
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
