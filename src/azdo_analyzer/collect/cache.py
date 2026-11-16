"""Raw payload cache: ``<cache_dir>/<source>/<project>/<resource>/<id>.json``.

Payloads are stored as received from the API. List endpoints are stored as one
``list.json`` holding ``{"count": n, "value": [...]}`` with all pages merged.
The cache is sensitive (identities, URLs, non-secret variable values).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..util import safe_name, short_hash

COLLECTION = "_collection"


class Cache:
    def __init__(self, root: str | Path, ttl_hours: float = 24, resume: bool = True) -> None:
        self.root = Path(root)
        self._ttl_seconds = ttl_hours * 3600
        self._resume = resume

    def path(self, source: str, project: str, resource: str, item_id: str | int = "list") -> Path:
        return self.root / safe_name(source) / safe_name(project) / resource / f"{safe_name(str(item_id))}.json"

    def file_path(self, source: str, project: str, repo_id: str, branch: str, file: str) -> Path:
        """Location of a cached repository file (content or missing marker)."""
        key = short_hash(f"{branch}\n{file.strip('/').lower()}")
        return self.root / safe_name(source) / safe_name(project) / "files" / safe_name(repo_id) / f"{key}.json"

    def is_fresh(self, path: Path, refresh: bool = False) -> bool:
        if refresh or not self._resume or not path.is_file():
            return False
        return (time.time() - path.stat().st_mtime) <= self._ttl_seconds

    def write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def read(self, path: Path) -> Any:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def get(self, source: str, project: str, resource: str, item_id: str | int = "list") -> Any:
        return self.read(self.path(source, project, resource, item_id))

    def get_list(self, source: str, project: str, resource: str) -> list[Any]:
        return self.get_list_item(source, project, resource, "list")

    def get_list_item(self, source: str, project: str, resource: str, item_id: str | int) -> list[Any]:
        payload = self.get(source, project, resource, item_id)
        if isinstance(payload, dict):
            value = payload.get("value")
            return value if isinstance(value, list) else []
        return payload if isinstance(payload, list) else []

    def item_ids(self, source: str, project: str, resource: str) -> list[str]:
        folder = self.root / safe_name(source) / safe_name(project) / resource
        if not folder.is_dir():
            return []
        return sorted(p.stem for p in folder.glob("*.json") if p.stem != "list")

    def source_file(self, source: str, name: str) -> Path:
        return self.root / safe_name(source) / name
