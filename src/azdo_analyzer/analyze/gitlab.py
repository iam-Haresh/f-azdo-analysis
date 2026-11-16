"""GitLab mapping (spec section 13): task mapping, runner and image needs, template fit."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from ..config.models import Config
from ..detect.match import any_task, task_matches
from ..detect.rules import ImageRule, RuleSet
from ..model.entities import (
    BuildPipeline,
    Component,
    Estate,
    GitLabPlan,
    ReleasePipeline,
    Step,
    StepMapping,
    iter_steps,
)
from ..util import find_data_file, matches_any


class GitLabHint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approach: str = ""
    image: str = ""
    script_hint: str = ""
    keywords: list[str] = []
    notes: str = ""


class TaskMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str
    versions: str | list[int] = "*"
    gitlab: GitLabHint = GitLabHint()
    runner_os: str = "any"  # any | linux | windows | macos
    stage: str = ""
    confidence: float = 0.8


class Concept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    azdo: str
    gitlab: str
    when: str = ""  # pipeline feature that makes this row relevant ("" = always shown in the catalogue only)


class Mappings:
    def __init__(self, config: Config) -> None:
        tasks_doc = _load(config.gitlab.task_mapping_file)
        self.stage_order: list[str] = list(tasks_doc.get("stage_order") or [])
        self.default_stage: str = tasks_doc.get("default_stage") or (self.stage_order[0] if self.stage_order else "build")
        entries = [TaskMapEntry.model_validate(entry) for entry in tasks_doc.get("tasks") or []]
        self._exact = {e.task.lower(): e for e in entries if not e.task.startswith("re:")}
        self._patterns = [e for e in entries if e.task.startswith("re:")]
        concepts_doc = _load(config.gitlab.concept_mapping_file)
        self.concepts = [Concept.model_validate(c) for c in concepts_doc.get("concepts") or []]
        connections_doc = _load(config.gitlab.service_connection_mapping_file)
        self.connection_types: dict[str, str] = {str(k).lower(): str(v) for k, v in (connections_doc.get("types") or {}).items()}
        self.connection_default: str = connections_doc.get("default") or ""

    def task(self, name: str) -> TaskMapEntry | None:
        entry = self._exact.get(name.lower())
        if entry is not None:
            return entry
        return next((e for e in self._patterns if task_matches(e.task, name)), None)

    def connection(self, connection_type: str) -> str:
        return self.connection_types.get(connection_type.lower(), self.connection_default)


def _load(path: str) -> dict[str, Any]:
    file: Path = find_data_file(path)
    if not file.is_file():
        return {}
    data = YAML(typ="safe").load(file.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


# -- facts used for image and template matching ----------------------------------------


def pipeline_facts(pipeline: BuildPipeline | ReleasePipeline, components: list[Component], rules: RuleSet) -> dict[str, set[str]]:
    facts: dict[str, set[str]] = {}

    def add(key: str, value: str) -> None:
        if value:
            facts.setdefault(key, set()).add(value.lower())

    for component in components:
        add("group", component.group)
        for key in ("language", "runtime_family", "framework", "build_tool", "package_manager"):
            add(key, getattr(component, key))
    for detection in pipeline.detections:
        if detection.fact in ("language", "runtime_family", "framework", "build_tool", "package_manager"):
            add(detection.fact, detection.value)
    for stage in pipeline.stages:
        for target in stage.targets:
            add("deploy_target", target.type)
    return facts


def _matches(match: dict[str, str], facts: dict[str, set[str]]) -> bool:
    return all(value.lower() in facts.get(key, set()) for key, value in match.items())


def _version(rule: ImageRule, pipeline: BuildPipeline | ReleasePipeline, components: list[Component]) -> tuple[str, bool]:
    """Runtime version by precedence: tool-installer input, then pin/manifest, then the rule default."""
    family = rule.match.get("runtime_family", "")
    matching = [c for c in components if all(getattr(c, key, "").lower() == value.lower() for key, value in rule.match.items())]
    families = {family, *[c.runtime_family for c in matching]} - {""}
    candidates = [
        d.value
        for d in pipeline.detections
        if d.fact == "runtime_version" and d.precedence >= 3 and (d.context in families or not d.context)
    ]
    candidates += [c.runtime_version for c in matching if c.runtime_version]
    if not candidates and family:
        candidates = [c.runtime_version for c in components if c.runtime_family == family and c.runtime_version]
    for candidate in candidates:
        if "$(" in candidate or "${{" in candidate:
            continue
        if candidate in rule.version_map:
            return rule.version_map[candidate], False
        found = re.search(rule.version_pattern, candidate)
        if found:
            value = found.group(1) if found.groups() else found.group(0)
            return rule.version_map.get(value, value), False
    return rule.default_version, True


# -- runner requirements (13.3) ---------------------------------------------------------


def _runner(
    pipeline: BuildPipeline | ReleasePipeline,
    components: list[Component],
    mappings: list[StepMapping],
    estate_pools: dict[str, list[str]],
    rules: RuleSet,
    config: Config,
    plan: GitLabPlan,
) -> None:
    runner = rules.runner
    windows: list[str] = []
    macos: list[str] = []
    for component in components:
        if component.runtime_family in runner.windows_runtime_families:
            windows.append(f"{component.runtime_family} {component.runtime_version} ({component.path})".replace("  ", " "))
        if component.group == "dotnet-special":
            windows.append(f"{component.project_kind or component.language} project ({component.path})")
        if component.group == "ios" or component.facts.get("mobile_platform") == "ios":
            macos.append(f"iOS build ({component.path})")
    for mapping in mappings:
        if mapping.runner_os == "windows":
            windows.append(f"task {mapping.task}")
        elif mapping.runner_os == "macos":
            macos.append(f"task {mapping.task}")
    for stage in pipeline.stages:
        for job in stage.jobs:
            for demand in job.demands:
                if matches_any(runner.windows_demands, demand):
                    windows.append(f"demand '{demand}'")
                elif matches_any(runner.macos_demands, demand):
                    macos.append(f"demand '{demand}'")
            if job.vm_image and matches_any(runner.windows_vm_images, job.vm_image):
                windows.append(f"vmImage {job.vm_image}")
            elif job.vm_image and matches_any(runner.macos_vm_images, job.vm_image):
                macos.append(f"vmImage {job.vm_image}")
            if job.kind == "deployment_group":
                plan.notes.append(
                    f"Deployment group '{job.deployment_group}' has no GitLab equivalent: "
                    "use a runner on the target with environment tags, or WinRM/SSH/Ansible from a runner (design decision)."
                )
    for pool_id in pipeline.pool_ids:
        systems = estate_pools.get(pool_id, [])
        if systems and all("windows" in s.lower() for s in systems):
            plan.notes.append(f"Currently runs on a Windows-only agent pool ({pool_id.rsplit('/', 1)[-1]}): scripts may need porting.")

    def unique(items: list[str]) -> list[str]:
        return sorted(set(items))

    if macos:
        plan.runner_os = "macos"
        plan.runner_reasons = unique(macos)
        plan.tags = list(config.gitlab.runner_tags.macos)
        if windows:
            plan.notes.append("Also needs Windows for: " + "; ".join(unique(windows)))
    elif windows:
        plan.runner_os = "windows"
        plan.runner_reasons = unique(windows)
        plan.tags = list(config.gitlab.runner_tags.windows)
    else:
        plan.runner_os = "linux"
        plan.tags = list(config.gitlab.runner_tags.linux)
    if plan.runner_os != "linux":
        plan.notes.append(
            f"Needs a {plan.runner_os} runner; hosted {plan.runner_os} runners are not assumed on the target - provisioning is a decision."
        )

    facts = pipeline_facts(pipeline, components, rules)
    tools: set[str] = set()
    for component in components:
        if component.group in rules.app_groups:
            runtime = " ".join(x for x in (component.runtime_family or component.language, component.runtime_version) if x)
            build = " ".join(x for x in (component.build_tool, component.build_tool_version) if x)
            tools.update(x for x in (runtime, build) if x)
    for detection in pipeline.detections:
        if detection.precedence >= 3 and detection.fact.endswith("version"):
            tools.add(f"{detection.fact.replace('_', ' ')} {detection.value} (pipeline input)")
    image_rule = next((r for r in runner.images if _matches(r.match, facts)), None)
    if plan.runner_os == "linux":
        image = runner.default_image
        if image_rule is not None:
            version, inferred = _version(image_rule, pipeline, components)
            image = image_rule.image.replace("{version}", version)
            if inferred and "{version}" in image_rule.image:
                plan.notes.append(f"Runtime version is not pinned in the repo or pipeline: image tag {version} is a default (inferred).")
        prefix = config.gitlab.image_registry_prefix.rstrip("/")
        plan.image = f"{prefix}/{image}" if prefix else image
    plan.tools = sorted(tools)


# -- template fit (13.4) ------------------------------------------------------------------


def _template_fit(
    pipeline: BuildPipeline | ReleasePipeline,
    components: list[Component],
    task_names: list[str],
    rules: RuleSet,
    config: Config,
    plan: GitLabPlan,
) -> None:
    facts = pipeline_facts(pipeline, components, rules)
    covered: list[str] = list(config.clustering.ignore_tasks)
    for entry in config.gitlab.template_catalog:
        if not _matches(entry.match, facts):
            continue
        plan.templates.append(entry.id)
        plan.includes.append(dict(entry.include))
        # A template is taken to cover the tasks that produce the facts it matches on.
        for key, value in entry.match.items():
            if key == "deploy_target":
                for target in rules.deploy.targets:
                    if target.type == value:
                        covered.extend(target.tasks)
                        covered.extend(c.get("task", "") for c in target.task_inputs)
            else:
                for rule in rules.pipeline.tasks:
                    if rule.emit.get(key, "").lower() == value.lower():
                        covered.append(rule.task)
    if plan.templates:
        plan.uncovered_tasks = sorted({name for name in task_names if not any_task([c for c in covered if c], name)})
    elif pipeline.stack_signature == "unknown" and not pipeline.deploy_targets:
        plan.gap_reason = "no stack detected - cannot match a template"
    else:
        plan.gap_reason = "no template - gap"


# -- plan -----------------------------------------------------------------------------------


def _step_mapping(step: Step, stage: str, job: str, mappings: Mappings) -> StepMapping:
    entry = None if (step.is_task_group and not step.children) else mappings.task(step.task_name)
    mapping = StepMapping(task=step.task_name, major=step.task_major, display_name=step.display_name, stage=stage, job=job)
    if entry is None:
        return mapping
    mapping.mapped = True
    mapping.approach = entry.gitlab.approach
    mapping.image = entry.gitlab.image
    mapping.script_hint = entry.gitlab.script_hint
    mapping.keywords = list(entry.gitlab.keywords)
    mapping.notes = entry.gitlab.notes
    mapping.runner_os = entry.runner_os
    if isinstance(entry.versions, list) and step.task_major is not None and step.task_major not in entry.versions:
        mapping.notes = f"{mapping.notes} (mapping written for major {entry.versions}, pipeline uses {step.task_major})".strip()
    return mapping


def plan_pipeline(
    pipeline: BuildPipeline | ReleasePipeline,
    components: list[Component],
    pools: dict[str, list[str]],
    mappings: Mappings,
    rules: RuleSet,
    config: Config,
) -> None:
    plan = GitLabPlan()
    stage_of: dict[str, str] = {}
    for stage in pipeline.stages:
        for job in stage.jobs:
            for step in iter_steps(job.steps):
                mapping = _step_mapping(step, stage.display_name or stage.name, job.display_name or job.name, mappings)
                plan.step_mappings.append(mapping)
                entry = mappings.task(step.task_name)
                if entry and entry.stage:
                    stage_of[entry.stage] = entry.stage
    if isinstance(pipeline, BuildPipeline):
        for step in pipeline.timeline_steps:
            plan.step_mappings.append(_step_mapping(step, "(timeline)", "(timeline)", mappings))
    plan.unmapped_tasks = sorted({m.task for m in plan.step_mappings if not m.mapped})

    if isinstance(pipeline, ReleasePipeline) or len(pipeline.stages) > 1:
        plan.stages = [
            ("deploy-" if isinstance(pipeline, ReleasePipeline) else "")
            + re.sub(r"[^a-z0-9]+", "-", (s.display_name or s.name).lower()).strip("-")
            for s in pipeline.stages
        ]
    else:
        ordered = [s for s in mappings.stage_order if s in stage_of]
        plan.stages = ordered or [mappings.default_stage]

    _runner(pipeline, components, plan.step_mappings, pools, rules, config, plan)
    task_names = sorted({m.task for m in plan.step_mappings})
    _template_fit(pipeline, components, task_names, rules, config, plan)
    plan.notes = sorted(set(plan.notes))
    pipeline.plan = plan


def plan_estate(estate: Estate, mappings: Mappings, rules: RuleSet, config: Config) -> None:
    components = {c.id: c for repo in estate.repos for c in repo.components}
    pools = {pool.id: pool.os for pool in estate.agent_pools}
    for build in estate.builds:
        plan_pipeline(build, [components[c] for c in build.component_ids if c in components], pools, mappings, rules, config)
    for release in estate.releases:
        plan_pipeline(release, [], pools, mappings, rules, config)
    for connection in estate.service_connections:
        connection.gitlab_replacement = mappings.connection(connection.type)
