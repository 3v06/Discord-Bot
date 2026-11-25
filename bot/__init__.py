"""Core package for the community moderation and utility bot.

The package is split into small modules so each concern stays testable:

* :mod:`bot.config`     – environment/configuration loading
* :mod:`bot.database`   – persistent SQLite storage
* :mod:`bot.filters`    – editable blacklist and auto-reply files
* :mod:`bot.client`     – the ``commands.Bot`` subclass
* :mod:`bot.logging_setup` – application logging + moderation log embeds
* :mod:`bot.utils`      – shared helpers (duration parsing, checks, ...)
"""

from .config import Config

__all__ = ["Config"]
