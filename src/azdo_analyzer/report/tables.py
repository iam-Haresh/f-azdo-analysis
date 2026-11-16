"""Report tables (spec section 14.1): one table per sheet id, shared by the Excel and CSV writers."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..model.entities import BuildPipeline, Estate, ReleasePipeline, Step, pipeline_steps

Cell = str | int | float | bool | None


@dataclass
class Table:
    id: str
    columns: list[str]
    rows: list[list[Cell]] = field(default_factory=list)

    def add(self, *cells: Any) -> None:
        self.rows.append([_cell(c) for c in cells])


def _cell(value: Any) -> Cell:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, Iterable):
        return "; ".join(str(v) for v in value)
    return str(value)


def yn(value: object) -> str:
    return "yes" if value else "no"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _pool_os(pipeline: BuildPipeline | ReleasePipeline) -> str:
    pools = sorted({job.pool or job.vm_image or job.deployment_group for s in pipeline.stages for job in s.jobs} - {""})
    return f"{', '.join(pools)} / {pipeline.plan.runner_os}" if pools else pipeline.plan.runner_os


def _triggers(pipeline: BuildPipeline | ReleasePipeline) -> str:
    return ", ".join(sorted({t.type for t in pipeline.triggers if "disabled" not in t.detail})) or "manual"


class Names:
    """Id -> display name lookups."""

    def __init__(self, estate: Estate) -> None:
        self.map: dict[str, str] = {}
        for collection in (
            estate.builds,
            estate.releases,
            estate.repos,
            estate.variable_groups,
            estate.task_groups,
            estate.service_connections,
            estate.agent_pools,
            estate.deployment_groups,
            estate.environments,
            estate.secure_files,
        ):
            for item in collection:
                self.map[item.id] = f"{getattr(item, 'project', '')}/{item.name}".lstrip("/")

    def of(self, object_id: str) -> str:
        return self.map.get(object_id, object_id)

    def many(self, ids: Iterable[str]) -> list[str]:
        return sorted(self.of(i) for i in ids)


@dataclass
class TaskUsage:
    name: str
    major: str
    author: str = ""
    marketplace: bool = False
    custom: bool = False
    in_catalog: bool = True
    builds: set[str] = field(default_factory=set)
    releases: set[str] = field(default_factory=set)
    mapped: bool = False
    approach: str = ""


def task_usage(estate: Estate) -> list[TaskUsage]:
    usage: dict[tuple[str, str], TaskUsage] = {}
    for pipeline in estate.pipelines():
        mapped = {m.task: m for m in pipeline.plan.step_mappings}
        steps: list[Step] = pipeline_steps(pipeline)
        for step in steps:
            if step.is_task_group and not step.children:
                label = f"{step.task_name} (task group, definition missing)"
            else:
                label = step.task_name
            key = (label, "" if step.task_major is None else str(step.task_major))
            row = usage.setdefault(key, TaskUsage(name=label, major=key[1]))
            row.author = row.author or step.task_author
            row.marketplace = row.marketplace or step.is_marketplace
            row.custom = row.custom or step.is_custom
            row.in_catalog = row.in_catalog and step.in_catalog
            (row.releases if isinstance(pipeline, ReleasePipeline) else row.builds).add(pipeline.id)
            mapping = mapped.get(step.task_name)
            if mapping is not None and mapping.mapped:
                row.mapped, row.approach = True, mapping.approach
    return sorted(usage.values(), key=lambda r: (-(len(r.builds) + len(r.releases)), r.name.lower(), r.major))


def build_tables(estate: Estate) -> dict[str, Table]:
    names = Names(estate)
    tables: dict[str, Table] = {}

    def table(table_id: str, *columns: str) -> Table:
        tables[table_id] = Table(table_id, list(columns))
        return tables[table_id]

    builds, releases = estate.builds, estate.releases
    pipelines = estate.pipelines()
    repos_by_project: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for repo in estate.repos:
        repos_by_project[(repo.source, repo.project)].append(repo)

    # summary -------------------------------------------------------------------
    t = table("summary", "metric", "key", "count")
    for source in estate.sources:
        t.add(
            "source",
            f"{source.name} ({source.type}, api {source.api_version or '?'})",
            sum(1 for p in estate.projects if p.source == source.name),
        )
    for label, count in (
        ("projects", len(estate.projects)),
        ("projects analysed", sum(1 for p in estate.projects if p.included)),
        ("migration candidate projects", sum(1 for p in estate.projects if p.migration_candidate)),
        ("repositories", len(estate.repos)),
        ("build pipelines", len(builds)),
        ("release pipelines", len(releases)),
    ):
        t.add("totals", label, count)
    for kind, count in sorted(Counter(b.kind for b in builds).items()):
        t.add("build pipeline type", kind, count)
    for status, count in sorted(Counter(p.stats.status for p in pipelines).items()):
        t.add("status", status, count)
    for stack, count in sorted(Counter(label for b in builds for label in (b.stack or ["unknown"])).items(), key=lambda i: (-i[1], i[0])):
        t.add("stack", stack, count)
    for target, count in sorted(Counter(x for p in pipelines for x in p.deploy_targets).items(), key=lambda i: (-i[1], i[0])):
        t.add("deploy target", target, count)
    for band, count in sorted(Counter(p.effort.band for p in pipelines).items()):
        t.add("effort band", band, count)
    for wave in estate.waves:
        t.add("wave", wave.name, len(wave.pipelines))

    # projects ------------------------------------------------------------------
    t = table(
        "projects",
        "source",
        "project",
        "state",
        "analysed",
        "source_control",
        "process_template",
        "repos",
        "build_defs",
        "release_defs",
        "active",
        "dormant",
        "stale",
        "other_status",
        "last_activity",
        "stacks",
        "deploy_targets",
        "effort_total",
        "migration_candidate",
        "gitlab_path",
    )
    for project in estate.projects:
        mine = [p for p in pipelines if p.source == project.source and p.project == project.name]
        counts = project.status_counts
        other = sum(v for k, v in counts.items() if k not in ("active", "dormant", "stale"))
        t.add(
            project.source,
            project.name,
            project.state,
            yn(project.included),
            project.source_control,
            project.process_template,
            len(repos_by_project[(project.source, project.name)]),
            sum(1 for p in mine if isinstance(p, BuildPipeline)),
            sum(1 for p in mine if isinstance(p, ReleasePipeline)),
            counts.get("active", 0),
            counts.get("dormant", 0),
            counts.get("stale", 0),
            other,
            project.last_activity,
            sorted({label for p in mine for label in p.stack}),
            sorted({x for p in mine for x in p.deploy_targets}),
            round(sum(p.effort.score for p in mine), 2),
            yn(project.migration_candidate),
            project.gitlab_path,
        )

    # repos / components ----------------------------------------------------------
    t = table(
        "repos",
        "source",
        "project",
        "repo",
        "type",
        "default_branch",
        "size_kb",
        "last_commit",
        "disabled",
        "scan_status",
        "components",
        "flags",
        "facts",
        "pipelines",
    )
    c = table(
        "components",
        "source",
        "project",
        "repo",
        "path",
        "group",
        "language",
        "runtime_family",
        "runtime_version",
        "framework",
        "framework_version",
        "build_tool",
        "build_tool_version",
        "package_manager",
        "test_tooling",
        "packaging",
        "project_kind",
        "container_build",
        "other_facts",
        "conflicts",
        "confidence",
        "evidence",
    )
    for repo in estate.repos:
        repo_kind = repo.type if repo.type != "external" else f"external ({repo.external_type})"
        t.add(
            repo.source,
            repo.project,
            repo.name,
            repo_kind,
            repo.default_branch,
            repo.size_kb,
            repo.last_commit,
            yn(repo.disabled),
            repo.scan_status,
            len(repo.components),
            repo.flags,
            repo.facts,
            names.many(repo.pipelines),
        )
        for comp in repo.components:
            c.add(
                repo.source,
                repo.project,
                repo.name,
                comp.path,
                comp.group,
                comp.language,
                comp.runtime_family,
                comp.runtime_version,
                comp.framework,
                comp.framework_version,
                comp.build_tool,
                comp.build_tool_version,
                comp.package_manager,
                comp.test_tooling,
                comp.packaging,
                comp.project_kind,
                yn(comp.container_build),
                comp.facts,
                comp.conflicts,
                comp.confidence,
                sorted({d.evidence for d in comp.detections}),
            )

    # build pipelines -------------------------------------------------------------
    t = table(
        "build_pipelines",
        "source",
        "project",
        "folder",
        "name",
        "type",
        "repo",
        "repo_project",
        "cross_project",
        "yaml_path",
        "status",
        "all_statuses",
        "last_run",
        "last_result",
        "runs_per_month",
        "success_pct",
        "median_duration",
        "p90_duration",
        "triggers",
        "pool_os",
        "stacks",
        "task_count",
        "deploy_targets",
        "template_match",
        "cluster",
        "effort_band",
        "effort_score",
        "wave",
        "flags",
        "url",
    )
    for b in builds:
        t.add(
            b.source,
            b.project,
            b.folder,
            b.name,
            b.kind,
            b.repo_name,
            b.repo_project,
            yn(b.cross_project),
            b.yaml_path,
            b.stats.status,
            b.stats.statuses,
            b.stats.last_run,
            b.stats.last_result,
            b.stats.runs_per_month,
            b.stats.success_rate,
            _duration(b.stats.median_duration_s),
            _duration(b.stats.p90_duration_s),
            _triggers(b),
            _pool_os(b),
            b.stack,
            len(pipeline_steps(b)),
            b.deploy_targets,
            b.plan.templates or b.plan.gap_reason,
            b.cluster_id,
            b.effort.band,
            b.effort.score,
            b.wave,
            b.flags,
            b.url,
        )

    # release pipelines / stages -----------------------------------------------------
    t = table(
        "release_pipelines",
        "source",
        "project",
        "folder",
        "name",
        "artifacts",
        "source_builds",
        "stage_count",
        "stage_flow",
        "status",
        "last_release",
        "last_prod_deploy",
        "deploy_targets",
        "approvals",
        "gates",
        "cluster",
        "effort_band",
        "effort_score",
        "wave",
        "flags",
        "url",
    )
    s = table(
        "release_stages",
        "source",
        "project",
        "release",
        "stage",
        "rank",
        "tier",
        "trigger",
        "depends_on",
        "pre_approvers",
        "post_approvers",
        "gates",
        "phase_types",
        "pool_or_deployment_group",
        "target_type",
        "target_identifier",
        "service_connections",
        "strategy",
        "config_transforms",
        "parallelism",
        "last_deployment",
        "last_status",
        "last_success",
    )

    def approvers(approval: Any) -> str:
        if approval is None or approval.automated:
            return ""
        return f"{', '.join(approval.approvers)} (need {approval.required_count or 'all'})"

    for r in releases:
        manual = sorted({stage.name for stage in r.stages if stage.pre_approval and not stage.pre_approval.automated})
        gated = sorted({stage.name for stage in r.stages if stage.pre_gates or stage.post_gates})
        t.add(
            r.source,
            r.project,
            r.folder,
            r.name,
            [f"{a.alias} ({a.type})" for a in r.artifacts],
            names.many(r.build_ids),
            len(r.stages),
            " | ".join(r.stage_flow),
            r.stats.status,
            r.stats.last_run,
            r.stats.last_prod_deployment,
            r.deploy_targets,
            manual,
            gated,
            r.cluster_id,
            r.effort.band,
            r.effort.score,
            r.wave,
            r.flags,
            r.url,
        )
        for stage in r.stages:
            gates = [*(stage.pre_gates.tasks if stage.pre_gates else []), *(stage.post_gates.tasks if stage.post_gates else [])]
            s.add(
                r.source,
                r.project,
                r.name,
                stage.name,
                stage.rank,
                stage.tier,
                stage.trigger,
                stage.depends_on,
                approvers(stage.pre_approval),
                approvers(stage.post_approval),
                gates,
                sorted({j.kind for j in stage.jobs}),
                sorted({j.deployment_group or j.pool for j in stage.jobs} - {""}),
                sorted({x.label for x in stage.targets}),
                sorted({x.identifier for x in stage.targets} - {""}),
                names.many(stage.service_connection_ids),
                sorted({x.strategy for x in stage.targets} - {""}),
                stage.config_transforms,
                stage.parallelism,
                stage.stats.last_deployment,
                stage.stats.last_status,
                stage.stats.last_success,
            )

    # mapping -----------------------------------------------------------------------
    t = table(
        "mapping",
        "source",
        "project",
        "repo_project",
        "repo",
        "build_project",
        "build",
        "release",
        "stage",
        "tier",
        "target",
        "cross_project",
    )
    for row in estate.mapping:
        t.add(
            row.source,
            row.project,
            row.repo_project,
            row.repo,
            row.build_project,
            row.build,
            row.release,
            row.stage,
            row.tier,
            row.target,
            yn(row.cross_project),
        )

    # tasks ---------------------------------------------------------------------------
    usage = task_usage(estate)
    t = table(
        "tasks_usage",
        "task",
        "major_version",
        "author",
        "marketplace",
        "custom",
        "in_catalog",
        "used_in_builds",
        "used_in_releases",
        "mapped",
        "gitlab_approach",
    )
    u = table(
        "unmapped_tasks", "task", "major_version", "author", "marketplace", "custom", "in_catalog", "usage_count", "example_pipelines"
    )
    for used in usage:
        t.add(
            used.name,
            used.major,
            used.author,
            yn(used.marketplace),
            yn(used.custom),
            yn(used.in_catalog),
            len(used.builds),
            len(used.releases),
            yn(used.mapped),
            used.approach,
        )
        if not used.mapped:
            examples = names.many([*used.builds, *used.releases])[:5]
            u.add(
                used.name,
                used.major,
                used.author,
                yn(used.marketplace),
                yn(used.custom),
                yn(used.in_catalog),
                len(used.builds) + len(used.releases),
                examples,
            )

    t = table("task_groups", "source", "project", "name", "version", "draft", "tasks", "used_by", "unused")
    for g in estate.task_groups:
        t.add(g.source, g.project, g.name, g.version, yn(g.is_draft), g.task_names, names.many(g.used_by), yn(not g.used_by))

    # variables -------------------------------------------------------------------------
    t = table("variable_groups", "source", "project", "name", "type", "key_vault", "variables", "secret_count", "used_by")
    v = table("variables", "source", "project", "scope", "owner", "name", "is_secret", "suspected_secret", "value", "overridable", "origin")
    for vg in estate.variable_groups:
        group_kind = "Key Vault" if vg.type.lower() == "azurekeyvault" else "plain"
        t.add(
            vg.source,
            vg.project,
            vg.name,
            group_kind,
            vg.key_vault,
            [x.name for x in vg.variables],
            vg.secret_count,
            names.many(vg.used_by),
        )
        for var in vg.variables:
            v.add(vg.source, vg.project, "group", vg.name, var.name, yn(var.is_secret), yn(var.suspected_secret), var.value, "", var.source)
    for p in pipelines:
        for var in p.variables:
            owner = p.name if var.owner == p.id else f"{p.name} / {var.owner.split('#', 1)[-1]}"
            v.add(
                p.source,
                p.project,
                var.scope,
                owner,
                var.name,
                yn(var.is_secret),
                yn(var.suspected_secret),
                var.value,
                yn(var.allow_override),
                var.source,
            )
        for stage in p.stages:
            for var in stage.variables:
                v.add(
                    p.source,
                    p.project,
                    "stage",
                    f"{p.name} / {stage.name}",
                    var.name,
                    yn(var.is_secret),
                    yn(var.suspected_secret),
                    var.value,
                    yn(var.allow_override),
                    var.source,
                )

    # connections, pools, targets, environments --------------------------------------------
    t = table(
        "service_connections",
        "source",
        "project",
        "name",
        "type",
        "auth_scheme",
        "url",
        "shared",
        "shared_projects",
        "used_by",
        "gitlab_replacement",
    )
    for sc in estate.service_connections:
        t.add(
            sc.source,
            sc.project,
            sc.name,
            sc.type,
            sc.auth_scheme,
            sc.url,
            yn(sc.is_shared),
            sc.shared_projects,
            names.many(sc.used_by),
            sc.gitlab_replacement,
        )

    t = table("agent_pools", "source", "name", "hosted", "type", "agents", "os", "key_capabilities", "used_by_pipelines")
    for pool in estate.agent_pools:
        t.add(
            pool.source, pool.name, yn(pool.is_hosted), pool.pool_type, len(pool.agents), pool.os, pool.key_capabilities, len(pool.used_by)
        )

    targets: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for p in pipelines:
        for stage in p.stages:
            for found in stage.targets:
                targets[(p.source, found.label or found.type, found.identifier, stage.tier)].add(
                    f"{p.project}/{p.name} / {stage.display_name or stage.name}"
                )
    t = table("deployment_targets", "source", "type", "identifier", "tier", "deployed_by_count", "deployed_by")
    for (target_source, label, identifier, tier), users in sorted(targets.items()):
        t.add(target_source, label, identifier, tier, len(users), sorted(users))

    t = table("environments", "source", "project", "kind", "name", "resources_or_targets", "checks_or_tags", "used_by")
    for environment in estate.environments:
        t.add(
            environment.source,
            environment.project,
            "environment",
            environment.name,
            [f"{r.name} ({r.type})" for r in environment.resources],
            environment.checks,
            names.many(environment.used_by),
        )
    for dg in estate.deployment_groups:
        t.add(
            dg.source,
            dg.project,
            "deployment group",
            dg.name,
            [f"{x.name} [{', '.join(x.tags)}]" for x in dg.targets],
            dg.tags,
            names.many(dg.used_by),
        )

    # triggers / policies / dependencies ------------------------------------------------------
    t = table(
        "triggers",
        "source",
        "project",
        "pipeline",
        "pipeline_kind",
        "type",
        "branches_include",
        "branches_exclude",
        "paths_include",
        "paths_exclude",
        "schedule",
        "timezone",
        "upstream",
        "batch",
        "detail",
    )
    for p in pipelines:
        for trig in p.triggers:
            t.add(
                p.source,
                p.project,
                p.name,
                "release" if isinstance(p, ReleasePipeline) else "build",
                trig.type,
                trig.branches_include,
                trig.branches_exclude,
                trig.paths_include,
                trig.paths_exclude,
                trig.cron,
                trig.timezone,
                trig.upstream,
                yn(trig.batch),
                trig.detail,
            )

    t = table(
        "branch_policies",
        "source",
        "project",
        "repo",
        "branch",
        "match",
        "build_validation_pipeline",
        "blocking",
        "enabled",
        "path_filters",
    )
    for policy in estate.branch_policies:
        t.add(
            policy.source,
            policy.project,
            policy.repo_name,
            policy.branch,
            policy.match_kind,
            policy.build_name or policy.build_id,
            yn(policy.blocking),
            yn(policy.enabled),
            policy.path_filters,
        )

    t = table("cross_project_deps", "from", "to", "kind", "evidence")
    for dep in estate.cross_project_deps:
        t.add(names.of(dep.from_id), names.of(dep.to_id), dep.kind, dep.evidence)

    # gitlab plan -------------------------------------------------------------------------------
    fit = table("template_fit", "source", "project", "pipeline", "kind", "stack", "matched_templates", "uncovered_tasks", "gap_reason")
    sec = table("security", "source", "project", "pipeline", "scanners_present", "gitlab_equivalents", "baseline_gaps", "findings")
    run = table("runner_requirements", "source", "project", "pipeline", "os", "reasons", "tools", "suggested_image", "tags", "notes")
    eff = table("effort", "source", "project", "pipeline", "kind", "status", "score", "band", "wave", "factors")
    for p in pipelines:
        pipeline_kind = "release" if isinstance(p, ReleasePipeline) else p.kind
        fit.add(p.source, p.project, p.name, pipeline_kind, p.stack_signature, p.plan.templates, p.plan.uncovered_tasks, p.plan.gap_reason)
        sec.add(
            p.source, p.project, p.name, p.security.scanners, p.security.gitlab_equivalents, p.security.baseline_gaps, p.security.findings
        )
        run.add(p.source, p.project, p.name, p.plan.runner_os, p.plan.runner_reasons, p.plan.tools, p.plan.image, p.plan.tags, p.plan.notes)
        eff.add(p.source, p.project, p.name, pipeline_kind, p.stats.status, p.effort.score, p.effort.band, p.wave, p.effort.factors)

    t = table("clusters", "cluster", "stack", "member_count", "representative", "common_tasks", "members", "deviations")
    for cluster in estate.clusters:
        deviations = [f"{names.of(k)}: +{', '.join(v)}" for k, v in cluster.deviations.items()]
        t.add(
            cluster.id,
            cluster.stack,
            len(cluster.members),
            names.of(cluster.representative),
            cluster.common_tasks,
            names.many(cluster.members),
            deviations,
        )

    t = table("orphans", "kind", "project", "name", "reason")
    for orphan in estate.orphans:
        t.add(orphan.kind, orphan.project, orphan.name, orphan.reason)

    t = table("extensions", "source", "publisher", "name", "version", "builtin", "disabled", "tasks_contributed", "used")
    for ext in estate.extensions:
        t.add(ext.source, ext.publisher, ext.name, ext.version, yn(ext.builtin), yn(ext.disabled), ext.tasks, yn(ext.used))

    t = table("errors", "source", "project", "collector", "message")
    for err in estate.errors:
        t.add(err.source, err.project, err.collector, err.message)
    return tables


SHEET_IDS = [
    "summary",
    "projects",
    "repos",
    "components",
    "build_pipelines",
    "release_pipelines",
    "release_stages",
    "mapping",
    "tasks_usage",
    "unmapped_tasks",
    "task_groups",
    "variable_groups",
    "variables",
    "service_connections",
    "agent_pools",
    "deployment_targets",
    "environments",
    "triggers",
    "branch_policies",
    "cross_project_deps",
    "template_fit",
    "clusters",
    "security",
    "runner_requirements",
    "effort",
    "orphans",
    "extensions",
    "errors",
]
