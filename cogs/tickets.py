"""Private, button-driven support ticket system."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.logging_setup import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_MODERATION,
    COLOR_SUCCESS,
)
from bot.utils import sanitize_channel_name, truncate

PANEL_CUSTOM_ID = "ticket:open"
CLOSE_CUSTOM_ID = "ticket:close"
DELETE_CUSTOM_ID = "ticket:delete"


class TicketModal(discord.ui.Modal, title="Open a support ticket"):
    subject = discord.ui.TextInput(
        label="Subject",
        placeholder="A short summary of your issue",
        max_length=100,
        required=True,
    )
    details = discord.ui.TextInput(
        label="Describe your issue",
        placeholder="Give staff as much detail as possible…",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=True,
    )

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_ticket(
            interaction, str(self.subject.value), str(self.details.value)
        )


class TicketPanelView(discord.ui.View):
    """Persistent view with the public "open ticket" button."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Open a ticket",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
        custom_id=PANEL_CUSTOM_ID,
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(TicketModal(self.cog))


class TicketControlView(discord.ui.View):
    """Persistent close/delete controls shown inside every ticket channel."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Close",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        custom_id=CLOSE_CUSTOM_ID,
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.cog.close_ticket(interaction)

    @discord.ui.button(
        label="Delete",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id=DELETE_CUSTOM_ID,
    )
    async def delete_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.cog.request_delete(interaction)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, cog: "Tickets", channel: discord.TextChannel) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.channel = channel

    @discord.ui.button(
        label="Delete ticket", style=discord.ButtonStyle.danger, emoji="🗑️"
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="Deleting the ticket channel…", embed=None, view=None
        )
        success, error = await self.cog.delete_ticket(self.channel, interaction.user)
        if not success:
            await interaction.followup.send(
                f"I could not delete the channel: {error}", ephemeral=True
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="Ticket deletion cancelled.", embed=None, view=None
        )
        self.stop()


class Tickets(commands.Cog):
    """Creates private channels and manages their lifecycle."""

    ticket = app_commands.Group(
        name="ticket",
        description="Support ticket system",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panel_view = TicketPanelView(self)
        self.control_view = TicketControlView(self)

    async def cog_load(self) -> None:
        # Persistent views must be registered on every startup so buttons on
        # old panel/ticket messages keep working after a restart.
        self.bot.add_view(self.panel_view)
        self.bot.add_view(self.control_view)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
            return True
        settings = await self.bot.guild_config(member.guild.id)
        role_id = settings.optional_id("ticket_staff_role_id")
        return bool(role_id and member.get_role(role_id))

    async def _ticket_log_channel_id(self, guild_id: int) -> int | None:
        settings = await self.bot.guild_config(guild_id)
        return settings.optional_id("ticket_log_channel_id")

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    async def create_ticket(
        self, interaction: discord.Interaction, subject: str, details: str
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        user = interaction.user

        existing = await self.bot.db.get_open_ticket(guild.id, user.id)
        if existing is not None:
            channel = guild.get_channel(existing["channel_id"])
            if channel is not None:
                await interaction.followup.send(
                    f"You already have an open ticket: {channel.mention}",
                    ephemeral=True,
                )
                return
            # The channel was removed manually; close the stale record.
            await self.bot.db.update_ticket_status(
                existing["channel_id"], "deleted", user.id
            )

        settings = await self.bot.guild_config(guild.id)
        me = guild.me
        if not me.guild_permissions.manage_channels:
            await interaction.followup.send(
                "I need the `Manage Channels` permission to create tickets.",
                ephemeral=True,
            )
            return

        category_id = settings.optional_id("ticket_category_id")
        category = guild.get_channel(category_id) if category_id else None
        if category is not None and not isinstance(category, discord.CategoryChannel):
            category = None
        if category is not None and not category.permissions_for(me).manage_channels:
            category = None

        staff_role_id = settings.optional_id("ticket_staff_role_id")
        staff_role = guild.get_role(staff_role_id) if staff_role_id else None

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                add_reactions=True,
            ),
            me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        if staff_role is not None:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
                add_reactions=True,
            )

        number = await self.bot.db.bump_counter(guild.id, "ticket_counter")
        name = f"ticket-{number:04d}-{sanitize_channel_name(user.name)}"[:95]

        try:
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket #{number:04d} • opened by {user} ({user.id})",
                reason=f"Ticket opened by {user} ({user.id})",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Discord refused channel creation. Check my `Manage Channels` "
                "permission and whether the ticket category is full.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            self.bot.logger.exception("Failed to create ticket channel")
            await interaction.followup.send(
                "Something went wrong while creating the ticket channel.",
                ephemeral=True,
            )
            return

        await self.bot.db.create_ticket(
            guild.id, number, channel.id, user.id, subject
        )

        embed = discord.Embed(
            title=f"🎫 Ticket #{number:04d}",
            description=truncate(details, 3000),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Opened by", value=user.mention, inline=True)
        embed.add_field(name="Subject", value=truncate(subject, 200), inline=True)
        embed.add_field(
            name="Staff role",
            value=staff_role.mention if staff_role else "Not configured",
            inline=True,
        )
        embed.add_field(
            name="How to proceed",
            value=(
                "A staff member will be with you shortly. Use **Close** when the "
                "issue is resolved; staff can **Delete** the channel when finished."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Ticket {number:04d} • {guild.name}")

        try:
            await channel.send(embed=embed, view=self.control_view)
        except discord.HTTPException:
            self.bot.logger.exception("Failed to send ticket control message")

        log_embed = discord.Embed(
            title="🎫 Ticket Created",
            description=f"Ticket #{number:04d} in {channel.mention}",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        log_embed.add_field(
            name="Opened by", value=f"{user.mention}\n`{user.id}`", inline=True
        )
        log_embed.add_field(name="Subject", value=truncate(subject, 500), inline=False)
        log_embed.set_footer(text=f"Ticket ID {number}")
        await self.bot.modlog.record(
            guild,
            "ticket_created",
            user_id=user.id,
            reason=f"Ticket #{number:04d}: {subject}",
            embed=log_embed,
            channel_id=await self._ticket_log_channel_id(guild.id),
        )

        await interaction.followup.send(
            f"✅ Your ticket has been created: {channel.mention}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------
    async def close_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This can only be used inside a ticket channel.", ephemeral=True
            )
            return

        ticket = await self.bot.db.get_ticket_by_channel(channel.id)
        if ticket is None:
            await interaction.response.send_message(
                "This channel is not a registered ticket.", ephemeral=True
            )
            return
        if ticket["status"] != "open":
            await interaction.response.send_message(
                "This ticket is already closed.", ephemeral=True
            )
            return

        is_staff = await self._is_staff(interaction.user)
        if interaction.user.id != ticket["user_id"] and not is_staff:
            await interaction.response.send_message(
                "Only the ticket creator or staff can close this ticket.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        creator = interaction.guild.get_member(ticket["user_id"])
        if creator is not None:
            try:
                await channel.set_permissions(
                    creator,
                    send_messages=False,
                    add_reactions=False,
                    reason=f"Ticket closed by {interaction.user}",
                )
            except discord.HTTPException:
                self.bot.logger.warning(
                    "Could not lock ticket channel for creator", exc_info=True
                )

        new_name = channel.name
        if not new_name.startswith("closed-"):
            new_name = f"closed-{new_name}"
        try:
            await channel.edit(
                name=new_name[:100],
                reason=f"Ticket closed by {interaction.user} ({interaction.user.id})",
            )
        except discord.HTTPException:
            self.bot.logger.warning("Could not rename ticket channel", exc_info=True)

        await self.bot.db.update_ticket_status(
            channel.id, "closed", interaction.user.id
        )

        confirmation = discord.Embed(
            title="🔒 Ticket closed",
            description=(
                f"Closed by {interaction.user.mention}. The creator can no longer "
                "send messages here. Staff can delete the channel with the button below."
            ),
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        try:
            await channel.send(embed=confirmation, view=self.control_view)
        except discord.HTTPException:
            self.bot.logger.warning("Could not send close confirmation", exc_info=True)

        log_embed = discord.Embed(
            title="🔒 Ticket Closed",
            description=f"Ticket #{ticket['number']:04d} in #{channel.name}",
            color=COLOR_MODERATION,
            timestamp=discord.utils.utcnow(),
        )
        log_embed.add_field(
            name="Created by",
            value=f"<@{ticket['user_id']}>\n`{ticket['user_id']}`",
            inline=True,
        )
        log_embed.add_field(
            name="Closed by",
            value=f"{interaction.user.mention}\n`{interaction.user.id}`",
            inline=True,
        )
        await self.bot.modlog.record(
            interaction.guild,
            "ticket_closed",
            user_id=ticket["user_id"],
            moderator_id=interaction.user.id,
            reason=f"Ticket #{ticket['number']:04d} closed",
            embed=log_embed,
            channel_id=await self._ticket_log_channel_id(interaction.guild.id),
        )

        try:
            await interaction.followup.send(
                "✅ Ticket closed and locked.", ephemeral=True
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Deleting
    # ------------------------------------------------------------------
    async def request_delete(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This can only be used inside a ticket channel.", ephemeral=True
            )
            return

        ticket = await self.bot.db.get_ticket_by_channel(channel.id)
        if ticket is None:
            await interaction.response.send_message(
                "This channel is not a registered ticket.", ephemeral=True
            )
            return
        if not await self._is_staff(interaction.user):
            await interaction.response.send_message(
                "Only staff can delete tickets.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Delete this ticket?",
            description=(
                f"Ticket #{ticket['number']:04d} and **all of its messages** will be "
                "permanently deleted. This cannot be undone."
            ),
            color=COLOR_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(
            embed=embed, view=ConfirmDeleteView(self, channel), ephemeral=True
        )

    async def delete_ticket(
        self, channel: discord.TextChannel, moderator: discord.Member
    ) -> tuple[bool, str | None]:
        ticket = await self.bot.db.get_ticket_by_channel(channel.id)
        if ticket is None:
            return False, "not a registered ticket channel"

        log_embed = discord.Embed(
            title="🗑️ Ticket Deleted",
            description=f"Ticket #{ticket['number']:04d} (`#{channel.name}`) was deleted.",
            color=COLOR_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        log_embed.add_field(
            name="Created by",
            value=f"<@{ticket['user_id']}>\n`{ticket['user_id']}`",
            inline=True,
        )
        log_embed.add_field(
            name="Deleted by",
            value=f"{moderator.mention}\n`{moderator.id}`",
            inline=True,
        )
        await self.bot.modlog.record(
            channel.guild,
            "ticket_deleted",
            user_id=ticket["user_id"],
            moderator_id=moderator.id,
            reason=f"Ticket #{ticket['number']:04d} deleted",
            embed=log_embed,
            channel_id=await self._ticket_log_channel_id(channel.guild.id),
        )

        try:
            await channel.delete(
                reason=f"Ticket deleted by {moderator} ({moderator.id})"
            )
        except discord.HTTPException as exc:
            self.bot.logger.warning("Could not delete ticket channel", exc_info=True)
            return False, str(exc)

        await self.bot.db.update_ticket_status(channel.id, "deleted", moderator.id)
        return True, None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @ticket.command(name="panel", description="Post the public ticket creation panel")
    @app_commands.describe(
        channel="Channel to post the panel in — defaults to the current channel"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_panel(
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
        if not target.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(
                f"I cannot send messages in {target.mention}.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎫 Support Tickets",
            description=(
                "Need help? Click the button below to open a **private** ticket.\n\n"
                "Only you and the server staff will be able to see it."
            ),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Our staff will assist you as soon as possible.")
        await target.send(embed=embed, view=self.panel_view)

        await interaction.response.send_message(
            f"Ticket panel posted in {target.mention}.", ephemeral=True
        )

    @ticket.command(name="close", description="Close the ticket in this channel")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def ticket_close(self, interaction: discord.Interaction) -> None:
        await self.close_ticket(interaction)

    @ticket.command(name="delete", description="Delete the ticket in this channel")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def ticket_delete(self, interaction: discord.Interaction) -> None:
        await self.request_delete(interaction)

    @ticket.command(
        name="settings", description="Show or change the ticket system settings"
    )
    @app_commands.describe(
        category="Category where ticket channels are created",
        staff_role="Role that can see and manage every ticket",
        log_channel="Channel for ticket logs (falls back to the mod log)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_settings(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel | None = None,
        staff_role: discord.Role | None = None,
        log_channel: discord.TextChannel | None = None,
    ) -> None:
        if log_channel is not None and not log_channel.permissions_for(
            interaction.guild.me
        ).send_messages:
            await interaction.response.send_message(
                f"I cannot send messages in {log_channel.mention}.", ephemeral=True
            )
            return

        if category is not None:
            await self.bot.set_guild_setting(
                interaction.guild.id, "ticket_category_id", category.id
            )
        if staff_role is not None:
            await self.bot.set_guild_setting(
                interaction.guild.id, "ticket_staff_role_id", staff_role.id
            )
        if log_channel is not None:
            await self.bot.set_guild_setting(
                interaction.guild.id, "ticket_log_channel_id", log_channel.id
            )

        settings = await self.bot.guild_config(interaction.guild.id)
        category_id = settings.optional_id("ticket_category_id")
        staff_role_id = settings.optional_id("ticket_staff_role_id")
        log_channel_id = settings.optional_id("ticket_log_channel_id")

        embed = discord.Embed(
            title="Ticket configuration",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Category",
            value=f"<#{category_id}>" if category_id else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Staff role",
            value=f"<@&{staff_role_id}>" if staff_role_id else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Log channel",
            value=f"<#{log_channel_id}>" if log_channel_id else "Falls back to `/setlogchannel`",
            inline=True,
        )
        embed.set_footer(
            text="Post the creation panel with /ticket panel"
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
