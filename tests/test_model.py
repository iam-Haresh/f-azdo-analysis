"""Normalize + link (phase 3) and parsers (phase 4)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from azdo_analyzer.config import Config
from azdo_analyzer.detect.pipeline import link_components
from azdo_analyzer.detect.rules import load_rules
from azdo_analyzer.model.entities import BuildPipeline, Estate, ReleasePipeline, iter_steps, pipeline_steps
from azdo_analyzer.parse.yaml_loader import RepoRef, TemplateResolver, load_yaml

from fake_azdo import R_WEB

SRC = "DefaultCollection"
GOLDEN = Path(__file__).parent / "golden"


def build(estate: Estate, name: str) -> BuildPipeline:
    return next(b for b in estate.builds if b.name == name)


def release(estate: Estate, name: str) -> ReleasePipeline:
    return next(r for r in estate.releases if r.name == name)


def rows(estate: Estate, **match: Any) -> list[dict[str, Any]]:
    return [r.model_dump() for r in estate.mapping if all(getattr(r, k) == v for k, v in match.items())]


# -- linkage ---------------------------------------------------------------------------


def test_stable_ids_and_schema_version(estate: Estate) -> None:
    assert estate.schema_version == 1
    assert build(estate, "web-api-ci").id == f"{SRC}/Alpha/build/1"
    assert release(estate, "web-release").id == f"{SRC}/Alpha/release/1"
    assert f"{SRC}/Alpha/repo/{R_WEB}" in {r.id for r in estate.repos}
    assert [p.name for p in estate.projects if p.included] == ["Alpha", "Beta"]
    assert "spike-poc" not in {b.name for b in estate.builds}


def test_mapping_same_project_build_through_release_to_targets(estate: Estate) -> None:
    found = rows(estate, build="legacy-app-build", release="web-release")
    assert [(r["stage"], r["tier"], r["target"]) for r in found] == [
        ("Dev", "dev", "Azure App Service"),
        ("Prod", "prod", "IIS / Windows VM"),
        ("QA", "qa", "Azure App Service"),
    ]
    assert all(r["project"] == "Alpha" and r["repo"] == "legacy-app" and not r["cross_project"] for r in found)


def test_mapping_cross_project_build(estate: Estate) -> None:
    pipeline = build(estate, "cross-proj-build")
    assert pipeline.cross_project and pipeline.repo_project == "Beta"
    (row,) = rows(estate, build="cross-proj-build")
    assert (row["project"], row["repo_project"], row["repo"], row["cross_project"]) == ("Alpha", "Beta", "orders-svc", True)
    assert any(d.kind == "repo" and d.from_id == pipeline.id for d in estate.cross_project_deps)
    edge = next(e for e in estate.edges if e.from_id == pipeline.id and e.kind == "builds")
    assert edge.cross_project and edge.evidence == "definition.repository"


def test_mapping_release_with_two_artifacts(estate: Estate) -> None:
    web = release(estate, "web-release")
    assert web.build_ids == [f"{SRC}/Alpha/build/2", f"{SRC}/Beta/build/10"]
    assert web.cross_project
    via_beta = rows(estate, build="orders-ci", release="web-release")
    assert len(via_beta) == 3
    assert all(r["project"] == "Beta" and r["cross_project"] for r in via_beta)
    assert any(d.kind == "artifact" and d.to_id == f"{SRC}/Beta/build/10" for d in estate.cross_project_deps)


def test_mapping_git_artifact_links_release_straight_to_repo(estate: Estate) -> None:
    git = release(estate, "git-release")
    assert git.build_ids == [] and git.repo_ids == [f"{SRC}/Alpha/repo/{R_WEB}"]
    (row,) = rows(estate, release="git-release")
    assert (row["repo"], row["build"], row["stage"], row["target"]) == ("web-api", "", "Production", "Kubernetes / AKS")


def test_orphans(estate: Estate) -> None:
    reasons = {(o.kind, o.name): o.reason for o in estate.orphans}
    assert "no longer exists" in reasons[("release_pipeline", "broken-release")]
    assert ("repo", "unused-repo") in reasons
    assert ("task_group", "Old steps") in reasons
    assert ("variable_group", "unused-group") in reasons
    assert ("service_connection", "unused-conn") in reasons
    assert ("build_pipeline", "never-run") in reasons
    assert ("variable_group", "web-api-common") not in reasons
    assert ("task_group", "Restore NuGet") not in reasons  # used through its parent task group


def test_triggers_policies_and_usage(estate: Estate) -> None:
    orders = build(estate, "orders-ci")
    assert orders.upstream_ids == [f"{SRC}/Alpha/build/3"]
    assert build(estate, "cross-proj-build").downstream_ids == [orders.id]
    assert any(d.kind == "trigger" for d in estate.cross_project_deps)
    (policy,) = estate.branch_policies
    assert (policy.repo_name, policy.branch, policy.build_name, policy.blocking) == ("web-api", "main", "web-api-ci", True)
    azure = next(c for c in estate.service_connections if c.name == "azure-prod")
    assert azure.used_by == [f"{SRC}/Alpha/release/1"]
    assert azure.url == "https://management.azure.com"
    used = {e.name: e.used for e in estate.extensions}
    assert used == {"Replace Tokens": True, "Unused Extension": False}
    assert next(p for p in estate.agent_pools if p.name == "Default").key_capabilities["VisualStudio_16.0"] == "2/2 agents"


# -- parsers ---------------------------------------------------------------------------


def test_classic_build_task_groups_expand_recursively(estate: Estate) -> None:
    legacy = build(estate, "legacy-app-build")
    job = legacy.stages[0].jobs[0]
    group = job.steps[0]
    assert group.is_task_group and group.task_name == "Build .NET Framework"
    assert [c.task_name for c in group.children] == ["Restore NuGet", "VSBuild"]
    assert [c.task_name for c in group.children[0].children] == ["NuGetCommand"]
    # collapsed and expanded views
    assert [s.task_name for s in iter_steps(job.steps, expand=False)][0] == "Build .NET Framework"
    expanded = [s.task_name for s in pipeline_steps(legacy)]
    assert expanded[:3] == ["NuGetCommand", "VSBuild", "VSTest"]
    assert "CopyFiles" not in expanded  # disabled step is reported but excluded from mapping
    assert any(not s.enabled and s.task_name == "CopyFiles" for s in job.steps)
    assert (job.strategy, job.matrix, job.demands) == ("multi-config", ["BuildConfiguration"], ["msbuild", "visualstudio"])
    assert job.pool == "Default"


def test_classic_build_task_origins_triggers_and_variables(estate: Estate) -> None:
    legacy = build(estate, "legacy-app-build")
    steps = {s.display_name: s for s in pipeline_steps(legacy)}
    assert steps["Replace tokens"].is_marketplace and steps["Replace tokens"].task_author == "Guillaume Rouchon"
    assert steps["Acme deploy prep"].is_custom
    assert not steps["Old extension task"].in_catalog
    ci, schedule = legacy.triggers
    assert (ci.branches_include, ci.branches_exclude, ci.paths_include, ci.paths_exclude, ci.batch) == (
        ["main"],
        ["experimental/*"],
        ["/src"],
        ["/docs"],
        True,
    )
    assert (schedule.cron, schedule.timezone) == ("30 3 * * 1,2,3,4,5", "W. Europe Standard Time")
    variables = {v.name: v for v in legacy.variables}
    assert variables["NugetApiKey"].is_secret and variables["NugetApiKey"].value == ""
    assert variables["BuildConfiguration"].allow_override
    assert legacy.variable_group_ids == [f"{SRC}/Alpha/variable_group/1"]


def test_legacy_flat_step_list_and_tfvc(estate: Estate) -> None:
    tfvc = build(estate, "tfvc-build")
    assert [s.task_name for s in pipeline_steps(tfvc)] == ["VSBuild"]
    assert "tfvc_source" in tfvc.flags and tfvc.tfvc_mappings == ["map:$/Beta/Main"]
    assert tfvc.triggers[0].type == "gated"
    repo = next(r for r in estate.repos if r.id == tfvc.repo_id)
    assert repo.type == "tfvc" and repo.scan_status == "scanned"
    # the 8-level-deep project is beyond tfvc.max_depth
    assert sorted(c.path for c in repo.components) == ["Dev/App", "Main/App", "Main/Tools"]
    assert repo.last_commit  # date of the newest changeset
    # workspace mapping $/Beta/Main drops the Dev branch; the solution path narrows to Main/App
    assert tfvc.component_ids == [f"{repo.id}#dotnet:Main/App"]
    assert tfvc.stack == ["csharp/aspnet-mvc/msbuild"]


def test_tfvc_workspace_mapping_scopes_components(estate: Estate, config: Config) -> None:
    rules = load_rules(config)
    tfvc = build(estate, "tfvc-build").model_copy(deep=True)
    repo = next(r for r in estate.repos if r.id == tfvc.repo_id)
    tfvc.stages = []  # no task inputs: only the mapping decides

    def linked(*mappings: str) -> list[str]:
        tfvc.tfvc_mappings, tfvc.flags = list(mappings), []
        return sorted(c.path for c in link_components(tfvc, repo, rules))

    assert linked("map:$/Beta/Main") == ["Main/App", "Main/Tools"]
    assert linked("map:$/Beta/Main", "cloak:$/Beta/Main/Tools") == ["Main/App"]
    assert linked("map:$/Beta") == ["Dev/App", "Main/App", "Main/Tools"]
    assert linked("map:$/Beta/Main/App/src") == ["Main/App"]  # mapping points inside a component
    assert linked("map:$/Other/Main") == [] and tfvc.flags == ["tfvc_mapping_unmatched"]
    assert linked("map:$/Beta/NotScanned") == [] and tfvc.flags == ["tfvc_mapping_unmatched"]


def test_yaml_loader_keeps_duplicate_expression_keys() -> None:
    doc = load_yaml(
        "variables:\n  ${{ if eq(1, 1) }}:\n    a: 1\n  ${{ if eq(1, 1) }}:\n    b: 3.10\nsteps:\n- ${{ if true }}:\n  - script: one\n- ${{ if true }}:\n  - script: two\n"
    )
    assert len(doc["variables"]) == 2
    assert [list(item.values())[0][0]["script"] for item in doc["steps"]] == ["one", "two"]
    assert "3.10" in json.dumps(doc)  # versions are not turned into floats


def test_yaml_pipeline_templates_conditions_and_cross_repo(estate: Estate) -> None:
    web = build(estate, "web-api-ci")
    assert web.kind == "yaml" and not [f for f in web.flags if f.startswith(("yaml_", "template_"))]
    assert web.templates == [
        "Alpha/pipeline-templates:steps/dotnet-build.yml",
        "Alpha/pipeline-templates:steps/publish.yml",
        "Alpha/pipeline-templates:jobs/angular.yml",
    ]
    assert web.resource_repos == ["Alpha/pipeline-templates"]
    assert any(e.kind == "includes_template" and e.from_id == web.id for e in estate.edges)
    stage_build, stage_prod = web.stages
    api, ui = stage_build.jobs
    assert [s.task_name for s in api.steps] == [
        "UseDotNet", "DotNetCoreCLI", "DotNetCoreCLI", "PublishPipelineArtifact", "DotNetCoreCLI", "PublishTestResults", "Docker",
    ]  # fmt: skip
    assert api.steps[1].inputs["projects"] == "src/Api/Api.csproj"  # template parameter substituted
    assert api.steps[1].inputs["arguments"] == "-c $(buildConfiguration)"
    assert api.steps[4].condition == "${{ if eq(parameters.runTests, true) }}"  # both duplicate-key branches kept
    assert api.steps[5].condition == "${{ if eq(parameters.runTests, true) }}"
    assert (ui.name, ui.pool, ui.demands, ui.template) == ("ui", "Linux-Pool", ["npm"], "jobs/angular.yml@templates")
    assert ui.steps[1].inputs["workingDirectory"] == "web"
    assert (stage_prod.display_name, stage_prod.tier, stage_prod.depends_on) == ("Prod", "prod", ["Build"])
    deploy = stage_prod.jobs[0]
    assert (deploy.kind, deploy.environment, deploy.environment_resource, deploy.strategy) == (
        "deployment",
        "web-prod",
        "web-ns",
        "runOnce",
    )
    assert [v.value for v in web.variables if v.name == "imageTag"] == ["stable", "preview"]
    assert web.variable_group_ids == [f"{SRC}/Alpha/variable_group/1"]
    assert web.resources["parameters"] == ["runTests"]
    ci = web.triggers[0]
    assert (ci.type, ci.batch, ci.branches_include, ci.paths_exclude) == ("ci", True, ["main", "release/*"], ["docs/*"])
    assert web.triggers[1].cron == "0 2 * * 1-5"


def test_timeline_expansion_is_ground_truth(estate: Estate) -> None:
    web = build(estate, "web-api-ci")
    assert web.timeline is not None
    assert web.timeline.tasks == ["UseDotNet@2", "DotNetCoreCLI@2", "SonarQubePrepare@5"]  # skipped records are ignored
    assert web.timeline.only_in_timeline == ["SonarQubePrepare"]
    assert "HelmDeploy" in web.timeline.only_in_static
    assert [s.task_name for s in web.timeline_steps] == ["SonarQubePrepare"]
    assert "SonarQube" in web.security.scanners  # found only through the timeline


def test_missing_yaml_file_and_unresolved_template() -> None:
    class Files:
        def __init__(self, files: dict[str, str]) -> None:
            self.files = files

        def read(self, repo: RepoRef, path: str) -> str | None:
            return self.files.get(path)

        def find_repo(self, project: str, name: str, ref: str | None) -> RepoRef | None:
            return None

    repo = RepoRef("P", "r", "repo", "main")
    assert TemplateResolver(Files({})).resolve(repo, "azure-pipelines.yml").missing
    resolved = TemplateResolver(
        Files({"ci/a.yml": "extends:\n  template: base.yml\n", "ci/base.yml": "steps:\n- template: ../gone.yml\n- script: x\n"})
    ).resolve(repo, "ci/a.yml")
    assert resolved.unresolved == ["../gone.yml"]
    assert [list(s)[0] for s in resolved.doc["steps"]] == ["template", "script"]
    looping = TemplateResolver(Files({"a.yml": "steps:\n- template: a.yml\n"}), max_depth=3).resolve(repo, "a.yml")
    assert looping.unresolved == ["a.yml"]  # recursion stops at max_template_depth
    assert "not a mapping" in TemplateResolver(Files({"a.yml": "- 1\n"})).resolve(repo, "a.yml").error


def test_release_parser(estate: Estate) -> None:
    web = release(estate, "web-release")
    assert [(a.alias, a.type, a.source_project, a.is_primary, a.cross_project) for a in web.artifacts] == [
        ("_legacy", "Build", "Alpha", True, False),
        ("_orders", "Build", "Beta", False, True),
    ]
    assert web.stage_flow == ["(after_release) -> Dev", "Dev -> QA", "QA -> Prod"]
    dev, qa, prod = web.stages
    assert (dev.trigger, dev.tier, dev.parallelism) == ("after_release", "dev", "concurrency=1,queueDepth=0")
    assert dev.pre_approval is not None and dev.pre_approval.automated
    assert prod.pre_approval is not None and not prod.pre_approval.automated
    assert prod.pre_approval.approvers[1] == "[Alpha]\\Release Approvers"  # groups are kept
    assert prod.pre_approval.approvers[0].startswith("user-")  # people are hashed
    assert prod.pre_gates is not None and prod.pre_gates.tasks == ["queryWorkItems"]
    iis, agentless = prod.jobs
    assert (iis.kind, iis.deployment_group, iis.deployment_group_tags) == ("deployment_group", "IIS-Prod", ["web"])
    assert (agentless.kind, agentless.steps[0].task_name) == ("server", "ManualIntervention")
    assert web.triggers[0].type == "artifact" and web.triggers[0].branches_include == ["main"]
    assert web.variable_group_ids == [f"{SRC}/Alpha/variable_group/2"]
    target = dev.targets[0]
    assert (target.type, target.service_connection, target.credential_type, target.strategy) == (
        "azure_app_service", "azure-prod", "service principal secret", "slot_swap",
    )  # fmt: skip
    assert dev.config_transforms == ["AzureRmWebAppDeployment(enableXmlTransform)"]
    assert release(estate, "broken-release").artifacts[0].missing


@pytest.mark.parametrize("name", ["legacy-app-build", "web-api-ci", "web-release"])
def test_parser_golden_files(estate: Estate, name: str) -> None:
    pipeline = next(p for p in estate.pipelines() if p.name == name)
    actual = json.dumps(
        {
            "stages": [
                s.model_dump(mode="json", exclude={"stats", "targets", "service_connection_ids", "config_transforms"})
                for s in pipeline.stages
            ],
            "triggers": [t.model_dump(mode="json") for t in pipeline.triggers],
        },
        indent=2,
        sort_keys=True,
    )
    golden = GOLDEN / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") or not golden.exists():
        golden.write_text(actual + "\n", encoding="utf-8")
    assert actual + "\n" == golden.read_text(encoding="utf-8")
