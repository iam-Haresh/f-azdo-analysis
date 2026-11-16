"""Classic release definitions (spec section 9.3)."""

from __future__ import annotations

import json
from typing import Any

from ..model.entities import Approval, Artifact, Gate, Job, ReleasePipeline, Stage, Trigger
from ..util import as_bool, as_dict, as_int, as_list, as_str, dig, short_branch
from .env import ParseEnv, schedule_trigger

_PHASE_KINDS = {
    "agentbaseddeployment": "agent",
    "machinegroupbaseddeployment": "deployment_group",
    "runonserver": "server",
    "deploymentgates": "server",
}
_PARALLEL = {"multiconfiguration": "multi-config", "multimachine": "multi-agent"}


def _artifact(raw: dict[str, Any]) -> Artifact:
    ref = as_dict(raw.get("definitionReference"))
    return Artifact(
        alias=as_str(raw.get("alias")),
        type=as_str(raw.get("type")),
        source_project=as_str(dig(ref, "project", "name")),
        source_definition_id=as_str(dig(ref, "definition", "id")),
        source_definition_name=as_str(dig(ref, "definition", "name")),
        default_version=as_str(dig(ref, "defaultVersionType", "name") or dig(ref, "defaultVersionType", "id")),
        branch=short_branch(as_str(dig(ref, "defaultVersionBranch", "name") or dig(ref, "branches", "name") or dig(ref, "branch", "name"))),
        is_primary=as_bool(raw.get("isPrimary")),
    )


def _approval(node: Any, env: ParseEnv) -> Approval | None:
    data = as_dict(node)
    approvals = [a for a in as_list(data.get("approvals")) if isinstance(a, dict)]
    if not approvals:
        return None
    manual = [a for a in approvals if not as_bool(a.get("isAutomated"))]
    options = as_dict(data.get("approvalOptions"))
    approvers = [
        env.privacy.identity(
            as_str(dig(a, "approver", "displayName") or dig(a, "approver", "uniqueName")),
            is_group=as_bool(dig(a, "approver", "isContainer")),
        )
        for a in manual
    ]
    return Approval(
        approvers=[a for a in approvers if a],
        automated=not manual,
        required_count=as_int(options.get("requiredApproverCount")),
        order=as_str(options.get("executionOrder")),
        timeout_minutes=as_int(options.get("timeoutInMinutes")),
    )


def _gate(node: Any, env: ParseEnv) -> Gate | None:
    data = as_dict(node)
    options = as_dict(data.get("gatesOptions"))
    if not as_bool(options.get("isEnabled")):
        return None
    tasks: list[str] = []
    for gate in as_list(data.get("gates")):
        for task in as_list(as_dict(gate).get("tasks")):
            if isinstance(task, dict) and as_bool(task.get("enabled", True)):
                step = env.factory.classic(task, "release")
                tasks.append(step.task_name or step.display_name)
    if not tasks:
        return None
    return Gate(
        tasks=tasks,
        timeout_minutes=as_int(options.get("timeout")),
        sampling_minutes=as_int(options.get("samplingInterval")),
    )


def _phase(raw: dict[str, Any], env: ParseEnv, release: ReleasePipeline) -> Job:
    deployment = as_dict(raw.get("deploymentInput"))
    kind = _PHASE_KINDS.get(as_str(raw.get("phaseType")).lower(), "agent")
    job = Job(
        name=as_str(raw.get("refName") or raw.get("name")),
        display_name=as_str(raw.get("name")),
        kind=kind,  # type: ignore[arg-type,unused-ignore]
        condition=as_str(deployment.get("condition")),
        timeout=as_int(deployment.get("timeoutInMinutes")) or None,
        demands=[as_str(d) for d in as_list(deployment.get("demands"))],
        skip_artifact_download=as_bool(deployment.get("skipArtifactsDownload")),
    )
    queue_id = deployment.get("queueId")
    if kind == "deployment_group":
        group = env.deployment_groups.get(as_str(queue_id))
        job.deployment_group = group.name if group else f"deployment-group-{as_str(queue_id)}"
        job.deployment_group_tags = [as_str(t) for t in as_list(deployment.get("tags"))]
        if group and group.id not in release.deployment_group_ids:
            release.deployment_group_ids.append(group.id)
    elif kind == "agent":
        pool_id, pool_name, hosted = env.pool_for_queue(queue_id)
        job.pool, job.pool_is_hosted = pool_name, hosted
        if pool_id in env.pools and env.pools[pool_id].id not in release.pool_ids:
            release.pool_ids.append(env.pools[pool_id].id)
        job.vm_image = as_str(dig(deployment, "agentSpecification", "identifier"))
    parallel = as_dict(deployment.get("parallelExecution"))
    job.strategy = _PARALLEL.get(as_str(parallel.get("parallelExecutionType")).lower(), "")
    if parallel.get("multipliers"):
        job.matrix = [m.strip() for m in as_str(parallel["multipliers"]).split(",") if m.strip()]
    job.steps = [env.factory.classic(task, "release") for task in as_list(raw.get("workflowTasks")) if isinstance(task, dict)]
    return job


