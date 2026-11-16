"""``report`` stage: write every configured format under ``output_dir``."""

from __future__ import annotations

import logging
from pathlib import Path

from ..analyze.gitlab import Mappings
from ..config.models import Config
from ..model.entities import BuildPipeline, Estate, ReleasePipeline
from ..util import json_dumps, safe_name, write_text
from .markdown import brief_data, render_brief, render_project, render_summary, summary_data
from .skeleton import render_skeleton
from .tables import build_tables
from .writers import render_html, write_csv, write_excel

log = logging.getLogger(__name__)


def pipeline_file(pipeline: BuildPipeline | ReleasePipeline) -> str:
    """File stem for a pipeline: its name, made unique with kind and id."""
    kind = "release" if isinstance(pipeline, ReleasePipeline) else "build"
    return safe_name(f"{pipeline.name}.{kind}-{pipeline.azdo_id}")


def brief_paths(pipeline: BuildPipeline | ReleasePipeline, out: Path) -> tuple[Path, Path]:
    folder = out / "briefs" / safe_name(pipeline.source) / safe_name(pipeline.project)
    stem = pipeline_file(pipeline)
    return folder / f"{stem}.md", folder / f"{stem}.json"


def write_brief(estate: Estate, pipeline: BuildPipeline | ReleasePipeline, mappings: Mappings, config: Config) -> Path:
    md_path, json_path = brief_paths(pipeline, Path(config.run.output_dir))
    write_text(md_path, render_brief(estate, pipeline, mappings, config))
    write_text(json_path, json_dumps(brief_data(estate, pipeline, mappings)))
    return md_path


def write_reports(estate: Estate, config: Config, formats: list[str] | None = None) -> list[Path]:
    out = Path(config.run.output_dir)
    wanted = formats or list(config.reports.formats)
    written: list[Path] = []
    tables = build_tables(estate)
    mappings = Mappings(config)

    if "json" in wanted:
        path = out / "model.json"
        write_text(path, json_dumps(estate.model_dump(mode="json")))
        written.append(path)
    if "csv" in wanted:
        written += write_csv(tables, out / "csv")
    if "xlsx" in wanted:
        written.append(write_excel(tables, out / "inventory.xlsx", config.reports.excel))
    if "markdown" in wanted:
        path = out / "summary.md"
        write_text(path, render_summary(estate))
        written.append(path)
        if config.reports.per_project_markdown:
            for project in estate.projects:
                if not project.included:
                    continue
                path = out / "projects" / safe_name(project.source) / f"{safe_name(project.name)}.md"
                write_text(path, render_project(estate, project, config))
                written.append(path)
        if config.reports.per_pipeline_brief:
            for pipeline in estate.pipelines():
                written.append(write_brief(estate, pipeline, mappings, config))
    if "html" in wanted:
        path = out / "summary.html"
        write_text(path, render_html(summary_data(estate)))
        written.append(path)
    if config.gitlab.generate_skeletons:
        for pipeline in estate.pipelines():
            if isinstance(pipeline, BuildPipeline) and pipeline.kind == "xaml":
                continue
            path = out / "skeletons" / safe_name(pipeline.source) / safe_name(pipeline.project) / f"{pipeline_file(pipeline)}.gitlab-ci.yml"
            write_text(path, render_skeleton(pipeline, estate, config))
            written.append(path)
    log.info("wrote %d report files under %s", len(written), out)
    return written
