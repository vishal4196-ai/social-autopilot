import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config

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
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status, created_at);
CREATE INDEX IF NOT EXISTS idx_samples_fetched ON viral_samples(fetched_at DESC);
"""


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


def init() -> None:
    with get_conn() as c:
        c.executescript(SCHEMA)


# ── Ideas ─────────────────────────────────────────────────
def add_idea(text: str, source: str = "telegram") -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO ideas (text, source, created_at) VALUES (?, ?, ?)",
            (text, source, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def next_queued_idea() -> sqlite3.Row | None:
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM ideas WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
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


# ── Viral samples ─────────────────────────────────────────
def save_viral_samples(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = datetime.utcnow().isoformat()
    with get_conn() as c:
        c.executemany(
            """
            INSERT INTO viral_samples (platform, author, text, engagement, url, fetched_at)
            VALUES (:platform, :author, :text, :engagement, :url, :fetched_at)
            """,
            [{**r, "fetched_at": now} for r in rows],
        )
        return len(rows)


def recent_viral(platform: str, limit: int = 8) -> list[sqlite3.Row]:
    with get_conn() as c:
        return c.execute(
            """
            SELECT * FROM viral_samples
            WHERE platform = ?
            ORDER BY engagement DESC, fetched_at DESC
            LIMIT ?
            """,
            (platform, limit),
        ).fetchall()
