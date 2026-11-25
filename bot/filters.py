"""Editable, hot-reloadable filter files.

Both loaders watch their file's modification time, so editing
``blacklisted_words.txt`` or ``auto_replies.json`` takes effect without a
restart.  Explicit reload commands are also provided for convenience.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .utils import compile_blacklist_word, normalize_text


# ---------------------------------------------------------------------------
# Blacklisted words
# ---------------------------------------------------------------------------
class Blacklist:
    """Loads one word/phrase per line from a text file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._words: list[str] = []
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        self._mtime_ns: int | None = None
        self._lock = asyncio.Lock()

    @property
    def words(self) -> list[str]:
        return list(self._words)

    @property
    def count(self) -> int:
        return len(self._patterns)

    async def ensure_loaded(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            if self._mtime_ns is not None:
                self._words, self._patterns = [], []
                self._mtime_ns = None
            return

        if mtime == self._mtime_ns:
            return

        async with self._lock:
            # Another task may have reloaded while we waited for the lock.
            try:
                if self.path.stat().st_mtime_ns == self._mtime_ns:
                    return
            except OSError:
                return
            words, patterns = await asyncio.to_thread(self._load_sync)
            self._words = words
            self._patterns = patterns
            self._mtime_ns = mtime

    async def reload(self) -> int:
        async with self._lock:
            words, patterns = await asyncio.to_thread(self._load_sync)
            self._words = words
            self._patterns = patterns
            try:
                self._mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                self._mtime_ns = None
            return len(patterns)

    def _load_sync(self) -> tuple[list[str], list[tuple[str, re.Pattern[str]]]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return [], []

        words: list[str] = []
        patterns: list[tuple[str, re.Pattern[str]]] = []
        seen: set[str] = set()

        for line in raw.splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            key = entry.casefold()
            if key in seen:
                continue
            seen.add(key)

            compiled = compile_blacklist_word(entry)
            if compiled is not None:
                words.append(entry)
                patterns.append((entry, compiled))

        return words, patterns

    def find(self, content: str) -> str | None:
        """Return the first blacklisted entry found in ``content``."""
        if not content or not self._patterns:
            return None
        normalized = normalize_text(content)
        for word, pattern in self._patterns:
            if pattern.search(normalized):
                return word
        return None


# ---------------------------------------------------------------------------
# Automatic replies
# ---------------------------------------------------------------------------
@dataclass
class AutoReplyRule:
    index: int
    keywords: list[str]
    response: str
    match: str = "word"
    patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)
    normalized_keywords: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, normalized_content: str) -> bool:
        if self.match == "exact":
            return normalized_content.strip() in self.normalized_keywords
        if self.match == "contains":
            return any(keyword in normalized_content for keyword in self.normalized_keywords)
        return any(pattern.search(normalized_content) for pattern in self.patterns)


class AutoReplies:
    """Loads keyword → response rules from ``auto_replies.json``.

    Supported JSON shapes::

        {"replies": [{"keywords": ["hello", "hi"], "response": "Hi!", "match": "word"}]}

    or a bare list of the same objects.  ``match`` may be ``word`` (default),
    ``contains`` or ``exact``.
    """

    VALID_MATCHES = {"word", "contains", "exact"}

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rules: list[AutoReplyRule] = []
        self.error: str | None = None
        self._mtime_ns: int | None = None
        self._lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._mtime_ns:
            return

        async with self._lock:
            try:
                if self.path.stat().st_mtime_ns == self._mtime_ns:
                    return
            except OSError:
                return
            rules, error = await asyncio.to_thread(self._load_sync)
            self._mtime_ns = mtime
            # Keep the previous good config if the new file is invalid.
            if error is None:
                self.rules = rules
                self.error = None
            else:
                self.error = error

    async def reload(self) -> tuple[int, str | None]:
        async with self._lock:
            rules, error = await asyncio.to_thread(self._load_sync)
            try:
                self._mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                self._mtime_ns = None
            if error is None:
                self.rules = rules
                self.error = None
            else:
                self.error = error
            return len(self.rules), error

    def _load_sync(self) -> tuple[list[AutoReplyRule], str | None]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], f"File not found: {self.path.name}"
        except OSError as exc:
            return [], f"Could not read {self.path.name}: {exc}"

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return [], f"Invalid JSON in {self.path.name} (line {exc.lineno}): {exc.msg}"

        if isinstance(data, dict):
            entries = data.get("replies")
        else:
            entries = data

        if not isinstance(entries, list):
            return [], "Expected a 'replies' list in auto_replies.json."

        rules: list[AutoReplyRule] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue

            keywords = entry.get("keywords")
            if isinstance(keywords, str):
                keywords = [keywords]
            if not isinstance(keywords, list):
                continue

            clean_keywords = [
                str(keyword).strip() for keyword in keywords if str(keyword).strip()
            ]
            response = entry.get("response")
            if not clean_keywords or not isinstance(response, str) or not response.strip():
                continue

            match = str(entry.get("match", "word")).strip().lower()
            if match not in self.VALID_MATCHES:
                match = "word"

            normalized_keywords: list[str] = []
            patterns: list[re.Pattern[str]] = []
            for keyword in clean_keywords:
                normalized = " ".join(normalize_text(keyword).split())
                if not normalized:
                    continue
                normalized_keywords.append(normalized)
                if match == "word":
                    tokens = normalized.split()
                    body = r"\s+".join(re.escape(token) for token in tokens)
                    try:
                        patterns.append(
                            re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)
                        )
                    except re.error:  # pragma: no cover - defensive
                        continue

            if not normalized_keywords:
                continue

            rules.append(
                AutoReplyRule(
                    index=index,
                    keywords=clean_keywords,
                    response=response.strip(),
                    match=match,
                    patterns=tuple(patterns),
                    normalized_keywords=tuple(normalized_keywords),
                )
            )

        return rules, None

    def match(self, content: str) -> AutoReplyRule | None:
        if not content or not self.rules:
            return None
        normalized = " ".join(normalize_text(content).split())
        for rule in self.rules:
            if rule.matches(normalized):
                return rule
        return None