def _stage(raw: dict[str, Any], env: ParseEnv, release: ReleasePipeline) -> Stage:
    name = as_str(raw.get("name"))
    stage = Stage(
        name=name,
        display_name=name,
        rank=as_int(raw.get("rank")) or 0,
        tier=env.tier(name),
        pre_approval=_approval(raw.get("preDeployApprovals"), env),
        post_approval=_approval(raw.get("postDeployApprovals"), env),
        pre_gates=_gate(raw.get("preDeploymentGates"), env),
        post_gates=_gate(raw.get("postDeploymentGates"), env),
    )
    owner = f"{release.id}#{name}"
    for var_name, var in sorted(as_dict(raw.get("variables")).items()):
        stage.variables.append(env.variable(var_name, var, scope="stage", owner=owner))
    for group in as_list(raw.get("variableGroups")):
        known = env.variable_groups.get(as_str(group.get("id") if isinstance(group, dict) else group))
        if known:
            stage.variable_group_ids.append(known.id)

    trigger = "manual"
    for condition in as_list(raw.get("conditions")):
        if not isinstance(condition, dict):
            continue
        kind = as_str(condition.get("conditionType")).lower()
        if kind == "event":
            trigger = "after_release"
        elif kind == "environmentstate":
            trigger = "after_stage"
            stage.depends_on.append(as_str(condition.get("name")))
        elif kind == "artifact":
            detail = as_str(condition.get("value"))
            try:
                parsed = as_dict(json.loads(detail)) if detail else {}
                detail = short_branch(as_str(parsed.get("sourceBranch"))) or detail
            except json.JSONDecodeError:
                pass
            stage.artifact_filters.append(f"{as_str(condition.get('name'))}: {detail}")
    for schedule in as_list(raw.get("schedules")):
        if isinstance(schedule, dict):
            stage.schedules.append(schedule_trigger(schedule, "daysToRelease").cron)
            if trigger == "manual":
                trigger = "scheduled"
    stage.trigger = trigger

    policy = as_dict(raw.get("executionPolicy"))
    if policy:
        stage.parallelism = f"concurrency={as_str(policy.get('concurrencyCount'))},queueDepth={as_str(policy.get('queueDepthCount'))}"
    retention = as_dict(raw.get("retentionPolicy"))
    if retention:
        stage.retention = f"days={as_str(retention.get('daysToKeep'))},keep={as_str(retention.get('releasesToKeep'))}"
    phases = sorted((p for p in as_list(raw.get("deployPhases")) if isinstance(p, dict)), key=lambda p: as_int(p.get("rank")) or 0)
    stage.jobs = [_phase(phase, env, release) for phase in phases]
    return stage


def parse_release(defn: dict[str, Any], release: ReleasePipeline, env: ParseEnv) -> None:
    release.release_name_format = as_str(defn.get("releaseNameFormat"))
    release.artifacts = [_artifact(a) for a in as_list(defn.get("artifacts")) if isinstance(a, dict)]
    for name, raw in sorted(as_dict(defn.get("variables")).items()):
        release.variables.append(env.variable(name, raw, scope="definition", owner=release.id))
    for group in as_list(defn.get("variableGroups")):
        known = env.variable_groups.get(as_str(group.get("id") if isinstance(group, dict) else group))
        if known and known.id not in release.variable_group_ids:
            release.variable_group_ids.append(known.id)

    for raw in as_list(defn.get("triggers")):
        if not isinstance(raw, dict):
            continue
        kind = as_str(raw.get("triggerType")).lower()
        if kind == "artifactsource":
            conditions = [c for c in as_list(raw.get("triggerConditions")) if isinstance(c, dict)]
            release.triggers.append(
                Trigger(
                    type="artifact",
                    upstream=as_str(raw.get("artifactAlias")),
                    branches_include=[short_branch(as_str(c.get("sourceBranch"))) for c in conditions if c.get("sourceBranch")],
                    detail="continuous deployment",
                )
            )
        elif kind == "schedule":
            release.triggers.append(schedule_trigger(as_dict(raw.get("schedule")), "daysToRelease"))
        elif kind == "sourcerepo":
            release.triggers.append(Trigger(type="artifact", upstream=as_str(raw.get("alias")), detail="source repo trigger"))
        elif kind == "pullrequest":
            release.triggers.append(Trigger(type="pr", upstream=as_str(raw.get("artifactAlias"))))
        elif kind:
            release.triggers.append(Trigger(type=kind))

    environments = sorted((e for e in as_list(defn.get("environments")) if isinstance(e, dict)), key=lambda e: as_int(e.get("rank")) or 0)
    release.stages = [_stage(raw, env, release) for raw in environments]
    flow: list[str] = []
    for stage in release.stages:
        if stage.depends_on:
            flow.extend(f"{upstream} -> {stage.name}" for upstream in stage.depends_on)
        else:
            flow.append(f"({stage.trigger}) -> {stage.name}")
    release.stage_flow = flow
