"""Classic (designer) build definitions, ``process.type == 1`` (spec section 9.1)."""

from __future__ import annotations

import json
from typing import Any

from ..model.entities import BuildPipeline, Job, Stage, Trigger
from ..util import as_bool, as_dict, as_int, as_list, as_str, dig, short_branch
from .env import ParseEnv, schedule_trigger, split_filters

_EXECUTION = {1: "multi-config", 2: "multi-agent"}


def parse_definition_triggers(defn: dict[str, Any], env: ParseEnv, *, ui_only: bool = False) -> list[Trigger]:
    """Triggers stored on the definition. For YAML pipelines only UI overrides are kept."""
    triggers: list[Trigger] = []
    for raw in as_list(defn.get("triggers")):
        if not isinstance(raw, dict):
            continue
        kind = as_str(raw.get("triggerType"))
        if ui_only and as_int(raw.get("settingsSourceType")) == 2 and kind != "buildCompletion":
            continue  # settings come from the YAML file
        detail = "ui-override" if ui_only else ""
        branches = split_filters(raw.get("branchFilters"))
        paths = split_filters(raw.get("pathFilters"))
        if kind == "continuousIntegration":
            triggers.append(
                Trigger(
                    type="ci",
                    branches_include=branches[0],
                    branches_exclude=branches[1],
                    paths_include=paths[0],
                    paths_exclude=paths[1],
                    batch=as_bool(raw.get("batchChanges")),
                    detail=detail,
                )
            )
        elif kind == "pullRequest":
            triggers.append(
                Trigger(
                    type="pr",
                    branches_include=branches[0],
                    branches_exclude=branches[1],
                    paths_include=paths[0],
                    paths_exclude=paths[1],
                    detail=detail,
                )
            )
        elif kind == "schedule":
            for schedule in as_list(raw.get("schedules")):
                if isinstance(schedule, dict):
                    trigger = schedule_trigger(schedule, "daysToBuild")
                    trigger.detail = ", ".join(x for x in (trigger.detail, detail) if x)
                    triggers.append(trigger)
        elif kind == "buildCompletion":
            upstream_project = as_str(dig(raw, "definition", "project", "name")) or env.project
            triggers.append(
                Trigger(
                    type="build_completion",
                    branches_include=branches[0],
                    branches_exclude=branches[1],
                    upstream=f"{upstream_project}/{as_str(dig(raw, 'definition', 'id'))}",
                    detail="requires success" if as_bool(raw.get("requiresSuccessfulBuild")) else "",
                )
            )
        elif kind == "gatedCheckIn":
            triggers.append(
                Trigger(
                    type="gated",
                    paths_include=paths[0],
                    paths_exclude=paths[1],
                    detail="TFVC gated check-in",
                )
            )
        elif kind:
            triggers.append(Trigger(type=kind, detail=detail))
    return triggers


def parse_common(defn: dict[str, Any], pipeline: BuildPipeline, env: ParseEnv) -> None:
    """Fields shared by classic and YAML definitions."""
    for name, raw in sorted(as_dict(defn.get("variables")).items()):
        pipeline.variables.append(env.variable(name, raw, scope="definition", owner=pipeline.id))
    for group in as_list(defn.get("variableGroups")):
        group_id = as_str(group.get("id") if isinstance(group, dict) else group)
        known = env.variable_groups.get(group_id)
        if known and known.id not in pipeline.variable_group_ids:
            pipeline.variable_group_ids.append(known.id)
        elif not known and group_id:
            pipeline.flags.append(f"variable_group_missing:{group_id}")
    pipeline.build_number_format = as_str(defn.get("buildNumberFormat"))
    pipeline.tags = sorted(as_str(t) for t in as_list(defn.get("tags")))
    rules = [
        f"daysToKeep={as_str(rule.get('daysToKeep'))},minimumToKeep={as_str(rule.get('minimumToKeep'))}"
        for rule in as_list(defn.get("retentionRules"))
        if isinstance(rule, dict)
    ]
    pipeline.retention = "; ".join(rules)

    repo = as_dict(defn.get("repository"))
    props = as_dict(repo.get("properties"))
    checkout = {
        "clean": as_str(repo.get("clean")),
        "submodules": as_str(repo.get("checkoutSubmodules")),
        "lfs": as_str(props.get("gitLfsSupport")),
        "fetchDepth": as_str(props.get("fetchDepth")),
    }
    pipeline.checkout = {k: v for k, v in checkout.items() if v not in ("", "null")}
    mapping = props.get("tfvcMapping")
    if mapping:
        try:
            parsed = json.loads(mapping) if isinstance(mapping, str) else mapping
        except json.JSONDecodeError:
            parsed = {}
        pipeline.tfvc_mappings = [
            f"{as_str(m.get('mappingType'))}:{as_str(m.get('serverPath'))}"
            for m in as_list(as_dict(parsed).get("mappings"))
            if isinstance(m, dict)
        ]


def _phase_job(phase: dict[str, Any], defn: dict[str, Any], env: ParseEnv, pipeline: BuildPipeline) -> Job:
    target = as_dict(phase.get("target"))
    job = Job(
        name=as_str(phase.get("refName") or phase.get("name")),
        display_name=as_str(phase.get("name")),
        kind="server" if as_int(target.get("type")) == 2 else "agent",
        condition=as_str(phase.get("condition")),
        timeout=as_int(phase.get("jobTimeoutInMinutes")) or as_int(defn.get("jobTimeoutInMinutes")) or None,
        depends_on=[as_str(dep.get("scope")) for dep in as_list(phase.get("dependencies")) if isinstance(dep, dict)],
        demands=[as_str(d) for d in [*as_list(target.get("demands")), *as_list(defn.get("demands"))]],
    )
    if job.kind == "agent":
        queue = as_dict(target.get("queue")) or as_dict(defn.get("queue"))
        pool_id, pool_name, hosted = env.pool_for_queue(queue.get("id"), queue)
        job.pool = pool_name
        job.pool_is_hosted = hosted
        if pool_id and pool_id in env.pools and env.pools[pool_id].id not in pipeline.pool_ids:
            pipeline.pool_ids.append(env.pools[pool_id].id)
        job.vm_image = as_str(dig(target, "agentSpecification", "identifier"))
    options = as_dict(target.get("executionOptions"))
    job.strategy = _EXECUTION.get(as_int(options.get("type")) or 0, "")
    if options.get("multipliers"):
        job.matrix = [m.strip() for m in as_str(options["multipliers"]).split(",") if m.strip()]
    job.steps = [env.factory.classic(step, "classic_build") for step in as_list(phase.get("steps")) if isinstance(step, dict)]
    return job


def parse_classic_build(defn: dict[str, Any], pipeline: BuildPipeline, env: ParseEnv) -> None:
    parse_common(defn, pipeline, env)
    pipeline.triggers = parse_definition_triggers(defn, env)
    phases = as_list(dig(defn, "process", "phases"))
    if not phases and isinstance(defn.get("build"), list):
        # API 4.1 and older: one implicit phase with a flat step list
        phases = [{"name": "Build", "steps": defn["build"]}]
    stage = Stage(name="Build", display_name="Build")
    stage.jobs = [_phase_job(phase, defn, env, pipeline) for phase in phases if isinstance(phase, dict)]
    pipeline.stages = [stage]
    pipeline.default_branch = short_branch(as_str(dig(defn, "repository", "defaultBranch")))
