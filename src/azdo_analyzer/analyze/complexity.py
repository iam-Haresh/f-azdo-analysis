"""Complexity and effort (spec section 13.7). Weights and bands come from the config."""

from __future__ import annotations

from ..config.models import Config
from ..model.entities import BuildPipeline, EffortScore, Estate, ReleasePipeline, pipeline_steps


def band_for(score: float, bands: dict[str, float]) -> str:
    for name, limit in sorted(bands.items(), key=lambda item: item[1]):
        if score <= limit:
            return name
    return "XL"


def score_pipeline(pipeline: BuildPipeline | ReleasePipeline, cross_project: bool, config: Config) -> EffortScore:
    weights = config.complexity.weights
    factors: list[tuple[str, float]] = []

    def add(label: str, points: float) -> None:
        if points:
            factors.append((label, points))

    steps = pipeline_steps(pipeline)
    collapsed = pipeline_steps(pipeline, expand=False)
    if isinstance(pipeline, ReleasePipeline) or pipeline.kind == "classic":
        add("classic definition", weights.classic_definition)
    add(f"{len(steps)} tasks", (len(steps) // 10) * weights.per_10_tasks)
    add(f"{len(pipeline.plan.unmapped_tasks)} unmapped task(s)", len(pipeline.plan.unmapped_tasks) * weights.per_unmapped_task)
    marketplace = {s.task_name for s in steps if s.is_marketplace or s.is_custom}
    add(f"{len(marketplace)} marketplace/custom task(s)", len(marketplace) * weights.per_marketplace_task)
    groups = {s.task_group_id or s.task_name for s in collapsed if s.is_task_group}
    add(f"{len(groups)} task group(s)", len(groups) * weights.per_task_group)
    stages = len(pipeline.stages) if isinstance(pipeline, ReleasePipeline) else max(0, len(pipeline.stages) - 1)
    add(f"{stages} stage(s)", stages * weights.per_stage)
    add(
        f"{len(pipeline.service_connection_ids)} service connection(s)",
        len(pipeline.service_connection_ids) * weights.per_service_connection,
    )
    if pipeline.plan.runner_os == "windows":
        add("Windows-only", weights.windows_only)
    if any(job.kind == "deployment_group" for stage in pipeline.stages for job in stage.jobs):
        add("deployment group", weights.deployment_group)
    if any(stage.pre_gates or stage.post_gates for stage in pipeline.stages):
        add("gates", weights.gates)
    if cross_project:
        add("cross-project dependency", weights.cross_project_dependency)
    if "tfvc_source" in pipeline.flags:
        add("TFVC source", weights.tfvc_source)
    long_scripts = sum(1 for s in steps if s.script_lines > config.complexity.inline_script_line_threshold)
    add(f"{long_scripts} long inline script(s)", long_scripts * weights.inline_script_over_50_lines)

    score = round(sum(points for _, points in factors), 2)
    return EffortScore(
        score=score,
        band=band_for(score, config.complexity.bands),
        factors=[f"{label} (+{points:g})" for label, points in factors],
    )


def score_estate(estate: Estate, config: Config) -> None:
    cross = {dep.from_id for dep in estate.cross_project_deps}
    for pipeline in estate.pipelines():
        pipeline.effort = score_pipeline(pipeline, pipeline.cross_project or pipeline.id in cross, config)
