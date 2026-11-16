"""YAML pipelines, ``process.type == 2`` (spec section 9.2)."""

from __future__ import annotations

from typing import Any

from ..model.entities import BuildPipeline, Job, Stage, Step, TimelineView, Trigger
from ..util import as_bool, as_dict, as_int, as_list, as_str, dig
from .classic_build import parse_common, parse_definition_triggers
from .env import ParseEnv
from .yaml_loader import FileProvider, RepoRef, TemplateResolver

_STRATEGIES = ("runOnce", "rolling", "canary")
_HOOKS = ("preDeploy", "deploy", "routeTraffic", "postRouteTraffic")


def _str_list(value: Any) -> list[str]:
    return [as_str(v) for v in as_list(value) if not isinstance(v, dict | list)]


def _filters(node: Any) -> tuple[list[str], list[str]]:
    if isinstance(node, dict):
        return _str_list(node.get("include")), _str_list(node.get("exclude"))
    return _str_list(node), []


def _branch_trigger(kind: str, node: Any) -> Trigger | None:
    if node is None:
        return None
    if isinstance(node, str):
        if node.strip().lower() == "none":
            return Trigger(type=kind, detail="disabled (none)")
        return Trigger(type=kind, branches_include=[node])
    if isinstance(node, list):
        return Trigger(type=kind, branches_include=_str_list(node))
    if isinstance(node, dict):
        branches = _filters(node.get("branches"))
        paths = _filters(node.get("paths"))
        tags = _filters(node.get("tags"))
        return Trigger(
            type=kind,
            branches_include=branches[0],
            branches_exclude=branches[1],
            paths_include=paths[0],
            paths_exclude=paths[1],
            batch=as_bool(node.get("batch")),
            detail=f"tags: {', '.join(tags[0])}" if tags[0] else "",
        )
    return None


def _pool(node: Any, job: Job) -> None:
    if isinstance(node, str):
        job.pool = node
    elif isinstance(node, dict):
        job.pool = as_str(node.get("name"))
        job.vm_image = as_str(node.get("vmImage"))
        job.demands = _str_list(node.get("demands"))
        if job.vm_image and not job.pool:
            job.pool_is_hosted = True


def _container(node: Any) -> str:
    return as_str(node.get("image") if isinstance(node, dict) else node)


class _Extractor:
    def __init__(self, env: ParseEnv, pipeline: BuildPipeline) -> None:
        self.env = env
        self.pipeline = pipeline

    def steps(self, items: Any) -> list[Step]:
        out: list[Step] = []
        for raw in as_list(items):
            if not isinstance(raw, dict):
                continue
            step = self.env.factory.yaml(raw)
            if step.task_name == "Checkout":
                repo = step.inputs.get("checkout", "")
                if repo and repo not in ("self", "none") and repo not in self.pipeline.resource_repos:
                    self.pipeline.resource_repos.append(repo)
            if raw.get("_unresolved"):
                self.pipeline.flags.append(f"template_unresolved:{as_str(raw.get('template'))}")
            out.append(step)
        return out

    def job(self, raw: dict[str, Any], inherited_pool: Any) -> Job:
        if "template" in raw and raw.get("_unresolved"):
            self.pipeline.flags.append(f"template_unresolved:{as_str(raw['template'])}")
            return Job(name=as_str(raw["template"]), template=as_str(raw["template"]))
        is_deployment = "deployment" in raw
        conditions = [as_str(c) for c in as_list(raw.get("_conditions"))]
        condition = as_str(raw.get("condition"))
        job = Job(
            name=as_str(raw.get("deployment") if is_deployment else raw.get("job")) or "job",
            display_name=as_str(raw.get("displayName")),
            kind="deployment" if is_deployment else "agent",
            depends_on=_str_list(raw.get("dependsOn")),
            condition=" && ".join([*conditions, condition] if condition else conditions),
            timeout=as_int(raw.get("timeoutInMinutes")) or None,
            container=_container(raw.get("container")),
            template=as_str(raw.get("_template")),
        )
        pool = raw.get("pool", inherited_pool)
        if isinstance(pool, str) and pool.strip().lower() == "server":
            job.kind = "server"
        else:
            _pool(pool, job)
        services = raw.get("services")
        if isinstance(services, dict):
            job.services = sorted(f"{alias}={_container(image)}" for alias, image in services.items())

        strategy = as_dict(raw.get("strategy"))
        if "matrix" in strategy:
            job.strategy = "matrix"
            matrix = strategy["matrix"]
            job.matrix = sorted(matrix) if isinstance(matrix, dict) else [as_str(matrix)]
        elif "parallel" in strategy:
            job.strategy = "parallel"
            job.matrix = [as_str(strategy["parallel"])]

        steps: list[Step] = self.steps(raw.get("steps"))
        if is_deployment:
            environment = raw.get("environment")
            if isinstance(environment, dict):
                job.environment = as_str(environment.get("name"))
                job.environment_resource = as_str(environment.get("resourceName"))
            else:
                name, _, resource = as_str(environment).partition(".")
                job.environment, job.environment_resource = name, resource
            for name in _STRATEGIES:
                if name not in strategy:
                    continue
                job.strategy = name
                body = as_dict(strategy[name])
                for hook in _HOOKS:
                    steps.extend(self.steps(as_dict(body.get(hook)).get("steps")))
                for outcome in ("success", "failure"):
                    steps.extend(self.steps(as_dict(as_dict(body.get("on")).get(outcome)).get("steps")))
        job.steps = steps
        return job

    def variables(self, items: Any, owner: str, scope: str) -> None:
        for entry in as_list(items):
            if not isinstance(entry, dict):
                continue
            if "group" in entry:
                name = as_str(entry["group"])
                group = self.env.variable_groups_by_name.get(name.lower())
                if group and group.id not in self.pipeline.variable_group_ids:
                    self.pipeline.variable_group_ids.append(group.id)
                elif not group:
                    self.pipeline.flags.append(f"variable_group_unresolved:{name}")
            elif "name" in entry:
                self.pipeline.variables.append(
                    self.env.variable(
                        as_str(entry["name"]),
                        entry.get("value"),
                        scope="stage" if scope == "stage" else "definition",
                        owner=owner,
                        source="template" if entry.get("_template") else "inline",
                    )
                )

    def stage(self, raw: dict[str, Any], rank: int, root_pool: Any) -> Stage:
        if "template" in raw and raw.get("_unresolved"):
            self.pipeline.flags.append(f"template_unresolved:{as_str(raw['template'])}")
            return Stage(name=as_str(raw["template"]), rank=rank, template=as_str(raw["template"]))
        conditions = [as_str(c) for c in as_list(raw.get("_conditions"))]
        condition = as_str(raw.get("condition"))
        name = as_str(raw.get("stage")) or f"stage{rank}"
        stage = Stage(
            name=name,
            display_name=as_str(raw.get("displayName")) or name,
            rank=rank,
            depends_on=_str_list(raw.get("dependsOn")),
            condition=" && ".join([*conditions, condition] if condition else conditions),
            template=as_str(raw.get("_template")),
        )
        pool = raw.get("pool", root_pool)
        stage.jobs = [self.job(job, pool) for job in as_list(raw.get("jobs")) if isinstance(job, dict)]
        self.variables(raw.get("variables"), f"{self.pipeline.id}#{name}", "stage")
        return stage


