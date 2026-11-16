"""``collect`` stage: walk every source and project, filling the raw cache (GET only)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..client.http import AccessDenied, AuthConfigError, AzdoClient, AzdoHttpError
from ..config.models import Config, SourceConfig
from ..detect.rules import load_rules
from ..filters import project_included
from ..util import as_dict, as_list, as_str, dig, iso, json_dumps, matches_any
from . import collectors
from .cache import Cache
from .context import CollectContext
from .files import RepoFiles

log = logging.getLogger(__name__)

ClientFactory = Callable[[SourceConfig], AzdoClient]


@dataclass
class CollectSummary:
    api_calls: int = 0
    projects: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)
    sources: int = 0

    @property
    def total_auth_failure(self) -> bool:
        return self.sources > 0 and len(self.failed_sources) == self.sources


def _has_tfvc(ctx: CollectContext, project: str) -> bool:
    detail = as_dict(ctx.cache.get(ctx.name, project, "project", "detail"))
    version_control = as_dict(dig(detail, "capabilities", "versioncontrol"))
    if as_str(version_control.get("sourceControlType")).lower() == "tfvc":
        return True
    if as_str(version_control.get("tfvcEnabled")).lower() == "true":
        return True
    for def_id in ctx.cache.item_ids(ctx.name, project, "build_definitions"):
        defn = as_dict(ctx.cache.get(ctx.name, project, "build_definitions", def_id))
        if as_str(dig(defn, "repository", "type")).lower() == "tfsversioncontrol":
            return True
    return False


def repo_index(cache: Cache, source: str, projects: list[str]) -> dict[str, str]:
    """Repository GUID -> owning project, from the cached repository lists."""
    index: dict[str, str] = {}
    for project in projects:
        for repo in cache.get_list(source, project, "repositories"):
            if isinstance(repo, dict) and repo.get("id"):
                index[as_str(repo["id"]).lower()] = project
    return index


def _collect_source(
    config: Config,
    source: SourceConfig,
    client: AzdoClient,
    cache: Cache,
    executor: ThreadPoolExecutor,
    project_filter: list[str],
    refresh: bool,
    summary: CollectSummary,
) -> None:
    ctx = CollectContext(config, source, client, cache, executor, refresh=refresh)
    api_version = client.api_version  # negotiates; raises on total auth failure
    projects = collectors.collect_projects(ctx)
    collectors.collect_collection_level(ctx)

    selected: list[dict[str, Any]] = []
    for project in projects:
        name = as_str(project["name"])
        if not project_included(source.projects, name):
            continue
        if project_filter and not matches_any(project_filter, name):
            continue
        if as_str(project.get("state") or "wellFormed").lower() != "wellformed":
            ctx.error(name, "projects", f"skipped: project state is {as_str(project.get('state'))}")
            continue
        selected.append(project)
    names = [as_str(p["name"]) for p in selected]
    log.info("source %s: %d of %d projects selected", source.name, len(selected), len(projects))

    # Pass 1: definitions and shared objects for every project, so that pass 2 can follow
    # cross-project references (template repositories, repos owned by another project).
    for i, project in enumerate(selected, start=1):
        log.info("[%s] %d/%d definitions: %s", source.name, i, len(selected), project["name"])
        with ctx.guard(as_str(project["name"]), "project"):
            collectors.collect_project_basics(ctx, project)

    rules = load_rules(config)
    files = RepoFiles(
        cache,
        source.name,
        client=client,
        refresh=refresh,
        max_file_size_kb=config.repo_scan.max_file_size_kb,
        on_error=ctx.error,
    )
    index = repo_index(cache, source.name, [as_str(p["name"]) for p in projects])
    for i, name in enumerate(names, start=1):
        log.info("[%s] %d/%d history and repositories: %s", source.name, i, len(names), name)
        with ctx.guard(name, "project"):
            collectors.collect_build_history(ctx, name)
            collectors.collect_release_history(ctx, name)
            collectors.collect_yaml_files(ctx, name, files, index)
            collectors.collect_repo_scans(ctx, name, rules, files, _has_tfvc(ctx, name))

    # Errors: replace rows for the projects handled in this run, keep the rest.
    errors_path = cache.source_file(source.name, "_errors.json")
    previous = [e for e in as_list(cache.read(errors_path)) if isinstance(e, dict)]
    handled = set(names)
    kept = [e for e in previous if e.get("project") and e.get("project") not in handled] if project_filter else []
    merged = sorted([*kept, *ctx.errors], key=lambda e: (e.get("project", ""), e.get("collector", ""), e.get("message", "")))
    errors_path.parent.mkdir(parents=True, exist_ok=True)
    errors_path.write_text(json_dumps(merged), encoding="utf-8")

    meta_path = cache.source_file(source.name, "_meta.json")
    meta = {
        "name": source.name,
        "type": source.type,
        "base_url": source.base_url,
        "api_version": api_version,
        "collected_at": iso(datetime.now(UTC)),
    }
    meta_path.write_text(json_dumps(meta), encoding="utf-8")
    summary.projects += len(selected)
    summary.errors.extend(ctx.errors)


def collect(
    config: Config,
    *,
    source_filter: str | None = None,
    project_filter: str | None = None,
    refresh: bool = False,
    client_factory: ClientFactory | None = None,
) -> CollectSummary:
    cache = Cache(config.run.cache_dir, config.run.cache_ttl_hours, config.run.resume)
    factory = client_factory or (lambda source: AzdoClient(source, config.run))
    summary = CollectSummary()
    projects = [project_filter] if project_filter else []
    with ThreadPoolExecutor(max_workers=config.run.concurrency, thread_name_prefix="collect") as executor:
        for source in config.sources:
            if source_filter and source.name != source_filter:
                continue
            summary.sources += 1
            try:
                client = factory(source)
            except AuthConfigError as exc:
                log.error("%s", exc)
                summary.failed_sources.append(source.name)
                summary.errors.append({"source": source.name, "project": "", "collector": "auth", "message": str(exc)})
                continue
            try:
                _collect_source(config, source, client, cache, executor, projects, refresh, summary)
            except AccessDenied as exc:
                log.error("source %s: authentication failed (HTTP %s)", source.name, exc.status)
                summary.failed_sources.append(source.name)
                summary.errors.append(
                    {"source": source.name, "project": "", "collector": "auth", "message": f"authentication failed (HTTP {exc.status})"}
                )
            except AzdoHttpError as exc:
                log.error("source %s: %s", source.name, exc)
                summary.failed_sources.append(source.name)
                summary.errors.append({"source": source.name, "project": "", "collector": "connect", "message": str(exc)})
            finally:
                summary.api_calls += client.calls
                client.close()
    return summary
