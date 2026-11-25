#!/usr/bin/env python3
"""Entry point for the Discord community bot.

Run with::

    ./venv/bin/python main-bot.py

All configuration is loaded from ``.env`` (see ``.env.example``).
"""

from __future__ import annotations

import logging
import sys

from bot.client import DiscordBot
from bot.config import Config
from bot.logging_setup import setup_logging


def main() -> int:
    try:
        config = Config.from_env()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    config.ensure_dirs()
    logger, file_handler = setup_logging(config.log_file, config.log_level)

    bot = DiscordBot(config)
    try:
        bot.run(
            config.token,
            log_handler=file_handler,
            log_level=getattr(logging, config.log_level, logging.INFO),
        )
    except KeyboardInterrupt:
        logger.info("Shutdown requested with Ctrl+C")
    except Exception:
        logger.exception("The bot crashed during startup or runtime")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
