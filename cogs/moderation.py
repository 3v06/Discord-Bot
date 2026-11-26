"""Moderation: blacklisted words, manual timeouts, bulk delete and log config."""

from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot.logging_setup import (
    COLOR_ERROR,
    COLOR_MODERATION,
    COLOR_SUCCESS,
    COLOR_WARNING,
)
from bot.utils import (
    MAX_TIMEOUT,
    chunked,
    format_duration,
    hierarchy_error,
    is_staff_member,
    parse_duration,
    safe_dm,
    truncate,
)

DURATION_SUGGESTIONS = ("60s", "5m", "10m", "30m", "1h", "6h", "12h", "1d", "3d", "1w")


class Moderation(commands.Cog):
    """Automod and staff moderation commands."""

    blacklist = app_commands.Group(
        name="blacklist",
        description="Manage the editable blacklisted-words file",
        guild_only=True,
    )
    logconfig = app_commands.Group(
        name="logconfig",
        description="Moderation logging configuration",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Manual timeout
    # ------------------------------------------------------------------
    @app_commands.command(
        name="timeout",
        description="Timeout a member for a specified duration",
    )
    @app_commands.describe(
        member="The member to timeout",
        duration="e.g. 60s, 10m, 2h, 1d (maximum 28 days)",
        reason="Why the member is being timed out",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: str,
    ) -> None:
        delta = parse_duration(duration)
        if delta is None:
            await interaction.response.send_message(
                "Invalid duration. Use formats such as `60s`, `10m`, `2h`, `1d` "
                "or `1h30m`.",
                ephemeral=True,
            )
            return
        if delta > MAX_TIMEOUT:
            await interaction.response.send_message(
                "Discord does not allow timeouts longer than 28 days.",
                ephemeral=True,
            )
            return

        problem = hierarchy_error(
            interaction.guild,
            interaction.user,
            member,
            interaction.guild.me,
        )
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        reason = reason.strip()[:400] or "No reason provided"
        seconds = int(delta.total_seconds())

        try:
            await member.timeout(
                delta,
                reason=f"{interaction.user} ({interaction.user.id}): {reason}"[:512],
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot timeout that member. Check my `Moderate Members` permission "
                "and that my role is above theirs.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            self.bot.logger.exception("Failed to apply timeout to %s", member)
            await interaction.response.send_message(
                "Discord rejected the timeout request. Please try again later.",
                ephemeral=True,
            )
            return

        dm_embed = discord.Embed(
            title=f"You were timed out in {interaction.guild.name}",
            description=reason,
            color=COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        dm_embed.add_field(name="Duration", value=format_duration(delta), inline=True)
        dm_embed.add_field(
            name="Expires",
            value=discord.utils.format_dt(discord.utils.utcnow() + delta, "f"),
            inline=True,
        )
        dm_embed.set_footer(
            text="You can contact the server staff if you believe this is a mistake."
        )
        notified = await safe_dm(member, dm_embed)

        response = discord.Embed(
            title="🔨 Member timed out",
            color=COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        response.add_field(name="Member", value=f"{member.mention}\n`{member.id}`", inline=True)
        response.add_field(name="Duration", value=format_duration(delta), inline=True)
        response.add_field(
            name="Expires",
            value=discord.utils.format_dt(discord.utils.utcnow() + delta, "t"),
            inline=True,
        )
        response.add_field(name="Reason", value=truncate(reason, 1000), inline=False)
        response.add_field(
            name="Member notified",
            value="Yes" if notified else "No (DMs closed)",
            inline=True,
        )
        await interaction.response.send_message(embed=response, ephemeral=True)

        log_embed = discord.Embed(
            title="🔨 Member Timed Out",
            description=truncate(reason, 2000),
            color=COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        log_embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
        log_embed.add_field(
            name="Moderator",
            value=f"{interaction.user.mention}\n`{interaction.user.id}`",
            inline=True,
        )
        log_embed.add_field(name="Duration", value=format_duration(delta), inline=True)
        log_embed.add_field(
            name="Expires",
            value=discord.utils.format_dt(discord.utils.utcnow() + delta, "f"),
            inline=True,
        )
        log_embed.set_footer(text="Moderation log")

        await self.bot.modlog.record(
            interaction.guild,
            "timeout",
            user_id=member.id,
            moderator_id=interaction.user.id,
            reason=reason,
            duration=seconds,
            embed=log_embed,
        )

    @timeout_member.autocomplete("duration")
    async def duration_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.casefold()
        return [
            app_commands.Choice(name=value, value=value)
            for value in DURATION_SUGGESTIONS
            if current in value
        ][:25]

    @app_commands.command(
        name="untimeout",
        description="Remove an active timeout from a member",
    )
    @app_commands.describe(member="The member whose timeout should be removed")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "Timeout removed by staff",
    ) -> None:
        problem = hierarchy_error(
            interaction.guild,
            interaction.user,
            member,
            interaction.guild.me,
        )
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        try:
            await member.timeout(
                None,
                reason=f"{interaction.user} ({interaction.user.id}): {reason}"[:512],
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot modify that member's timeout. Check my permissions.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            self.bot.logger.exception("Failed to remove timeout from %s", member)
            await interaction.response.send_message(
                "Discord rejected the request. Please try again later.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔓 Timeout removed",
            description=f"{member.mention}'s timeout was removed.",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reason", value=truncate(reason, 1000), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        log_embed = discord.Embed(
            title="🔓 Timeout Removed",
            description=truncate(reason, 2000),
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        log_embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
        log_embed.add_field(
            name="Moderator",
            value=f"{interaction.user.mention}\n`{interaction.user.id}`",
            inline=True,
        )
        await self.bot.modlog.record(
            interaction.guild,
            "untimeout",
            user_id=member.id,
            moderator_id=interaction.user.id,
            reason=reason,
            embed=log_embed,
        )

    # ------------------------------------------------------------------
    # Bulk message deletion
    # ------------------------------------------------------------------
    @app_commands.command(
        name="clearm",
        description="Delete a number of recent messages in this channel",
    )
    @app_commands.describe(amount="How many recent messages to delete (1-200)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(
        manage_messages=True, read_message_history=True
    )
    async def clear_messages(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 200],
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "This command can only be used in text channels and threads.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            fetched = [message async for message in channel.history(limit=amount)]
        except discord.HTTPException:
            await interaction.followup.send(
                "I could not read the channel history. Check my permissions.",
                ephemeral=True,
            )
            return

        pinned = [message for message in fetched if message.pinned]
        candidates = [message for message in fetched if not message.pinned]
        self.bot.suppress_delete_log(*(message.id for message in candidates))
        if isinstance(channel, discord.TextChannel):
            # /clearm writes its own audit entry; do not also log the bulk event.
            self.bot.suppress_bulk_delete(channel.id)

        cutoff = discord.utils.utcnow() - timedelta(days=14)
        deleted = 0
        deleted_ids: set[int] = set()

        if isinstance(channel, discord.TextChannel):
            recent = [message for message in candidates if message.created_at >= cutoff]
            old = [message for message in candidates if message.created_at < cutoff]

            for batch in chunked(recent, 100):
                try:
                    if len(batch) == 1:
                        await batch[0].delete()
                    else:
                        await channel.delete_messages(batch)
                    deleted += len(batch)
                    deleted_ids.update(message.id for message in batch)
                except discord.HTTPException:
                    # Fall back to individual deletion; one bad message must
                    # not abort the whole batch.
                    for message in batch:
                        try:
                            await message.delete()
                            deleted += 1
                            deleted_ids.add(message.id)
                        except discord.HTTPException:
                            continue
        else:
            # Threads do not support the bulk-delete endpoint.
            old = candidates

        for message in old:
            try:
                await message.delete()
                deleted += 1
                deleted_ids.add(message.id)
            except discord.HTTPException:
                continue

        # Messages that could not be deleted would otherwise stay in the
        # suppression set forever and hide a later manual deletion.
        self.bot.release_suppressed_deletes(
            *(message.id for message in candidates if message.id not in deleted_ids)
        )

        failed = len(candidates) - deleted
        embed = discord.Embed(
            title="🧹 Messages cleared",
            color=COLOR_SUCCESS if failed == 0 else COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Requested", value=str(amount), inline=True)
        embed.add_field(name="Deleted", value=str(deleted), inline=True)
        embed.add_field(
            name="Pinned (skipped)", value=str(len(pinned)), inline=True
        )
        embed.add_field(name="Failed", value=str(failed), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

        log_embed = discord.Embed(
            title="🧹 Messages Cleared",
            description=f"Deleted **{deleted}** message(s) in {channel.mention}.",
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        log_embed.add_field(
            name="Moderator",
            value=f"{interaction.user.mention}\n`{interaction.user.id}`",
            inline=True,
        )
        log_embed.add_field(name="Requested", value=str(amount), inline=True)
        log_embed.add_field(name="Failed", value=str(failed), inline=True)
        await self.bot.modlog.record(
            interaction.guild,
            "clear",
            moderator_id=interaction.user.id,
            reason=f"Deleted {deleted} message(s) in #{getattr(channel, 'name', channel.id)}",
            embed=log_embed,
        )

    # ------------------------------------------------------------------
    # Logging configuration
    # ------------------------------------------------------------------
    @app_commands.command(
        name="setlogchannel",
        description="Set the channel used for moderation log embeds",
    )
    @app_commands.describe(
        channel="Log channel — defaults to the channel you run the command in"
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def set_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Please choose a regular text channel.", ephemeral=True
            )
            return

        permissions = target.permissions_for(interaction.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await interaction.response.send_message(
                f"I cannot send embeds in {target.mention}.", ephemeral=True
            )
            return

        await self.bot.set_guild_setting(
            interaction.guild.id, "log_channel_id", target.id
        )
        embed = discord.Embed(
            title="✅ Log channel updated",
            description=f"Moderation logs will be sent to {target.mention}.",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @logconfig.command(name="show", description="Show moderation logging settings")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def logconfig_show(self, interaction: discord.Interaction) -> None:
        settings = await self.bot.guild_config(interaction.guild.id)
        channel_id = settings.optional_id("log_channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None

        embed = discord.Embed(
            title="Logging configuration",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Log channel",
            value=channel.mention if channel else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Log deleted messages",
            value="Enabled" if settings.bool("log_deletes") else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Database",
            value=f"`{self.bot.db.path}`",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @logconfig.command(
        name="deletes",
        description="Enable or disable logging of deleted messages",
    )
    @app_commands.describe(enabled="Whether deleted messages should be logged")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def logconfig_deletes(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.bot.set_guild_setting(
            interaction.guild.id, "log_deletes", "1" if enabled else "0"
        )
        embed = discord.Embed(
            title="✅ Deleted-message logging updated",
            description=f"Deleted message logging is now **{'enabled' if enabled else 'disabled'}**.",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Blacklist commands
    # ------------------------------------------------------------------
    @blacklist.command(
        name="reload", description="Reload blacklisted_words.txt without restarting"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_reload(self, interaction: discord.Interaction) -> None:
        count = await self.bot.blacklist.reload()
        embed = discord.Embed(
            title="✅ Blacklist reloaded",
            description=f"Loaded **{count}** entr{'y' if count == 1 else 'ies'} from "
            f"`{self.bot.blacklist.path.name}`.",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @blacklist.command(name="list", description="List blacklisted words and phrases")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_list(self, interaction: discord.Interaction) -> None:
        await self.bot.blacklist.ensure_loaded()
        words = self.bot.blacklist.words
        if not words:
            description = "The blacklist is empty. Add one word or phrase per line."
        else:
            description = "\n".join(f"• `{word}`" for word in words[:80])
            if len(words) > 80:
                description += f"\n… and {len(words) - 80} more."
        embed = discord.Embed(
            title=f"Blacklisted entries ({len(words)})",
            description=truncate(description, 3900),
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"File: {self.bot.blacklist.path.name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @blacklist.command(name="status", description="Show blacklist automod settings")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_status(self, interaction: discord.Interaction) -> None:
        await self.bot.blacklist.ensure_loaded()
        settings = await self.bot.guild_config(interaction.guild.id)
        timeout = settings.int("blacklist_timeout_seconds")

        embed = discord.Embed(
            title="Blacklist status",
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Automod",
            value="Enabled" if settings.bool("blacklist_enabled") else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Entries", value=str(self.bot.blacklist.count), inline=True
        )
        embed.add_field(
            name="Timeout",
            value=format_duration(timeout) if timeout > 0 else "Disabled (delete only)",
            inline=True,
        )
        embed.add_field(
            name="Ignore staff",
            value="Yes" if settings.bool("blacklist_ignore_staff") else "No",
            inline=True,
        )
        embed.set_footer(text=f"File: {self.bot.blacklist.path}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @blacklist.command(name="toggle", description="Enable or disable blacklist automod")
    @app_commands.describe(enabled="Whether blacklisted words are moderated")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_toggle(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.bot.set_guild_setting(
            interaction.guild.id, "blacklist_enabled", "1" if enabled else "0"
        )
        embed = discord.Embed(
            title="✅ Blacklist automod updated",
            description=f"Blacklist automod is now **{'enabled' if enabled else 'disabled'}**.",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @blacklist.command(
        name="timeout", description="Set the timeout applied for blacklisted words"
    )
    @app_commands.describe(seconds="Timeout in seconds; 0 disables the timeout")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_timeout(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 2_419_200],
    ) -> None:
        await self.bot.set_guild_setting(
            interaction.guild.id, "blacklist_timeout_seconds", seconds
        )
        embed = discord.Embed(
            title="✅ Blacklist timeout updated",
            description=(
                f"Users will be timed out for **{format_duration(seconds)}**."
                if seconds
                else "Timeouts are disabled; offending messages will only be deleted."
            ),
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Automod listener
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._handle_blacklisted(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if before.content != after.content:
            await self._handle_blacklisted(after)

    async def _handle_blacklisted(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if self.bot.is_moderated(message.id):
            return

        content = message.content
        if not content:
            return

        settings = await self.bot.guild_config(message.guild.id)
        if not settings.bool("blacklist_enabled"):
            return

        staff_role_id = settings.optional_id("ticket_staff_role_id")
        if settings.bool("blacklist_ignore_staff") and is_staff_member(
            message.author, staff_role_id
        ):
            return

        await self.bot.blacklist.ensure_loaded()
        matched = self.bot.blacklist.find(content)
        if matched is None:
            return

        self.bot.mark_moderated(message.id)
        me = message.guild.me
        if me is None:
            return
        deleted = False
        timed_out = False
        timeout_seconds = settings.int("blacklist_timeout_seconds")

        if me.guild_permissions.manage_messages and message.channel.permissions_for(
            me
        ).manage_messages:
            self.bot.suppress_delete_log(message.id)
            try:
                await message.delete()
                deleted = True
            except discord.HTTPException:
                self.bot.logger.warning(
                    "Failed to delete blacklisted message %s", message.id, exc_info=True
                )

        if timeout_seconds > 0 and me.guild_permissions.moderate_members:
            if hierarchy_error(message.guild, me, message.author, me) is None:
                try:
                    await message.author.timeout(
                        timedelta(seconds=timeout_seconds),
                        reason=f"Blacklisted word: {matched}"[:512],
                    )
                    timed_out = True
                except discord.HTTPException:
                    self.bot.logger.warning(
                        "Failed to timeout %s for a blacklisted word",
                        message.author,
                        exc_info=True,
                    )

        dm_embed = discord.Embed(
            title=f"Your message was removed in {message.guild.name}",
            description=f"Reason: a blacklisted word was detected (`{matched}`).",
            color=COLOR_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        if timed_out:
            dm_embed.add_field(
                name="Timeout", value=format_duration(timeout_seconds), inline=True
            )
        notified = await safe_dm(message.author, dm_embed)

        log_embed = discord.Embed(
            title="🚫 Blacklisted Word Detected",
            color=COLOR_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        log_embed.add_field(
            name="User",
            value=f"{message.author.mention}\n`{message.author.id}`",
            inline=True,
        )
        log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        log_embed.add_field(name="Matched", value=f"`{truncate(matched, 100)}`", inline=True)
        log_embed.add_field(
            name="Timeout",
            value=format_duration(timeout_seconds) if timed_out else "Not applied",
            inline=True,
        )
        log_embed.add_field(
            name="Message deleted", value="Yes" if deleted else "No", inline=True
        )
        log_embed.add_field(
            name="Member notified",
            value="Yes" if notified else "No (DMs closed)",
            inline=True,
        )
        log_embed.add_field(
            name="Message content",
            value=truncate(content, 1000),
            inline=False,
        )
        log_embed.add_field(
            name="Message ID", value=f"`{message.id}`", inline=True
        )
        log_embed.set_footer(text="Moderation log")

        await self.bot.modlog.record(
            message.guild,
            "blacklist",
            user_id=message.author.id,
            reason=f"Matched blacklisted entry: {matched}",
            duration=timeout_seconds if timed_out else None,
            embed=log_embed,
        )

    # ------------------------------------------------------------------
    # Deleted-message logging
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        if self.bot.consume_suppressed_delete(payload.message_id):
            return

        message = payload.cached_message
        if message is not None and message.author.bot:
            return

        settings = await self.bot.guild_config(payload.guild_id)
        if not settings.bool("log_deletes"):
            return
        # Never log deletions inside the log channel itself (avoids feedback).
        if settings.int("log_channel_id") == payload.channel_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id)
        embed = discord.Embed(
            title="🗑️ Message Deleted",
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        if message is not None:
            author_value = f"{message.author.mention}\n`{message.author.id}`"
            content = message.content or "[embed/attachment]"
            reason = message.content[:500] if message.content else "[embed/attachment]"
            user_id = message.author.id
        else:
            # The message was sent before the bot started or was not cached;
            # log what is available instead of silently dropping the event.
            author_value = "Unknown (message not cached)"
            content = "Not available"
            reason = "Deleted message was not cached"
            user_id = None

        embed.add_field(name="Author", value=author_value, inline=True)
        embed.add_field(
            name="Channel",
            value=channel.mention if channel else f"`{payload.channel_id}`",
            inline=True,
        )
        embed.add_field(name="Content", value=truncate(content, 1000), inline=False)
        embed.set_footer(text=f"Message ID: {payload.message_id}")

        await self.bot.modlog.record(
            guild,
            "message_delete",
            user_id=user_id,
            reason=reason,
            embed=embed,
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        if self.bot.consume_suppressed_bulk(payload.channel_id):
            return

        settings = await self.bot.guild_config(payload.guild_id)
        if not settings.bool("log_deletes"):
            return
        if settings.int("log_channel_id") == payload.channel_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        count = len(payload.message_ids)
        if not count:
            return

        channel = guild.get_channel(payload.channel_id)
        channel_label = channel.mention if channel else f"`{payload.channel_id}`"
        authors: list[str] = []
        seen: set[int] = set()
        for message in payload.cached_messages:
            if message.author.id in seen:
                continue
            seen.add(message.author.id)
            authors.append(message.author.mention)
            if len(authors) >= 10:
                break

        embed = discord.Embed(
            title="🗑️ Bulk Messages Deleted",
            description=f"**{count}** message(s) were deleted in {channel_label}.",
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        if authors:
            embed.add_field(name="Authors", value=", ".join(authors), inline=False)
        embed.set_footer(text="Bulk deletion")

        await self.bot.modlog.record(
            guild,
            "bulk_message_delete",
            reason=f"{count} message(s) deleted in #{getattr(channel, 'name', payload.channel_id)}",
            embed=embed,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
