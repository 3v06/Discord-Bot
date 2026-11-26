"""Shared helpers used across cogs.

The important pieces here are:

* :func:`parse_duration` / :func:`format_duration` for moderation durations.
* :func:`compile_blacklist_word` which turns a plain word into a regex that
  tolerates casing, leetspeak, repeated letters and simple separator bypasses.
* :func:`hierarchy_error` for the role checks every moderation action needs.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from typing import Iterator, Sequence

import discord

MAX_TIMEOUT = timedelta(days=28)

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)

# Common Cyrillic/Greek lookalikes.  Mapping them to Latin letters defeats the
# cheapest "type the word with another alphabet" bypass without affecting
# normal text in most communities.
_CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ѕ": "s",
        "ј": "j",
        "һ": "h",
        "ԁ": "d",
        "ԛ": "q",
        "ԝ": "w",
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
    }
)


def normalize_text(text: str) -> str:
    """Normalise text to defeat common blacklist bypasses.

    NFKD decomposition removes accents (``bád`` → ``bad``), zero-width
    characters are stripped, case is folded, and common Cyrillic/Greek
    lookalikes are mapped to Latin letters.  Case-folding happens *before* the
    lookalike mapping so uppercase Cyrillic (``А``) is translated too.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.translate(_ZERO_WIDTH)
    text = text.casefold()
    text = text.translate(_CONFUSABLES)
    return text.casefold()


_LEET_VARIANTS = {
    "a": "@4",
    "b": "8",
    "c": "(",
    "e": "3",
    "g": "9",
    "i": "1!|",
    "l": "1|",
    "o": "0",
    "s": "5$",
    "t": "7",
    "z": "2",
}

_SEPARATOR = r"[\W_]*"  # optional junk between letters ("b.a.d", "b a d")
_PHRASE_SEPARATOR = r"[\W_]+"  # required separator between words


def _char_pattern(char: str) -> str:
    variants = {char}
    variants.update(_LEET_VARIANTS.get(char, ""))
    inner = "".join(re.escape(item) for item in sorted(variants))
    return f"[{inner}]+"


def compile_blacklist_word(word: str) -> re.Pattern[str] | None:
    """Compile a blacklist entry into a tolerant, boundary-aware regex.

    Short entries (two characters or fewer) are matched literally to avoid
    false positives; longer entries tolerate leetspeak, repeated characters and
    punctuation/whitespace inserted between letters.
    """
    word = normalize_text(word.strip())
    if not word:
        return None

    tokens = word.split()
    fuzzy = len(word.replace(" ", "")) >= 3

    compiled_tokens: list[str] = []
    for token in tokens:
        if fuzzy:
            compiled_tokens.append(_SEPARATOR.join(_char_pattern(c) for c in token))
        else:
            compiled_tokens.append(re.escape(token))

    body = _PHRASE_SEPARATOR.join(compiled_tokens)
    try:
        return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)
    except re.error:  # pragma: no cover - defensive, patterns are escaped
        return None


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------
_UNIT_SECONDS: dict[str, int] = {}
for _names, _seconds in (
    (("w", "week", "weeks"), 604800),
    (("d", "day", "days"), 86400),
    (("h", "hr", "hrs", "hour", "hours"), 3600),
    (("m", "min", "mins", "minute", "minutes"), 60),
    (("s", "sec", "secs", "second", "seconds"), 1),
):
    for _name in _names:
        _UNIT_SECONDS[_name] = _seconds

_DURATION_PART = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)",
    re.IGNORECASE,
)


def parse_duration(text: str) -> timedelta | None:
    """Parse strings like ``10m``, ``1h30m``, ``2 days`` or ``45``.

    A bare number is interpreted as minutes, which matches how people usually
    type moderation durations (``/timeout @user 10`` = ten minutes).
    Returns ``None`` when the input cannot be parsed.
    """
    if not text:
        return None

    value = text.strip().lower().replace(",", "")
    if not value:
        return None

    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        seconds = float(value) * 60
        return timedelta(seconds=seconds) if seconds > 0 else None

    matches = list(_DURATION_PART.finditer(value))
    if not matches:
        return None

    remainder = _DURATION_PART.sub("", value).strip()
    if remainder:
        return None

    seconds = 0.0
    for match in matches:
        unit = match.group("unit").lower()
        multiplier = _UNIT_SECONDS.get(unit)
        if multiplier is None:
            return None
        seconds += float(match.group("value")) * multiplier

    return timedelta(seconds=seconds) if seconds > 0 else None


def format_duration(seconds: float | int | timedelta) -> str:
    """Format a number of seconds (or a timedelta) as ``1d 2h 30m``."""
    if isinstance(seconds, timedelta):
        total = int(seconds.total_seconds())
    else:
        total = int(seconds)
    if total < 0:
        total = 0

    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------
def truncate(text: str | None, limit: int = 1024) -> str:
    if not text:
        return "—"
    text = str(text)
    return text if len(text) <= limit else f"{text[: max(0, limit - 1)]}…"


def sanitize_channel_name(name: str) -> str:
    """Turn an arbitrary display name into a valid channel-name fragment."""
    lowered = name.casefold()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", lowered)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:90] or "user"


def chunked(items: Sequence[discord.Message], size: int) -> Iterator[list[discord.Message]]:
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def is_staff_member(member: discord.Member, staff_role_id: int | None = None) -> bool:
    """Return ``True`` for members who should bypass automod/auto-replies."""
    if member.bot:
        return True
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    if staff_role_id:
        return member.get_role(staff_role_id) is not None
    return False


def hierarchy_error(
    guild: discord.Guild,
    moderator: discord.Member,
    target: discord.Member,
    me: discord.Member,
) -> str | None:
    """Check role hierarchy for a moderation action.

    Returns a human-readable error string, or ``None`` when the action is
    allowed.  The bot must always be above the target; a human moderator must be
    above the target unless they are the server owner.
    """
    if target.id == moderator.id:
        return "You cannot moderate yourself."
    if target.bot:
        return "Bots cannot be moderated."
    if target.id == guild.owner_id:
        return "The server owner cannot be moderated."
    if me.top_role <= target.top_role:
        return (
            "My highest role is not above the target's highest role, so Discord "
            "will reject the action. Move my role higher in the role list."
        )
    if moderator.id != guild.owner_id:
        if target.guild_permissions.administrator:
            return "You cannot moderate a member with Administrator."
        if moderator.top_role <= target.top_role:
            return "Your highest role must be above the target's highest role."
    return None


async def safe_dm(user: discord.abc.User, embed: discord.Embed | str) -> bool:
    """Best-effort DM.  Returns ``False`` when the user has DMs closed."""
    try:
        if isinstance(embed, discord.Embed):
            await user.send(embed=embed)
        else:
            await user.send(embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False
