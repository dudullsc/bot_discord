"""PostgreSQL persistence shared by the Discord bot and the web panel."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from dotenv import load_dotenv
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# backend/bot/db.py → project root /.env
ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ROOT_ENV)

_pool: ConnectionPool | None = None


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "music").strip() or "music"
    password = os.getenv("POSTGRES_PASSWORD", "music").strip() or "music"
    host = os.getenv("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("POSTGRES_PORT", "5432").strip() or "5432"
    name = os.getenv("POSTGRES_DB", "music").strip() or "music"
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=database_url(),
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        ensure_schema()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connect() -> Iterator[Connection]:
    with get_pool().connection() as conn:
        yield conn
        conn.commit()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ensure_schema() -> None:
    with Connection.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_runtime (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    bot_user_id TEXT,
                    bot_username TEXT,
                    started_at TIMESTAMPTZ,
                    last_heartbeat TIMESTAMPTZ,
                    online BOOLEAN NOT NULL DEFAULT FALSE,
                    guild_count INTEGER NOT NULL DEFAULT 0
                );

                INSERT INTO bot_runtime (id, online, guild_count)
                VALUES (1, FALSE, 0)
                ON CONFLICT (id) DO NOTHING;

                CREATE TABLE IF NOT EXISTS guilds (
                    guild_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    member_count INTEGER,
                    icon_url TEXT,
                    joined_at TIMESTAMPTZ,
                    last_seen TIMESTAMPTZ NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                );
                """
            )
        conn.commit()


def mark_online(*, bot_user_id: str, bot_username: str, guild_count: int) -> None:
    """Called when the bot process becomes ready — starts a new uptime session."""
    now = _utc_now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_runtime
                SET bot_user_id = %s,
                    bot_username = %s,
                    started_at = %s,
                    last_heartbeat = %s,
                    online = TRUE,
                    guild_count = %s
                WHERE id = 1
                """,
                (bot_user_id, bot_username, now, now, guild_count),
            )


def heartbeat(*, guild_count: int) -> None:
    now = _utc_now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_runtime
                SET last_heartbeat = %s, online = TRUE, guild_count = %s
                WHERE id = 1
                """,
                (now, guild_count),
            )


def mark_offline() -> None:
    now = _utc_now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_runtime
                SET online = FALSE, last_heartbeat = %s
                WHERE id = 1
                """,
                (now,),
            )


def upsert_guild(
    *,
    guild_id: str,
    name: str,
    member_count: int | None = None,
    icon_url: str | None = None,
    joined_at: datetime | None = None,
) -> None:
    now = _utc_now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO guilds (guild_id, name, member_count, icon_url, joined_at, last_seen, active)
                VALUES (%s, %s, %s, %s, COALESCE(%s, %s), %s, TRUE)
                ON CONFLICT (guild_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    member_count = EXCLUDED.member_count,
                    icon_url = COALESCE(EXCLUDED.icon_url, guilds.icon_url),
                    joined_at = COALESCE(guilds.joined_at, EXCLUDED.joined_at),
                    last_seen = EXCLUDED.last_seen,
                    active = TRUE
                """,
                (guild_id, name, member_count, icon_url, joined_at, now, now),
            )


def deactivate_guild(guild_id: str) -> None:
    now = _utc_now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE guilds
                SET active = FALSE, last_seen = %s
                WHERE guild_id = %s
                """,
                (now, guild_id),
            )


def sync_guilds(guilds: list[dict[str, Any]]) -> None:
    now = _utc_now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE guilds SET active = FALSE, last_seen = %s",
                (now,),
            )
            for guild in guilds:
                cur.execute(
                    """
                    INSERT INTO guilds (guild_id, name, member_count, icon_url, joined_at, last_seen, active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (guild_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        member_count = EXCLUDED.member_count,
                        icon_url = COALESCE(EXCLUDED.icon_url, guilds.icon_url),
                        joined_at = COALESCE(guilds.joined_at, EXCLUDED.joined_at),
                        last_seen = EXCLUDED.last_seen,
                        active = TRUE
                    """,
                    (
                        guild["guild_id"],
                        guild["name"],
                        guild.get("member_count"),
                        guild.get("icon_url"),
                        now,
                        now,
                    ),
                )
            cur.execute(
                "UPDATE bot_runtime SET guild_count = %s WHERE id = 1",
                (len(guilds),),
            )


def get_status(*, stale_after_seconds: int = 90) -> dict[str, Any]:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bot_runtime WHERE id = 1")
            runtime = cur.fetchone()
            cur.execute(
                """
                SELECT guild_id, name, member_count, icon_url, joined_at, last_seen, active
                FROM guilds
                WHERE active = TRUE
                ORDER BY name ASC
                """
            )
            guilds = cur.fetchall()

    online = False
    uptime_seconds = 0
    started_at = None
    last_heartbeat = None
    bot_username = None
    bot_user_id = None
    guild_count = 0

    if runtime:
        bot_username = runtime["bot_username"]
        bot_user_id = runtime["bot_user_id"]
        started_at = runtime["started_at"]
        last_heartbeat = runtime["last_heartbeat"]
        guild_count = runtime["guild_count"] or 0

        if runtime["online"] and last_heartbeat:
            hb = last_heartbeat
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - hb).total_seconds()
            online = age <= stale_after_seconds

        if online and started_at:
            start = started_at
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            uptime_seconds = max(
                0, int((datetime.now(timezone.utc) - start).total_seconds())
            )

    def _iso(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        return str(value)

    return {
        "online": online,
        "bot_username": bot_username,
        "bot_user_id": bot_user_id,
        "started_at": _iso(started_at),
        "last_heartbeat": _iso(last_heartbeat),
        "uptime_seconds": uptime_seconds,
        "guild_count": guild_count if online else len(guilds),
        "database": _safe_db_label(),
        "guilds": [
            {
                "guild_id": g["guild_id"],
                "name": g["name"],
                "member_count": g["member_count"],
                "icon_url": g["icon_url"],
                "joined_at": _iso(g["joined_at"]),
                "last_seen": _iso(g["last_seen"]),
                "active": g["active"],
            }
            for g in guilds
        ],
    }


def _safe_db_label() -> str:
    parsed = urlparse(database_url())
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    db = (parsed.path or "/music").lstrip("/") or "music"
    return f"{host}:{port}/{db}"


def format_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    if not days and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts)
