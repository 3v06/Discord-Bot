#!/usr/bin/env python3
"""Offline self-check for the bot.

This does not connect to Discord.  It verifies that:

* every Python file imports correctly,
* all cogs load and register their slash commands,
* the duration parser works,
* the blacklist and auto-reply files parse,
* SQLite storage works.

Run it after editing code or configuration::

    ./venv/bin/python scripts/selfcheck.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.client import DiscordBot  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.utils import format_duration, parse_duration  # noqa: E402

EXPECTED_TOP_LEVEL = {
    "ping",
    "hi",
    "afk",
    "unafk",
    "timeout",
    "untimeout",
    "clearm",
    "setlogchannel",
    "setwelcomechannel",
    "setwelcomemessage",
    "welcomeconfig",
    "testwelcome",
    "blacklist",
    "logconfig",
    "antispam",
    "ticket",
    "autoreplies",
}


def check_durations() -> None:
    cases = {
        "10": 600,
        "10m": 600,
        "1h30m": 5400,
        "2 days": 172800,
        "45s": 45,
        "1w": 604800,
    }
    for text, expected in cases.items():
        parsed = parse_duration(text)
        assert parsed is not None, f"Could not parse duration {text!r}"
        assert int(parsed.total_seconds()) == expected, (
            f"Duration {text!r} parsed as {parsed} instead of {expected}s"
        )
    for invalid in ("abc", "10x", "0", ""):
        assert parse_duration(invalid) is None, f"{invalid!r} should be invalid"
    assert format_duration(5400) == "1h 30m"


async def check_bot() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="discord-bot-selfcheck-"))
    database = None
    try:
        config = Config(
            token="selfcheck-token",
            guild_id=None,
            db_path=workspace / "bot.sqlite3",
            blacklist_path=ROOT / "blacklisted_words.txt",
            auto_replies_path=ROOT / "auto_replies.json",
            log_file=workspace / "bot.log",
            log_level="INFO",
        )
        config.ensure_dirs()

        bot = DiscordBot(config)
        database = bot.db
        await bot.setup_hook()

        registered = {command.name for command in bot.tree.get_commands()}
        missing = EXPECTED_TOP_LEVEL - registered
        assert not missing, f"Missing slash commands: {sorted(missing)}"

        for group_name in ("blacklist", "logconfig", "antispam", "ticket", "autoreplies"):
            group = bot.tree.get_command(group_name)
            assert group is not None, f"Missing group /{group_name}"
            assert group.commands, f"/{group_name} has no subcommands"

        assert await bot.is_blacklisted_content("badword1", 1)
        assert not await bot.is_blacklisted_content("this is clean", 1)

        await bot.auto_replies.ensure_loaded()
        assert bot.auto_replies.error is None, bot.auto_replies.error
        assert bot.auto_replies.match("hello") is not None

        ticket_id = await bot.db.create_ticket(1, 1, 100, 200, "self check")
        row = await bot.db.get_ticket_by_channel(100)
        assert row is not None and row["id"] == ticket_id and row["number"] == 1
        await bot.db.update_ticket_status(100, "closed", 200)
        assert await bot.db.get_open_ticket(1, 200) is None

        await bot.db.set_afk(1, 200, "testing")
        assert (await bot.db.get_afk(1, 200))["reason"] == "testing"
        await bot.db.delete_afk(1, 200)

        print(f"✅ {len(registered)} top-level slash commands registered")
        print("✅ blacklist, auto-replies, durations and SQLite checks passed")
    finally:
        if database is not None:
            await database.close()
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    check_durations()
    asyncio.run(check_bot())
    print("✅ self-check complete — the bot is ready to start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
