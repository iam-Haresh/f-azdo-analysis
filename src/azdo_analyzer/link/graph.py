"""Linkage graph (spec section 8): the mapping every report is a view of.

Every edge records the payload field that produced it (``evidence``).
"""

from __future__ import annotations

from collections import defaultdict

from ..model.entities import (
    BuildPipeline,
    CrossProjectDep,
    Edge,
    Estate,
    MappingRow,
    Orphan,
    ReleasePipeline,
    Repo,
    ServiceConnection,
    Step,
    make_id,
)
from ..parse.steps import is_guid


def _add(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _all_steps(steps: list[Step]) -> list[Step]:
    """Every step including task groups and their children (collapsed and expanded view)."""
    out: list[Step] = []
    for step in steps:
        out.append(step)
        if step.children:
            out.extend(_all_steps(step.children))
    return out


class Linker:
    def __init__(self, estate: Estate) -> None:
        self.estate = estate
        self.edges: list[Edge] = []
        self.deps: list[CrossProjectDep] = []
        self.builds = {b.id: b for b in estate.builds}
        self.repos = {r.id: r for r in estate.repos}
        self.builds_by_name: dict[tuple[str, str, str], BuildPipeline] = {
            (b.source, b.project.lower(), b.name.lower()): b for b in estate.builds
        }
        self.repos_by_name: dict[tuple[str, str, str], Repo] = {(r.source, r.project.lower(), r.name.lower()): r for r in estate.repos}
        self.repos_by_guid: dict[tuple[str, str], Repo] = {(r.source, r.azdo_id.lower()): r for r in estate.repos if r.type == "git"}
        self.connections: dict[tuple[str, str], dict[str, ServiceConnection]] = defaultdict(dict)
        for connection in estate.service_connections:
            index = self.connections[(connection.source, connection.project)]
            index[connection.azdo_id.lower()] = connection
            if len(connection.name) >= 3:
                index.setdefault("name:" + connection.name.lower(), connection)
        self.secure_files = defaultdict(dict)  # type: ignore[var-annotated]
        for secure in estate.secure_files:
            self.secure_files[(secure.source, secure.project)][secure.azdo_id.lower()] = secure
            self.secure_files[(secure.source, secure.project)]["name:" + secure.name.lower()] = secure

    def edge(self, from_id: str, to_id: str, kind: str, evidence: str, cross: bool = False) -> None:
        self.edges.append(Edge(from_id=from_id, to_id=to_id, kind=kind, evidence=evidence, cross_project=cross))

    def dep(self, from_id: str, to_id: str, kind: str, evidence: str) -> None:
        self.deps.append(CrossProjectDep(from_id=from_id, to_id=to_id, kind=kind, evidence=evidence))

    # -- builds ------------------------------------------------------------------

    def link_builds(self) -> None:
        for build in self.estate.builds:
            if build.repo_id:
                self.edge(build.id, build.repo_id, "builds", "definition.repository", build.cross_project)
                if build.repo_id in self.repos:
                    _add(self.repos[build.repo_id].pipelines, build.id)
                if build.cross_project:
                    self.dep(build.id, build.repo_id, "repo", "definition.repository")

            for label in build.resource_repos:
                project, _, name = label.rpartition("/")
                repo = self.repos_by_name.get((build.source, (project or build.project).lower(), name.lower()))
                if repo is None:
                    continue
                cross = repo.project != build.project
                self.edge(build.id, repo.id, "checks_out", "resources.repositories / checkout", cross)
                if cross:
                    self.dep(build.id, repo.id, "repo", "resources.repositories")

            for template in build.templates:
                label, _, path = template.partition(":")
                project, _, name = label.rpartition("/")
                repo = self.repos_by_name.get((build.source, project.lower(), name.lower()))
                if repo is None or repo.id == build.repo_id:
                    continue
                cross = repo.project != build.project
                self.edge(build.id, repo.id, "includes_template", f"template: {path}", cross)
                if cross:
                    self.dep(build.id, repo.id, "template", f"template: {path}")
                    build.cross_project = True

            for trigger in build.triggers:
                if trigger.type not in ("build_completion", "pipeline_resource") or not trigger.upstream:
                    continue
                project, _, ref = trigger.upstream.rpartition("/")
                upstream = self.builds.get(make_id(build.source, project, "build", ref))
                if upstream is None:
                    upstream = self.builds_by_name.get((build.source, project.lower(), ref.lower()))
                if upstream is None:
                    _add(build.flags, f"upstream_missing:{trigger.upstream}")
                    continue
                _add(build.upstream_ids, upstream.id)
                _add(upstream.downstream_ids, build.id)
                cross = upstream.project != build.project
                evidence = "triggers[buildCompletion]" if trigger.type == "build_completion" else "resources.pipelines"
                self.edge(build.id, upstream.id, "triggered_by", evidence, cross)
                if cross:
                    self.dep(build.id, upstream.id, "trigger", evidence)

        for policy in self.estate.branch_policies:
            validated = self.builds.get(policy.build_id)
            repo = self.repos_by_guid.get((policy.source, policy.repo_id)) if policy.repo_id else None
            policy.build_name = validated.name if validated else ""
            policy.repo_name = repo.name if repo else ("(all repositories)" if not policy.repo_id else "")
            if validated and repo:
                self.edge(validated.id, repo.id, "validates", f"policy.settings.scope[{policy.branch}]")

    # -- releases ----------------------------------------------------------------

    def link_releases(self) -> None:
        for release in self.estate.releases:
            for artifact in release.artifacts:
                kind = artifact.type.lower()
                evidence = f"artifacts[{artifact.alias}].definitionReference"
                if kind == "build":
                    project = artifact.source_project or release.project
                    build = self.builds.get(make_id(release.source, project, "build", artifact.source_definition_id))
                    artifact.cross_project = project != release.project
                    if build is None:
                        artifact.missing = True
                        continue
                    artifact.build_id = build.id
                    artifact.repo_id = build.repo_id
                    _add(release.build_ids, build.id)
                    _add(build.release_ids, release.id)
                    if build.repo_id:
                        _add(release.repo_ids, build.repo_id)
                    self.edge(release.id, build.id, "consumes", evidence, artifact.cross_project)
                    if artifact.cross_project:
                        release.cross_project = True
                        self.dep(release.id, build.id, "artifact", evidence)
                elif kind == "git":
                    repo = self.repos_by_guid.get((release.source, artifact.source_definition_id.lower()))
                    if repo is None:
                        artifact.missing = True
                        continue
                    artifact.repo_id = repo.id
                    artifact.cross_project = repo.project != release.project
                    _add(release.repo_ids, repo.id)
                    self.edge(release.id, repo.id, "consumes", evidence, artifact.cross_project)
                    if artifact.cross_project:
                        release.cross_project = True
                        self.dep(release.id, repo.id, "artifact", evidence)

    # -- shared objects ----------------------------------------------------------

    def _scan_step(self, pipeline: BuildPipeline | ReleasePipeline, step: Step, stage_connections: list[str]) -> None:
        key = (pipeline.source, pipeline.project)
        connections = self.connections.get(key, {})
        secure_files = self.secure_files.get(key, {})
        for name, value in step.inputs.items():
            for part in (p.strip() for p in value.split(",")):
                if not part:
                    continue
                lowered = part.lower()
                connection = connections.get(lowered) if is_guid(part) else connections.get("name:" + lowered)
                if connection is not None:
                    _add(pipeline.service_connection_ids, connection.id)
                    _add(stage_connections, connection.id)
                    _add(connection.used_by, pipeline.id)
                    continue
                secure = secure_files.get(lowered) if is_guid(part) else None
                if secure is None and "secure" in name.lower():
                    secure = secure_files.get("name:" + lowered)
                if secure is not None:
                    _add(pipeline.secure_file_ids, secure.id)
                    _add(secure.used_by, pipeline.id)

    def link_usage(self) -> None:
        estate = self.estate
        groups = {g.id: g for g in estate.variable_groups}
        task_groups: dict[tuple[str, str, str], list] = defaultdict(list)  # type: ignore[type-arg]
        for task_group in estate.task_groups:
            task_groups[(task_group.source, task_group.project, task_group.azdo_id)].append(task_group)
        pools = {p.id: p for p in estate.agent_pools}
        pools_by_name = {(p.source, p.name.lower()): p for p in estate.agent_pools}
        deployment_groups = {g.id: g for g in estate.deployment_groups}
        environments = {(e.source, e.project, e.name.lower()): e for e in estate.environments}
        task_defs = {(t.source, t.id): t for t in estate.task_definitions}
        extension_by_task: dict[tuple[str, str], list] = defaultdict(list)  # type: ignore[type-arg]
        for extension in estate.extensions:
            for task in extension.tasks:
                extension_by_task[(extension.source, task.lower())].append(extension)

        for pipeline in estate.pipelines():
            for stage in pipeline.stages:
                for group_id in stage.variable_group_ids:
                    _add(pipeline.variable_group_ids, group_id)
                for job in stage.jobs:
                    if job.pool:
                        pool = pools_by_name.get((pipeline.source, job.pool.lower()))
                        if pool:
                            _add(pipeline.pool_ids, pool.id)
                    if job.environment:
                        environment = environments.get((pipeline.source, pipeline.project, job.environment.lower()))
                        if environment:
                            _add(pipeline.environment_ids, environment.id)
                            _add(environment.used_by, pipeline.id)
                    for step in _all_steps(job.steps):
                        if not step.enabled:
                            continue
                        self._scan_step(pipeline, step, stage.service_connection_ids)
                        if step.is_task_group:
                            versions = task_groups.get((pipeline.source, pipeline.project, step.task_group_id), [])
                            exact = [g for g in versions if g.version == str(step.task_major)]
                            for task_group in exact or versions:
                                _add(pipeline.task_group_ids, task_group.id)
                                _add(task_group.used_by, pipeline.id)
                        else:
                            definition = task_defs.get((pipeline.source, step.task_id))
                            contribution = definition.contribution_id.lower() if definition else ""
                            for extension in extension_by_task.get((pipeline.source, step.task_name.lower()), []):
                                # Prefer the contribution id; fall back to a unique task name.
                                publisher_ext = extension.id.rsplit("/", 1)[-1].lower()
                                if contribution.startswith(publisher_ext) or (not contribution and step.is_marketplace):
                                    extension.used = True
            for group_id in pipeline.variable_group_ids:
                if group_id in groups:
                    _add(groups[group_id].used_by, pipeline.id)
            for pool_id in pipeline.pool_ids:
                if pool_id in pools:
                    _add(pools[pool_id].used_by, pipeline.id)
            for group_id in pipeline.deployment_group_ids:
                if group_id in deployment_groups:
                    _add(deployment_groups[group_id].used_by, pipeline.id)
            for kind, ids in (
                ("variable_group", pipeline.variable_group_ids),
                ("task_group", pipeline.task_group_ids),
                ("service_connection", pipeline.service_connection_ids),
                ("secure_file", pipeline.secure_file_ids),
                ("agent_pool", pipeline.pool_ids),
                ("deployment_group", pipeline.deployment_group_ids),
                ("environment", pipeline.environment_ids),
            ):
                for target in ids:
                    self.edge(pipeline.id, target, f"uses_{kind}", "definition / step inputs")

        # Task groups nested in task groups count as used when their parent is used.
        by_guid = defaultdict(list)
        for task_group in estate.task_groups:
            by_guid[(task_group.source, task_group.project, task_group.name.lower())].append(task_group)
        changed = True
        while changed:
            changed = False
            for parent in estate.task_groups:
                if not parent.used_by:
                    continue
                for child_name in parent.task_names:
                    for child in by_guid.get((parent.source, parent.project, child_name.lower()), []):
                        if child.id != parent.id:
                            for user in parent.used_by:
                                if user not in child.used_by:
                                    child.used_by.append(user)
                                    changed = True

    def run(self) -> None:
        self.link_builds()
        self.link_releases()
        self.link_usage()
        estate = self.estate
        estate.edges = sorted(self.edges, key=lambda e: (e.from_id, e.kind, e.to_id, e.evidence))
        seen: set[tuple[str, str, str]] = set()
        deps = []
        for dep in sorted(self.deps, key=lambda d: (d.from_id, d.to_id, d.kind)):
            if (dep.from_id, dep.to_id, dep.kind) not in seen:
                seen.add((dep.from_id, dep.to_id, dep.kind))
                deps.append(dep)
        estate.cross_project_deps = deps
        for collection in (
            estate.variable_groups,
            estate.task_groups,
            estate.service_connections,
            estate.agent_pools,
            estate.deployment_groups,
            estate.environments,
            estate.secure_files,
        ):
            for item in collection:
                item.used_by.sort()
        for repo in estate.repos:
            repo.pipelines.sort()


def link(estate: Estate) -> None:
    Linker(estate).run()


def build_mapping(estate: Estate) -> None:
    """One row per path: project <-> repo <-> build <-> release <-> stage <-> target."""
    repos = {r.id: r for r in estate.repos}
    builds = {b.id: b for b in estate.builds}
    releases = {r.id: r for r in estate.releases}
    rows: list[MappingRow] = []

    def release_rows(base: MappingRow, release: ReleasePipeline) -> None:
        if not release.stages:
            rows.append(base.model_copy(update={"release": release.name, "release_id": release.id}))
        for stage in release.stages:
            targets = [t.label or t.type for t in stage.targets] or [""]
            for target in sorted(set(targets)):
                rows.append(
                    base.model_copy(
                        update={
                            "release": release.name,
                            "release_id": release.id,
                            "stage": stage.name,
                            "tier": stage.tier,
                            "target": target,
                            "cross_project": base.cross_project or release.project != base.project,
                        }
                    )
                )

    for build in estate.builds:
        repo = repos.get(build.repo_id)
        base = MappingRow(
            source=build.source,
            project=build.project,
            repo=repo.name if repo else build.repo_name,
            repo_project=repo.project if repo else build.repo_project,
            repo_id=build.repo_id,
            build=build.name,
            build_project=build.project,
            build_id=build.id,
            cross_project=build.cross_project,
        )
        consumers = [releases[r] for r in build.release_ids if r in releases]
        if consumers:
            for release in consumers:
                release_rows(base, release)
            continue
        deploying = [s for s in build.stages if s.targets]
        if not deploying:
            rows.append(base)
        for stage in deploying:  # YAML multi-stage deployments
            for target in sorted({t.label or t.type for t in stage.targets}):
                rows.append(base.model_copy(update={"stage": stage.display_name or stage.name, "tier": stage.tier, "target": target}))

    for release in estate.releases:
        if any(b in builds for b in release.build_ids):
            continue  # covered through its builds
        linked = [repos[r] for r in release.repo_ids if r in repos]
        for repo in linked or [None]:  # type: ignore[list-item]
            release_rows(
                MappingRow(
                    source=release.source,
                    project=release.project,
                    repo=repo.name if repo else "",
                    repo_project=repo.project if repo else "",
                    repo_id=repo.id if repo else "",
                    cross_project=release.cross_project,
                ),
                release,
            )

    for repo in estate.repos:
        if not repo.pipelines and not any(repo.id in r.repo_ids for r in estate.releases):
            rows.append(MappingRow(source=repo.source, project=repo.project, repo=repo.name, repo_project=repo.project, repo_id=repo.id))

    rows.sort(key=lambda r: (r.source, r.project.lower(), r.repo.lower(), r.build.lower(), r.release.lower(), r.stage.lower(), r.target))
    estate.mapping = rows


def find_orphans(estate: Estate) -> None:
    orphans: list[Orphan] = []

    def add(object_id: str, kind: str, name: str, project: str, reason: str) -> None:
        orphans.append(Orphan(object_id=object_id, kind=kind, name=name, project=project, reason=reason))

    for build in estate.builds:
        if "repo_missing" in build.flags:
            add(build.id, "build_pipeline", build.name, build.project, "repository is missing or not readable")
        elif "repo_disabled" in build.flags:
            add(build.id, "build_pipeline", build.name, build.project, "repository is disabled")
        if "yaml_missing" in build.flags:
            add(build.id, "build_pipeline", build.name, build.project, f"YAML file {build.yaml_path} not found on {build.default_branch}")
        if build.kind != "xaml" and not build.release_ids and not build.deploy_targets:
            add(
                build.id,
                "build_pipeline",
                build.name,
                build.project,
                "no release pipeline consumes this build and it deploys nothing itself",
            )
    for release in estate.releases:
        for artifact in release.artifacts:
            if artifact.missing:
                add(
                    release.id,
                    "release_pipeline",
                    release.name,
                    release.project,
                    f"artifact '{artifact.alias}' points at a {artifact.type} source that no longer exists "
                    f"({artifact.source_project}/{artifact.source_definition_name or artifact.source_definition_id})",
                )
    for repo in estate.repos:
        if repo.type != "external" and not repo.pipelines and not any(repo.id in r.repo_ids for r in estate.releases):
            add(repo.id, "repo", repo.name, repo.project, "no pipeline builds or consumes this repository")
    for task_group in estate.task_groups:
        if not task_group.used_by:
            add(task_group.id, "task_group", task_group.name, task_group.project, "not used by any pipeline")
    for group in estate.variable_groups:
        if not group.used_by:
            add(group.id, "variable_group", group.name, group.project, "not used by any pipeline")
    for connection in estate.service_connections:
        if not connection.used_by:
            add(connection.id, "service_connection", connection.name, connection.project, "not referenced by any pipeline step")
    orphans.sort(key=lambda o: (o.kind, o.project.lower(), o.name.lower(), o.reason))
    estate.orphans = orphans
