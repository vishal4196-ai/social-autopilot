"""Print Postsyncer workspace + account IDs in `.env`-ready form.

Run AFTER you've connected your LinkedIn + X accounts in the Postsyncer
dashboard and set POSTSYNCER_API_KEY in .env.

    python -m scripts.bootstrap_postsyncer
"""
from __future__ import annotations

import sys

import requests

from src import config

BASE = "https://postsyncer.com/api/v1"
PLATFORM_TO_ENV = {
    "linkedin": "POSTSYNCER_LINKEDIN_ACCOUNT_ID",
    "twitter":  "POSTSYNCER_X_ACCOUNT_ID",  # Postsyncer calls X "twitter"
}


def main() -> int:
    r = requests.get(
        f"{BASE}/workspaces",
        headers={"Authorization": f"Bearer {config.POSTSYNCER_API_KEY}"},
        timeout=20,
    )
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}")
        return 1

    # Postsyncer's live response is a bare array; docs show {data: [...]}. Handle both.
    body = r.json()
    workspaces = body.get("data", []) if isinstance(body, dict) else body
    if not workspaces:
        print("No workspaces found on this API key.")
        return 1

    print(f"Found {len(workspaces)} workspace(s):\n")
    for w in workspaces:
        print(f"── workspace: {w.get('name')} ──")
        print(f"  POSTSYNCER_WORKSPACE_ID={w['id']}")
        accounts = w.get("accounts") or []
        if not accounts:
            print("  (no connected social accounts — connect LinkedIn + X "
                  "in the Postsyncer dashboard first)")
            continue
        for a in accounts:
            platform = (a.get("platform") or "").lower()
            env_name = PLATFORM_TO_ENV.get(platform)
            label = f"{platform} @{a.get('username') or a.get('name')}"
            if env_name:
                print(f"  {env_name}={a['id']}   # {label}")
            else:
                print(f"  # {label} (platform={platform}) account_id={a['id']}")
        print()
    print("Paste the lines above into your .env file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
