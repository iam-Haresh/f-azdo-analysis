"""Repository file access through the cache.

With a client it fetches missing files (GET only) and caches them; without one it is the
offline provider used by ``analyze``. Missing files are cached as ``{"_missing": true}``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..client.http import AccessDenied, AzdoClient, AzdoHttpError, NotFound, quote_segment
from ..parse.yaml_loader import RepoRef
from ..util import as_str, short_branch
from .cache import COLLECTION, Cache


def version_params(branch: str) -> dict[str, str]:
    if branch.startswith("refs/tags/"):
        return {"versionDescriptor.version": branch[len("refs/tags/") :], "versionDescriptor.versionType": "tag"}
    return {"versionDescriptor.version": short_branch(branch), "versionDescriptor.versionType": "branch"}


class RepoFiles:
    def __init__(
        self,
        cache: Cache,
        source: str,
        *,
        client: AzdoClient | None = None,
        refresh: bool = False,
        max_file_size_kb: int = 512,
        on_error: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._cache = cache
        self._source = source
        self._client = client
        self._refresh = refresh
        self._max_bytes = max_file_size_kb * 1024
        self._on_error = on_error
        self._projects: dict[str, str] | None = None

    def _canonical_project(self, project: str) -> str:
        """Project names in YAML are case-insensitive; cache folders are not."""
        if self._projects is None:
            listed = self._cache.get_list(self._source, COLLECTION, "projects")
            self._projects = {as_str(p.get("name")).lower(): as_str(p.get("name")) for p in listed if isinstance(p, dict)}
        return self._projects.get(project.lower(), project)

    def read(self, repo: RepoRef, path: str) -> str | None:
        path = path.strip("/") if repo.kind == "git" else path
        target = self._cache.file_path(self._source, repo.project, repo.repo_id, repo.branch, path)
        if self._client is None or self._cache.is_fresh(target, self._refresh):
            return self._content(self._cache.read(target))
        try:
            payload = self._fetch(repo, path)
        except NotFound:
            payload = {"_missing": True, "path": path}
        except AzdoHttpError as exc:
            if self._on_error:
                kind = "no access" if isinstance(exc, AccessDenied) else str(exc)
                self._on_error(repo.project, "repo_files", f"{repo.name}:{path}: {kind}")
            return None
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, str) and len(content.encode("utf-8", "ignore")) > self._max_bytes:
            payload = {"_too_large": True, "path": path}
        self._cache.write(target, payload)
        return self._content(payload)

    def _fetch(self, repo: RepoRef, path: str) -> Any:
        assert self._client is not None
        project = quote_segment(repo.project)
        if repo.kind == "tfvc":
            return self._client.get_json(f"{project}/_apis/tfvc/items", {"path": path, "includeContent": True})
        params: dict[str, Any] = {"path": "/" + path, "includeContent": True, "$format": "json"}
        params.update(version_params(repo.branch))
        return self._client.get_json(f"{project}/_apis/git/repositories/{quote_segment(repo.repo_id)}/items", params)

    @staticmethod
    def _content(payload: Any) -> str | None:
        if not isinstance(payload, dict) or payload.get("_missing") or payload.get("_too_large"):
            return None
        content = payload.get("content")
        return content if isinstance(content, str) else None

    def find_repo(self, project: str, name: str, ref: str | None) -> RepoRef | None:
        """Resolve ``project/name`` to a repo using the cached repository list."""
        wanted = name.lower()
        project = self._canonical_project(project)
        for raw in self._cache.get_list(self._source, project, "repositories"):
            if not isinstance(raw, dict):
                continue
            if as_str(raw.get("name")).lower() == wanted or as_str(raw.get("id")).lower() == wanted:
                branch = ref or as_str(raw.get("defaultBranch"))
                if not branch:
                    return None
                branch = branch if branch.startswith("refs/tags/") else short_branch(branch)
                return RepoRef(project, as_str(raw.get("id")), as_str(raw.get("name")), branch)
        return None
