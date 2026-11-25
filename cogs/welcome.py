"""Welcome messages with persistent per-guild configuration."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.logging_setup import COLOR_SUCCESS


def render_welcome(template: str, member: discord.Member) -> str:
    """Replace the supported placeholders in a welcome template."""
    replacements = {
        "mention": member.mention,
        "user": str(member),
        "username": member.name,
        "display_name": member.display_name,
        "server": member.guild.name,
        "count": str(member.guild.member_count or 0),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered[:4000]


def build_welcome_embed(member: discord.Member, template: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Welcome to {member.guild.name}!",
        description=render_welcome(template, member),
        color=COLOR_SUCCESS,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="Member",
        value=f"{member.mention}\n`{discord.utils.escape_markdown(str(member))}`",
        inline=True,
    )
    embed.add_field(
        name="Member count",
        value=str(member.guild.member_count or "—"),
        inline=True,
    )
    embed.add_field(
        name="Account created",
        value=discord.utils.format_dt(member.created_at, "R"),
        inline=True,
    )
    embed.set_footer(text="Welcome system")
    return embed


class Welcome(commands.Cog):
    """Sends a configurable welcome message for every new member."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild is None:
            return

        settings = await self.bot.guild_config(member.guild.id)
        template = settings.text("welcome_message")
        embed = build_welcome_embed(member, template)

        channel_id = settings.optional_id("welcome_channel_id")
        channel = member.guild.get_channel(channel_id) if channel_id else None
        delivered = False
        me = member.guild.me

        if isinstance(channel, discord.TextChannel) and me is not None:
            permissions = channel.permissions_for(member.guild.me)
            if permissions.send_messages and permissions.embed_links:
                try:
                    await channel.send(
                        content=member.mention,
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(
                            users=[member], everyone=False, roles=False
                        ),
                    )
                    delivered = True
                except discord.HTTPException:
                    self.bot.logger.warning(
                        "Failed to send welcome message in #%s", channel, exc_info=True
                    )

        if delivered:
            return

        # Preserve the original bot behaviour: fall back to a DM when no
        # welcome channel is configured (or it is not reachable).
        try:
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            self.bot.logger.debug(
                "Could not DM welcome message to %s (DMs closed?)", member
            )

    # -- configuration -----------------------------------------------------
    @app_commands.command(
        name="setwelcomechannel",
        description="Set the channel where new-member welcomes are posted",
    )
    @app_commands.describe(
        channel="Target channel — defaults to the channel you run the command in"
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def set_welcome_channel(
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
                f"I cannot send embeds in {target.mention}. Check my permissions there.",
                ephemeral=True,
            )
            return

        await self.bot.set_guild_setting(
            interaction.guild.id, "welcome_channel_id", target.id
        )

        embed = discord.Embed(
            title="✅ Welcome channel updated",
            description=(
                f"New members will now be welcomed in {target.mention}.\n"
                "Use `/setwelcomemessage` to customise the text and "
                "`/testwelcome` to preview it."
            ),
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setwelcomemessage",
        description="Customise the welcome message template",
    )
    @app_commands.describe(
        template=(
            "Placeholders: {mention} {user} {username} {display_name} {server} {count}"
        )
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def set_welcome_message(
        self, interaction: discord.Interaction, template: str
    ) -> None:
        template = template.strip()
        if not template:
            await interaction.response.send_message(
                "The template cannot be empty.", ephemeral=True
            )
            return
        if len(template) > 1800:
            await interaction.response.send_message(
                "Please keep the template under 1800 characters.", ephemeral=True
            )
            return

        await self.bot.set_guild_setting(
            interaction.guild.id, "welcome_message", template
        )
        preview = render_welcome(template, interaction.user)

        embed = discord.Embed(
            title="✅ Welcome message updated",
            description=preview,
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Template",
            value=f"```{template[:1000]}```",
            inline=False,
        )
        embed.set_footer(text="Preview uses your own account")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="welcomeconfig",
        description="Show the current welcome configuration",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_config(self, interaction: discord.Interaction) -> None:
        settings = await self.bot.guild_config(interaction.guild.id)
        channel_id = settings.optional_id("welcome_channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        template = settings.text("welcome_message")

        embed = discord.Embed(
            title="Welcome configuration",
            color=COLOR_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Channel",
            value=channel.mention if channel else "Not set (falls back to DMs)",
            inline=True,
        )
        embed.add_field(name="Template", value=f"```{template[:1000]}```", inline=False)
        embed.set_footer(text="Placeholders: {mention} {user} {username} {server} {count}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="testwelcome",
        description="Send a test welcome message to the configured channel",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def test_welcome(self, interaction: discord.Interaction) -> None:
        settings = await self.bot.guild_config(interaction.guild.id)
        channel_id = settings.optional_id("welcome_channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "No welcome channel is configured. Use `/setwelcomechannel` first.",
                ephemeral=True,
            )
            return

        embed = build_welcome_embed(interaction.user, settings.text("welcome_message"))
        embed.set_footer(text="Test welcome message")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            await interaction.response.send_message(
                f"I could not send a message in {channel.mention}. Check my permissions.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Test welcome sent in {channel.mention}.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
