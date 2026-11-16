"""Technology detection on fixture repositories (phase 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from azdo_analyzer.config import Config
from azdo_analyzer.detect.engine import ParsedFile, condition, extract, json_path
from azdo_analyzer.detect.repo import RepoScan, detect_components
from azdo_analyzer.detect.rules import RepoRule, load_rules
from azdo_analyzer.model.entities import Component, Estate
from azdo_analyzer.util import path_matches

from conftest import FIXTURES, ConfigFactory

REPOS = FIXTURES / "repos"


def scan(name: str, config: Config) -> RepoScan:
    root = REPOS / name
    paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def read(path: str) -> str | None:
        return (root / path).read_text(encoding="utf-8")

    return detect_components(f"repo/{name}", paths, read, load_rules(config), config)


def component(result: RepoScan, path: str, group: str) -> Component:
    return next(c for c in result.components if c.path == path and c.group == group)


def evidence(comp: Component, fact: str) -> list[str]:
    return [d.evidence for d in comp.detections if d.fact == fact]


def test_dotnet_framework(config: Config) -> None:
    result = scan("dotnet-framework", config)
    web = component(result, "src/LegacyWeb", "dotnet")
    assert (web.language, web.runtime_family, web.runtime_version, web.build_tool) == ("csharp", "netframework", "v4.8", "msbuild")
    assert (web.framework, web.project_kind, web.package_manager) == ("aspnet-mvc", "Web Application", "nuget (packages.config)")
    assert evidence(web, "runtime_version") == ["src/LegacyWeb/LegacyWeb.csproj [dotnet-legacy-csproj]"]
    tests = component(result, "src/LegacyWeb.Tests", "dotnet")
    assert tests.project_kind == "Test" and tests.test_tooling == ["mstest"]
    assert "requires_windows" in result.flags
    assert result.facts["solution"] == "LegacyWeb.sln" and result.facts["config_transforms"] == "present"


def test_dotnet8_with_angular_docker_and_helm(config: Config) -> None:
    result = scan("dotnet8-angular", config)
    api = component(result, "src/Api", "dotnet")
    assert (api.language, api.runtime_family, api.runtime_version, api.framework, api.build_tool) == (
        "csharp",
        "dotnet",
        "net8.0",
        "aspnetcore",
        "dotnet-cli",
    )
    assert api.build_tool_version == "8.0.204"  # pinned by global.json at the repo root
    assert api.facts["key_packages"] == "EF Core, Swagger" and api.container_build
    assert component(result, "src/Api.Tests", "dotnet").test_tooling == ["coverlet", "xunit"]
    ui = component(result, "web", "node")
    assert (ui.language, ui.framework, ui.framework_version, ui.package_manager) == ("typescript", "angular", "^17.3.0", "npm")
    assert ui.runtime_version == "20.11.1"  # .nvmrc (pin) beats engines.node (manifest)
    assert ui.test_tooling == ["jasmine", "karma"] and ui.facts["script_build"] == "ng build"
    assert (
        component(result, ".", "container").facts["base_images"] == "mcr.microsoft.com/dotnet/aspnet:8.0, mcr.microsoft.com/dotnet/sdk:8.0"
    )
    assert component(result, "charts/web-api", "helm").facts["chart_version"] == "0.3.1"
    assert {"monorepo", "polyglot", "has_dockerfile", "has_helm", "has_kubernetes"} <= set(result.flags)
    assert "requires_windows" not in result.flags


def test_spring_boot_maven(config: Config) -> None:
    result = scan("spring-boot-maven", config)
    app = component(result, ".", "java")
    assert (app.language, app.build_tool, app.runtime_version, app.framework, app.framework_version, app.packaging) == (
        "java", "maven", "17", "spring-boot", "3.2.4", "jar",
    )  # fmt: skip
    assert app.build_tool_version == "3.9.6"  # maven wrapper
    assert app.test_tooling == ["junit"]
    assert app.facts["repositories"] == "https://artifactory.example.com/libs-release"
    assert result.facts["scanner"] == "SonarQube"
    assert app.confidence == 0.95


def test_gradle(config: Config) -> None:
    app = component(scan("gradle-app", config), ".", "java")
    assert (app.build_tool, app.build_tool_version, app.runtime_version, app.framework, app.framework_version) == (
        "gradle",
        "8.5",
        "21",
        "spring-boot",
        "3.1.5",
    )
    assert app.test_tooling == ["junit"]


def test_node_react(config: Config) -> None:
    app = component(scan("node-react", config), ".", "node")
    assert (app.language, app.framework, app.package_manager, app.runtime_version) == ("javascript", "react", "pnpm", "18.19.0")
    assert app.facts["package_manager_version"] == "8.15.4" and app.facts["bundler"] == "vite"
    assert app.test_tooling == ["vitest"]


def test_python(config: Config) -> None:
    app = component(scan("python-app", config), ".", "python")
    assert (app.language, app.framework, app.build_tool, app.package_manager) == ("python", "fastapi", "poetry", "poetry")
    assert app.runtime_version == "3.11.8"  # .python-version pin over "^3.11"
    assert app.test_tooling == ["pytest"]


def test_docker_helm_and_terraform(config: Config) -> None:
    result = scan("docker-helm", config)
    assert component(result, ".", "container").facts["exposed_ports"] == "8000"
    assert component(result, "chart", "helm").facts["artifact"] == "svc"
    assert result.flags == ["has_dockerfile", "has_helm", "has_kubernetes"]
    terraform = scan("terraform", config)
    infra = component(terraform, "infra", "terraform")
    assert (infra.build_tool, infra.build_tool_version, infra.facts["providers"], infra.facts["backend"]) == (
        "terraform",
        ">= 1.6.0",
        "azurerm",
        "azurerm",
    )
    assert terraform.flags == ["has_iac"]


def test_polyglot_monorepo(config: Config) -> None:
    result = scan("monorepo", config)
    found = {(c.path, c.group): c for c in result.components}
    assert set(found) == {
        ("services/orders", "java"), ("services/orders", "container"), ("services/billing", "python"),
        ("ui", "node"), ("infra", "terraform"), ("deploy", "openshift"),
    }  # fmt: skip
    assert found[("services/orders", "java")].container_build and not found[("ui", "node")].container_build
    assert found[("deploy", "openshift")].facts["kinds"] == "Route"
    assert {"monorepo", "polyglot", "has_openshift", "has_iac", "git_submodules", "git_lfs"} <= set(result.flags)


def test_min_confidence_and_user_rules_override_builtins(make_config: ConfigFactory, tmp_path: Path) -> None:
    strict = scan("node-react", make_config(detection={"min_confidence": 0.99}))
    assert strict.components == []
    extra = tmp_path / "rules"
    extra.mkdir()
    (extra / "mine.yaml").write_text(
        "- id: react\n  files: ['**/package.json']\n  parser: json\n  when: { jsonpath: $.dependencies.react }\n  group: node\n"
        "  emit: { framework: react-custom }\n  confidence: 0.99\n"
    )
    overridden = scan("node-react", make_config(detection={"extra_rules_dirs": [str(extra)]}))
    assert component(overridden, ".", "node").framework == "react-custom"


def test_ignore_paths_and_manifest_cap(make_config: ConfigFactory) -> None:
    assert path_matches(["**/node_modules/**"], "web/node_modules/x/package.json")
    assert path_matches(["**/node_modules/**"], "node_modules/x/package.json")
    capped = scan("monorepo", make_config(repo_scan={"max_manifest_files_per_repo": 1}))
    assert len([c for c in capped.components if c.group in ("java", "python", "node")]) <= 1


def test_engine_extractors() -> None:
    data = {"dependencies": {"@angular/core": "17"}, "a": [{"b": 1}, {"b": 2}]}
    assert json_path(data, "$.dependencies['@angular/core']") == ["17"]
    assert json_path(data, "$.a[*].b") == [1, 2] and json_path(data, "$.a[1].b") == [2] and json_path(data, "$.missing.x") == []
    rule = RepoRule(id="r", files=["*.csproj"], parser="xml")
    file = ParsedFile(
        "a.csproj",
        '<Project xmlns="urn:x" Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>',
    )
    assert extract(file, rule, {"xpath": "//TargetFramework"}) == ["net8.0"]  # namespace-tolerant
    assert extract(file, rule, {"xpath": "/Project/@Sdk", "pattern": r"Sdk\.(\w+)$"}) == ["Web"]
    assert extract(file, rule, {"from_extension": {"csproj": "csharp"}}) == ["csharp"]
    assert condition(file, rule, {"xpath": "/Project[@Sdk]"}) and not condition(file, rule, {"not": {"xpath": "/Project[@Sdk]"}})
    assert extract(ParsedFile("x.xml", "<<<not xml"), rule, {"xpath": "//a"}) == []  # malformed files yield nothing


# -- pipeline evidence, component linkage and deploy targets (on the fake estate) ------------


def build(estate: Estate, name: str):  # type: ignore[no-untyped-def]
    return next(b for b in estate.builds if b.name == name)


def test_pipeline_evidence_and_stack(estate: Estate) -> None:
    orders = build(estate, "orders-ci")
    facts = {(d.fact, d.value): d for d in orders.detections}
    assert facts[("build_tool", "maven")].evidence == "task Maven@2"
    assert facts[("runtime_version", "1.17")].precedence == 3  # tool-installer input outranks manifests
    assert orders.stack == ["java/spring-boot/maven"]
    assert orders.plan.image == "maven:3.9-eclipse-temurin-17"
    legacy = build(estate, "legacy-app-build")
    assert "inline script in 'Legacy script'" in {d.evidence for d in legacy.detections if d.value == "nuget"}
    assert legacy.stack == ["csharp/aspnet-mvc/msbuild", "csharp/netframework/msbuild"]


def test_external_and_empty_repos_fall_back_to_pipeline_evidence(estate: Estate) -> None:
    unused = next(r for r in estate.repos if r.name == "unused-repo")
    assert unused.scan_status == "empty" and unused.components == []
    web = next(r for r in estate.repos if r.name == "web-api")
    assert web.scan_status == "scanned" and web.last_commit and web.size_kb == 4000


def test_deploy_targets(estate: Estate) -> None:
    web = build(estate, "web-api-ci")
    assert web.deploy_targets == ["Container registry", "Kubernetes / AKS"]
    registry = web.stages[0].targets[0]
    assert (registry.type, registry.identifier, registry.service_connection, registry.credential_type) == (
        "container_registry", "acr-prod", "acr-prod", "username / password",
    )  # fmt: skip
    kube = {t.identifier: t for t in web.stages[1].targets}
    assert kube["web"].strategy == "runOnce" and kube["web"].service_connection == "aks-prod"
    assert "environment 'web-prod'" in kube["web-prod"].evidence
    release = next(r for r in estate.releases if r.name == "web-release")
    prod = release.stages[2]
    assert [(t.type, t.identifier) for t in prod.targets] == [("iis_windows_vm", "IIS-Prod [web]"), ("iis_windows_vm", "LegacyWeb")]
    assert prod.config_transforms == ["IISWebAppDeploymentOnMachineGroup(XmlVariableSubstitution)"]
    assert build(estate, "orders-ci").deploy_targets == ["Package repository"]  # mvn deploy


@pytest.mark.parametrize("script_scan", [True, False])
def test_script_scan_toggle(make_config: ConfigFactory, script_scan: bool, fake, request) -> None:  # type: ignore[no-untyped-def]
    from azdo_analyzer.analyze.run import analyze
    from azdo_analyzer.collect.runner import collect

    from conftest import client_factory

    config = make_config(detection={"script_scan": script_scan})
    collect(config, client_factory=client_factory(fake, config))
    legacy = build(analyze(config), "legacy-app-build")
    assert any(d.evidence.startswith("inline script") for d in legacy.detections) is script_scan
