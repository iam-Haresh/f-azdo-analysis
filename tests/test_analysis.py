"""Activity, complexity, clustering, security, runner needs, template fit, waves (phases 6 and 8)."""

from __future__ import annotations

from typing import Any

from azdo_analyzer.analyze.clustering import fingerprint, jaccard
from azdo_analyzer.analyze.complexity import band_for
from azdo_analyzer.analyze.run import analyze
from azdo_analyzer.collect.runner import collect
from azdo_analyzer.model.entities import Estate

from conftest import ConfigFactory, client_factory
from fake_azdo import FakeAzdo


def analysed(make_config: ConfigFactory, fake: FakeAzdo, **sections: Any) -> Estate:
    config = make_config(**sections)
    collect(config, client_factory=client_factory(fake, config))
    return analyze(config)


def status(estate: Estate) -> dict[str, str]:
    return {p.name: p.stats.status for p in estate.pipelines()}


def named(estate: Estate, name: str):  # type: ignore[no-untyped-def]
    return next(p for p in estate.pipelines() if p.name == name)


def test_activity_classification(estate: Estate) -> None:
    assert status(estate) == {
        "web-api-ci": "active",
        "legacy-app-build": "failing",
        "cross-proj-build": "dormant",
        "never-run": "disabled",
        "orders-ci": "active",
        "tfvc-build": "stale",  # only known from the definition's latestBuild, outside the lookback window
        "web-release": "active",
        "git-release": "never_run",
        "broken-release": "orphaned",
    }
    legacy = named(estate, "legacy-app-build").stats
    assert legacy.statuses == ["active", "failing"]
    assert (legacy.runs_in_window, legacy.success_rate, legacy.low_success) == (6, 16.7, True)
    assert legacy.trigger_mix == {"ci": 1, "manual": 5} and legacy.distinct_branches == 2
    web = named(estate, "web-api-ci").stats
    assert (web.last_result, web.last_branch, web.median_duration_s) == ("succeeded", "main", 432.0)
    assert web.last_requester.startswith("user-")  # identities are hashed by default
    release = named(estate, "web-release")
    prod = release.stages[2].stats
    assert prod.last_status == "failed" and prod.last_success and prod.last_success < prod.last_deployment
    assert release.stats.last_prod_deployment == prod.last_success
    alpha, beta = (p for p in estate.projects if p.included)
    assert alpha.migration_candidate and beta.migration_candidate
    assert alpha.status_counts == {"active": 2, "disabled": 1, "dormant": 1, "failing": 1, "never_run": 1, "orphaned": 1}


def test_activity_thresholds_come_from_config(make_config: ConfigFactory, fake: FakeAzdo) -> None:
    estate = analysed(make_config, fake, activity={"active_within_days": 1, "dormant_within_days": 3, "failing_last_n_runs": 6})
    result = status(estate)
    assert result["orders-ci"] == "dormant"  # last run 2 days ago
    assert result["legacy-app-build"] == "dormant"  # no longer failing: the 6th run back succeeded
    assert result["web-release"] == "stale"
    assert not any(p.migration_candidate for p in estate.projects if p.name == "Beta")
    lenient = analysed(make_config, fake, activity={"low_success_rate_pct": 10})
    assert not named(lenient, "legacy-app-build").stats.low_success


def test_complexity_weights_and_bands_change_results(make_config: ConfigFactory, fake: FakeAzdo, estate: Estate) -> None:
    legacy = named(estate, "legacy-app-build").effort
    assert (legacy.score, legacy.band) == (15, "L")
    assert legacy.factors == [
        "classic definition (+2)",
        "2 unmapped task(s) (+6)",
        "2 marketplace/custom task(s) (+4)",
        "1 task group(s) (+1)",
        "Windows-only (+2)",
    ]
    release = named(estate, "web-release").effort
    assert {"deployment group (+3)", "gates (+2)", "cross-project dependency (+2)", "3 stage(s) (+3)"} <= set(release.factors)
    assert "TFVC source (+3)" in named(estate, "tfvc-build").effort.factors

    heavier = analysed(
        make_config, fake, complexity={"weights": {"per_unmapped_task": 10, "windows_only": 0}, "bands": {"S": 1, "M": 2, "L": 3}}
    )
    changed = named(heavier, "legacy-app-build").effort
    assert (changed.score, changed.band) == (27, "XL")
    assert "Windows-only" not in " ".join(changed.factors)
    assert band_for(5, {"S": 5, "M": 12}) == "S" and band_for(5.1, {"S": 5, "M": 12}) == "M" and band_for(13, {"S": 5, "M": 12}) == "XL"


