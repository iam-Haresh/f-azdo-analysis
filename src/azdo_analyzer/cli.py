"""Command line interface (spec section 16).

Exit code is non-zero only on config errors, total auth failure, or when
``run.fail_on_project_error`` is set and a collector reported an error.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import __version__
from .analyze.gitlab import Mappings
from .analyze.run import analyze, load_model, save_model
from .collect.doctor import format_table, run_doctor
from .collect.runner import CollectSummary, collect
from .config import Config, ConfigError, config_json_schema, load_config
from .model.entities import Estate
from .report.run import write_brief, write_reports

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Read-only Azure DevOps pipeline analyzer (HTTP GET only).")
config_app = typer.Typer(no_args_is_help=True, help="Config file helpers.")
app.add_typer(config_app, name="config")

ConfigOption = typer.Option(..., "-c", "--config", help="Path to the YAML or JSON config file.")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )


def _load(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _logging(config: Config) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(config.run.log_level)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    out = Path(config.run.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    structured = logging.FileHandler(out / "azdo-analyzer.log", encoding="utf-8")
    structured.setFormatter(_JsonFormatter())
    root.addHandler(structured)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request lines would flood the log


def _collect_summary(summary: CollectSummary, config: Config) -> None:
    typer.echo(
        f"collect: {summary.projects} project(s), {summary.api_calls} API call(s), {len(summary.errors)} error(s)"
        + (f", failed sources: {', '.join(summary.failed_sources)}" if summary.failed_sources else "")
    )
    for error in summary.errors[:20]:
        typer.echo(f"  [{error['source']}/{error['project'] or '-'}] {error['collector']}: {error['message']}")
    if len(summary.errors) > 20:
        typer.echo(f"  ... {len(summary.errors) - 20} more (see the errors sheet)")
    if summary.total_auth_failure:
        typer.echo("No source could be reached or authenticated.", err=True)
        raise typer.Exit(1)
    if config.run.fail_on_project_error and summary.errors:
        raise typer.Exit(1)


def _estate(config: Config) -> Estate:
    estate = load_model(config)
    if estate is None:
        estate = analyze(config)
        save_model(estate, config)
    return estate


@app.command()
def version() -> None:
    """Print the tool version."""
    typer.echo(__version__)


@app.command()
def doctor(config: Path = ConfigOption) -> None:
    """Connectivity, api-version and PAT read-scope check per source."""
    cfg = _load(config)
    results, healthy = run_doctor(cfg)
    typer.echo(format_table(results))
    if not healthy:
        raise typer.Exit(1)


@app.command("collect")
def collect_command(
    config: Path = ConfigOption,
    source: str | None = typer.Option(None, "--source", help="Only this source."),
    project: str | None = typer.Option(None, "--project", help="Only this project (glob or re: pattern)."),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore fresh cache entries and fetch again."),
) -> None:
    """Fill the raw cache from Azure DevOps (HTTP GET only)."""
    cfg = _load(config)
    _logging(cfg)
    _collect_summary(collect(cfg, source_filter=source, project_filter=project, refresh=refresh), cfg)


@app.command("analyze")
def analyze_command(config: Path = ConfigOption) -> None:
    """Offline: normalize, link, detect, score and map from the cache."""
    cfg = _load(config)
    _logging(cfg)
    estate = analyze(cfg)
    path = save_model(estate, cfg)
    typer.echo(f"analyze: {len(estate.builds)} build and {len(estate.releases)} release pipeline(s) -> {path}")


@app.command()
def report(
    config: Path = ConfigOption,
    formats: str | None = typer.Option(None, "--format", help="Comma-separated subset: xlsx,markdown,json,csv,html."),
) -> None:
    """Write reports from the analysed model (runs analyze first when needed)."""
    cfg = _load(config)
    _logging(cfg)
    chosen = [f.strip() for f in formats.split(",") if f.strip()] if formats else None
    valid = {"xlsx", "markdown", "json", "csv", "html"}
    if chosen and set(chosen) - valid:
        typer.echo(f"Unknown format(s): {', '.join(sorted(set(chosen) - valid))}", err=True)
        raise typer.Exit(2)
    written = write_reports(_estate(cfg), cfg, chosen)
    typer.echo(f"report: {len(written)} file(s) under {cfg.run.output_dir}")


@app.command()
def run(
    config: Path = ConfigOption,
    refresh: bool = typer.Option(False, "--refresh", help="Ignore fresh cache entries and fetch again."),
) -> None:
    """collect + analyze + report."""
    cfg = _load(config)
    _logging(cfg)
    summary = collect(cfg, refresh=refresh)
    if not summary.total_auth_failure:
        estate = analyze(cfg)
        save_model(estate, cfg)
        written = write_reports(estate, cfg)
        typer.echo(f"report: {len(written)} file(s) under {cfg.run.output_dir}")
    _collect_summary(summary, cfg)


@app.command()
def brief(
    config: Path = ConfigOption,
    pipeline: str = typer.Option(..., "--pipeline", help='"<source>/<project>/<name>" or a stable pipeline id.'),
) -> None:
    """Write the brief for one pipeline and print its path."""
    cfg = _load(config)
    _logging(cfg)
    estate = _estate(cfg)
    wanted = pipeline.strip().lower()
    matches = [p for p in estate.pipelines() if wanted in (p.id.lower(), f"{p.source}/{p.project}/{p.name}".lower())]
    if not matches:
        typer.echo(f"No pipeline matches {pipeline!r}.", err=True)
        raise typer.Exit(2)
    mappings = Mappings(cfg)
    for match in matches:
        typer.echo(str(write_brief(estate, match, mappings, cfg)))


@config_app.command("schema")
def config_schema() -> None:
    """Print the JSON Schema of the config file."""
    typer.echo(json.dumps(config_json_schema(), indent=2, sort_keys=True))


@config_app.command("validate")
def config_validate(config: Path = ConfigOption) -> None:
    """Validate a config file and report problems."""
    cfg = _load(config)
    typer.echo(f"OK: {config} is valid ({len(cfg.sources)} source(s)).")
