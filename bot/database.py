"""Persistent storage backed by SQLite.

All database work runs in a worker thread through :func:`asyncio.to_thread` so
the event loop never blocks on disk I/O.  A single connection guarded by a lock
is sufficient for the write volume of a Discord bot and avoids SQLite's
"database is locked" errors.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# Default guild settings.  Every value is stored as text in SQLite and parsed
# through the small typed helpers below.  Keeping the defaults here means a
# fresh database needs no migration step.
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, str] = {
    # Welcome system
    "welcome_channel_id": "0",
    "welcome_message": (
        "Welcome {mention} to **{server}**! :wave:\n"
        "We're glad to have you here — say hello and introduce yourself!"
    ),
    # Moderation logging
    "log_channel_id": "0",
    "log_deletes": "1",
    # Tickets
    "ticket_category_id": "0",
    "ticket_staff_role_id": "0",
    "ticket_log_channel_id": "0",
    "ticket_counter": "0",
    # Anti-spam
    "antispam_enabled": "1",
    "antispam_window": "10",
    "antispam_max_messages": "8",
    "antispam_burst_limit": "5",
    "antispam_burst_window": "3",
    "antispam_duplicate_limit": "3",
    "antispam_mention_limit": "6",
    "antispam_warn_threshold": "1",
    "antispam_timeout_threshold": "3",
    "antispam_timeout_seconds": "300",
    "antispam_strike_window": "600",
    "antispam_ignore_staff": "1",
    # Blacklisted words
    "blacklist_enabled": "1",
    "blacklist_timeout_seconds": "600",
    "blacklist_ignore_staff": "1",
    # Auto replies
    "autoreplies_enabled": "1",
    "autoreplies_cooldown": "30",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS afk (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    reason   TEXT    NOT NULL,
    since    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL,
    subject    TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    closed_at  INTEGER,
    closed_by  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tickets_lookup
    ON tickets (guild_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_channel
    ON tickets (channel_id);

CREATE TABLE IF NOT EXISTS moderation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    action       TEXT    NOT NULL,
    user_id      INTEGER,
    moderator_id INTEGER,
    reason       TEXT,
    duration     INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_modlog_guild
    ON moderation_log (guild_id, created_at);
"""


class Database:
    """Small async wrapper around a single SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._closed = False
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row

        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    # -- lifecycle ---------------------------------------------------------
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            try:
                self._connection.commit()
            finally:
                self._connection.close()

    # -- low level helpers -------------------------------------------------
    def _execute_sync(self, sql: str, params: Sequence[Any]) -> int | None:
        with self._lock:
            try:
                cursor = self._connection.execute(sql, tuple(params))
                self._connection.commit()
                return cursor.lastrowid
            except Exception:
                self._connection.rollback()
                raise

    def _fetchone_sync(self, sql: str, params: Sequence[Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    def _fetchall_sync(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int | None:
        return await asyncio.to_thread(self._execute_sync, sql, tuple(params))

    async def fetchone(
        self, sql: str, params: Iterable[Any] = ()
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._fetchone_sync, sql, tuple(params))

    async def fetchall(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._fetchall_sync, sql, tuple(params))

    # -- guild settings ----------------------------------------------------
    async def get_all_settings(self, guild_id: int) -> dict[str, str]:
        rows = await self.fetchall(
            "SELECT key, value FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        return {row["key"]: row["value"] for row in rows}

    async def get_setting(
        self, guild_id: int, key: str, default: str | None = None
    ) -> str | None:
        row = await self.fetchone(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        if row is not None:
            return row["value"]
        return DEFAULTS.get(key, default)

    async def set_setting(self, guild_id: int, key: str, value: Any) -> None:
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value
            """,
            (guild_id, key, str(value)),
        )

    async def delete_setting(self, guild_id: int, key: str) -> None:
        await self.execute(
            "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )

    async def bump_counter(self, guild_id: int, key: str) -> int:
        """Atomically increment an integer setting and return the new value."""
        row = await self.fetchone(
            """
            INSERT INTO guild_settings (guild_id, key, value)
            VALUES (?, ?, '1')
            ON CONFLICT(guild_id, key)
            DO UPDATE SET value = CAST(guild_settings.value AS INTEGER) + 1
            RETURNING CAST(value AS INTEGER) AS value
            """,
            (guild_id, key),
        )
        # The INSERT ... RETURNING statement always produces a row.
        return int(row["value"]) if row else 1

    # -- AFK ---------------------------------------------------------------
    async def set_afk(self, guild_id: int, user_id: int, reason: str) -> None:
        await self.execute(
            """
            INSERT INTO afk (guild_id, user_id, reason, since)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET reason = excluded.reason, since = excluded.since
            """,
            (guild_id, user_id, reason, int(time.time())),
        )

    async def get_afk(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM afk WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def delete_afk(self, guild_id: int, user_id: int) -> None:
        await self.execute(
            "DELETE FROM afk WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    # -- tickets -----------------------------------------------------------
    async def create_ticket(
        self,
        guild_id: int,
        number: int,
        channel_id: int,
        user_id: int,
        subject: str,
    ) -> int:
        row_id = await self.execute(
            """
            INSERT INTO tickets
                (number, guild_id, channel_id, user_id, subject, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (number, guild_id, channel_id, user_id, subject[:1000], int(time.time())),
        )
        return int(row_id or 0)

    async def get_ticket_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
        )

    async def get_open_ticket(
        self, guild_id: int, user_id: int
    ) -> dict[str, Any] | None:
        return await self.fetchone(
            """
            SELECT * FROM tickets
            WHERE guild_id = ? AND user_id = ? AND status = 'open'
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        )

    async def update_ticket_status(
        self,
        channel_id: int,
        status: str,
        closed_by: int | None = None,
    ) -> None:
        await self.execute(
            """
            UPDATE tickets
            SET status = ?, closed_at = ?, closed_by = ?
            WHERE channel_id = ?
            """,
            (status, int(time.time()), closed_by, channel_id),
        )

    # -- moderation audit log ---------------------------------------------
    async def add_mod_log(
        self,
        guild_id: int,
        action: str,
        *,
        user_id: int | None = None,
        moderator_id: int | None = None,
        reason: str | None = None,
        duration: int | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO moderation_log
                (guild_id, action, user_id, moderator_id, reason, duration, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                action,
                user_id,
                moderator_id,
                reason,
                duration,
                int(time.time()),
            ),
        )
