"""Markdown reports: executive summary, per-project summary, per-pipeline brief (spec 14.2 - 14.4)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from ..analyze.features import pipeline_features
from ..analyze.gitlab import Mappings
from ..config.models import Config
from ..model.entities import BuildPipeline, Estate, Project, ReleasePipeline, Step, pipeline_steps
from .tables import Names, _cell, _duration, task_usage


def esc(value: Any) -> str:
    text = "" if value is None else str(_cell(value) if not isinstance(value, str) else value)
    return text.replace("|", "\\|").replace("\r", "").replace("\n", " ").strip()


def md_table(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    body = ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    if not body:
        return "_None._\n"
    head = "| " + " | ".join(columns) + " |\n|" + "|".join(" --- " for _ in columns) + "|\n"
    return head + "\n".join(body) + "\n"


def bullets(items: Iterable[str], empty: str = "_None._") -> str:
    lines = [f"- {item}" for item in items]
    return "\n".join(lines) + "\n" if lines else empty + "\n"


def mermaid_id(text: str) -> str:
    return "n" + re.sub(r"[^A-Za-z0-9]", "_", text)


def mermaid_label(text: str) -> str:
    return text.replace('"', "'").replace("[", "(").replace("]", ")")


def kind_of(pipeline: BuildPipeline | ReleasePipeline) -> str:
    if isinstance(pipeline, ReleasePipeline):
        return "release"
    return {"classic": "classic build", "yaml": "YAML", "xaml": "XAML build"}[pipeline.kind]


# -- summary --------------------------------------------------------------------------


def risks(estate: Estate) -> list[str]:
    """Risks and blockers called out in the executive summary."""
    builds, pipelines = estate.builds, estate.pipelines()
    items: list[tuple[int, str]] = [
        (sum(1 for b in builds if "tfvc_source" in b.flags), "pipeline(s) build from TFVC (source must move to Git first)"),
        (sum(1 for b in builds if b.kind == "xaml"), "legacy XAML build definition(s)"),
        (
            sum(1 for p in pipelines if any(j.kind == "deployment_group" for s in p.stages for j in s.jobs)),
            "pipeline(s) use deployment groups (no GitLab equivalent)",
        ),
        (sum(1 for p in pipelines if p.plan.runner_os == "windows"), "pipeline(s) need Windows runners"),
        (sum(1 for p in pipelines if p.plan.runner_os == "macos"), "pipeline(s) need macOS runners"),
        (len({s.task_name for p in pipelines for s in pipeline_steps(p) if s.is_custom}), "custom / in-house task(s) in use"),
        (
            len({s.task_name for p in pipelines for s in pipeline_steps(p) if not s.in_catalog and not s.is_task_group}),
            "task(s) whose extension is no longer installed",
        ),
        (sum(1 for b in builds if "yaml_missing" in b.flags), "YAML pipeline(s) whose file is missing on the default branch"),
        (sum(1 for p in pipelines if p.cross_project), "pipeline(s) with cross-project dependencies"),
        (sum(1 for f in estate.findings if f.severity == "high"), "high-severity security finding(s)"),
        (len(estate.errors), "collection error(s) - parts of the estate may be missing (see errors sheet)"),
    ]
    return [f"**{count}** {text}" for count, text in items if count]


def summary_data(estate: Estate) -> dict[str, Any]:
    builds, releases, pipelines = estate.builds, estate.releases, estate.pipelines()
    names = Names(estate)
    active = sum(1 for p in pipelines if p.stats.status in ("active", "failing"))
    gaps = Counter(p.stack_signature for p in pipelines if p.plan.gap_reason.startswith("no template"))
    return {
        "as_of": estate.as_of,
        "totals": [
            ("Sources", len(estate.sources)),
            ("Projects (analysed / total)", f"{sum(1 for p in estate.projects if p.included)} / {len(estate.projects)}"),
            ("Migration-candidate projects", sum(1 for p in estate.projects if p.migration_candidate)),
            ("Repositories", len(estate.repos)),
            ("Build pipelines", len(builds)),
            ("Release pipelines", len(releases)),
            ("Active or failing pipelines", active),
            ("Inactive pipelines", len(pipelines) - active),
        ],
        "status": sorted(Counter(p.stats.status for p in pipelines).items()),
        "kinds": sorted(Counter(kind_of(p) for p in pipelines).items()),
        "stacks": sorted(Counter(label for b in builds for label in (b.stack or ["unknown"])).items(), key=lambda i: (-i[1], i[0])),
        "targets": sorted(Counter(t for p in pipelines for t in p.deploy_targets).items(), key=lambda i: (-i[1], i[0])),
        "effort": sorted(Counter(p.effort.band for p in pipelines).items()),
        "unmapped": [
            (
                row.name,
                row.major,
                len(row.builds) + len(row.releases),
                "custom" if row.custom else ("marketplace" if row.marketplace else ("not installed" if not row.in_catalog else "built-in")),
            )
            for row in task_usage(estate)
            if not row.mapped
        ][:15],
        "gaps": sorted(gaps.items(), key=lambda i: (-i[1], i[0])),
        "risks": risks(estate),
        "waves": [
            (wave.name, len(wave.pipelines), ", ".join(names.many(wave.pipelines)[:8]) + (" ..." if len(wave.pipelines) > 8 else ""))
            for wave in estate.waves
        ],
    }


def render_summary(estate: Estate) -> str:
    data = summary_data(estate)
    parts = [
        "# Azure DevOps pipeline estate - migration summary\n",
        f"Data as of {data['as_of'] or 'unknown'}. Read-only analysis; nothing in Azure DevOps was changed.\n",
        "## Estate totals\n",
        md_table(["Metric", "Value"], data["totals"]),
        "## Active vs inactive\n",
        md_table(["Status", "Pipelines"], data["status"]),
        "## Classic vs YAML\n",
        md_table(["Type", "Pipelines"], data["kinds"]),
        "## Stack distribution (build pipelines)\n",
        md_table(["Stack", "Pipelines"], data["stacks"]),
        "## Deploy-target distribution\n",
        md_table(["Target", "Pipelines"], data["targets"]),
        "## Effort by band\n",
        md_table(["Band", "Pipelines"], data["effort"]),
        "## Top unmapped tasks\n",
        "Backlog for `mappings/tasks.yaml`.\n\n",
        md_table(["Task", "Major", "Used by", "Origin"], data["unmapped"]),
        "## Template gaps by stack\n",
        md_table(["Stack", "Pipelines without a template"], data["gaps"]),
        "## Risks and blockers\n",
        bullets(data["risks"], "_None found._"),
        "## Suggested migration waves\n",
        "Active pipelines first, low effort first, upstream pipelines before their consumers.\n\n",
        md_table(["Wave", "Pipelines", "Members"], data["waves"]),
    ]
    return "\n".join(parts)


# -- per project ------------------------------------------------------------------------


def project_graph(estate: Estate, project: Project) -> str:
    names = Names(estate)
    builds = [b for b in estate.builds if b.source == project.source and b.project == project.name]
    releases = [r for r in estate.releases if r.source == project.source and r.project == project.name]
    lines = ["```mermaid", "flowchart LR"]
    declared: set[str] = set()

    def node(object_id: str, shape: tuple[str, str]) -> str:
        key = mermaid_id(object_id)
        if key not in declared:
            declared.add(key)
            lines.append(f'  {key}{shape[0]}"{mermaid_label(names.of(object_id))}"{shape[1]}')
        return key

    for build in builds:
        b = node(build.id, ("[", "]"))
        if build.repo_id:
            lines.append(f"  {node(build.repo_id, ('[(', ')]'))} --> {b}")
        for upstream in build.upstream_ids:
            lines.append(f"  {node(upstream, ('[', ']'))} -. triggers .-> {b}")
    for release in releases:
        r = node(release.id, ("([", "])"))
        for build_id in release.build_ids:
            lines.append(f"  {node(build_id, ('[', ']'))} --> {r}")
        for repo_id in release.repo_ids:
            if not release.build_ids:
                lines.append(f"  {node(repo_id, ('[(', ')]'))} --> {r}")
        for target in release.deploy_targets:
            key = mermaid_id("target:" + target)
            if key not in declared:
                declared.add(key)
                lines.append(f'  {key}{{{{"{mermaid_label(target)}"}}}}')
            lines.append(f"  {r} --> {key}")
    lines.append("```")
    return "\n".join(lines) + "\n" if len(lines) > 3 else "_Nothing to draw._\n"


def render_project(estate: Estate, project: Project, config: Config) -> str:
    names = Names(estate)

    def here(item: Any) -> bool:
        return bool(item.source == project.source and item.project == project.name)

    repos = [r for r in estate.repos if here(r)]
    builds = [b for b in estate.builds if here(b)]
    releases = [r for r in estate.releases if here(r)]
    pipelines = [*builds, *releases]
    questions: list[str] = []
    if any("tfvc_source" in b.flags for b in builds):
        questions.append("TFVC-sourced pipelines: which TFVC paths become Git repositories, and when?")
    if any(j.kind == "deployment_group" for p in pipelines for s in p.stages for j in s.jobs):
        questions.append("Deployment groups: runner on the target VMs, or WinRM/SSH/Ansible from a shared runner?")
    if any(p.plan.runner_os != "linux" for p in pipelines):
        questions.append("Windows/macOS runners: who provisions them on the GitLab side?")
    if any(p.plan.unmapped_tasks for p in pipelines):
        questions.append("Unmapped tasks: replace with scripts, or build shared CI components?")
    if any(p.cross_project for p in pipelines):
        questions.append(
            "Cross-project dependencies: how do the other projects map to GitLab groups (include:project / multi-project pipelines)?"
        )
    if any(p.stats.status in ("dormant", "stale", "never_run", "disabled") for p in pipelines):
        questions.append("Inactive pipelines: confirm with the owners which ones can be retired instead of migrated.")

    parts = [
        f"# {project.name}\n",
        f"Source: {project.source} | State: {project.state} | Source control: {', '.join(project.source_control) or 'unknown'} | "
        f"Process: {project.process_template or 'unknown'}\n",
        f"Proposed GitLab path: `{project.gitlab_path}` | Migration candidate: **{'yes' if project.migration_candidate else 'no'}** | "
        f"Last activity: {project.last_activity or 'none'}\n",
        "## Overview\n",
        md_table(["Status", "Pipelines"], sorted(project.status_counts.items())),
        "## Repositories and components\n",
        md_table(
            ["Repo", "Type", "Branch", "Last commit", "Scan", "Flags", "Pipelines"],
            [(r.name, r.type, r.default_branch, r.last_commit, r.scan_status, r.flags, len(r.pipelines)) for r in repos],
        ),
        md_table(
            ["Repo", "Path", "Language", "Runtime", "Framework", "Build tool", "Package manager", "Tests"],
            [
                (
                    r.name,
                    c.path,
                    c.language,
                    f"{c.runtime_family} {c.runtime_version}".strip(),
                    f"{c.framework} {c.framework_version}".strip(),
                    f"{c.build_tool} {c.build_tool_version}".strip(),
                    c.package_manager,
                    c.test_tooling,
                )
                for r in repos
                for c in r.components
            ],
        ),
        "## Build pipelines\n",
        md_table(
            ["Name", "Type", "Repo", "Status", "Last run", "Success %", "Stack", "Template", "Effort", "Wave"],
            [
                (
                    b.name,
                    b.kind,
                    f"{b.repo_project}/{b.repo_name}" if b.cross_project else b.repo_name,
                    b.stats.status,
                    b.stats.last_run,
                    b.stats.success_rate,
                    b.stack,
                    b.plan.templates or b.plan.gap_reason,
                    f"{b.effort.band} ({b.effort.score:g})",
                    b.wave,
                )
                for b in builds
            ],
        ),
        "## Release pipelines\n",
        md_table(
            ["Name", "Source builds", "Stage flow", "Status", "Last prod deploy", "Targets", "Effort", "Wave"],
            [
                (
                    r.name,
                    names.many(r.build_ids),
                    " ; ".join(r.stage_flow),
                    r.stats.status,
                    r.stats.last_prod_deployment,
                    r.deploy_targets,
                    f"{r.effort.band} ({r.effort.score:g})",
                    r.wave,
                )
                for r in releases
            ],
        ),
    ]
    if config.reports.dependency_graph == "mermaid":
        parts += ["## Dependency graph\n", project_graph(estate, project)]
    parts += [
        "## Shared objects\n",
        "### Variable groups\n",
        md_table(
            ["Name", "Type", "Variables", "Secrets", "Used by"],
            [(g.name, g.type, len(g.variables), g.secret_count, names.many(g.used_by)) for g in estate.variable_groups if here(g)],
        ),
        "### Service connections\n",
        md_table(
            ["Name", "Type", "Auth", "Used by", "GitLab replacement"],
            [(c.name, c.type, c.auth_scheme, names.many(c.used_by), c.gitlab_replacement) for c in estate.service_connections if here(c)],
        ),
        "### Task groups\n",
        md_table(
            ["Name", "Version", "Tasks", "Used by"],
            [(g.name, g.version, g.task_names, names.many(g.used_by)) for g in estate.task_groups if here(g)],
        ),
        "## Runner needs\n",
        md_table(
            ["Pipeline", "OS", "Why", "Image", "Tags"],
            [(p.name, p.plan.runner_os, p.plan.runner_reasons, p.plan.image, p.plan.tags) for p in pipelines],
        ),
        "## Security posture\n",
        md_table(
            ["Pipeline", "Scanners", "Baseline gaps", "Findings"],
            [(p.name, p.security.scanners, p.security.baseline_gaps, p.security.findings) for p in pipelines],
        ),
        "## Effort\n",
        md_table(
            ["Band", "Pipelines", "Score"],
            [
                (
                    band,
                    sum(1 for p in pipelines if p.effort.band == band),
                    round(sum(p.effort.score for p in pipelines if p.effort.band == band), 2),
                )
                for band in sorted({p.effort.band for p in pipelines})
            ],
        ),
        "## Open questions\n",
        bullets(questions),
    ]
    return "\n".join(parts)


# -- per pipeline brief --------------------------------------------------------------------


def _key_inputs(step: Step, limit: int = 4) -> str:
    shown: list[str] = []
    for key, value in step.inputs.items():
        if not value or len(shown) >= limit:
            continue
        text = value if len(value) <= 60 else value[:57] + "..."
        shown.append(f"{key}={text}")
    return "; ".join(shown)


def _flow_rows(steps: list[Step], stage: str, job: str, depth: int = 0) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for step in steps:
        indent = "↳ " * depth
        version = "" if step.task_major is None else step.task_major
        label = f"{indent}{step.task_name}" + (" (task group)" if step.is_task_group else "") + ("" if step.enabled else " [disabled]")
        rows.append((stage, job, label, version, step.display_name, _key_inputs(step), step.condition))
        if step.children:
            rows.extend(_flow_rows(step.children, stage, job, depth + 1))
    return rows


def brief_data(estate: Estate, pipeline: BuildPipeline | ReleasePipeline, mappings: Mappings) -> dict[str, Any]:
    """Machine-readable brief (written next to the Markdown as .json)."""
    names = Names(estate)
    components = {c.id: c for repo in estate.repos for c in repo.components}
    groups = {g.id: g for g in estate.variable_groups}
    connections = {c.id: c for c in estate.service_connections}
    features = pipeline_features(pipeline, estate)
    data: dict[str, Any] = {
        "id": pipeline.id,
        "name": pipeline.name,
        "kind": kind_of(pipeline),
        "source": pipeline.source,
        "project": pipeline.project,
        "status": pipeline.stats.status,
        "effort": pipeline.effort.model_dump(),
        "url": pipeline.url,
        "wave": pipeline.wave,
        "flags": pipeline.flags,
        "stats": pipeline.stats.model_dump(),
        "stack": pipeline.stack,
        "detections": [d.model_dump() for d in pipeline.detections],
        "triggers": [t.model_dump() for t in pipeline.triggers],
        "stages": [s.model_dump() for s in pipeline.stages],
        "variables": [v.model_dump() for v in pipeline.variables],
        "variable_groups": [groups[g].model_dump() for g in pipeline.variable_group_ids if g in groups],
        "service_connections": [connections[c].model_dump() for c in pipeline.service_connection_ids if c in connections],
        "plan": pipeline.plan.model_dump(),
        "security": pipeline.security.model_dump(),
        "concepts": [c.model_dump() for c in mappings.concepts if c.when and c.when in features],
    }
    if isinstance(pipeline, BuildPipeline):
        data["linkage"] = {
            "repo": names.of(pipeline.repo_id) if pipeline.repo_id else pipeline.repo_name,
            "branch": pipeline.default_branch,
            "yaml_path": pipeline.yaml_path,
            "components": [components[c].model_dump() for c in pipeline.component_ids if c in components],
            "releases": names.many(pipeline.release_ids),
            "triggered_by": names.many(pipeline.upstream_ids),
            "triggers": names.many(pipeline.downstream_ids),
            "templates": pipeline.templates,
            "resource_repos": pipeline.resource_repos,
        }
        data["timeline"] = pipeline.timeline.model_dump() if pipeline.timeline else None
    else:
        data["linkage"] = {
            "artifacts": [a.model_dump() for a in pipeline.artifacts],
            "builds": names.many(pipeline.build_ids),
            "repos": names.many(pipeline.repo_ids),
            "stage_flow": pipeline.stage_flow,
        }
    return data


def render_brief(estate: Estate, pipeline: BuildPipeline | ReleasePipeline, mappings: Mappings, config: Config) -> str:
    names = Names(estate)
    components = {c.id: c for repo in estate.repos for c in repo.components}
    groups = {g.id: g for g in estate.variable_groups}
    connections = {c.id: c for c in estate.service_connections}
    plan = pipeline.plan
    features = pipeline_features(pipeline, estate)
    out: list[str] = [
        f"# {pipeline.name}  ({kind_of(pipeline)})\n",
        f"Source: {pipeline.source}/{pipeline.project}  |  Status: {pipeline.stats.status}  |  "
        f"Effort: {pipeline.effort.band} ({pipeline.effort.score:g})  |  Wave: {pipeline.wave or '-'}  |  AZDO link: {pipeline.url or 'n/a'}\n",
    ]
    if pipeline.flags:
        out.append("Flags: " + ", ".join(f"`{flag}`" for flag in pipeline.flags) + "\n")

    out.append("## Linkage\n")
    if isinstance(pipeline, BuildPipeline):
        repo = names.of(pipeline.repo_id) if pipeline.repo_id else (pipeline.repo_name or "unknown")
        paths = sorted({components[c].path for c in pipeline.component_ids if c in components})
        out.append(
            f"Repo: {repo} @ {pipeline.default_branch or '?'} ({pipeline.repo_type or 'unknown'})  |  Component path(s): {', '.join(paths) or 'n/a'}\n"
        )
        if pipeline.yaml_path:
            out.append(f"YAML file: `{pipeline.yaml_path}`  |  Templates: {', '.join(f'`{t}`' for t in pipeline.templates) or 'none'}\n")
        if pipeline.tfvc_mappings:
            out.append("TFVC mappings: " + ", ".join(f"`{m}`" for m in pipeline.tfvc_mappings) + "\n")
        out.append(
            f"Feeds release(s): {', '.join(names.many(pipeline.release_ids)) or 'none'}  |  Triggered by: {', '.join(names.many(pipeline.upstream_ids)) or 'none'}"
            f"  |  Triggers: {', '.join(names.many(pipeline.downstream_ids)) or 'none'}\n"
        )
        if pipeline.resource_repos:
            out.append("Other repositories used: " + ", ".join(pipeline.resource_repos) + "\n")
        if pipeline.repo_type and pipeline.repo_type.lower() not in ("tfsgit", "tfsversioncontrol"):
            out.append("_The repository is external and could not be scanned: the stack below comes from pipeline evidence only._\n")
    else:
        out.append(
            md_table(
                ["Alias", "Type", "Source", "Default version", "Primary", "Cross-project", "Missing"],
                [
                    (
                        a.alias,
                        a.type,
                        f"{a.source_project}/{a.source_definition_name or a.source_definition_id}",
                        a.default_version,
                        a.is_primary,
                        a.cross_project,
                        a.missing,
                    )
                    for a in pipeline.artifacts
                ],
            )
        )
        out.append(
            f"Source build(s): {', '.join(names.many(pipeline.build_ids)) or 'none'}  |  Repo(s): {', '.join(names.many(pipeline.repo_ids)) or 'none'}\n"
        )
        out.append("Stage flow: " + (" ; ".join(pipeline.stage_flow) or "n/a") + "\n")

    out.append("## Stack\n")
    used = [components[c] for c in pipeline.component_ids if c in components] if isinstance(pipeline, BuildPipeline) else []
    out.append(
        md_table(
            ["Component", "Language", "Runtime", "Framework", "Build tool", "Package manager", "Tests", "Confidence"],
            [
                (
                    c.path,
                    c.language,
                    f"{c.runtime_family} {c.runtime_version}".strip(),
                    f"{c.framework} {c.framework_version}".strip(),
                    f"{c.build_tool} {c.build_tool_version}".strip(),
                    c.package_manager,
                    c.test_tooling,
                    c.confidence,
                )
                for c in used
            ],
        )
    )
    evidence = [(d.fact, d.value, d.confidence, "repo", d.evidence) for c in used for d in c.detections]
    evidence += [(d.fact, d.value, d.confidence, "pipeline", d.evidence) for d in pipeline.detections]
    if evidence:
        out.append("Evidence and confidence per fact:\n")
        out.append(md_table(["Fact", "Value", "Confidence", "Source", "Evidence"], evidence))
    conflicts = [f"{c.path}: {conflict}" for c in used for conflict in c.conflicts]
    if conflicts:
        out.append("Conflicting evidence (not resolved automatically):\n\n" + bullets(conflicts))

    out.append("## Current flow\n")
    rows: list[tuple[Any, ...]] = []
    for stage in pipeline.stages:
        for job in stage.jobs:
            label = (job.display_name or job.name) + f" [{job.kind}; {job.pool or job.vm_image or job.deployment_group or 'no pool'}]"
            rows.extend(_flow_rows(job.steps, stage.display_name or stage.name, label))
    out.append(md_table(["Stage", "Job", "Task", "Version", "Display name", "Key inputs", "Condition"], rows))
    if isinstance(pipeline, BuildPipeline) and pipeline.timeline:
        view = pipeline.timeline
        out.append(
            f"Last successful run (build {view.build_id or '?'}, pool {view.pool or '?'}) executed: {', '.join(view.tasks) or 'n/a'}.\n"
        )
        if view.only_in_timeline:
            out.append("Ran but not visible in static parsing: " + ", ".join(view.only_in_timeline) + "\n")
        if view.only_in_static:
            out.append("In the YAML but not executed in that run: " + ", ".join(view.only_in_static) + "\n")

    out.append("## Triggers\n")
    out.append(
        md_table(
            ["Type", "Branches", "Excluded branches", "Paths", "Excluded paths", "Schedule", "Upstream", "Detail"],
            [
                (
                    t.type,
                    t.branches_include,
                    t.branches_exclude,
                    t.paths_include,
                    t.paths_exclude,
                    f"{t.cron} {t.timezone}".strip(),
                    t.upstream,
                    t.detail,
                )
                for t in pipeline.triggers
            ],
        )
    )
    policies = [p for p in estate.branch_policies if p.build_id == pipeline.id]
    if policies:
        out.append(
            "Build-validation policies: "
            + "; ".join(f"{p.repo_name}@{p.branch} ({'blocking' if p.blocking else 'optional'})" for p in policies)
            + "\n"
        )

    out.append("## Variables and secrets to recreate\n")
    variables: list[tuple[str, str, bool, str, str]] = [
        (v.name, v.scope if v.owner == pipeline.id else f"stage {v.owner.split('#', 1)[-1]}", v.is_secret, v.source, v.value)
        for v in pipeline.variables
    ]
    for stage in pipeline.stages:
        variables += [(v.name, f"stage {stage.name}", v.is_secret, v.source, v.value) for v in stage.variables]
    for group_id in pipeline.variable_group_ids:
        if group_id in groups:
            group = groups[group_id]
            variables += [
                (
                    v.name,
                    f"group {group.name}",
                    v.is_secret,
                    str("Key Vault " + group.key_vault if v.source == "keyvault" else "group"),
                    v.value,
                )
                for v in group.variables
            ]
    out.append(md_table(["Name", "Scope", "Secret", "Source", "Value (redacted)"], variables))

    out.append("## Credentials and connections\n")
    out.append(
        md_table(
            ["Service connection", "Type", "Auth", "Suggested GitLab replacement"],
            [
                (connections[c].name, connections[c].type, connections[c].auth_scheme, connections[c].gitlab_replacement)
                for c in pipeline.service_connection_ids
                if c in connections
            ],
        )
    )
    secure = [f.name for f in estate.secure_files if f.id in pipeline.secure_file_ids]
    if secure:
        out.append("Secure files: " + ", ".join(secure) + " -> project Secure Files or file-type variables.\n")

    out.append("## Deployment (release / deployment jobs)\n")
    deployment_rows = []
    for stage in pipeline.stages:
        if not (stage.targets or isinstance(pipeline, ReleasePipeline)):
            continue
        approvals = ", ".join(stage.pre_approval.approvers) if stage.pre_approval and not stage.pre_approval.automated else "automatic"
        gates = ", ".join([*(stage.pre_gates.tasks if stage.pre_gates else []), *(stage.post_gates.tasks if stage.post_gates else [])])
        deployment_rows.append(
            (
                stage.display_name or stage.name,
                stage.tier,
                "; ".join(f"{t.label}" + (f" ({t.identifier})" if t.identifier else "") for t in stage.targets),
                ", ".join(sorted({t.strategy for t in stage.targets} - {""})),
                approvals,
                gates,
                stage.config_transforms,
                stage.stats.last_success,
            )
        )
    out.append(
        md_table(["Stage", "Tier", "Target", "Strategy", "Approvals", "Gates", "Config transforms", "Last success"], deployment_rows)
    )

    out.append("## GitLab build plan\n")
    out.append(f"- Runner OS: **{plan.runner_os}**" + (f" ({'; '.join(plan.runner_reasons)})" if plan.runner_reasons else "") + "\n")
    out.append(f"- Runner tags: {', '.join(f'`{t}`' for t in plan.tags) or 'n/a'}\n")
    out.append(f"- Image: `{plan.image}`\n" if plan.image else "- Image: n/a (shell executor on a Windows/macOS runner)\n")
    if plan.tools:
        out.append("- Tools and versions: " + ", ".join(plan.tools) + "\n")
    if plan.templates:
        for template, include in zip(plan.templates, plan.includes, strict=False):
            out.append(f"- Matching template: **{template}** - `include: {{{', '.join(f'{k}: {v}' for k, v in include.items())}}}`\n")
        if plan.uncovered_tasks:
            out.append("- Not covered by the template: " + ", ".join(plan.uncovered_tasks) + "\n")
    else:
        out.append(f"- Template: {plan.gap_reason}\n")
    out.append(f"- Proposed stages: {', '.join(f'`{s}`' for s in plan.stages)}\n")
    out.append(
        f"- Proposed project path: `{config.gitlab.group_path_template.format(source=pipeline.source, project=pipeline.project)}`\n\n"
    )
    out.append("Step-by-step mapping:\n")
    out.append(
        md_table(
            ["Stage", "Job", "AZDO task", "GitLab approach", "Image", "Script hint", "Keywords", "Notes"],
            [
                (
                    m.stage,
                    m.job,
                    f"{m.task}@{m.major}" if m.major is not None else m.task,
                    m.approach if m.mapped else "**UNMAPPED**",
                    m.image,
                    m.script_hint,
                    m.keywords,
                    m.notes,
                )
                for m in plan.step_mappings
            ],
        )
    )
    relevant = [c for c in mappings.concepts if c.when and c.when in features]
    if relevant:
        out.append("Concepts this pipeline uses (`rules`, `needs`, `environment` and friends):\n")
        out.append(md_table(["Azure DevOps", "GitLab CI/CD"], [(c.azdo, c.gitlab) for c in relevant]))

    out.append("## Gaps and decisions\n")
    gaps = [f"Unmapped task: `{task}`" for task in plan.unmapped_tasks]
    gaps += plan.notes
    gaps += [f"Security baseline gap: {gap}" for gap in pipeline.security.baseline_gaps]
    gaps += [f"Security finding: {finding}" for finding in pipeline.security.findings]
    gaps += [f"Scanner in use: {equivalent}" for equivalent in pipeline.security.gitlab_equivalents]
    if pipeline.stats.low_success:
        gaps.append(f"Low success rate ({pipeline.stats.success_rate}%): fix or understand failures before migrating.")
    gaps += [f"Effort factor: {factor}" for factor in pipeline.effort.factors]
    out.append(bullets(gaps))
    out.append(
        f"\nRuns: {pipeline.stats.runs_in_window} in window, {pipeline.stats.runs_per_month}/month, last {pipeline.stats.last_run or 'never'} "
        f"({pipeline.stats.last_result or '-'}), median {_duration(pipeline.stats.median_duration_s) or '-'}, p90 {_duration(pipeline.stats.p90_duration_s) or '-'}.\n"
    )
    return "\n".join(out)
