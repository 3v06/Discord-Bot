"""General utility commands and the persistent AFK system."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

from bot.logging_setup import COLOR_INFO
from bot.utils import format_duration, parse_duration


class General(commands.Cog):
    """Ping, greetings and AFK handling."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- latency -----------------------------------------------------------
    @commands.hybrid_command(name="ping", description="Show the bot's latency")
    async def ping(self, ctx: commands.Context) -> None:
        db_start = time.perf_counter()
        db_ok = True
        try:
            await self.bot.db.fetchone("SELECT 1")
        except Exception:  # pragma: no cover - only if SQLite is unavailable
            db_ok = False
        db_ms = (time.perf_counter() - db_start) * 1000

        websocket_ms = self.bot.latency * 1000
        websocket_text = (
            "unavailable" if math.isnan(websocket_ms) else f"{websocket_ms:.0f} ms"
        )

        embed = discord.Embed(
            title="🏓 Pong!",
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="WebSocket", value=websocket_text, inline=True)
        embed.add_field(
            name="Database", value=f"{db_ms:.1f} ms" if db_ok else "error", inline=True
        )
        origin = (
            ctx.interaction.created_at
            if ctx.interaction is not None
            else ctx.message.created_at
        )
        request_ms = max(
            0.0, (discord.utils.utcnow() - origin).total_seconds() * 1000
        )
        embed.add_field(name="Your request", value=f"{request_ms:.0f} ms", inline=True)
        embed.add_field(name="Round trip", value="measuring…", inline=True)

        started = time.perf_counter()
        message = await ctx.reply(embed=embed, mention_author=False)
        round_trip_ms = (time.perf_counter() - started) * 1000

        embed.set_field_at(
            3, name="Round trip", value=f"{round_trip_ms:.0f} ms", inline=True
        )
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            self.bot.logger.debug("Could not edit ping embed", exc_info=True)

    # -- greeting ----------------------------------------------------------
    @commands.hybrid_command(name="hi", description="Say hello to the bot")
    async def hi(self, ctx: commands.Context) -> None:
        await ctx.reply(
            f"Hello {ctx.author.mention} :wave:, how was your day today?",
            mention_author=False,
        )

    # -- AFK ---------------------------------------------------------------
    @commands.hybrid_command(
        name="afk",
        description="Mark yourself as away from keyboard",
    )
    async def afk(
        self,
        ctx: commands.Context,
        duration: str | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        if ctx.guild is None:
            await ctx.reply("AFK status can only be used inside a server.")
            return

        details: list[str] = []
        if duration:
            parsed = parse_duration(duration)
            if parsed is not None:
                details.append(f"for {format_duration(parsed)}")
            else:
                # Prefix convenience: `-afk lunch` means the reason, not a
                # malformed duration.
                reason = f"{duration} {reason}".strip() if reason else duration
        if reason:
            details.append(f"because of {reason}")

        stored_reason = " ".join(details) if details else "No reason provided"
        await self.bot.db.set_afk(ctx.guild.id, ctx.author.id, stored_reason)

        message = f"{ctx.author.mention} is now AFK :sleeping:"
        if details:
            message = f"{message} {' '.join(details)}"
        await ctx.reply(message, mention_author=False)

    @commands.hybrid_command(name="unafk", description="Remove your AFK status")
    async def unafk(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("AFK status can only be used inside a server.")
            return
        await self.bot.db.delete_afk(ctx.guild.id, ctx.author.id)
        await ctx.reply(
            f"{ctx.author.mention} is no longer AFK :smiley:", mention_author=False
        )

    def _is_afk_command(self, content: str) -> bool:
        prefix = self.bot.command_prefix
        if not isinstance(prefix, str) or not content.startswith(prefix):
            return False
        remainder = content[len(prefix) :].strip()
        if not remainder:
            return False
        return remainder.split(maxsplit=1)[0].casefold() in {"afk", "unafk"}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        # Do not instantly clear the status that the -afk command just set.
        if self._is_afk_command(message.content):
            return

        cleared = await self.bot.db.get_afk(message.guild.id, message.author.id)
        if cleared is not None:
            await self.bot.db.delete_afk(message.guild.id, message.author.id)
            try:
                await message.channel.send(
                    f"Welcome back {message.author.mention}! I removed your AFK status :smiley:",
                    allowed_mentions=discord.AllowedMentions(
                        users=[message.author], everyone=False, roles=False
                    ),
                )
            except discord.HTTPException:
                self.bot.logger.debug("Could not send AFK clear message", exc_info=True)

        notes: list[str] = []
        for user in message.mentions:
            if user.bot or user.id == message.author.id:
                continue
            row = await self.bot.db.get_afk(message.guild.id, user.id)
            if row is None:
                continue
            since = datetime.fromtimestamp(int(row["since"]), tz=timezone.utc)
            notes.append(
                f"**{discord.utils.escape_markdown(user.display_name)}** is AFK: "
                f"{row['reason']} ({discord.utils.format_dt(since, 'R')})"
            )

        if notes:
            try:
                await message.channel.send(
                    "\n".join(notes),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                self.bot.logger.debug("Could not send AFK mention notice", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
