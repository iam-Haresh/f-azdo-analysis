"""Raw cache -> normalized model. Works fully offline.

Payload handling is tolerant: unknown fields are ignored and missing ones never crash
(older Azure DevOps Server versions omit many).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..collect.cache import COLLECTION, Cache
from ..collect.collectors import last_successful_build, yaml_repo_ref
from ..collect.files import RepoFiles
from ..collect.runner import repo_index
from ..config.models import Config
from ..detect.rules import RuleSet
from ..filters import definition_included, project_included
from ..model.entities import (
    Agent,
    AgentPool,
    AgentQueue,
    BranchPolicy,
    BuildPipeline,
    CollectorError,
    DeploymentGroup,
    DeploymentGroupTarget,
    Environment,
    EnvironmentResource,
    Estate,
    Extension,
    Feed,
    Project,
    ReleasePipeline,
    Repo,
    SecureFile,
    ServiceConnection,
    ServiceHook,
    Source,
    TaskDefinition,
    TaskGroup,
    VariableGroup,
    make_id,
)
from ..parse.classic_build import parse_classic_build, parse_common
from ..parse.env import ParseEnv
from ..parse.release import parse_release
from ..parse.steps import StepFactory, TaskCatalog
from ..parse.yaml_pipeline import parse_timeline, parse_yaml_pipeline
from ..util import as_bool, as_dict, as_int, as_list, as_str, compile_pattern, dig, short_branch, short_hash
from .privacy import Privacy

log = logging.getLogger(__name__)


@dataclass
class RunRecord:
    finished: str = ""
    started: str = ""
    result: str = ""
    branch: str = ""
    requester: str = ""
    reason: str = ""
    stage: str = ""  # releases: stage name


@dataclass
class RunData:
    """Run history kept beside the model; ``analyze.activity`` turns it into ``RunStats``."""

    builds: dict[str, list[RunRecord]] = field(default_factory=dict)
    latest_build: dict[str, RunRecord] = field(default_factory=dict)  # from the definition list
    releases: dict[str, list[RunRecord]] = field(default_factory=dict)
    deployments: dict[str, list[RunRecord]] = field(default_factory=dict)


def _web(payload: dict[str, Any]) -> str:
    return as_str(dig(payload, "_links", "web", "href"))


class Normalizer:
    def __init__(self, config: Config, cache: Cache, rules: RuleSet) -> None:
        self.config = config
        self.cache = cache
        self.rules = rules
        self.privacy = Privacy(config)
        self.estate = Estate()
        self.runs = RunData()

    def run(self) -> tuple[Estate, RunData]:
        collected: list[str] = []
        for source in self.config.sources:
            meta = as_dict(self.cache.read(self.cache.source_file(source.name, "_meta.json")))
            self.estate.sources.append(
                Source(
                    name=source.name,
                    type=source.type,
                    base_url=source.base_url,
                    api_version=as_str(meta.get("api_version")),
                    collected_at=as_str(meta.get("collected_at")),
                )
            )
            if meta.get("collected_at"):
                collected.append(as_str(meta["collected_at"]))
            self._source(source.name)
            for row in as_list(self.cache.read(self.cache.source_file(source.name, "_errors.json"))):
                if isinstance(row, dict):
                    self.estate.errors.append(CollectorError(**{k: as_str(v) for k, v in row.items()}))
        # "Now" for activity classification is the newest collection time, so that the same
        # cache and config always produce the same output.
        self.estate.as_of = max(collected) if collected else ""
        return self.estate, self.runs

    # -- per source ----------------------------------------------------------------

    def _source(self, source: str) -> None:
        cache, estate = self.cache, self.estate
        raw_projects = [p for p in cache.get_list(source, COLLECTION, "projects") if isinstance(p, dict)]
        source_config = next(s for s in self.config.sources if s.name == source)
        projects: list[Project] = []
        for raw in sorted(raw_projects, key=lambda p: as_str(p.get("name")).lower()):
            name = as_str(raw.get("name"))
            state = as_str(raw.get("state") or "wellFormed")
            included = project_included(source_config.projects, name) and state.lower() == "wellformed"
            detail = as_dict(cache.get(source, name, "project", "detail"))
            version_control = as_dict(dig(detail, "capabilities", "versioncontrol"))
            kinds = []
            if (
                as_str(version_control.get("gitEnabled")).lower() == "true"
                or as_str(version_control.get("sourceControlType")).lower() == "git"
            ):
                kinds.append("Git")
            if (
                as_str(version_control.get("tfvcEnabled")).lower() == "true"
                or as_str(version_control.get("sourceControlType")).lower() == "tfvc"
            ):
                kinds.append("TFVC")
            project = Project(
                id=make_id(source, name, "project", as_str(raw.get("id"))),
                source=source,
                azdo_id=as_str(raw.get("id")),
                name=name,
                state=state,
                source_control=kinds,
                process_template=as_str(dig(detail, "capabilities", "processTemplate", "templateName")),
                included=included,
                gitlab_path=self.config.gitlab.group_path_template.format(source=source, project=name),
            )
            projects.append(project)
        estate.projects.extend(projects)

        raw_tasks = cache.get_list(source, COLLECTION, "task_catalog")
        catalog = TaskCatalog(raw_tasks)
        self._task_definitions(source, catalog)
        self._extensions(source)
        pools = self._pools(source)
        self._hooks_and_feeds(source, {p.azdo_id: p.name for p in projects})

        analysed = [p for p in projects if p.included]
        index = repo_index(cache, source, [p.name for p in projects])
        envs: dict[str, ParseEnv] = {}
        for project in analysed:
            envs[project.name] = self._project_objects(source, project, catalog, pools)
        repos_by_guid = {r.azdo_id.lower(): r for r in estate.repos if r.source == source and r.type == "git"}
        files = RepoFiles(cache, source)
        for project in analysed:
            self._builds(source, project, envs[project.name], index, repos_by_guid, files)
            self._releases(source, project, envs[project.name])

    def _task_definitions(self, source: str, catalog: TaskCatalog) -> None:
        for task_id, versions in sorted(catalog.entries().items()):
            latest = versions[max(versions)]
            self.estate.task_definitions.append(
                TaskDefinition(
                    id=task_id,
                    source=source,
                    name=as_str(latest.get("name")),
                    friendly_name=as_str(latest.get("friendlyName")),
                    majors=sorted(versions),
                    author=as_str(latest.get("author")),
                    contribution_id=as_str(latest.get("contributionIdentifier")),
                    origin=TaskCatalog.origin(latest),  # type: ignore[arg-type,unused-ignore]
                )
            )

    def _extensions(self, source: str) -> None:
        for raw in self.cache.get_list(source, COLLECTION, "extensions"):
            if not isinstance(raw, dict):
                continue
            tasks = sorted(
                {
                    as_str(dig(c, "properties", "name") or as_str(c.get("id")).rsplit(".", 1)[-1])
                    for c in as_list(raw.get("contributions"))
                    if isinstance(c, dict) and as_str(c.get("type")).endswith("distributed-task.task")
                }
            )
            publisher, name = as_str(raw.get("publisherId")), as_str(raw.get("extensionId"))
            self.estate.extensions.append(
                Extension(
                    id=f"{source}/extension/{publisher}.{name}",
                    source=source,
                    publisher=as_str(raw.get("publisherName")) or publisher,
                    name=as_str(raw.get("extensionName")) or name,
                    version=as_str(raw.get("version")),
                    builtin="builtin" in as_str(raw.get("flags")).lower(),
                    disabled="disabled" in as_str(dig(raw, "installState", "flags")).lower(),
                    tasks=tasks,
                )
            )
        self.estate.extensions.sort(key=lambda e: e.id)

    def _pools(self, source: str) -> dict[str, AgentPool]:
        keys = [compile_pattern(p) for p in self.rules.runner.capability_keys]
        pools: dict[str, AgentPool] = {}
        for raw in self.cache.get_list(source, COLLECTION, "pools"):
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            pool_id = as_str(raw["id"])
            pool = AgentPool(
                id=f"{source}/pool/{pool_id}",
                source=source,
                azdo_id=pool_id,
                name=as_str(raw.get("name")),
                is_hosted=as_bool(raw.get("isHosted")),
                pool_type=as_str(raw.get("poolType")),
            )
            presence: dict[str, int] = {}
            for agent_raw in self.cache.get_list_item(source, COLLECTION, "pool_agents", pool_id):
                if not isinstance(agent_raw, dict):
                    continue
                capabilities = {
                    **{str(k): as_str(v) for k, v in as_dict(agent_raw.get("systemCapabilities")).items()},
                    **{str(k): as_str(v) for k, v in as_dict(agent_raw.get("userCapabilities")).items()},
                }
                kept = {k: v for k, v in capabilities.items() if any(rx.search(k) for rx in keys)}
                os_name = capabilities.get("Agent.OS") or as_str(agent_raw.get("osDescription"))
                pool.agents.append(
                    Agent(
                        name=as_str(agent_raw.get("name")),
                        os=os_name,
                        version=as_str(agent_raw.get("version")),
                        status=as_str(agent_raw.get("status")),
                        enabled=as_bool(agent_raw.get("enabled", True)),
                        capabilities=dict(sorted(kept.items())),
                    )
                )
                for key in kept:
                    presence[key] = presence.get(key, 0) + 1
            pool.agents.sort(key=lambda a: a.name.lower())
            pool.os = sorted({a.os for a in pool.agents if a.os})
            total = len(pool.agents)
            pool.key_capabilities = {key: f"{count}/{total} agents" for key, count in sorted(presence.items())}
            pools[pool_id] = pool
        self.estate.agent_pools.extend(sorted(pools.values(), key=lambda p: p.id))
        return pools

    def _hooks_and_feeds(self, source: str, project_names: dict[str, str]) -> None:
        for raw in self.cache.get_list(source, COLLECTION, "service_hooks"):
            if not isinstance(raw, dict):
                continue
            inputs = as_dict(raw.get("consumerInputs"))
            self.estate.service_hooks.append(
                ServiceHook(
                    id=f"{source}/hook/{as_str(raw.get('id'))}",
                    source=source,
                    project=project_names.get(as_str(dig(raw, "publisherInputs", "projectId")), ""),
                    event_type=as_str(raw.get("eventType")),
                    consumer=as_str(raw.get("consumerId")),
                    action=as_str(raw.get("consumerActionId")),
                    target=Privacy.host_only(as_str(inputs.get("url") or inputs.get("serverBaseUrl"))),
                    status=as_str(raw.get("status")),
                )
            )
        for raw in self.cache.get_list(source, COLLECTION, "feeds"):
            if not isinstance(raw, dict):
                continue
            self.estate.feeds.append(
                Feed(
                    id=f"{source}/feed/{as_str(raw.get('id'))}",
                    source=source,
                    project=as_str(dig(raw, "project", "name")),
                    name=as_str(raw.get("name")),
                    upstreams=sorted(
                        f"{as_str(u.get('protocol'))}:{as_str(u.get('name'))}"
                        for u in as_list(raw.get("upstreamSources"))
                        if isinstance(u, dict)
                    ),
                )
            )
        self.estate.service_hooks.sort(key=lambda h: h.id)
        self.estate.feeds.sort(key=lambda f: f.id)

    # -- per project: shared objects ----------------------------------------------------

    def _project_objects(self, source: str, project: Project, catalog: TaskCatalog, pools: dict[str, AgentPool]) -> ParseEnv:
        cache, estate, name = self.cache, self.estate, project.name

        for raw in cache.get_list(source, name, "repositories"):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            repo_id = as_str(raw["id"])
            commits = cache.get_list_item(source, name, "repo_commits", repo_id)
            size = as_int(raw.get("size"))
            estate.repos.append(
                Repo(
                    id=make_id(source, name, "repo", repo_id),
                    source=source,
                    project=name,
                    azdo_id=repo_id,
                    name=as_str(raw.get("name")),
                    url=as_str(raw.get("webUrl") or raw.get("remoteUrl")),
                    default_branch=short_branch(as_str(raw.get("defaultBranch"))),
                    size_kb=size // 1024 if size is not None else None,
                    last_commit=as_str(dig(commits, 0, "committer", "date") or dig(commits, 0, "author", "date")),
                    disabled=as_bool(raw.get("isDisabled")),
                    is_fork=as_bool(raw.get("isFork")),
                    scan_status="empty" if not raw.get("defaultBranch") else "not_scanned",
                )
            )
        if "TFVC" in project.source_control or cache.get(source, name, "repo_trees", "tfvc") is not None:
            estate.repos.append(self._tfvc_repo(source, name))

        queues: dict[str, AgentQueue] = {}
        for raw in cache.get_list(source, name, "queues"):
            if isinstance(raw, dict) and raw.get("id") is not None:
                queue = AgentQueue(
                    id=make_id(source, name, "queue", raw["id"]),
                    source=source,
                    project=name,
                    azdo_id=as_str(raw["id"]),
                    name=as_str(raw.get("name")),
                    pool_id=as_str(dig(raw, "pool", "id")),
                    pool_name=as_str(dig(raw, "pool", "name")),
                )
                queues[queue.azdo_id] = queue
        estate.agent_queues.extend(sorted(queues.values(), key=lambda q: q.id))

        env = ParseEnv(
            config=self.config,
            source=source,
            project=name,
            factory=StepFactory(catalog, {}, self.privacy, self.rules.pipeline),
            privacy=self.privacy,
            queues=queues,
            pools=pools,
        )

        for raw in cache.get_list(source, name, "variable_groups"):
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            group_id = make_id(source, name, "variable_group", raw["id"])
            kind = as_str(raw.get("type") or "Vsts")
            group = VariableGroup(
                id=group_id,
                source=source,
                project=name,
                azdo_id=as_str(raw["id"]),
                name=as_str(raw.get("name")),
                type=kind,
                key_vault=as_str(dig(raw, "providerData", "vault")),
            )
            for var_name, var in sorted(as_dict(raw.get("variables")).items()):
                is_vault = kind.lower() == "azurekeyvault"
                variable = env.variable(
                    var_name,
                    {"isSecret": True} if is_vault else var,
                    scope="group",
                    owner=group_id,
                    source="keyvault" if is_vault else "group",
                )
                group.variables.append(variable)
            group.secret_count = sum(1 for v in group.variables if v.is_secret)
            estate.variable_groups.append(group)
            env.variable_groups[group.azdo_id] = group
            env.variable_groups_by_name[group.name.lower()] = group

        raw_groups: dict[str, list[dict[str, Any]]] = {}
        for raw in cache.get_list(source, name, "task_groups"):
            if isinstance(raw, dict) and raw.get("id"):
                raw_groups.setdefault(as_str(raw["id"]).lower(), []).append(raw)
        env.factory = StepFactory(catalog, raw_groups, self.privacy, self.rules.pipeline)
        for guid, versions in sorted(raw_groups.items()):
            for raw in sorted(versions, key=lambda v: as_int(dig(v, "version", "major")) or 0):
                major = as_int(dig(raw, "version", "major")) or 0
                names = []
                for task in as_list(raw.get("tasks")):
                    if isinstance(task, dict):
                        names.append(env.factory.classic(task, "classic_build").task_name)
                estate.task_groups.append(
                    TaskGroup(
                        id=make_id(source, name, "task_group", f"{guid}@{major}"),
                        source=source,
                        project=name,
                        azdo_id=guid,
                        name=as_str(raw.get("name")),
                        version=str(major),
                        is_draft=as_bool(dig(raw, "version", "isTest")) or bool(raw.get("parentDefinitionId")),
                        task_names=names,
                    )
                )

        for raw in cache.get_list(source, name, "service_connections"):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            shared = sorted(
                as_str(dig(ref, "projectReference", "name"))
                for ref in as_list(raw.get("serviceEndpointProjectReferences"))
                if isinstance(ref, dict) and dig(ref, "projectReference", "name")
            )
            estate.service_connections.append(
                ServiceConnection(
                    id=make_id(source, name, "service_connection", raw["id"]),
                    source=source,
                    project=name,
                    azdo_id=as_str(raw["id"]).lower(),
                    name=as_str(raw.get("name")),
                    type=as_str(raw.get("type")),
                    url=Privacy.host_only(as_str(raw.get("url"))),
                    auth_scheme=as_str(dig(raw, "authorization", "scheme")),
                    is_shared=as_bool(raw.get("isShared")) or len(shared) > 1,
                    shared_projects=shared,
                )
            )

        for raw in cache.get_list(source, name, "deployment_groups"):
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            group_azdo = as_str(raw["id"])
            targets = []
            for target in cache.get_list_item(source, name, "deployment_group_targets", group_azdo):
                if isinstance(target, dict):
                    targets.append(
                        DeploymentGroupTarget(
                            name=as_str(dig(target, "agent", "name")),
                            tags=sorted(as_str(t) for t in as_list(target.get("tags"))),
                            os=as_str(dig(target, "agent", "osDescription")),
                            online=as_bool(target.get("online")) or as_str(dig(target, "agent", "status")).lower() == "online",
                        )
                    )
            deployment_group = DeploymentGroup(
                id=make_id(source, name, "deployment_group", group_azdo),
                source=source,
                project=name,
                azdo_id=group_azdo,
                name=as_str(raw.get("name")),
                pool_name=as_str(dig(raw, "pool", "name")),
                targets=sorted(targets, key=lambda t: t.name.lower()),
                tags=sorted({tag for t in targets for tag in t.tags} | {as_str(t) for t in as_list(raw.get("machineTags"))}),
            )
            estate.deployment_groups.append(deployment_group)
            env.deployment_groups[group_azdo] = deployment_group

        for raw in cache.get_list(source, name, "environments"):
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            env_id = as_str(raw["id"])
            detail = as_dict(cache.get(source, name, "environment_details", env_id)) or raw
            checks = []
            for check in cache.get_list_item(source, name, "environment_checks", env_id):
                if isinstance(check, dict):
                    kind = as_str(dig(check, "settings", "displayName") or dig(check, "type", "name"))
                    approvers = [
                        self.privacy.identity(as_str(a.get("displayName")), is_group=as_bool(a.get("isContainer")))
                        for a in as_list(dig(check, "settings", "approvers"))
                        if isinstance(a, dict)
                    ]
                    checks.append(f"{kind}: {', '.join(approvers)}" if approvers else kind)
            estate.environments.append(
                Environment(
                    id=make_id(source, name, "environment", env_id),
                    source=source,
                    project=name,
                    azdo_id=env_id,
                    name=as_str(raw.get("name")),
                    resources=[
                        EnvironmentResource(name=as_str(r.get("name")), type=as_str(r.get("type")))
                        for r in as_list(detail.get("resources"))
                        if isinstance(r, dict)
                    ],
                    checks=sorted(checks),
                )
            )

        for raw in cache.get_list(source, name, "secure_files"):
            if isinstance(raw, dict) and raw.get("id"):
                estate.secure_files.append(
                    SecureFile(
                        id=make_id(source, name, "secure_file", raw["id"]),
                        source=source,
                        project=name,
                        azdo_id=as_str(raw["id"]).lower(),
                        name=as_str(raw.get("name")),
                    )
                )

        for raw in cache.get_list(source, name, "branch_policies"):
            settings = as_dict(as_dict(raw).get("settings"))
            if not isinstance(raw, dict) or settings.get("buildDefinitionId") is None:
                continue  # only build-validation policies link pipelines to branches
            for scope in as_list(settings.get("scope")) or [{}]:
                scope = as_dict(scope)
                estate.branch_policies.append(
                    BranchPolicy(
                        id=make_id(source, name, "policy", f"{as_str(raw.get('id'))}-{short_hash(str(scope), 6)}"),
                        source=source,
                        project=name,
                        repo_id=as_str(scope.get("repositoryId")).lower(),
                        branch=short_branch(as_str(scope.get("refName"))),
                        match_kind=as_str(scope.get("matchKind")),
                        build_id=make_id(source, name, "build", settings["buildDefinitionId"]),
                        blocking=as_bool(raw.get("isBlocking")),
                        enabled=as_bool(raw.get("isEnabled", True)),
                        path_filters=[as_str(p) for p in as_list(settings.get("filenamePatterns"))],
                    )
                )
        return env

    def _tfvc_repo(self, source: str, project: str) -> Repo:
        return Repo(
            id=make_id(source, project, "repo", "tfvc"),
            source=source,
            project=project,
            azdo_id="tfvc",
            name=f"$/{project}",
            type="tfvc",
            flags=["tfvc_source"],
        )

    # -- per project: pipelines ----------------------------------------------------------

    def _link_repo(self, pipeline: BuildPipeline, defn: dict[str, Any], source: str, project: str, repos_by_guid: dict[str, Repo]) -> None:
        repo = as_dict(defn.get("repository"))
        kind = as_str(repo.get("type"))
        pipeline.repo_type = kind
        pipeline.repo_name = as_str(repo.get("name"))
        pipeline.default_branch = short_branch(as_str(repo.get("defaultBranch")))
        if not repo:
            pipeline.flags.append("repo_missing")
            return
        if kind.lower() == "tfsgit":
            found = repos_by_guid.get(as_str(repo.get("id")).lower())
            if found is None:
                pipeline.flags.append("repo_missing")
                return
            pipeline.repo_id, pipeline.repo_project = found.id, found.project
            if found.disabled:
                pipeline.flags.append("repo_disabled")
        elif kind.lower() == "tfsversioncontrol":
            repo_id = make_id(source, project, "repo", "tfvc")
            if not any(r.id == repo_id for r in self.estate.repos):
                self.estate.repos.append(self._tfvc_repo(source, project))
            pipeline.repo_id, pipeline.repo_project = repo_id, project
            pipeline.flags.append("tfvc_source")
        else:
            # GitHub / Bitbucket / external Git / SVN: cannot be scanned, pipeline evidence only.
            ext_id = make_id(source, project, "repo", "ext-" + short_hash(f"{kind}:{as_str(repo.get('id') or repo.get('name'))}", 10))
            if not any(r.id == ext_id for r in self.estate.repos):
                self.estate.repos.append(
                    Repo(
                        id=ext_id,
                        source=source,
                        project=project,
                        azdo_id=as_str(repo.get("id")),
                        name=as_str(repo.get("name")) or as_str(repo.get("id")),
                        type="external",
                        external_type=kind,
                        default_branch=pipeline.default_branch,
                        scan_status="external",
                    )
                )
            pipeline.repo_id, pipeline.repo_project = ext_id, project
            pipeline.flags.append("external_repo")
        pipeline.cross_project = bool(pipeline.repo_project) and pipeline.repo_project != project

    def _builds(
        self,
        source: str,
        project: Project,
        env: ParseEnv,
        index: dict[str, str],
        repos_by_guid: dict[str, Repo],
        files: RepoFiles,
    ) -> None:
        cache, name = self.cache, project.name
        listed = {as_str(d.get("id")): d for d in cache.get_list(source, name, "build_definitions") if isinstance(d, dict)}
        for def_id in sorted(cache.item_ids(source, name, "build_definitions"), key=lambda i: (len(i), i)):
            defn = as_dict(cache.get(source, name, "build_definitions", def_id))
            if not defn or not definition_included(self.config.filters, defn):
                continue
            pipeline = BuildPipeline(
                id=make_id(source, name, "build", def_id),
                source=source,
                project=name,
                azdo_id=def_id,
                name=as_str(defn.get("name")),
                folder=as_str(defn.get("path")) or "\\",
                url=_web(defn),
                queue_status=as_str(defn.get("queueStatus") or "enabled"),
                is_draft=as_str(defn.get("quality")).lower() == "draft",
            )
            self._link_repo(pipeline, defn, source, name, repos_by_guid)
            process_type = as_int(dig(defn, "process", "type")) or 1
            try:
                if process_type == 2:
                    pipeline.kind = "yaml"
                    parse_yaml_pipeline(defn, pipeline, env, files, yaml_repo_ref(defn, name, index))
                else:
                    parse_classic_build(defn, pipeline, env)
            except Exception as exc:  # noqa: BLE001 - one odd definition must not stop the run
                log.warning("cannot parse build definition %s: %s", pipeline.id, exc)
                pipeline.flags.append(f"parse_error:{type(exc).__name__}")
                self.estate.errors.append(
                    CollectorError(source=source, project=name, collector="parse_build", message=f"{pipeline.name}: {exc}")
                )

            builds = cache.get_list_item(source, name, "build_runs", def_id)
            self.runs.builds[pipeline.id] = [self._build_record(b) for b in builds if isinstance(b, dict)]
            latest = as_dict(dig(listed, def_id, "latestBuild") or defn.get("latestBuild"))
            if latest:
                self.runs.latest_build[pipeline.id] = self._build_record(latest)
            timeline = cache.get(source, name, "build_timelines", def_id)
            if isinstance(timeline, dict):
                parse_timeline(timeline, last_successful_build(builds), pipeline, env)
            self.estate.builds.append(pipeline)

        for raw in cache.get_list(source, name, "xaml_builds"):
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            xaml = BuildPipeline(
                id=make_id(source, name, "xaml", raw["id"]),
                source=source,
                project=name,
                azdo_id=as_str(raw["id"]),
                name=as_str(raw.get("name")),
                kind="xaml",
                url=_web(raw),
                flags=["xaml_legacy"],
            )
            parse_common({}, xaml, env)
            self.estate.builds.append(xaml)

    def _build_record(self, build: dict[str, Any]) -> RunRecord:
        requester = as_str(dig(build, "requestedFor", "uniqueName") or dig(build, "requestedFor", "displayName"))
        return RunRecord(
            finished=as_str(build.get("finishTime") or build.get("queueTime")),
            started=as_str(build.get("startTime")),
            result=as_str(build.get("result") or build.get("status")),
            branch=short_branch(as_str(build.get("sourceBranch"))),
            requester=self.privacy.identity(requester),
            reason=as_str(build.get("reason")),
        )

    def _releases(self, source: str, project: Project, env: ParseEnv) -> None:
        cache, name = self.cache, project.name
        for def_id in sorted(cache.item_ids(source, name, "release_definitions"), key=lambda i: (len(i), i)):
            defn = as_dict(cache.get(source, name, "release_definitions", def_id))
            if not defn or not definition_included(self.config.filters, defn, release=True):
                continue
            release = ReleasePipeline(
                id=make_id(source, name, "release", def_id),
                source=source,
                project=name,
                azdo_id=def_id,
                name=as_str(defn.get("name")),
                folder=as_str(defn.get("path")) or "\\",
                url=_web(defn),
                is_disabled=as_bool(defn.get("isDisabled")),
                is_deleted=as_bool(defn.get("isDeleted")),
            )
            try:
                parse_release(defn, release, env)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot parse release definition %s: %s", release.id, exc)
                release.flags.append(f"parse_error:{type(exc).__name__}")
                self.estate.errors.append(
                    CollectorError(source=source, project=name, collector="parse_release", message=f"{release.name}: {exc}")
                )
            self.runs.releases[release.id] = [
                RunRecord(
                    finished=as_str(r.get("createdOn")),
                    result=as_str(r.get("status")),
                    requester=self.privacy.identity(as_str(dig(r, "createdBy", "uniqueName") or dig(r, "createdBy", "displayName"))),
                    reason=as_str(r.get("reason")),
                )
                for r in cache.get_list_item(source, name, "releases", def_id)
                if isinstance(r, dict)
            ]
            self.runs.deployments[release.id] = [
                RunRecord(
                    finished=as_str(d.get("completedOn") or d.get("startedOn") or d.get("queuedOn")),
                    started=as_str(d.get("startedOn")),
                    result=as_str(d.get("deploymentStatus")),
                    stage=as_str(dig(d, "releaseEnvironment", "name")),
                    reason=as_str(d.get("reason")),
                    requester=self.privacy.identity(as_str(dig(d, "requestedFor", "uniqueName") or dig(d, "requestedFor", "displayName"))),
                )
                for d in cache.get_list_item(source, name, "deployments", def_id)
                if isinstance(d, dict)
            ]
            self.estate.releases.append(release)


def normalize(config: Config, cache: Cache, rules: RuleSet) -> tuple[Estate, RunData]:
    return Normalizer(config, cache, rules).run()
