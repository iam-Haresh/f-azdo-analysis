"""Deployment target detection per stage (spec section 11)."""

from __future__ import annotations

from ..model.entities import DeploymentTarget, Estate, Job, Stage, Step, iter_steps
from .match import any_regex, any_task, task_matches
from .rules import DeployRules, RuleSet, TargetRule


def _signature(step: Step) -> str:
    inputs = " ".join(f"{key}={value}" for key, value in step.inputs.items())
    return f"{step.task_name} {inputs}\n{step.script_text}"


def _step_hits(rule: TargetRule, step: Step) -> bool:
    if any_task(rule.tasks, step.task_name):
        return True
    inputs = {k.lower(): v for k, v in step.inputs.items()}
    for condition in rule.task_inputs:
        if task_matches(condition.get("task", ""), step.task_name) and any_regex(
            [condition.get("regex", "")], inputs.get(condition.get("input", "").lower(), "")
        ):
            return True
    return any_regex(rule.script_regex, step.script_text)


def _identifier(rule: TargetRule, step: Step) -> str:
    inputs = {k.lower(): v for k, v in step.inputs.items()}
    for name in rule.identifier_inputs:
        value = inputs.get(name.lower(), "").strip()
        if value:
            return value
    return ""


class TargetDetector:
    def __init__(self, estate: Estate, rules: DeployRules) -> None:
        self.rules = rules
        self.connections = {c.id: c for c in estate.service_connections}
        self.environments = {(e.source, e.project, e.name.lower()): e for e in estate.environments}

    def _strategy(self, job: Job, steps: list[Step]) -> str:
        if job.strategy in ("rolling", "canary", "runOnce"):
            return job.strategy
        text = "\n".join(_signature(step) for step in steps)
        for strategy, patterns in self.rules.strategy_patterns.items():
            if any_regex(patterns, text):
                return strategy
        return ""

    def stage(self, stage: Stage, source: str, project: str) -> None:
        targets: dict[tuple[str, str], DeploymentTarget] = {}
        transforms: list[str] = []
        stage_connections = [self.connections[c] for c in stage.service_connection_ids if c in self.connections]
        transform_inputs = {name.lower() for name in self.rules.config_transform_inputs}

        def add(rule: TargetRule, identifier: str, evidence: str, strategy: str, step: Step | None) -> None:
            connection = None
            if step is not None:
                values = {v.strip().lower() for value in step.inputs.values() for v in value.split(",")}
                connection = next((c for c in stage_connections if c.azdo_id.lower() in values or c.name.lower() in values), None)
            if connection is None:
                connection = next((c for c in stage_connections if c.type in rule.connection_types), None)
            key = (rule.type, identifier)
            if key in targets:
                existing = targets[key]
                if not existing.service_connection and connection is not None:
                    existing.service_connection = connection.name
                    existing.credential_type = self.rules.credential_types.get(connection.auth_scheme, connection.auth_scheme)
                return
            targets[key] = DeploymentTarget(
                type=rule.type,
                label=rule.label,
                identifier=identifier,
                service_connection=connection.name if connection else "",
                credential_type=(self.rules.credential_types.get(connection.auth_scheme, connection.auth_scheme) if connection else ""),
                strategy=strategy,
                evidence=evidence,
            )

        for job in stage.jobs:
            steps = iter_steps(job.steps)
            strategy = self._strategy(job, steps)
            environment = self.environments.get((source, project, job.environment.lower())) if job.environment else None
            resource_types = {r.type.lower() for r in environment.resources} if environment else set()
            for rule in self.rules.targets:
                if job.kind in rule.phase_types:
                    tags = f" [{', '.join(job.deployment_group_tags)}]" if job.deployment_group_tags else ""
                    add(rule, f"{job.deployment_group}{tags}", f"deployment group phase '{job.display_name or job.name}'", strategy, None)
                if resource_types & {t.lower() for t in rule.environment_resource_types}:
                    add(rule, job.environment, f"environment '{job.environment}' resource type", strategy, None)
                for step in steps:
                    if _step_hits(rule, step):
                        add(rule, _identifier(rule, step), f"task {step.task_name} in '{job.display_name or job.name}'", strategy, step)
            for step in steps:
                if any_task(self.rules.config_transform_tasks, step.task_name):
                    transforms.append(step.task_name)
                for name, value in step.inputs.items():
                    if name.lower() in transform_inputs and value.strip().lower() not in ("", "false", "0"):
                        transforms.append(f"{step.task_name}({name})")

        # A connection type alone is a signal too (e.g. an OpenShift connection used by a script step).
        for rule in self.rules.targets:
            if not any(key[0] == rule.type for key in targets):
                connection = next((c for c in stage_connections if c.type in rule.connection_types), None)
                if connection is not None:
                    add(rule, "", f"service connection '{connection.name}' of type {connection.type}", "", None)

        stage.targets = sorted(targets.values(), key=lambda t: (t.type, t.identifier))
        stage.config_transforms = sorted(set(transforms))


def detect_targets(estate: Estate, rules: RuleSet) -> None:
    detector = TargetDetector(estate, rules.deploy)
    for pipeline in estate.pipelines():
        labels: set[str] = set()
        for stage in pipeline.stages:
            detector.stage(stage, pipeline.source, pipeline.project)
            labels.update(t.label or t.type for t in stage.targets)
        pipeline.deploy_targets = sorted(labels)
