"""Automatic keyword → response replies driven by ``auto_replies.json``."""

from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.logging_setup import COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING
from bot.utils import truncate


class AutoReplies(commands.Cog):
    """Replies once to messages that match a configured keyword rule."""

    autoreplies = app_commands.Group(
        name="autoreplies",
        description="Manage the automatic keyword replies",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # (guild_id, user_id, rule_index) -> monotonic timestamp
        self._last_reply: dict[tuple[int, int, int], float] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not message.content or self.bot.is_moderated(message.id):
            return

        if self.bot.is_command_message(message.content):
            return

        settings = await self.bot.guild_config(message.guild.id)
        if not settings.bool("autoreplies_enabled"):
            return

        if await self.bot.is_blacklisted_content(message.content, message.guild.id):
            return

        store = self.bot.auto_replies
        await store.ensure_loaded()
        rule = store.match(message.content)
        if rule is None:
            return

        cooldown = max(0, settings.int("autoreplies_cooldown"))
        key = (message.guild.id, message.author.id, rule.index)
        now = time.monotonic()
        if now - self._last_reply.get(key, 0.0) < cooldown:
            return

        if len(self._last_reply) > 10_000:
            self._last_reply.clear()
        self._last_reply[key] = now

        try:
            await message.reply(
                truncate(rule.response, 1900),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            self.bot.logger.warning(
                "Failed to send automatic reply in #%s", message.channel, exc_info=True
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @autoreplies.command(
        name="reload", description="Reload auto_replies.json without restarting"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autoreplies_reload(self, interaction: discord.Interaction) -> None:
        count, error = await self.bot.auto_replies.reload()
        if error:
            embed = discord.Embed(
                title="❌ Could not load auto replies",
                description=truncate(error, 1500),
                color=COLOR_WARNING,
                timestamp=discord.utils.utcnow(),
            )
        else:
            embed = discord.Embed(
                title="✅ Auto replies reloaded",
                description=(
                    f"Loaded **{count}** rule(s) from "
                    f"`{self.bot.auto_replies.path.name}`."
                ),
                color=COLOR_SUCCESS,
                timestamp=discord.utils.utcnow(),
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoreplies.command(name="list", description="List configured auto replies")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autoreplies_list(self, interaction: discord.Interaction) -> None:
        store = self.bot.auto_replies
        await store.ensure_loaded()

        if store.error:
            embed = discord.Embed(
                title="⚠️ Auto-reply file error",
                description=truncate(store.error, 1500),
                color=COLOR_WARNING,
                timestamp=discord.utils.utcnow(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not store.rules:
            embed = discord.Embed(
                title="Auto replies",
                description=(
                    "No rules configured. Add entries to "
                    f"`{store.path.name}` and run `/autoreplies reload`."
                ),
                color=COLOR_INFO,
                timestamp=discord.utils.utcnow(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        lines: list[str] = []
        for rule in store.rules[:25]:
            keywords = ", ".join(f"`{keyword}`" for keyword in rule.keywords[:6])
            lines.append(
                f"**{keywords}** ({rule.match}) → {truncate(rule.response, 150)}"
            )
        if len(store.rules) > 25:
            lines.append(f"… and {len(store.rules) - 25} more rule(s).")

        embed = discord.Embed(
            title=f"Auto replies ({len(store.rules)})",
            description=truncate("\n".join(lines), 3900),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"File: {store.path.name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoreplies.command(
        name="toggle", description="Enable or disable automatic replies"
    )
    @app_commands.describe(enabled="Whether automatic replies are active")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autoreplies_toggle(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.bot.set_guild_setting(
            interaction.guild.id, "autoreplies_enabled", "1" if enabled else "0"
        )
        embed = discord.Embed(
            title="✅ Auto replies updated",
            description=(
                f"Automatic replies are now **{'enabled' if enabled else 'disabled'}**."
            ),
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoreplies.command(
        name="cooldown", description="Set the per-user cooldown for auto replies"
    )
    @app_commands.describe(seconds="Seconds between auto replies to the same user")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autoreplies_cooldown(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 3600],
    ) -> None:
        await self.bot.set_guild_setting(
            interaction.guild.id, "autoreplies_cooldown", seconds
        )
        embed = discord.Embed(
            title="✅ Auto-reply cooldown updated",
            description=(
                f"Users will receive at most one reply per rule every **{seconds}s**."
            ),
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoReplies(bot))
