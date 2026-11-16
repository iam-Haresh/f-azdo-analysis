"""Task-name and regex matching shared by detection and analysis."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=2048)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def task_matches(pattern: str, name: str) -> bool:
    """Exact task name (case-insensitive) or a ``re:`` pattern."""
    if pattern.startswith("re:"):
        return bool(_rx(pattern[3:]).search(name))
    return pattern.lower() == name.lower()


def any_task(patterns: Iterable[str], name: str) -> bool:
    return any(task_matches(p, name) for p in patterns)


def any_regex(patterns: Iterable[str], text: str) -> bool:
    return bool(text) and any(_rx(p).search(text) for p in patterns)