def parse_yaml_pipeline(defn: dict[str, Any], pipeline: BuildPipeline, env: ParseEnv, files: FileProvider, repo: RepoRef | None) -> None:
    parse_common(defn, pipeline, env)
    pipeline.yaml_path = as_str(dig(defn, "process", "yamlFilename"))
    ui_triggers = parse_definition_triggers(defn, env, ui_only=True)
    if repo is None:
        pipeline.flags.append("yaml_unreadable_repo")
        pipeline.triggers = ui_triggers
        return

    yaml_config = env.config.yaml_pipelines
    resolved = TemplateResolver(files, resolve_templates=yaml_config.resolve_templates, max_depth=yaml_config.max_template_depth).resolve(
        repo, pipeline.yaml_path
    )
    if resolved.missing:
        pipeline.flags.append("yaml_missing")
        pipeline.triggers = ui_triggers
        return
    if resolved.error:
        pipeline.flags.append(f"yaml_parse_error:{resolved.error[:120]}")
        pipeline.triggers = ui_triggers
        return

    doc = resolved.doc
    pipeline.templates = list(resolved.templates)
    for cross in resolved.cross_repos:
        if cross.label not in pipeline.resource_repos:
            pipeline.resource_repos.append(cross.label)
    for external in resolved.external_repos:
        pipeline.flags.append(f"external_template_repo:{external}")
    for reference in resolved.unresolved:
        flag = f"template_unresolved:{reference}"
        if flag not in pipeline.flags:
            pipeline.flags.append(flag)

    extractor = _Extractor(env, pipeline)
    root_pool = doc.get("pool")
    if "stages" in doc:
        stages = [s for s in as_list(doc["stages"]) if isinstance(s, dict)]
        pipeline.stages = [extractor.stage(raw, rank, root_pool) for rank, raw in enumerate(stages, start=1)]
    elif "jobs" in doc:
        stage = Stage(name="__default", display_name="(single stage)", rank=1)
        stage.jobs = [extractor.job(job, root_pool) for job in as_list(doc["jobs"]) if isinstance(job, dict)]
        pipeline.stages = [stage]
    elif "steps" in doc:
        stage = Stage(name="__default", display_name="(single stage)", rank=1)
        stage.jobs = [extractor.job({"job": "__default", "steps": doc["steps"], **_job_keys(doc)}, root_pool)]
        pipeline.stages = [stage]
    for stage in pipeline.stages:
        if not stage.tier:
            stage.tier = env.tier(stage.display_name or stage.name) if stage.name != "__default" else ""
    extractor.variables(doc.get("variables"), pipeline.id, "definition")

    triggers: list[Trigger] = []
    ci = _branch_trigger("ci", doc.get("trigger"))
    if not any(t.type == "ci" for t in ui_triggers):
        # A YAML file without `trigger:` builds every branch on push.
        triggers.append(ci or Trigger(type="ci", branches_include=["*"], detail="implicit (no trigger key)"))
    pr = _branch_trigger("pr", doc.get("pr"))
    if pr and not any(t.type == "pr" for t in ui_triggers):
        triggers.append(pr)
    for schedule in as_list(doc.get("schedules")):
        if isinstance(schedule, dict):
            branches = _filters(schedule.get("branches"))
            triggers.append(
                Trigger(
                    type="schedule",
                    cron=as_str(schedule.get("cron")),
                    timezone="UTC",
                    branches_include=branches[0],
                    branches_exclude=branches[1],
                    detail="always" if as_bool(schedule.get("always")) else "only with changes",
                )
            )
    resources = as_dict(doc.get("resources"))
    for entry in as_list(resources.get("pipelines")):
        if not isinstance(entry, dict):
            continue
        upstream_project = as_str(entry.get("project")) or env.project
        trigger = entry.get("trigger")
        enabled = bool(trigger) and as_str(trigger).lower() != "none"
        branches = _filters(as_dict(trigger).get("branches")) if isinstance(trigger, dict) else ([], [])
        triggers.append(
            Trigger(
                type="pipeline_resource",
                upstream=f"{upstream_project}/{as_str(entry.get('source') or entry.get('pipeline'))}",
                branches_include=branches[0],
                branches_exclude=branches[1],
                detail="triggers run" if enabled else "artifact only",
            )
        )
    pipeline.triggers = [*triggers, *ui_triggers]

    extras = {
        "containers": [
            f"{as_str(c.get('container'))}={as_str(c.get('image'))}" for c in as_list(resources.get("containers")) if isinstance(c, dict)
        ],
        "packages": [as_str(p.get("package")) for p in as_list(resources.get("packages")) if isinstance(p, dict)],
        "repositories": [
            f"{as_str(r.get('repository'))}={as_str(r.get('type') or 'git')}:{as_str(r.get('name'))}"
            for r in as_list(resources.get("repositories"))
            if isinstance(r, dict)
        ],
        "parameters": [as_str(p.get("name")) for p in as_list(doc.get("parameters")) if isinstance(p, dict) and p.get("name")],
    }
    pipeline.resources = {key: value for key, value in extras.items() if value}


