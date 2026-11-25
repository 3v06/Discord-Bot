"""Application logging and the shared moderation log dispatcher."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from .utils import format_duration, truncate

if TYPE_CHECKING:  # pragma: no cover - import cycle only for type checking
    from .client import DiscordBot

# Shared embed colours
COLOR_INFO = 0x5865F2
COLOR_SUCCESS = 0x57F287
COLOR_WARNING = 0xFEE75C
COLOR_ERROR = 0xED4245
COLOR_MODERATION = 0xEB459E

_ACTION_ICONS = {
    "timeout": "🔨",
    "untimeout": "🔓",
    "spam_detection": "⚠️",
    "spam_timeout": "🔨",
    "blacklist": "🚫",
    "clear": "🧹",
    "message_delete": "🗑️",
    "ticket_created": "🎫",
    "ticket_closed": "🔒",
    "ticket_deleted": "🗑️",
}


def setup_logging(
    log_file: Path, level: str
) -> tuple[logging.Logger, logging.Handler]:
    """Configure the application logger and return it with its file handler.

    The returned handler is also handed to ``bot.run(...)`` so discord.py's own
    logs land in the same rotating file instead of creating a second handler on
    the same path.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger("discordbot")
    logger.setLevel(numeric_level)
    logger.propagate = False

    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric_level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)-8s | %(message)s"))
    console.setLevel(numeric_level)

    logger.addHandler(file_handler)
    logger.addHandler(console)

    return logger, file_handler


class ModLog:
    """Writes moderation actions to SQLite and to the configured log channel."""

    def __init__(self, bot: "DiscordBot") -> None:
        self.bot = bot
        self.logger = logging.getLogger("discordbot.modlog")

    async def _resolve_channel(
        self, guild: discord.Guild, preferred_id: int | None = None
    ) -> discord.TextChannel | None:
        async def lookup(channel_id: int) -> discord.TextChannel | None:
            if not channel_id:
                return None
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except discord.HTTPException:
                    return None
            return channel if isinstance(channel, discord.TextChannel) else None

        if preferred_id:
            channel = await lookup(preferred_id)
            if channel is not None:
                return channel

        settings = await self.bot.guild_config(guild.id)
        return await lookup(settings.int("log_channel_id"))

    async def send(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
        *,
        channel_id: int | None = None,
    ) -> bool:
        channel = await self._resolve_channel(guild, channel_id)
        if channel is None:
            self.logger.info(
                "No moderation log channel configured for guild %s (%s); "
                "action was still written to the database/file.",
                guild.name,
                guild.id,
            )
            return False
        try:
            await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            return True
        except discord.HTTPException:
            self.logger.exception("Failed to send moderation log embed")
            return False

    async def record(
        self,
        guild: discord.Guild,
        action: str,
        *,
        user_id: int | None = None,
        moderator_id: int | None = None,
        reason: str | None = None,
        duration: int | None = None,
        embed: discord.Embed | None = None,
        channel_id: int | None = None,
    ) -> bool:
        """Persist the action and post an embed (a default one if none given)."""
        try:
            await self.bot.db.add_mod_log(
                guild.id,
                action,
                user_id=user_id,
                moderator_id=moderator_id,
                reason=reason,
                duration=duration,
            )
        except Exception:  # pragma: no cover - storage failure should not crash
            self.logger.exception("Failed to write moderation log to SQLite")

        if embed is None:
            embed = self.default_embed(
                action,
                user_id=user_id,
                moderator_id=moderator_id,
                reason=reason,
                duration=duration,
            )

        self.logger.info(
            "Moderation action %s in %s (%s): user=%s moderator=%s reason=%s",
            action,
            guild.name,
            guild.id,
            user_id,
            moderator_id,
            reason,
        )
        return await self.send(guild, embed, channel_id=channel_id)

    @staticmethod
    def default_embed(
        action: str,
        *,
        user_id: int | None = None,
        moderator_id: int | None = None,
        reason: str | None = None,
        duration: int | None = None,
    ) -> discord.Embed:
        icon = _ACTION_ICONS.get(action, "📋")
        embed = discord.Embed(
            title=f"{icon} {action.replace('_', ' ').title()}",
            description=truncate(reason, 2000),
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        if user_id:
            embed.add_field(
                name="User",
                value=f"<@{user_id}>\n`{user_id}`",
                inline=True,
            )
        if moderator_id:
            embed.add_field(
                name="Moderator",
                value=f"<@{moderator_id}>\n`{moderator_id}`",
                inline=True,
            )
        if duration is not None:
            embed.add_field(
                name="Duration",
                value=format_duration(duration),
                inline=True,
            )
        embed.set_footer(text="Moderation log")
        return embed

    @staticmethod
    def add_user_fields(
        embed: discord.Embed,
        *,
        user: discord.abc.User | None = None,
        moderator: discord.abc.User | None = None,
        channel: discord.abc.GuildChannel | None = None,
    ) -> discord.Embed:
        if user is not None:
            embed.add_field(
                name="User",
                value=f"{user.mention}\n`{user.id}`",
                inline=True,
            )
        if moderator is not None:
            embed.add_field(
                name="Moderator",
                value=f"{moderator.mention}\n`{moderator.id}`",
                inline=True,
            )
        if channel is not None:
            embed.add_field(name="Channel", value=channel.mention, inline=True)
        return embed
