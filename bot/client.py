"""The Discord client: extension loading, settings cache and error handling."""

from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .database import DEFAULTS, Database
from .filters import AutoReplies, Blacklist
from .logging_setup import ModLog
from .utils import truncate

# Load order matters: moderation marks handled messages before anti-spam and
# auto-replies inspect them, and general runs first so AFK status updates early.
EXTENSIONS = (
    "cogs.general",
    "cogs.welcome",
    "cogs.moderation",
    "cogs.antispam",
    "cogs.tickets",
    "cogs.autoreplies",
)

CONFIG_CACHE_SECONDS = 30.0


class GuildSettings:
    """Typed read access to one guild's merged settings."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def raw(self, key: str) -> str:
        return str(self._values.get(key, DEFAULTS.get(key, "")))

    def int(self, key: str) -> int:
        try:
            return int(float(self.raw(key)))
        except (TypeError, ValueError):
            try:
                return int(float(DEFAULTS.get(key, "0")))
            except (TypeError, ValueError):
                return 0

    def bool(self, key: str) -> bool:
        return self.raw(key).strip().lower() in {"1", "true", "yes", "on"}

    def text(self, key: str) -> str:
        return self.raw(key)

    def optional_id(self, key: str) -> int | None:
        value = self.int(key)
        return value or None


class DiscordBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="-",
            intents=intents,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True,
                replied_user=False,
            ),
            case_insensitive=True,
            strip_after_prefix=True,
        )

        self.config = config
        self.logger = logging.getLogger("discordbot")
        self.db = Database(config.db_path)
        self.blacklist = Blacklist(config.blacklist_path)
        self.auto_replies = AutoReplies(config.auto_replies_path)
        self.modlog = ModLog(self)

        self.started_at = discord.utils.utcnow()
        self._config_cache: dict[int, tuple[float, GuildSettings]] = {}
        self._synced = False

        # Message IDs whose deletion must not produce a "message deleted" log
        # (bulk clears, automod deletions, temporary warnings...).
        self.suppressed_deletes: set[int] = set()
        # Channel ID -> monotonic expiry for bulk-delete events triggered by
        # /clearm, which already writes its own audit entry.
        self.suppressed_bulk_deletes: dict[int, float] = {}
        # Message IDs already handled by a moderation system; other listeners
        # skip them so one bad message never triggers two punishments.
        self.moderated_messages: set[int] = set()

        self.tree.on_error = self.on_app_command_error

    # -- settings cache ----------------------------------------------------
    async def guild_config(self, guild_id: int) -> GuildSettings:
        now = time.monotonic()
        cached = self._config_cache.get(guild_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        raw = await self.db.get_all_settings(guild_id)
        merged = {**DEFAULTS, **raw}
        settings = GuildSettings(merged)

        if len(self._config_cache) > 1000:
            self._config_cache.clear()
        self._config_cache[guild_id] = (now + CONFIG_CACHE_SECONDS, settings)
        return settings

    def invalidate_config(self, guild_id: int) -> None:
        self._config_cache.pop(guild_id, None)

    async def set_guild_setting(self, guild_id: int, key: str, value: Any) -> None:
        await self.db.set_setting(guild_id, key, value)
        self.invalidate_config(guild_id)

    # -- cross-cog bookkeeping --------------------------------------------
    def suppress_delete_log(self, *message_ids: int) -> None:
        if not message_ids:
            return
        if len(self.suppressed_deletes) > 5000:
            self.suppressed_deletes.clear()
        self.suppressed_deletes.update(int(message_id) for message_id in message_ids)

    def consume_suppressed_delete(self, message_id: int) -> bool:
        if message_id in self.suppressed_deletes:
            self.suppressed_deletes.discard(message_id)
            return True
        return False

    def release_suppressed_deletes(self, *message_ids: int) -> None:
        """Forget suppressions for messages that were not actually deleted."""
        for message_id in message_ids:
            self.suppressed_deletes.discard(int(message_id))

    def suppress_bulk_delete(self, channel_id: int) -> None:
        if len(self.suppressed_bulk_deletes) > 1000:
            self.suppressed_bulk_deletes.clear()
        self.suppressed_bulk_deletes[int(channel_id)] = time.monotonic() + 60.0

    def consume_suppressed_bulk(self, channel_id: int) -> bool:
        expires = self.suppressed_bulk_deletes.get(int(channel_id))
        if expires is None:
            return False
        if expires > time.monotonic():
            return True
        self.suppressed_bulk_deletes.pop(int(channel_id), None)
        return False

    def mark_moderated(self, message_id: int) -> None:
        if len(self.moderated_messages) > 5000:
            self.moderated_messages.clear()
        self.moderated_messages.add(message_id)

    def is_moderated(self, message_id: int) -> bool:
        return message_id in self.moderated_messages

    def is_command_message(self, content: str) -> bool:
        """Return True only for actual registered prefix-command invocations.

        A plain check for ``content.startswith("-")`` would let users bypass
        automod by starting a bad message with a hyphen, so the command name is
        validated against the registry.
        """
        prefix = self.command_prefix
        if not isinstance(prefix, str) or not content.startswith(prefix):
            return False
        remainder = content[len(prefix) :].lstrip()
        if not remainder:
            return False
        name = remainder.split(maxsplit=1)[0]
        return self.get_command(name) is not None

    async def is_blacklisted_content(self, content: str, guild_id: int) -> bool:
        """Pre-check used by other listeners.

        Event listeners run concurrently, so a blacklisted message may not have
        been marked as handled yet when anti-spam or auto-replies inspect it.
        Checking the filter directly avoids an auto-reply to a message that is
        about to be deleted.
        """
        if not content:
            return False
        settings = await self.guild_config(guild_id)
        if not settings.bool("blacklist_enabled"):
            return False
        await self.blacklist.ensure_loaded()
        return self.blacklist.find(content) is not None

    # -- lifecycle ---------------------------------------------------------
    async def setup_hook(self) -> None:
        # discord.py's default tree error handler only writes to the library
        # log; bind ours so slash-command failures produce a friendly response.
        self.tree.on_error = self.on_app_command_error
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            self.logger.info("Loaded extension %s", extension)

    async def on_ready(self) -> None:
        if not self._synced:
            await self._sync_commands()

        print(f"We are ready to go in {self.user.name}")
        self.logger.info(
            "Logged in as %s (ID: %s) in %d guild(s)",
            self.user,
            self.user.id,
            len(self.guilds),
        )
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{len(self.guilds)} server(s) · -help",
                )
            )
        except discord.HTTPException:
            self.logger.warning("Could not update bot presence", exc_info=True)

    async def _sync_commands(self) -> None:
        try:
            if self.config.guild_id:
                guild = discord.Object(id=self.config.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                self.logger.info(
                    "Synced %d slash command(s) to guild %s for instant updates.",
                    len(synced),
                    self.config.guild_id,
                )
            else:
                synced = await self.tree.sync()
                self.logger.info(
                    "Synced %d global slash command(s). Global propagation can "
                    "take up to one hour; set GUILD_ID in .env for instant sync.",
                    len(synced),
                )
            self._synced = True
        except Exception:
            self.logger.exception("Failed to sync application commands")

    async def close(self) -> None:
        try:
            await self.db.close()
        finally:
            await super().close()

    # -- error handling ----------------------------------------------------
    async def _respond_error(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            self.logger.debug("Could not deliver error response", exc_info=True)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = error
        if isinstance(error, app_commands.CommandInvokeError):
            original = error.original

        if isinstance(original, app_commands.MissingPermissions):
            missing = ", ".join(f"`{item}`" for item in original.missing_permissions)
            message = f"You need the {missing} permission(s) to use this command."
        elif isinstance(original, app_commands.BotMissingPermissions):
            missing = ", ".join(f"`{item}`" for item in original.missing_permissions)
            message = f"I need the {missing} permission(s) to do that."
        elif isinstance(original, app_commands.CheckFailure):
            message = "You do not have permission to use this command."
        elif isinstance(original, app_commands.CommandOnCooldown):
            message = f"Slow down — try again in `{original.retry_after:.1f}s`."
        elif isinstance(original, app_commands.NoPrivateMessage):
            message = "This command can only be used inside a server."
        elif isinstance(original, discord.Forbidden):
            message = (
                "Discord refused the action (missing permissions or role "
                "hierarchy). Check my role position and permissions."
            )
        else:
            self.logger.error(
                "Unhandled application command error in /%s",
                interaction.command.qualified_name if interaction.command else "unknown",
                exc_info=(type(original), original, original.__traceback__),
            )
            message = "Something went wrong while running that command. The error was logged."

        await self._respond_error(interaction, message)

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            message = f"Missing argument: `{error.param.name}`. Try `{ctx.prefix}help {ctx.command}`."
        elif isinstance(error, commands.BadArgument):
            message = f"Invalid argument: {error}"
        elif isinstance(error, commands.CheckFailure):
            message = "You do not have permission to use this command."
        elif isinstance(error, commands.CommandInvokeError):
            message = "Something went wrong while running that command. The error was logged."
            self.logger.error(
                "Unhandled prefix command error in %s",
                ctx.command,
                exc_info=(type(error.original), error.original, error.original.__traceback__),
            )
        else:
            message = "Something went wrong while running that command. The error was logged."
            self.logger.error(
                "Unhandled prefix command error in %s",
                ctx.command,
                exc_info=(type(error), error, error.__traceback__),
            )

        try:
            await ctx.reply(
                f"⚠️ {truncate(message, 1500)}", mention_author=False
            )
        except discord.HTTPException:
            self.logger.debug("Could not deliver prefix command error", exc_info=True)
