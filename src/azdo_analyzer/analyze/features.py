"""Pipeline features: which Azure DevOps concepts a pipeline uses (drives the concept mapping rows)."""

from __future__ import annotations

from ..model.entities import BuildPipeline, Estate, ReleasePipeline, pipeline_steps

_ARTIFACT_TASKS = {"publishbuildartifacts", "publishpipelineartifact", "downloadbuildartifacts", "downloadpipelineartifact"}
_AGENTLESS_TASKS = {"manualintervention", "manualvalidation", "delay", "invokerestapi"}


def pipeline_features(pipeline: BuildPipeline | ReleasePipeline, estate: Estate) -> set[str]:
    features: set[str] = set()
    steps = pipeline_steps(pipeline)
    collapsed = pipeline_steps(pipeline, expand=False)
    names = {s.task_name.lower() for s in steps}
    groups = {g.id: g for g in estate.variable_groups}
    connections = {c.id: c for c in estate.service_connections}

    def flag(name: str, condition: object) -> None:
        if condition:
            features.add(name)

    jobs = [job for stage in pipeline.stages for job in stage.jobs]
    flag("demands", any(job.demands or job.pool for job in jobs))
    flag("vm_image", any(job.vm_image for job in jobs))
    flag("container_job", any(job.container or job.services for job in jobs))
    flag("task_group", any(s.is_task_group for s in collapsed))
    flag("variable_group", pipeline.variable_group_ids or pipeline.variables)
    flag(
        "key_vault",
        "azurekeyvault" in names or any(groups[g].type.lower() == "azurekeyvault" for g in pipeline.variable_group_ids if g in groups),
    )
    flag("secure_file", pipeline.secure_file_ids or "downloadsecurefile" in names)
    flag("service_connection", pipeline.service_connection_ids)
    flag("k8s_connection", any(connections[c].type.lower() == "kubernetes" for c in pipeline.service_connection_ids if c in connections))
    for trigger in pipeline.triggers:
        flag("ci_trigger", trigger.type == "ci")
        flag("path_filters", trigger.paths_include or trigger.paths_exclude)
        flag("pr_trigger", trigger.type in ("pr", "gated"))
        flag("schedule", trigger.type == "schedule")
        flag("build_completion", trigger.type in ("build_completion", "pipeline_resource"))
        flag("batch", trigger.batch)
    flag("pr_trigger", any(p.build_id == pipeline.id for p in estate.branch_policies))
    flag("depends_on", any(job.depends_on for job in jobs) or any(stage.depends_on for stage in pipeline.stages))
    flag("conditions", any(s.condition or s.continue_on_error or s.timeout for s in steps))
    flag("matrix", any(job.strategy in ("matrix", "parallel", "multi-config", "multi-agent") for job in jobs))
    flag("artifacts", names & _ARTIFACT_TASKS)
    flag("feed", any(t.type == "package_repository" for stage in pipeline.stages for t in stage.targets))
    flag("test_results", "publishtestresults" in names or "vstest" in names)
    flag("coverage", "publishcodecoverageresults" in names)
    flag("release_stage", isinstance(pipeline, ReleasePipeline) or any(job.kind == "deployment" for job in jobs))
    flag(
        "approvals",
        any(
            (stage.pre_approval and not stage.pre_approval.automated) or (stage.post_approval and not stage.post_approval.automated)
            for stage in pipeline.stages
        ),
    )
    flag("gates", any(stage.pre_gates or stage.post_gates for stage in pipeline.stages))
    flag("parallelism", any(stage.parallelism for stage in pipeline.stages))
    flag("deployment_group", any(job.kind == "deployment_group" for job in jobs))
    flag("agentless", any(job.kind == "server" for job in jobs) or names & _AGENTLESS_TASKS)
    flag("stage_variables", any(stage.variables for stage in pipeline.stages))
    flag("config_transform", any(stage.config_transforms for stage in pipeline.stages))
    flag("tfvc", "tfvc_source" in pipeline.flags)
    if isinstance(pipeline, BuildPipeline):
        flag("yaml_template", pipeline.templates or any(f.startswith("template_unresolved") for f in pipeline.flags))
        flag("resource_repos", pipeline.resource_repos)
        flag("retention", pipeline.retention)
    else:
        flag("retention", any(stage.retention for stage in pipeline.stages))
    return features
