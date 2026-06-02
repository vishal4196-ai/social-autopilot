import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config

# Sources that are AI-generated (lower priority than human-supplied ideas).
AGENT_SOURCES = ("research_agent",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'telegram',
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | used | skipped
    created_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id INTEGER,
    platform TEXT NOT NULL,                 -- linkedin | x
    text TEXT NOT NULL,
    cta_url TEXT NOT NULL,
    postsyncer_post_id TEXT,
    status TEXT NOT NULL,                   -- generated | scheduled | failed
    error TEXT,
    scheduled_for TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (idea_id) REFERENCES ideas(id)
);

CREATE TABLE IF NOT EXISTS viral_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    author TEXT,
    text TEXT NOT NULL,
    engagement INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    source_creator TEXT,                    -- handle if from tracked creator, NULL if keyword-scraped
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS creators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,                 -- linkedin | x
    handle TEXT NOT NULL,                   -- lowercased username (no @, no URL)
    display_name TEXT,                      -- optional, for nicer listing
    added_at TEXT NOT NULL,
    last_scraped_at TEXT,
    UNIQUE(platform, handle)
);

CREATE TABLE IF NOT EXISTS research_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT,
    themes TEXT,            -- JSON array
    hooks TEXT,             -- JSON array
    gaps TEXT,              -- JSON array
    questions TEXT,         -- JSON array
    raw TEXT,               -- full JSON blob
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status, created_at);
CREATE INDEX IF NOT EXISTS idx_samples_fetched ON viral_samples(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_samples_creator ON viral_samples(source_creator);
CREATE INDEX IF NOT EXISTS idx_briefs_created ON research_briefs(created_at DESC);
"""

# Lightweight migration: add columns if upgrading an older DB. SQLite has no
# IF NOT EXISTS for ALTER, so we check the column list first.
_MIGRATIONS = [
    ("viral_samples", "source_creator", "TEXT"),
    ("ideas", "score", "REAL"),
    ("ideas", "meta", "TEXT"),      # JSON: rationale, format, pain_point, platform_fit
]


def _ensure_dir() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _apply_migrations(conn) -> None:
    for table, column, col_type in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init() -> None:
    with get_conn() as c:
        c.executescript(SCHEMA)
        _apply_migrations(c)


# ── Ideas ─────────────────────────────────────────────────
def add_idea(
    text: str,
    source: str = "telegram",
    score: float | None = None,
    meta: dict | None = None,
) -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO ideas (text, source, score, meta, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                text,
                source,
                score,
                json.dumps(meta) if meta else None,
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def next_queued_idea() -> sqlite3.Row | None:
    """Human-supplied ideas (telegram/web/url_remix) take priority over
    AI-generated ones; within each tier, oldest first."""
    with get_conn() as c:
        return c.execute(
            """
            SELECT * FROM ideas
            WHERE status = 'queued'
            ORDER BY
                CASE WHEN source = 'research_agent' THEN 1 ELSE 0 END ASC,
                created_at ASC
            LIMIT 1
            """
        ).fetchone()


def mark_idea_used(idea_id: int) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE ideas SET status = 'used', used_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), idea_id),
        )


def skip_idea(idea_id: int) -> None:
    with get_conn() as c:
        c.execute("UPDATE ideas SET status = 'skipped' WHERE id = ?", (idea_id,))


def list_queued(limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM ideas WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()


# ── Posts ─────────────────────────────────────────────────
def log_post(
    *,
    idea_id: int | None,
    platform: str,
    text: str,
    cta_url: str,
    status: str,
    postsyncer_post_id: str | None = None,
    scheduled_for: str | None = None,
    error: str | None = None,
) -> int:
    with get_conn() as c:
        cur = c.execute(
            """
            INSERT INTO posts
                (idea_id, platform, text, cta_url, postsyncer_post_id,
                 status, error, scheduled_for, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idea_id,
                platform,
                text,
                cta_url,
                postsyncer_post_id,
                status,
                error,
                scheduled_for,
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def recent_posts(limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


# ── Viral samples (keyword-scraped or creator-scraped) ────
def save_viral_samples(rows: list[dict]) -> int:
    """Each row may include source_creator (handle) — NULL if keyword-scraped."""
    if not rows:
        return 0
    now = datetime.utcnow().isoformat()
    with get_conn() as c:
        c.executemany(
            """
            INSERT INTO viral_samples
                (platform, author, text, engagement, url, source_creator, fetched_at)
            VALUES (:platform, :author, :text, :engagement, :url, :source_creator, :fetched_at)
            """,
            [{"source_creator": None, **r, "fetched_at": now} for r in rows],
        )
        return len(rows)


def recent_viral(platform: str, limit: int = 8) -> list[sqlite3.Row]:
    """Keyword-scraped trending posts (creators excluded)."""
    with get_conn() as c:
        return c.execute(
            """
            SELECT * FROM viral_samples
            WHERE platform = ? AND source_creator IS NULL
            ORDER BY engagement DESC, fetched_at DESC
            LIMIT ?
            """,
            (platform, limit),
        ).fetchall()


def recent_creator_posts(platform: str, limit: int = 8) -> list[sqlite3.Row]:
    """Posts scraped from tracked creators on this platform, newest first."""
    with get_conn() as c:
        return c.execute(
            """
            SELECT * FROM viral_samples
            WHERE platform = ? AND source_creator IS NOT NULL
            ORDER BY fetched_at DESC, engagement DESC
            LIMIT ?
            """,
            (platform, limit),
        ).fetchall()


# ── Tracked creators ──────────────────────────────────────
def add_creator(platform: str, handle: str, display_name: str | None = None) -> tuple[int, bool]:
    """Returns (id, was_new). was_new=False if creator already tracked."""
    handle = handle.strip().lstrip("@").lower()
    platform = platform.strip().lower()
    with get_conn() as c:
        existing = c.execute(
            "SELECT id FROM creators WHERE platform = ? AND handle = ?",
            (platform, handle),
        ).fetchone()
        if existing:
            return existing["id"], False
        cur = c.execute(
            """
            INSERT INTO creators (platform, handle, display_name, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (platform, handle, display_name, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid, True


def remove_creator(platform: str, handle: str) -> bool:
    handle = handle.strip().lstrip("@").lower()
    platform = platform.strip().lower()
    with get_conn() as c:
        cur = c.execute(
            "DELETE FROM creators WHERE platform = ? AND handle = ?",
            (platform, handle),
        )
        return cur.rowcount > 0


def list_creators() -> list[sqlite3.Row]:
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM creators ORDER BY platform, handle"
        ).fetchall()


def mark_creator_scraped(creator_id: int) -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE creators SET last_scraped_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), creator_id),
        )


# ── Research briefs ───────────────────────────────────────
def save_research_brief(parsed: dict) -> int:
    with get_conn() as c:
        cur = c.execute(
            """
            INSERT INTO research_briefs
                (summary, themes, hooks, gaps, questions, raw, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed.get("summary", ""),
                json.dumps(parsed.get("themes", [])),
                json.dumps(parsed.get("hooks_working", [])),
                json.dumps(parsed.get("content_gaps", [])),
                json.dumps(parsed.get("audience_questions", [])),
                json.dumps(parsed),
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def latest_research_brief() -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM research_briefs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "summary": row["summary"],
        "themes": json.loads(row["themes"] or "[]"),
        "hooks": json.loads(row["hooks"] or "[]"),
        "gaps": json.loads(row["gaps"] or "[]"),
        "questions": json.loads(row["questions"] or "[]"),
        "created_at": row["created_at"],
    }


def recent_cycles_with_cta(cycles: int = 5) -> int:
    """How many of the most recent post cycles included a CTA on any platform.
    A 'cycle' = posts sharing an idea_id (LinkedIn+X+Threads from the same idea).
    Used to enforce a hard 1-in-N CTA budget — once we've hit the cap, the
    generator strips {{CTA}} from outputs regardless of what Claude wants.
    """
    # Pull recent posts; group by idea_id; count idea_ids where any post had cta_url.
    with get_conn() as c:
        rows = c.execute(
            "SELECT idea_id, cta_url FROM posts ORDER BY created_at DESC LIMIT ?",
            (cycles * 4,),  # over-fetch so we get at least N distinct cycles
        ).fetchall()
    seen_cycles: list[int] = []
    cycles_with_cta: set[int] = set()
    for r in rows:
        iid = r["idea_id"]
        if iid is None:
            continue
        if iid not in seen_cycles:
            seen_cycles.append(iid)
        if (r["cta_url"] or "").strip():
            cycles_with_cta.add(iid)
        if len(seen_cycles) >= cycles:
            break
    return len([c for c in seen_cycles if c in cycles_with_cta])


def recent_idea_texts(limit: int = 10) -> list[str]:
    """Recent idea + post text, so the ideator avoids repeating angles."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT text FROM ideas ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [r["text"] for r in rows]
