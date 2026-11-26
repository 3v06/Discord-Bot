"""Advanced anti-spam detection with warnings and automatic timeouts."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot.logging_setup import COLOR_SUCCESS, COLOR_WARNING
from bot.utils import (
    format_duration,
    hierarchy_error,
    is_staff_member,
    normalize_text,
    safe_dm,
    truncate,
)

SPAM_SETTINGS: dict[str, tuple[str, int, int]] = {
    "antispam_window": ("Window (seconds)", 3, 120),
    "antispam_max_messages": ("Max messages per window", 3, 50),
    "antispam_burst_limit": ("Flood limit (messages)", 2, 20),
    "antispam_burst_window": ("Flood window (seconds)", 1, 30),
    "antispam_duplicate_limit": ("Duplicate limit", 2, 20),
    "antispam_mention_limit": ("Mention limit", 2, 20),
    "antispam_warn_threshold": ("Warn threshold", 1, 10),
    "antispam_timeout_threshold": ("Timeout threshold", 2, 20),
    "antispam_timeout_seconds": ("Timeout duration (seconds)", 60, 2_419_200),
    "antispam_strike_window": ("Strike window (seconds)", 60, 86_400),
}

SPAM_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, (label, _minimum, _maximum) in SPAM_SETTINGS.items()
]

WARN_COOLDOWN = 5.0
LOG_COOLDOWN = 5.0
ACTIVITY_TTL = 3600.0


@dataclass
class Activity:
    messages: deque[float] = field(default_factory=deque)
    contents: deque[tuple[float, str]] = field(default_factory=deque)
    mentions: deque[tuple[float, int]] = field(default_factory=deque)
    strikes: deque[float] = field(default_factory=deque)
    last_warning: float = 0.0
    last_log: float = 0.0


class AntiSpam(commands.Cog):
    """Rate, duplicate and mention-flood detection."""

    antispam = app_commands.Group(
        name="antispam",
        description="Configure the anti-spam system",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.activity: dict[tuple[int, int], Activity] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def cog_unload(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(300)
                cutoff = time.monotonic() - ACTIVITY_TTL
                for key, activity in list(self.activity.items()):
                    if not activity.messages or activity.messages[-1] < cutoff:
                        self.activity.pop(key, None)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if self.bot.is_moderated(message.id):
            return

        if self.bot.is_command_message(message.content):
            return

        settings = await self.bot.guild_config(message.guild.id)
        if not settings.bool("antispam_enabled"):
            return

        if await self.bot.is_blacklisted_content(message.content, message.guild.id):
            return

        staff_role_id = settings.optional_id("ticket_staff_role_id")
        if settings.bool("antispam_ignore_staff") and is_staff_member(
            message.author, staff_role_id
        ):
            return

        window = max(3, settings.int("antispam_window"))
        max_messages = max(3, settings.int("antispam_max_messages"))
        burst_limit = max(2, settings.int("antispam_burst_limit"))
        burst_window = max(1, settings.int("antispam_burst_window"))
        duplicate_limit = max(2, settings.int("antispam_duplicate_limit"))
        mention_limit = max(2, settings.int("antispam_mention_limit"))
        warn_threshold = max(1, settings.int("antispam_warn_threshold"))
        timeout_threshold = max(2, settings.int("antispam_timeout_threshold"))
        strike_window = max(60, settings.int("antispam_strike_window"))

        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        activity = self.activity.setdefault(key, Activity())

        cutoff = now - window
        while activity.messages and activity.messages[0] < cutoff:
            activity.messages.popleft()
        while activity.contents and activity.contents[0][0] < cutoff:
            activity.contents.popleft()
        while activity.mentions and activity.mentions[0][0] < cutoff:
            activity.mentions.popleft()

        normalized = re.sub(r"\s+", " ", normalize_text(message.content)).strip()
        mention_count = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            # @everyone/@here is far more disruptive than ordinary mentions.
            mention_count = max(mention_count, mention_limit)

        activity.messages.append(now)
        if normalized:
            activity.contents.append((now, normalized))
        if mention_count:
            activity.mentions.append((now, mention_count))

        reasons: list[str] = []
        if len(activity.messages) > max_messages:
            reasons.append(
                f"message flood ({len(activity.messages)} messages in {window}s)"
            )
        burst_cutoff = now - burst_window
        burst_count = sum(
            1 for timestamp in activity.messages if timestamp >= burst_cutoff
        )
        if burst_count >= burst_limit:
            reasons.append(
                f"message flooding ({burst_count} messages in {burst_window}s)"
            )
        if normalized:
            duplicates = sum(
                1 for _timestamp, content in activity.contents if content == normalized
            )
            if duplicates >= duplicate_limit:
                reasons.append(f"repeated message ({duplicates}×)")
        total_mentions = sum(count for _timestamp, count in activity.mentions)
        if total_mentions >= mention_limit:
            reasons.append(f"excessive mentions ({total_mentions})")

        if not reasons:
            return

        self.bot.mark_moderated(message.id)

        channel = message.channel
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            me = message.guild.me
            if me is not None and channel.permissions_for(me).manage_messages:
                self.bot.suppress_delete_log(message.id)
                try:
                    await message.delete()
                except discord.HTTPException:
                    self.bot.logger.debug(
                        "Could not delete spam message %s", message.id, exc_info=True
                    )

        strike_cutoff = now - strike_window
        while activity.strikes and activity.strikes[0] < strike_cutoff:
            activity.strikes.popleft()
        activity.strikes.append(now)
        strike_count = len(activity.strikes)

        warned = False
        if strike_count >= warn_threshold and (now - activity.last_warning) >= WARN_COOLDOWN:
            warned = await self._warn(message, reasons, strike_count, timeout_threshold)
            activity.last_warning = now

        if strike_count >= timeout_threshold:
            applied, failure = await self._apply_timeout(message, settings, reasons)
            activity.strikes.clear()

            log_embed = discord.Embed(
                title="🔨 Spam Timeout",
                color=COLOR_WARNING,
                timestamp=discord.utils.utcnow(),
            )
            self._add_spam_fields(
                log_embed, message, reasons, strike_count, timeout_threshold
            )
            log_embed.add_field(
                name="Timeout",
                value=(
                    format_duration(settings.int("antispam_timeout_seconds"))
                    if applied
                    else f"Not applied — {failure}"
                ),
                inline=True,
            )
            log_embed.add_field(
                name="Warning sent", value="Yes" if warned else "No", inline=True
            )
            await self.bot.modlog.record(
                message.guild,
                "spam_timeout" if applied else "spam_detection",
                user_id=message.author.id,
                reason=", ".join(reasons),
                duration=settings.int("antispam_timeout_seconds") if applied else None,
                embed=log_embed,
            )
            activity.last_log = now
            return

        # Log detections even before the timeout threshold, but not more than
        # once every few seconds per user to keep the log channel readable.
        if now - activity.last_log >= LOG_COOLDOWN:
            activity.last_log = now
            log_embed = discord.Embed(
                title="⚠️ Spam Detected",
                color=COLOR_WARNING,
                timestamp=discord.utils.utcnow(),
            )
            self._add_spam_fields(
                log_embed, message, reasons, strike_count, timeout_threshold
            )
            log_embed.add_field(
                name="Warning sent", value="Yes" if warned else "No", inline=True
            )
            await self.bot.modlog.record(
                message.guild,
                "spam_detection",
                user_id=message.author.id,
                reason=", ".join(reasons),
                embed=log_embed,
            )

    def _add_spam_fields(
        self,
        embed: discord.Embed,
        message: discord.Message,
        reasons: list[str],
        strikes: int,
        threshold: int,
    ) -> None:
        embed.add_field(
            name="User",
            value=f"{message.author.mention}\n`{message.author.id}`",
            inline=True,
        )
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(
            name="Strikes", value=f"{strikes}/{threshold}", inline=True
        )
        embed.add_field(
            name="Reasons", value=truncate("\n".join(f"• {item}" for item in reasons), 1000)
        )
        embed.add_field(
            name="Message",
            value=truncate(message.content or "[embed/attachment]", 900),
            inline=False,
        )
        embed.set_footer(text="Anti-spam")

    async def _warn(
        self,
        message: discord.Message,
        reasons: list[str],
        strike_count: int,
        timeout_threshold: int,
    ) -> bool:
        embed = discord.Embed(
            title="⚠️ Please slow down",
            description=(
                f"{message.author.mention}, I detected "
                f"{', '.join(reasons)}.\n"
                f"Continued spam will result in a timeout "
                f"(strike {strike_count}/{timeout_threshold})."
            ),
            color=COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        try:
            warning = await message.channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author], everyone=False, roles=False
                ),
            )
        except discord.HTTPException:
            return False

        asyncio.create_task(self._delete_later(warning))
        return True

    async def _delete_later(self, message: discord.Message) -> None:
        await asyncio.sleep(8)
        self.bot.suppress_delete_log(message.id)
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    async def _apply_timeout(
        self,
        message: discord.Message,
        settings,
        reasons: list[str],
    ) -> tuple[bool, str | None]:
        seconds = settings.int("antispam_timeout_seconds")
        me = message.guild.me
        if seconds <= 0:
            return False, "timeout disabled"
        if me is None:
            return False, "bot member unavailable"
        if not me.guild_permissions.moderate_members:
            return False, "missing Moderate Members permission"

        problem = hierarchy_error(message.guild, me, message.author, me)
        if problem:
            return False, problem

        try:
            await message.author.timeout(
                timedelta(seconds=seconds),
                reason=f"Spam: {', '.join(reasons)}"[:512],
            )
        except discord.HTTPException:
            self.bot.logger.warning(
                "Failed to timeout %s for spam", message.author, exc_info=True
            )
            return False, "Discord rejected the timeout"

        dm_embed = discord.Embed(
            title=f"You were timed out in {message.guild.name}",
            description="Automatic moderation: " + ", ".join(reasons),
            color=COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        dm_embed.add_field(
            name="Duration", value=format_duration(seconds), inline=True
        )
        dm_embed.add_field(
            name="Expires",
            value=discord.utils.format_dt(
                discord.utils.utcnow() + timedelta(seconds=seconds), "f"
            ),
            inline=True,
        )
        await safe_dm(message.author, dm_embed)
        return True, None

    # ------------------------------------------------------------------
    # Configuration commands
    # ------------------------------------------------------------------
    @antispam.command(name="settings", description="Show the anti-spam configuration")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antispam_settings(self, interaction: discord.Interaction) -> None:
        settings = await self.bot.guild_config(interaction.guild.id)
        embed = discord.Embed(
            title="Anti-spam configuration",
            color=COLOR_SUCCESS if settings.bool("antispam_enabled") else COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Enabled",
            value="Yes" if settings.bool("antispam_enabled") else "No",
            inline=True,
        )
        embed.add_field(
            name="Window",
            value=f"{settings.int('antispam_window')}s",
            inline=True,
        )
        embed.add_field(
            name="Max messages",
            value=str(settings.int("antispam_max_messages")),
            inline=True,
        )
        embed.add_field(
            name="Flood limit",
            value=(
                f"{settings.int('antispam_burst_limit')} / "
                f"{settings.int('antispam_burst_window')}s"
            ),
            inline=True,
        )
        embed.add_field(
            name="Duplicate limit",
            value=str(settings.int("antispam_duplicate_limit")),
            inline=True,
        )
        embed.add_field(
            name="Mention limit",
            value=str(settings.int("antispam_mention_limit")),
            inline=True,
        )
        embed.add_field(
            name="Warn / timeout threshold",
            value=(
                f"{settings.int('antispam_warn_threshold')} / "
                f"{settings.int('antispam_timeout_threshold')}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Timeout duration",
            value=format_duration(settings.int("antispam_timeout_seconds")),
            inline=True,
        )
        embed.add_field(
            name="Strike window",
            value=format_duration(settings.int("antispam_strike_window")),
            inline=True,
        )
        embed.add_field(
            name="Ignore staff",
            value="Yes" if settings.bool("antispam_ignore_staff") else "No",
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @antispam.command(name="set", description="Change a numeric anti-spam setting")
    @app_commands.describe(key="Setting to change", value="New value")
    @app_commands.choices(key=SPAM_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antispam_set(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        value: int,
    ) -> None:
        db_key = key.value
        label, minimum, maximum = SPAM_SETTINGS[db_key]
        if value < minimum or value > maximum:
            await interaction.response.send_message(
                f"`{label}` must be between **{minimum}** and **{maximum}**.",
                ephemeral=True,
            )
            return

        current = await self.bot.guild_config(interaction.guild.id)
        warn_threshold = current.int("antispam_warn_threshold")
        timeout_threshold = current.int("antispam_timeout_threshold")
        if db_key == "antispam_warn_threshold":
            warn_threshold = value
        elif db_key == "antispam_timeout_threshold":
            timeout_threshold = value

        if timeout_threshold <= warn_threshold:
            await interaction.response.send_message(
                "The timeout threshold must be greater than the warn threshold.",
                ephemeral=True,
            )
            return

        await self.bot.set_guild_setting(interaction.guild.id, db_key, value)
        embed = discord.Embed(
            title="✅ Anti-spam setting updated",
            description=f"`{label}` is now **{value}**.",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @antispam.command(name="toggle", description="Enable or disable anti-spam")
    @app_commands.describe(enabled="Whether anti-spam is active")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antispam_toggle(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.bot.set_guild_setting(
            interaction.guild.id, "antispam_enabled", "1" if enabled else "0"
        )
        embed = discord.Embed(
            title="✅ Anti-spam updated",
            description=f"Anti-spam is now **{'enabled' if enabled else 'disabled'}**.",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @antispam.command(
        name="ignorestaff", description="Toggle staff exemption from anti-spam"
    )
    @app_commands.describe(enabled="Whether staff bypass anti-spam checks")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def antispam_ignore_staff(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.bot.set_guild_setting(
            interaction.guild.id, "antispam_ignore_staff", "1" if enabled else "0"
        )
        embed = discord.Embed(
            title="✅ Staff exemption updated",
            description=(
                "Staff now bypass anti-spam checks."
                if enabled
                else "Staff are now subject to anti-spam checks."
            ),
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiSpam(bot))