def _job_keys(doc: dict[str, Any]) -> dict[str, Any]:
    keys = ("container", "services", "strategy", "timeoutInMinutes")
    return {key: doc[key] for key in keys if key in doc}


def parse_timeline(timeline: dict[str, Any], build: dict[str, Any] | None, pipeline: BuildPipeline, env: ParseEnv) -> None:
    """Record what actually ran in the last successful build and how it differs from static parsing."""
    records = [r for r in as_list(timeline.get("records")) if isinstance(r, dict)]
    if not records:
        return
    records.sort(key=lambda r: (as_int(r.get("order")) or 0, as_str(r.get("name"))))
    ran = [r for r in records if as_str(r.get("result")).lower() != "skipped"]
    view = TimelineView(
        build_id=as_str(as_dict(build).get("id")),
        stages=[as_str(r.get("name")) for r in ran if r.get("type") == "Stage"],
        jobs=[as_str(r.get("name")) for r in ran if r.get("type") == "Job"],
        pool=as_str(dig(build, "queue", "pool", "name") or dig(build, "queue", "name")),
    )
    static_names = {
        step.task_name.lower() for stage in pipeline.stages for job in stage.jobs for step in _flatten(job.steps) if step.enabled
    }
    seen: dict[str, Step] = {}
    for record in ran:
        if record.get("type") != "Task":
            continue
        step = env.factory.timeline(record)
        if step is None:
            continue
        label = f"{step.task_name}@{step.task_major}" if step.task_major is not None else step.task_name
        if label not in view.tasks:
            view.tasks.append(label)
        seen.setdefault(step.task_name.lower(), step)
    view.only_in_timeline = sorted(s.task_name for key, s in seen.items() if key not in static_names)
    view.only_in_static = sorted(
        {
            step.task_name
            for stage in pipeline.stages
            for job in stage.jobs
            for step in _flatten(job.steps)
            if step.enabled and step.task_name.lower() not in seen and step.task_name != "Template"
        }
    )
    pipeline.timeline = view
    pipeline.timeline_steps = [seen[key] for key in sorted(seen) if key not in static_names]


def _flatten(steps: list[Step]) -> list[Step]:
    out: list[Step] = []
    for step in steps:
        if step.is_task_group and step.children:
            out.extend(_flatten(step.children))
        else:
            out.append(step)
    return out
