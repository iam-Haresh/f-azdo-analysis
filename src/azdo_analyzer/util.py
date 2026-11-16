"""Small shared helpers: tolerant JSON access, pattern matching, dates, deterministic JSON."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any


def dig(obj: Any, *keys: str | int, default: Any = None) -> Any:
    """Walk nested dicts/lists; return ``default`` when any hop is missing."""
    cur = obj
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_name(name: str) -> str:
    """File-system safe name (used for cache and report paths)."""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return cleaned or "_"


def short_hash(text: str, length: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


@lru_cache(maxsize=4096)
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Config pattern: glob by default (case-insensitive), ``re:`` prefix for regex."""
    if pattern.startswith("re:"):
        return re.compile(pattern[3:])
    return re.compile(fnmatch.translate(pattern), re.IGNORECASE)


def matches_any(patterns: Iterable[str], value: str) -> bool:
    return any(compile_pattern(p).search(value) for p in patterns)


@lru_cache(maxsize=4096)
def path_glob(glob: str) -> re.Pattern[str]:
    """Path glob with ``**`` support; paths are compared without a leading slash."""
    glob = glob.lstrip("/")
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def path_matches(globs: Iterable[str], path: str) -> bool:
    path = path.lstrip("/")
    return any(path_glob(g).match(path) for g in globs)


_FRACTION = re.compile(r"\.(\d+)")


def parse_dt(value: Any) -> datetime | None:
    """Parse AZDO timestamps (variable fraction digits, optional Z)."""
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value.strip(), count=1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def short_branch(ref: str | None) -> str:
    if not ref:
        return ""
    for prefix in ("refs/heads/", "+refs/heads/", "-refs/heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


_VAR_REFS = (
    re.compile(r"\$\(([A-Za-z_][\w.\-]*)\)"),
    re.compile(r"variables\[['\"]([^'\"]+)['\"]\]"),
    re.compile(r"\bvariables\.([A-Za-z_][\w]*)"),
    re.compile(r"\bparameters\.([A-Za-z_][\w]*)"),
)


def var_refs(text: str) -> list[str]:
    """Variables referenced through $(x), $[variables.x] or ${{ variables.x / parameters.x }}."""
    found: set[str] = set()
    for rx in _VAR_REFS:
        found.update(rx.findall(text))
    return sorted(found)


def json_dumps(obj: Any) -> str:
    """Deterministic JSON: sorted keys, stable indentation, trailing newline."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def find_data_dir(name: str, base: Path | None = None) -> Path:
    """Locate ``rules/`` or ``mappings/``: next to the config/CWD, repo root, or packaged copy."""
    here = Path(__file__).resolve()
    candidates = [
        (base or Path.cwd()) / name,
        Path.cwd() / name,
        here.parents[2] / name,
        here.parent / "_data" / name,
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


def find_data_file(rel: str) -> Path:
    path = Path(rel)
    if path.is_file():
        return path
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / rel, here.parent / "_data" / rel):
        if cand.is_file():
            return cand
    return path
