"""Collectors: one function (or table row) per AZDO resource. Everything is HTTP GET.

Simple list endpoints are declared in ``PROJECT_LISTS`` / ``COLLECTION_LISTS``; resources
that need follow-up calls have their own function below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..client.http import quote_segment
from ..detect.rules import RuleSet, manifest_globs, select_manifests
from ..filters import definition_included
from ..parse.yaml_loader import RepoRef, TemplateResolver
from ..util import as_bool, as_dict, as_int, as_list, as_str, dig, iso, short_branch
from .cache import COLLECTION
from .context import CollectContext
from .files import RepoFiles, version_params

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ListSpec:
    toggle: str  # name in the `collectors` config section
    resource: str  # cache folder
    path: str  # relative to base_url; {p} is the url-quoted project
    params: dict[str, Any] = field(default_factory=dict)
    area: str = "core"


COLLECTION_LISTS = [
    ListSpec("task_catalog", "task_catalog", "_apis/distributedtask/tasks"),
    ListSpec("installed_extensions", "extensions", "_apis/extensionmanagement/installedextensions", area="extmgmt"),
    ListSpec("agent_pools", "pools", "_apis/distributedtask/pools"),
    ListSpec("service_hooks", "service_hooks", "_apis/hooks/subscriptions"),
    ListSpec("package_feeds", "feeds", "_apis/packaging/feeds", area="feeds"),
]

PROJECT_LISTS = [
    ListSpec("repositories", "repositories", "{p}/_apis/git/repositories", {"includeHidden": True}),
    ListSpec("agent_pools", "queues", "{p}/_apis/distributedtask/queues"),
    ListSpec("task_groups", "task_groups", "{p}/_apis/distributedtask/taskgroups", {"expanded": True}),
    ListSpec("variable_groups", "variable_groups", "{p}/_apis/distributedtask/variablegroups"),
    ListSpec("service_connections", "service_connections", "{p}/_apis/serviceendpoint/endpoints", {"includeFailed": True}),
    ListSpec("deployment_groups", "deployment_groups", "{p}/_apis/distributedtask/deploymentgroups"),
    ListSpec("environments", "environments", "{p}/_apis/distributedtask/environments"),
    ListSpec("secure_files", "secure_files", "{p}/_apis/distributedtask/securefiles"),
    ListSpec("branch_policies", "branch_policies", "{p}/_apis/policy/configurations"),
    ListSpec("xaml_builds", "xaml_builds", "{p}/_apis/build/definitions", {"type": "xaml"}),
]


def _enabled(ctx: CollectContext, toggle: str) -> bool:
    return bool(getattr(ctx.config.collectors, toggle))


def _collect_list(ctx: CollectContext, project: str, spec: ListSpec) -> list[Any]:
    if not _enabled(ctx, spec.toggle):
        return []
    scope = COLLECTION if not project else project
    path = spec.path.replace("{p}", quote_segment(project))
    result: list[Any] = []
    with ctx.guard(project, spec.toggle):
        result = ctx.cached_list(scope, spec.resource, lambda: ctx.client.get_list(path, spec.params, area=spec.area))
    return result


# -- collection level ----------------------------------------------------------------


def collect_projects(ctx: CollectContext) -> list[dict[str, Any]]:
    """Project list is always needed, whatever the collector toggles say."""
    projects = ctx.cached_list(
        COLLECTION,
        "projects",
        lambda: ctx.client.get_list("_apis/projects", {"stateFilter": "All"}, page_size=100),
    )
    return [p for p in projects if isinstance(p, dict) and p.get("name")]


def collect_collection_level(ctx: CollectContext) -> None:
    for spec in COLLECTION_LISTS:
        items = _collect_list(ctx, "", spec)
        if spec.resource == "pools" and _enabled(ctx, "agent_capabilities"):

            def agents(pool: Any) -> None:
                pool_id = as_str(as_dict(pool).get("id"))
                if not pool_id:
                    return
                with ctx.guard("", "agent_capabilities"):
                    ctx.cached_list(
                        COLLECTION,
                        "pool_agents",
                        lambda: ctx.client.get_list(f"_apis/distributedtask/pools/{pool_id}/agents", {"includeCapabilities": True}),
                        pool_id,
                    )

            ctx.pmap(agents, items)


# -- project level: definitions and shared objects -------------------------------------


def collect_project_basics(ctx: CollectContext, project: dict[str, Any]) -> None:
    name = as_str(project["name"])
    p = quote_segment(name)
    if _enabled(ctx, "projects"):
        with ctx.guard(name, "projects"):
            ctx.cached_item(
                name,
                "project",
                "detail",
                lambda: ctx.client.get_json(
                    f"_apis/projects/{quote_segment(as_str(project.get('id')) or name)}", {"includeCapabilities": True}
                ),
            )
    lists = {spec.resource: _collect_list(ctx, name, spec) for spec in PROJECT_LISTS}

    def group_targets(group: Any) -> None:
        group_id = as_str(as_dict(group).get("id"))
        with ctx.guard(name, "deployment_groups"):
            ctx.cached_list(
                name,
                "deployment_group_targets",
                lambda: ctx.client.get_list(f"{p}/_apis/distributedtask/deploymentgroups/{group_id}/targets"),
                group_id,
            )

    ctx.pmap(group_targets, [g for g in lists["deployment_groups"] if as_dict(g).get("id") is not None])

    def environment(env: Any) -> None:
        env_id = as_str(as_dict(env).get("id"))
        with ctx.guard(name, "environments"):
            ctx.cached_item(
                name,
                "environment_details",
                env_id,
                lambda: ctx.client.get_json(f"{p}/_apis/distributedtask/environments/{env_id}", {"expands": "resourceReferences"}),
            )
            ctx.cached_list(
                name,
                "environment_checks",
                lambda: ctx.client.get_list(
                    f"{p}/_apis/pipelines/checks/configurations",
                    {"resourceType": "environment", "resourceId": env_id, "$expand": "settings"},
                ),
                env_id,
            )

    ctx.pmap(environment, [e for e in lists["environments"] if as_dict(e).get("id") is not None])

    if _enabled(ctx, "build_definitions"):
        with ctx.guard(name, "build_definitions"):
            listed = ctx.cached_list(
                name,
                "build_definitions",
                lambda: ctx.client.get_list(f"{p}/_apis/build/definitions", {"includeLatestBuilds": True}),
            )

            def build_definition(item: Any) -> None:
                def_id = as_str(as_dict(item).get("id"))
                with ctx.guard(name, "build_definitions"):
                    ctx.cached_item(name, "build_definitions", def_id, lambda: ctx.client.get_json(f"{p}/_apis/build/definitions/{def_id}"))

            ctx.pmap(build_definition, [d for d in listed if _wanted(ctx, d)])

    if _enabled(ctx, "release_definitions"):
        with ctx.guard(name, "release_definitions"):
            listed = ctx.cached_list(
                name,
                "release_definitions",
                lambda: ctx.client.get_list(
                    f"{p}/_apis/release/definitions",
                    {"$expand": "Environments,Artifacts,Triggers,Variables"},
                    area="release",
                ),
            )

            def release_definition(item: Any) -> None:
                def_id = as_str(as_dict(item).get("id"))
                with ctx.guard(name, "release_definitions"):
                    ctx.cached_item(
                        name,
                        "release_definitions",
                        def_id,
                        lambda: ctx.client.get_json(f"{p}/_apis/release/definitions/{def_id}", area="release"),
                    )

            ctx.pmap(release_definition, [d for d in listed if _wanted(ctx, d, release=True)])


def _wanted(ctx: CollectContext, defn: Any, *, release: bool = False) -> bool:
    return isinstance(defn, dict) and defn.get("id") is not None and definition_included(ctx.config.filters, defn, release=release)


# -- project level: history -----------------------------------------------------------


def _since(ctx: CollectContext) -> str:
    return iso(datetime.now(UTC) - timedelta(days=ctx.config.history.lookback_days))


def collect_build_history(ctx: CollectContext, project: str) -> None:
    if not _enabled(ctx, "build_runs"):
        return
    p = quote_segment(project)
    since = _since(ctx)
    limit = ctx.config.history.max_runs_per_definition
    timelines = ctx.config.yaml_pipelines.use_timeline_expansion

    def runs(item: dict[str, Any]) -> None:
        def_id = as_str(item.get("id"))
        if not item.get("latestBuild") and not item.get("latestCompletedBuild"):
            return  # never ran: nothing to fetch
        with ctx.guard(project, "build_runs"):
            builds = ctx.cached_list(
                project,
                "build_runs",
                lambda: ctx.client.get_list(
                    f"{p}/_apis/build/builds",
                    {"definitions": def_id, "minTime": since, "$top": limit, "queryOrder": "finishTimeDescending"},
                    max_items=limit,
                ),
                def_id,
            )
            full = as_dict(ctx.cache.get(ctx.name, project, "build_definitions", def_id))
            if not timelines or as_int(dig(full, "process", "type")) != 2:
                return
            good = last_successful_build(builds)
            if good is not None:
                build_id = as_str(good.get("id"))
                ctx.cached_item(
                    project,
                    "build_timelines",
                    def_id,
                    lambda: ctx.client.get_json(f"{p}/_apis/build/builds/{build_id}/timeline"),
                )

    listed = ctx.cache.get_list(ctx.name, project, "build_definitions")
    ctx.pmap(runs, [d for d in listed if _wanted(ctx, d)])


def last_successful_build(builds: list[Any]) -> dict[str, Any] | None:
    for build in builds:
        if isinstance(build, dict) and as_str(build.get("result")).lower() in ("succeeded", "partiallysucceeded"):
            return build
    return None


def collect_release_history(ctx: CollectContext, project: str) -> None:
    if not _enabled(ctx, "release_runs"):
        return
    p = quote_segment(project)
    since = _since(ctx)
    limit = ctx.config.history.max_runs_per_definition

    def runs(item: dict[str, Any]) -> None:
        def_id = as_str(item.get("id"))
        stages = max(1, len(as_list(item.get("environments"))))
        with ctx.guard(project, "release_runs"):
            ctx.cached_list(
                project,
                "releases",
                lambda: ctx.client.get_list(
                    f"{p}/_apis/release/releases",
                    {"definitionId": def_id, "minCreatedTime": since, "$top": limit, "$expand": "environments"},
                    area="release",
                    max_items=limit,
                ),
                def_id,
            )
            ctx.cached_list(
                project,
                "deployments",
                lambda: ctx.client.get_list(
                    f"{p}/_apis/release/deployments",
                    {"definitionId": def_id, "minStartedTime": since, "$top": limit * stages},
                    area="release",
                    max_items=limit * stages,
                ),
                def_id,
            )

    listed = ctx.cache.get_list(ctx.name, project, "release_definitions")
    ctx.pmap(runs, [d for d in listed if _wanted(ctx, d, release=True)])


# -- project level: repositories -----------------------------------------------------


def scan_branch(ctx_branch: str, default_branch: str) -> str:
    return short_branch(default_branch) if ctx_branch == "default" else ctx_branch


def tfvc_relative(path: str, project: str) -> str:
    prefix = f"$/{project}/"
    return path[len(prefix) :] if path.lower().startswith(prefix.lower()) else path.lstrip("$/")


def collect_repo_scans(ctx: CollectContext, project: str, rules: RuleSet, files: RepoFiles, tfvc: bool) -> None:
    scan = ctx.config.repo_scan
    if not scan.enabled or not _enabled(ctx, "repositories"):
        return
    p = quote_segment(project)
    globs = manifest_globs(rules)

    def git_repo(repo: dict[str, Any]) -> None:
        repo_id = as_str(repo.get("id"))
        branch = scan_branch(scan.branch, as_str(repo.get("defaultBranch")))
        if as_bool(repo.get("isDisabled")) or not branch:
            return
        ref = RepoRef(project, repo_id, as_str(repo.get("name")), branch)
        base = f"{p}/_apis/git/repositories/{quote_segment(repo_id)}"
        with ctx.guard(project, "repo_scan"):
            ctx.cached_list(
                project,
                "repo_commits",
                lambda: ctx.client.get_list(
                    f"{base}/commits",
                    {"searchCriteria.$top": 1, "searchCriteria.itemVersion.version": branch},
                    max_items=1,
                ),
                repo_id,
            )

            def tree() -> dict[str, Any]:
                items = ctx.client.get_list(f"{base}/items", {"recursionLevel": "Full", **version_params(branch)})
                return _tree_payload(items, scan.max_tree_entries)

            payload = as_dict(ctx.cached_item(project, "repo_trees", repo_id, tree))
            paths = [
                as_str(i.get("path")).lstrip("/")
                for i in as_list(payload.get("value"))
                if isinstance(i, dict) and not i.get("isFolder") and as_str(i.get("gitObjectType") or "blob") == "blob"
            ]
            selected, _ = select_manifests(paths, globs, scan)
            for path in selected:
                files.read(ref, path)

    repos = [r for r in ctx.cache.get_list(ctx.name, project, "repositories") if isinstance(r, dict) and r.get("id")]
    ctx.pmap(git_repo, repos)

    if tfvc and scan.tfvc.enabled:
        with ctx.guard(project, "tfvc_scan"):
            # Newest changeset under the project root: the TFVC counterpart of "last commit".
            ctx.cached_list(
                project,
                "repo_commits",
                lambda: ctx.client.get_list(
                    f"{p}/_apis/tfvc/changesets",
                    {"searchCriteria.itemPath": f"$/{project}", "$top": 1, "$orderby": "id desc"},
                    max_items=1,
                ),
                "tfvc",
            )
        with ctx.guard(project, "tfvc_scan"):

            def tfvc_tree() -> dict[str, Any]:
                items = ctx.client.get_list(f"{p}/_apis/tfvc/items", {"scopePath": f"$/{project}", "recursionLevel": "Full"})
                # $/Project/a/b has depth 2: keep entries within max_depth below the project root.
                kept = [
                    i
                    for i in items
                    if isinstance(i, dict) and tfvc_relative(as_str(i.get("path")), project).count("/") < scan.tfvc.max_depth
                ]
                return _tree_payload(kept, scan.max_tree_entries)

            payload = as_dict(ctx.cached_item(project, "repo_trees", "tfvc", tfvc_tree))
            full_paths = {
                tfvc_relative(as_str(i.get("path")), project): as_str(i.get("path"))
                for i in as_list(payload.get("value"))
                if isinstance(i, dict) and not i.get("isFolder")
            }
            selected, _ = select_manifests(sorted(full_paths), globs, scan)
            ref = RepoRef(project, "tfvc", f"$/{project}", "tfvc", kind="tfvc")
            for path in selected:
                files.read(ref, full_paths[path])


def _tree_payload(items: list[Any], limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"count": min(len(items), limit), "value": items[:limit]}
    if len(items) > limit:
        payload["_truncated"] = True
    return payload


def collect_yaml_files(ctx: CollectContext, project: str, files: RepoFiles, repo_index: dict[str, str]) -> None:
    """Fetch each YAML pipeline file and every template it includes, so analysis works offline."""
    if not _enabled(ctx, "build_definitions"):
        return
    yaml_config = ctx.config.yaml_pipelines

    def fetch(def_id: str) -> None:
        defn = as_dict(ctx.cache.get(ctx.name, project, "build_definitions", def_id))
        if as_int(dig(defn, "process", "type")) != 2:
            return
        repo = yaml_repo_ref(defn, project, repo_index)
        if repo is None:
            return
        with ctx.guard(project, "yaml_files"):
            TemplateResolver(files, resolve_templates=yaml_config.resolve_templates, max_depth=yaml_config.max_template_depth).resolve(
                repo, as_str(dig(defn, "process", "yamlFilename"))
            )

    ctx.pmap(fetch, ctx.cache.item_ids(ctx.name, project, "build_definitions"))


def yaml_repo_ref(defn: dict[str, Any], project: str, repo_index: dict[str, str]) -> RepoRef | None:
    """The Azure Repos Git repo a YAML definition reads its file from, if we can read it.

    ``repo_index`` maps repository GUID -> owning project (the repo may be in another project).
    """
    repo = as_dict(defn.get("repository"))
    if as_str(repo.get("type")).lower() != "tfsgit" or not repo.get("id"):
        return None
    branch = short_branch(as_str(repo.get("defaultBranch")))
    if not branch:
        return None
    owner = repo_index.get(as_str(repo["id"]).lower(), project)
    return RepoRef(owner, as_str(repo["id"]), as_str(repo.get("name")), branch)
