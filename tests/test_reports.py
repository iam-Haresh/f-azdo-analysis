"""Reports (phase 7) and GitLab extras (phase 8)."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

from openpyxl import load_workbook
from ruamel.yaml import YAML
from typer.testing import CliRunner

from azdo_analyzer.analyze.run import analyze
from azdo_analyzer.cli import app
from azdo_analyzer.collect.runner import CollectSummary, collect
from azdo_analyzer.config import Config
from azdo_analyzer.model.entities import Estate
from azdo_analyzer.report.run import write_reports
from azdo_analyzer.report.tables import SHEET_IDS, build_tables

from conftest import ConfigFactory, client_factory
from fake_azdo import FakeAzdo

SPEC_SHEETS = [
    "summary", "projects", "repos", "components", "build_pipelines", "release_pipelines", "release_stages", "mapping",
    "tasks_usage", "unmapped_tasks", "task_groups", "variable_groups", "variables", "service_connections", "agent_pools",
    "deployment_targets", "environments", "triggers", "branch_policies", "cross_project_deps", "template_fit", "clusters",
    "security", "runner_requirements", "effort", "orphans", "extensions", "errors",
]  # fmt: skip
SECRETS = ["test-token-not-real", "PlainTextPassw0rd", "hunter2hunter2", "Sup3rS3cretValue", "abcdef123456", "devpass123", "SECRETTOKEN123"]


def digest(root: Path, patterns: tuple[str, ...]) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for pattern in patterns for p in sorted(root.rglob(pattern))
    }


def read_csv(out: Path, name: str) -> list[dict[str, str]]:
    with (out / "csv" / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_all_sheets_present_in_excel_and_csv(config: Config, estate: Estate) -> None:
    assert SHEET_IDS == SPEC_SHEETS
    written = write_reports(estate, config)
    out = Path(config.run.output_dir)
    workbook = load_workbook(out / "inventory.xlsx")
    assert workbook.sheetnames == SPEC_SHEETS
    sheet = workbook["build_pipelines"]
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref.startswith("A1:")
    assert [c.value for c in sheet[1]][:4] == ["source", "project", "folder", "name"]
    assert sheet.max_row == len(estate.builds) + 1
    assert sorted(p.stem for p in (out / "csv").glob("*.csv")) == sorted(SPEC_SHEETS)
    for name in ("summary.md", "summary.html", "model.json", "projects/DefaultCollection/Alpha.md", "projects/DefaultCollection/Beta.md"):
        assert (out / name).is_file(), name
    assert len(list((out / "briefs").rglob("*.md"))) == len(estate.pipelines()) == len(list((out / "briefs").rglob("*.json")))
    assert all(p.is_file() for p in written)


def test_table_contents(config: Config, estate: Estate) -> None:
    write_reports(estate, config, ["csv"])
    out = Path(config.run.output_dir)
    builds = {r["name"]: r for r in read_csv(out, "build_pipelines")}
    assert builds["cross-proj-build"]["cross_project"] == "yes" and builds["cross-proj-build"]["repo_project"] == "Beta"
    assert builds["web-api-ci"]["type"] == "yaml" and builds["web-api-ci"]["yaml_path"] == "azure-pipelines.yml"
    unmapped = {r["task"]: r for r in read_csv(out, "unmapped_tasks")}
    assert set(unmapped) == {"AcmeDeploy", "Old extension task"}
    assert unmapped["AcmeDeploy"]["custom"] == "yes" and unmapped["Old extension task"]["in_catalog"] == "no"
    assert unmapped["AcmeDeploy"]["example_pipelines"] == "Alpha/legacy-app-build"
    usage = {r["task"]: r for r in read_csv(out, "tasks_usage")}
    assert usage["replacetokens"]["marketplace"] == "yes" and usage["replacetokens"]["mapped"] == "yes"
    assert usage["Maven"]["used_in_builds"] == "2"
    errors = read_csv(out, "errors")
    assert {
        "source": "DefaultCollection",
        "project": "Beta",
        "collector": "service_connections",
        "message": "no access (HTTP 403)",
    } in errors
    stages = {(r["release"], r["stage"]): r for r in read_csv(out, "release_stages")}
    assert stages[("web-release", "Prod")]["gates"] == "queryWorkItems" and stages[("web-release", "Prod")]["tier"] == "prod"
    assert any(r["kind"] == "deployment group" and r["name"] == "IIS-Prod" for r in read_csv(out, "environments"))
    assert {r["key"] for r in read_csv(out, "summary") if r["metric"] == "wave"} >= {"Wave 1", "Wave 3"}


def test_outputs_are_deterministic(config: Config, fake: FakeAzdo, collected: CollectSummary, tmp_path: Path) -> None:
    out = Path(config.run.output_dir)
    patterns = ("*.json", "*.csv", "*.md", "*.html", "*.yml")
    write_reports(analyze(config), config)
    first = digest(out, patterns)
    for file in out.rglob("*"):
        if file.is_file():
            file.unlink()
    write_reports(analyze(config), config)
    assert digest(out, patterns) == first and len(first) >= 60


def test_privacy_no_secrets_or_identities_in_reports(config: Config, estate: Estate) -> None:
    write_reports(estate, config)
    out = Path(config.run.output_dir)
    for file in out.rglob("*"):
        if not file.is_file() or file.suffix == ".xlsx":
            continue
        text = file.read_text(encoding="utf-8")
        for secret in (*SECRETS, "Jane Dev", "jdev", "Rita Release", "svc-build"):
            assert secret not in text, f"{secret} leaked into {file}"
    workbook = load_workbook(out / "inventory.xlsx")
    cells = " ".join(str(c) for sheet in workbook for row in sheet.iter_rows(values_only=True) for c in row if c)
    assert not any(secret in cells for secret in SECRETS)
    variables = {(r["owner"], r["name"]): r for r in read_csv(out, "variables")}
    assert variables[("legacy-app-build", "DeployPassword")]["value"] == "***REDACTED***"
    assert variables[("legacy-app-build", "DeployPassword")]["suspected_secret"] == "yes"
    assert (
        variables[("legacy-app-build", "NugetApiKey")] | {"is_secret": "yes", "value": ""} == variables[("legacy-app-build", "NugetApiKey")]
    )
    assert variables[("web-release / Dev", "ConnString")]["value"] == "Server=dev;***REDACTED***"


def test_privacy_settings_are_configurable(make_config: ConfigFactory, fake: FakeAzdo) -> None:
    config = make_config(reports={"privacy": {"include_identities": True, "max_variable_value_length": 5, "redact_value_patterns": []}})
    collect(config, client_factory=client_factory(fake, config))
    estate = analyze(config)
    assert next(b for b in estate.builds if b.name == "web-api-ci").stats.last_requester == "CORP\\jdev"
    group = next(g for g in estate.variable_groups if g.name == "web-api-common")
    assert next(v for v in group.variables if v.name == "ApiUrl").value == "https…"


def test_summary_and_project_markdown(config: Config, estate: Estate) -> None:
    write_reports(estate, config, ["markdown", "html"])
    out = Path(config.run.output_dir)
    summary = (out / "summary.md").read_text(encoding="utf-8")
    for heading in (
        "Estate totals",
        "Active vs inactive",
        "Classic vs YAML",
        "Stack distribution",
        "Deploy-target distribution",
        "Effort by band",
        "Top unmapped tasks",
        "Template gaps by stack",
        "Risks and blockers",
        "Suggested migration waves",
    ):
        assert f"## {heading}" in summary
    assert "| AcmeDeploy | 2 | 1 | custom |" in summary
    assert "pipeline(s) build from TFVC" in summary and "use deployment groups" in summary and "need Windows runners" in summary
    html = (out / "summary.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "AcmeDeploy" in html
    assert not re.search(r'(src|href)="http', html) and "<script" not in html  # single self-contained file
    alpha = (out / "projects/DefaultCollection/Alpha.md").read_text(encoding="utf-8")
    assert "```mermaid" in alpha and "flowchart LR" in alpha
    for heading in (
        "Overview",
        "Repositories and components",
        "Build pipelines",
        "Release pipelines",
        "Dependency graph",
        "Shared objects",
        "Runner needs",
        "Security posture",
        "Effort",
        "Open questions",
    ):
        assert f"## {heading}" in alpha
    assert "Deployment groups: runner on the target VMs" in alpha


def test_pipeline_brief(config: Config, estate: Estate) -> None:
    write_reports(estate, config, ["markdown"])
    folder = Path(config.run.output_dir) / "briefs/DefaultCollection/Alpha"
    brief = (folder / "legacy-app-build.build-2.md").read_text(encoding="utf-8")
    assert brief.startswith("# legacy-app-build  (classic build)\n")
    for heading in (
        "Linkage",
        "Stack",
        "Current flow",
        "Triggers",
        "Variables and secrets to recreate",
        "Credentials and connections",
        "Deployment (release / deployment jobs)",
        "GitLab build plan",
        "Gaps and decisions",
    ):
        assert f"\n## {heading}\n" in brief
    assert "Feeds release(s): Alpha/web-release" in brief
    assert "| Build | Build [agent; Default] | ↳ ↳ NuGetCommand |" in brief  # task groups shown expanded
    assert "CopyFiles [disabled]" in brief
    assert "**UNMAPPED**" in brief and "Unmapped task: `AcmeDeploy`" in brief
    assert "Runner OS: **windows**" in brief and "Matching template: **dotnet-framework**" in brief
    assert "| Task group; YAML template; extends |" in brief  # relevant concept rows only
    assert "Deployment group (agents on target VMs)" not in brief
    data = json.loads((folder / "legacy-app-build.build-2.json").read_text(encoding="utf-8"))
    assert data["plan"]["unmapped_tasks"] == ["AcmeDeploy", "Old extension task"] and data["linkage"]["releases"] == ["Alpha/web-release"]
    release = (folder / "web-release.release-1.md").read_text(encoding="utf-8")
    assert "Stage flow: (after_release) -> Dev ; Dev -> QA ; QA -> Prod" in release
    assert "Deployment group (agents on target VMs)" in release and "| Gates |" in release


def test_draft_skeletons_are_valid_yaml_with_todo_per_unmapped_step(config: Config, estate: Estate) -> None:
    write_reports(estate, config, ["json"])
    folder = Path(config.run.output_dir) / "skeletons"
    files = sorted(folder.rglob("*.gitlab-ci.yml"))
    assert len(files) == len(estate.pipelines())
    yaml = YAML(typ="safe")
    for file in files:
        text = file.read_text(encoding="utf-8")
        assert text.splitlines()[0] == "# DRAFT generated by azdo-analyzer — review before use"
        document = yaml.load(text)
        assert isinstance(document["stages"], list) and document["stages"]
        jobs = {k: v for k, v in document.items() if isinstance(v, dict) and "script" in v}
        assert jobs and all(job["stage"] in document["stages"] for job in jobs.values())
    for pipeline in estate.pipelines():
        text = next(f for f in files if f.name.startswith(f"{pipeline.name}.")).read_text(encoding="utf-8")
        for task in pipeline.plan.unmapped_tasks:
            if task in {m.task for m in pipeline.plan.step_mappings if m.stage != "(timeline)"}:
                assert f"# TODO(azdo:{task})" in text
    legacy = yaml.load((folder / "DefaultCollection/Alpha/legacy-app-build.build-2.gitlab-ci.yml").read_text(encoding="utf-8"))
    assert legacy["include"] == [{"project": "devsecops/templates", "file": "dotnetfx/build.yml", "ref": "v1"}]
    assert legacy["build"]["tags"] == ["windows", "shell"] and "image" not in legacy["build"]
    assert legacy["workflow"]["rules"][0] == {"if": '$CI_COMMIT_BRANCH == "main"', "changes": ["src/**/*"]}
    assert legacy["variables"]["BUILDCONFIGURATION"] == "Release" and "NUGETAPIKEY" not in legacy["variables"]
    web = yaml.load((folder / "DefaultCollection/Alpha/web-api-ci.build-1.gitlab-ci.yml").read_text(encoding="utf-8"))
    assert web["stages"] == ["build", "prod"] and web["build-ui"]["image"] == "mcr.microsoft.com/dotnet/sdk:8.0"
    assert web["prod-deploy"]["environment"] == {"name": "web-prod", "deployment_tier": "production"}
    release = yaml.load((folder / "DefaultCollection/Alpha/web-release.release-1.gitlab-ci.yml").read_text(encoding="utf-8"))
    assert release["prod-iis-deploy"]["when"] == "manual" and release["prod-iis-deploy"]["resource_group"] == "prod"


def test_skeletons_and_formats_are_optional(make_config: ConfigFactory, fake: FakeAzdo) -> None:
    config = make_config(gitlab={"generate_skeletons": False}, reports={"formats": ["csv"], "excel": {"sheets": ["summary"]}})
    collect(config, client_factory=client_factory(fake, config))
    write_reports(analyze(config), config)
    out = Path(config.run.output_dir)
    assert not (out / "skeletons").exists() and not (out / "inventory.xlsx").exists() and (out / "csv/mapping.csv").is_file()


def test_untrusted_cells_cannot_become_formulas(estate: Estate) -> None:
    from azdo_analyzer.report.writers import _safe

    assert _safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _safe("@cmd") == "'@cmd" and _safe("plain\x07text") == "plaintext"
    assert set(build_tables(estate)) == set(SPEC_SHEETS)


def test_cli_offline_stages(config: Config, collected: CollectSummary, tmp_path: Path) -> None:
    file = tmp_path / "config.json"
    file.write_text(config.model_dump_json(), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["analyze", "-c", str(file)])
    assert result.exit_code == 0 and "6 build and 3 release" in result.output
    result = runner.invoke(app, ["report", "-c", str(file), "--format", "markdown,csv"])
    assert result.exit_code == 0, result.output
    out = Path(config.run.output_dir)
    assert (out / "summary.md").is_file() and not (out / "inventory.xlsx").exists()
    log_line = json.loads((out / "azdo-analyzer.log").read_text(encoding="utf-8").splitlines()[0])
    assert {"time", "level", "logger", "message"} <= set(log_line)
    result = runner.invoke(app, ["brief", "-c", str(file), "--pipeline", "DefaultCollection/Alpha/web-api-ci"])
    assert result.exit_code == 0 and result.output.strip().endswith("web-api-ci.build-1.md")
    assert runner.invoke(app, ["brief", "-c", str(file), "--pipeline", "nope"]).exit_code == 2
    assert runner.invoke(app, ["report", "-c", str(file), "--format", "pdf"]).exit_code == 2
