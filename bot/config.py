"""Configuration loaded from ``.env`` and environment variables.

Nothing sensitive is hardcoded here.  The token is only ever read from the
environment (or the local ``.env`` file), and every path can be overridden.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

_PLACEHOLDER_TOKENS = {"", "your-token-here", "YOUR_TOKEN_HERE", "changeme"}


def _resolve_path(value: str | None, default: str) -> Path:
    path = Path(value).expanduser() if value else Path(default)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration."""

    token: str
    guild_id: int | None
    db_path: Path
    blacklist_path: Path
    auto_replies_path: Path
    log_file: Path
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(BASE_DIR / ".env")

        token = (os.getenv("DISCORD_TOKEN") or "").strip()
        if token in _PLACEHOLDER_TOKENS:
            raise RuntimeError(
                "DISCORD_TOKEN is missing. Copy .env.example to .env and paste "
                "your bot token from https://discord.com/developers/applications."
            )

        raw_guild = (os.getenv("GUILD_ID") or "").strip()
        guild_id: int | None = None
        if raw_guild:
            if not raw_guild.isdigit():
                raise RuntimeError("GUILD_ID must be a numeric Discord server ID.")
            guild_id = int(raw_guild)

        level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
        if not isinstance(getattr(logging, level, None), int):
            level = "INFO"

        return cls(
            token=token,
            guild_id=guild_id,
            db_path=_resolve_path(os.getenv("DB_PATH"), "data/bot.sqlite3"),
            blacklist_path=_resolve_path(
                os.getenv("BLACKLIST_PATH"), "blacklisted_words.txt"
            ),
            auto_replies_path=_resolve_path(
                os.getenv("AUTO_REPLIES_PATH"), "auto_replies.json"
            ),
            log_file=_resolve_path(os.getenv("LOG_FILE"), "discord.log"),
            log_level=level,
        )

    def ensure_dirs(self) -> None:
        """Create the parent directories used by all configured files."""
        for path in {
            self.db_path.parent,
            self.blacklist_path.parent,
            self.auto_replies_path.parent,
            self.log_file.parent,
        }:
            path.mkdir(parents=True, exist_ok=True)
