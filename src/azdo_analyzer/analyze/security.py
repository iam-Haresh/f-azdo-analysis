"""Security posture (spec section 13.6): scanners in use, baseline gaps, risky patterns."""

from __future__ import annotations

from ..config.models import Config
from ..detect.match import any_regex, any_task
from ..detect.rules import RuleSet
from ..model.entities import BuildPipeline, Estate, Finding, ReleasePipeline, SecurityPosture, pipeline_steps


def _evidence(pipeline: BuildPipeline | ReleasePipeline, estate_components: dict[str, object], rules: RuleSet) -> set[str]:
    found: set[str] = set()
    if any(d.fact == "container_tool" for d in pipeline.detections):
        found.add("container")
    if any(d.fact == "iac_tool" for d in pipeline.detections):
        found.add("iac")
    for stage in pipeline.stages:
        for target in stage.targets:
            if target.type == "container_registry":
                found.add("container")
            if target.type == "azure_iac":
                found.add("iac")
    if isinstance(pipeline, BuildPipeline):
        for component_id in pipeline.component_ids:
            component = estate_components.get(component_id)
            if component is None:
                continue
            if getattr(component, "container_build", False):
                found.add("container")
            if getattr(component, "group", "") in rules.flag_groups.get("has_iac", []):
                found.add("iac")
    return found


def analyze_security(estate: Estate, rules: RuleSet, config: Config) -> None:
    findings: list[Finding] = []
    components = {c.id: c for repo in estate.repos for c in repo.components}
    repos = {r.id: r for r in estate.repos}
    groups = {g.id: g for g in estate.variable_groups}
    environments = {e.id: e for e in estate.environments}
    production = set(config.environments.production_tiers)

    for pipeline in estate.pipelines():
        posture = SecurityPosture()
        steps = pipeline_steps(pipeline)
        covered: set[str] = set()
        for scanner in rules.scanners:
            used = any(any_task(scanner.tasks, step.task_name) or any_regex(scanner.script_regex, step.script_text) for step in steps)
            if used:
                posture.scanners.append(scanner.name)
                covered.update(scanner.covers)
                if scanner.gitlab_equivalent:
                    posture.gitlab_equivalents.append(f"{scanner.name} -> {scanner.gitlab_equivalent}")
        if isinstance(pipeline, BuildPipeline) and pipeline.repo_id in repos:
            configured = repos[pipeline.repo_id].facts.get("scanner", "")
            for name in (n.strip() for n in configured.split(",") if n.strip()):
                if name not in posture.scanners:
                    posture.scanners.append(f"{name} (repo config only)")

        if isinstance(pipeline, BuildPipeline) and pipeline.kind != "xaml":
            evidence = _evidence(pipeline, components, rules)  # type: ignore[arg-type]
            for item in config.gitlab.security_baseline:
                needs = rules.baseline_requires.get(item)
                if item not in covered and (needs is None or needs in evidence):
                    posture.baseline_gaps.append(item)

        def finding(kind: str, severity: str, message: str, object_id: str = pipeline.id, posture: SecurityPosture = posture) -> None:
            findings.append(Finding(object_id=object_id, kind=kind, severity=severity, message=message))  # type: ignore[arg-type,unused-ignore]
            posture.findings.append(message)

        plain = [v.name for v in pipeline.variables if v.suspected_secret]
        plain += [v.name for stage in pipeline.stages for v in stage.variables if v.suspected_secret]
        for group_id in pipeline.variable_group_ids:
            if group_id in groups:
                plain += [f"{groups[group_id].name}.{v.name}" for v in groups[group_id].variables if v.suspected_secret]
        if plain:
            finding("plain_secret_variable", "high", "possible secrets in plain (non-secret) variables: " + ", ".join(sorted(set(plain))))
        leaking = sorted({step.display_name or step.task_name for step in steps if step.has_inline_credential})
        if leaking:
            finding("inline_credential", "high", "inline credentials in step inputs/scripts (redacted): " + ", ".join(leaking))
        missing = sorted({step.task_name for step in steps if not step.in_catalog and not step.is_task_group})
        if missing:
            finding("task_not_installed", "medium", "tasks not in the task catalog (extension removed?): " + ", ".join(missing))

        for stage in pipeline.stages:
            if stage.tier not in production:
                continue
            if isinstance(pipeline, ReleasePipeline):
                approved = bool(stage.pre_approval and not stage.pre_approval.automated) or bool(stage.pre_gates)
                if not approved and stage.jobs:
                    finding("prod_without_approval", "high", f"stage '{stage.name}' deploys to {stage.tier} without approval or gate")
            else:
                for job in stage.jobs:
                    if job.kind != "deployment":
                        continue
                    environment = next(
                        (
                            environments[e]
                            for e in pipeline.environment_ids
                            if environments.get(e) and environments[e].name.lower() == job.environment.lower()
                        ),
                        None,
                    )
                    if environment is None or not environment.checks:
                        finding(
                            "prod_without_approval",
                            "high",
                            f"deployment job '{job.name}' targets {stage.tier} environment '{job.environment}' without checks",
                        )
        pipeline.security = posture

    threshold = config.security.shared_connection_project_threshold
    for connection in estate.service_connections:
        if len(connection.shared_projects) >= threshold:
            findings.append(
                Finding(
                    object_id=connection.id,
                    kind="shared_service_connection",
                    severity="medium",
                    message=f"service connection '{connection.name}' is shared with {len(connection.shared_projects)} projects",
                )
            )
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for candidate in sorted(findings, key=lambda f: (f.kind, f.object_id, f.message)):
        identity = (candidate.kind, candidate.object_id, candidate.message)
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    estate.findings = unique