def test_gitlab_plan_mapping_runner_and_templates(estate: Estate) -> None:
    legacy = named(estate, "legacy-app-build").plan
    assert legacy.unmapped_tasks == ["AcmeDeploy", "Old extension task"]
    assert legacy.runner_os == "windows" and legacy.tags == ["windows", "shell"] and legacy.image == ""
    assert "demand 'msbuild'" in legacy.runner_reasons and "task VSBuild" in legacy.runner_reasons
    assert any("netframework v4.8" in reason for reason in legacy.runner_reasons)
    assert legacy.templates == ["dotnet-framework"] and legacy.includes[0]["file"] == "dotnetfx/build.yml"
    assert "replacetokens" in legacy.uncovered_tasks and "VSBuild" not in legacy.uncovered_tasks
    assert legacy.stages == ["build", "test", "package", "deploy"]

    orders = named(estate, "orders-ci").plan
    assert (orders.runner_os, orders.tags, orders.templates, orders.uncovered_tasks) == ("linux", ["linux", "docker"], ["maven-app"], [])
    web = named(estate, "web-api-ci").plan
    assert web.image == "mcr.microsoft.com/dotnet/sdk:8.0" and web.gap_reason == "no template - gap" and web.stages == ["build", "prod"]
    release = named(estate, "web-release").plan
    assert release.stages == ["deploy-dev", "deploy-qa", "deploy-prod"]
    assert any("Deployment group 'IIS-Prod'" in note for note in release.notes)
    azure = next(c for c in estate.service_connections if c.name == "azure-prod")
    assert azure.gitlab_replacement.startswith("OIDC")


def test_image_registry_prefix_and_runner_tags_from_config(make_config: ConfigFactory, fake: FakeAzdo) -> None:
    estate = analysed(
        make_config,
        fake,
        gitlab={"image_registry_prefix": "registry.internal/ci/", "runner_tags": {"linux": ["k8s"], "windows": ["win"], "macos": ["mac"]}},
    )
    assert named(estate, "orders-ci").plan.image == "registry.internal/ci/maven:3.9-eclipse-temurin-17"
    assert named(estate, "orders-ci").plan.tags == ["k8s"] and named(estate, "legacy-app-build").plan.tags == ["win"]


def test_clustering(make_config: ConfigFactory, fake: FakeAzdo, estate: Estate) -> None:
    orders, cross = named(estate, "orders-ci"), named(estate, "cross-proj-build")
    ignore = {"cmdline", "publishbuildartifacts"}
    assert fingerprint(orders, ignore) == fingerprint(cross, ignore) == frozenset({"Maven"})
    assert jaccard(frozenset("ab"), frozenset("abc")) == 2 / 3 and jaccard(frozenset(), frozenset()) == 1.0
    (cluster,) = estate.clusters
    assert cluster.members == [cross.id, orders.id] and cluster.common_tasks == ["Maven"]
    assert cluster.stack == "build: java/spring-boot/maven" and orders.cluster_id == "C001"
    # With nothing ignored the task sets differ ({Maven} vs {Maven, CmdLine, PublishBuildArtifacts}): J = 1/3.
    strict = analysed(make_config, fake, clustering={"ignore_tasks": [], "similarity_threshold": 0.8})
    assert strict.clusters == []
    loose = analysed(make_config, fake, clustering={"ignore_tasks": [], "similarity_threshold": 0.3})
    assert loose.clusters[0].deviations[orders.id] == ["CmdLine", "PublishBuildArtifacts"]
    assert analysed(make_config, fake, clustering={"enabled": False}).clusters == []


def test_security_posture(make_config: ConfigFactory, fake: FakeAzdo, estate: Estate) -> None:
    orders = named(estate, "orders-ci").security
    assert orders.scanners == ["SonarQube"] and orders.baseline_gaps == ["secret_detection", "dependency_scanning"]
    assert named(estate, "web-api-ci").security.baseline_gaps == ["secret_detection", "dependency_scanning", "container_scanning"]
    kinds = {(f.kind, f.object_id.rsplit("/", 2)[-2] + "/" + f.object_id.rsplit("/", 1)[-1]) for f in estate.findings}
    assert ("plain_secret_variable", "build/2") in kinds  # DeployPassword + group DbPassword
    assert ("inline_credential", "build/2") in kinds and ("inline_credential", "build/1") in kinds
    assert ("task_not_installed", "build/2") in kinds
    assert ("prod_without_approval", "release/2") in kinds  # git-release deploys to Production unattended
    assert ("prod_without_approval", "release/1") not in kinds  # approval + gate
    assert ("prod_without_approval", "build/1") not in kinds  # environment web-prod has an approval check
    assert any(f.kind == "shared_service_connection" and "azure-prod" in f.message for f in estate.findings)
    relaxed = analysed(make_config, fake, gitlab={"security_baseline": ["dast"]}, security={"shared_connection_project_threshold": 10})
    assert named(relaxed, "orders-ci").security.baseline_gaps == ["dast"]
    assert not any(f.kind == "shared_service_connection" for f in relaxed.findings)


def test_migration_waves(make_config: ConfigFactory, fake: FakeAzdo, estate: Estate) -> None:
    waves = {w.name: [p.rsplit("/", 2)[-2] + "/" + p.rsplit("/", 1)[-1] for p in w.pipelines] for w in estate.waves}
    assert list(waves) == ["Wave 1", "Wave 3", "Review (dormant)", "Do not migrate (inactive)"]
    assert waves["Wave 1"] == ["build/1", "build/10"]
    # web-release is L, and its upstream legacy-app-build is L too: provider and consumer share wave 3
    assert waves["Wave 3"] == ["build/2", "release/1"]
    assert waves["Review (dormant)"] == ["build/3"]
    assert named(estate, "tfvc-build").wave == "Do not migrate (inactive)"
    moved = analysed(make_config, fake, waves={"band_wave": {"S": 2, "M": 2, "L": 9, "XL": 9}})
    assert named(moved, "orders-ci").wave == "Wave 2" and named(moved, "web-release").wave == "Wave 9"
