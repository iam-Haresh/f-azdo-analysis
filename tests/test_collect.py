from __future__ import annotations

import json
from pathlib import Path

from azdo_analyzer.collect.cache import Cache
from azdo_analyzer.collect.doctor import format_table, run_doctor
from azdo_analyzer.collect.runner import CollectSummary, collect
from azdo_analyzer.config import Config

from conftest import ConfigFactory, client_factory
from fake_azdo import R_LEGACY, R_WEB, FakeAzdo

SRC = "DefaultCollection"


def cache_of(config: Config) -> Cache:
    return Cache(config.run.cache_dir)


def test_collect_fills_cache_from_mocked_api(config: Config, fake: FakeAzdo, collected: CollectSummary) -> None:
    cache = cache_of(config)
    assert [p["name"] for p in cache.get_list(SRC, "_collection", "projects")] == ["Alpha", "Beta", "Sandbox1", "Gamma"]
    assert len(cache.get_list(SRC, "_collection", "task_catalog")) > 10
    assert cache.item_ids(SRC, "Alpha", "build_definitions") == ["1", "2", "3", "5"]  # 4 is excluded by name filter
    assert cache.item_ids(SRC, "Alpha", "release_definitions") == ["1", "2", "3"]
    assert cache.get(SRC, "Alpha", "build_definitions", 2)["process"]["phases"][0]["steps"]  # raw payload, untouched
    assert len(cache.get_list(SRC, "Alpha", "repositories")) == 4
    assert cache.get(SRC, "Alpha", "repo_trees", R_WEB)["count"] > 10
    assert cache.get(SRC, "Alpha", "build_timelines", 1)["records"]
    assert not cache.path(SRC, "Alpha", "build_timelines", 2).exists()  # classic pipelines need no timeline
    assert not (Path(config.run.cache_dir) / SRC / "Sandbox1").exists()
    assert collected.projects == 2
    meta = json.loads(cache.source_file(SRC, "_meta.json").read_text())
    assert meta["api_version"] == "6.0"  # negotiated down from 7.1


def test_collect_is_get_only_and_never_stores_the_token(config: Config, fake: FakeAzdo, collected: CollectSummary) -> None:
    assert fake.non_get == []
    assert {r.method for r in fake.requests} == {"GET"}
    for file in Path(config.run.cache_dir).rglob("*"):
        if file.is_file():
            assert "test-token-not-real" not in file.read_text(encoding="utf-8")


def test_per_collector_403_lands_in_errors_and_run_continues(config: Config, collected: CollectSummary) -> None:
    errors = {(e["project"], e["collector"]): e["message"] for e in collected.errors}
    assert errors[("Beta", "service_connections")] == "no access (HTTP 403)"
    assert "deleting" in errors[("Gamma", "projects")]
    cache = cache_of(config)
    assert cache.item_ids(SRC, "Beta", "build_definitions") == ["10", "11"]
    stored = json.loads(cache.source_file(SRC, "_errors.json").read_text())
    assert {"source": SRC, "project": "Beta", "collector": "service_connections", "message": "no access (HTTP 403)"} in stored
    assert not collected.total_auth_failure


def test_collect_resumes_from_fresh_cache_and_refresh_forces(config: Config, fake: FakeAzdo, collected: CollectSummary) -> None:
    first = collected.api_calls
    again = collect(config, client_factory=client_factory(fake, config))
    # Only the version probe and the collector that failed (403, nothing cached) are repeated.
    assert again.api_calls < 6 < first
    forced = collect(config, client_factory=client_factory(fake, config), refresh=True)
    assert forced.api_calls == first


def test_collector_toggles_are_honoured(make_config: ConfigFactory, fake: FakeAzdo) -> None:
    config = make_config(
        collectors={
            "release_definitions": False,
            "release_runs": False,
            "task_catalog": False,
            "variable_groups": False,
            "build_runs": False,
            "service_hooks": False,
            "agent_capabilities": False,
            "repositories": False,
        }
    )
    collect(config, client_factory=client_factory(fake, config))
    paths = [str(r.url) for r in fake.requests]
    for fragment in (
        "release/",
        "distributedtask/tasks",
        "variablegroups",
        "build/builds",
        "hooks/",
        "/agents",
        "tfvc/items",
        "recursionLevel",
    ):
        assert not any(fragment in p for p in paths), fragment
    assert not any(r.url.path.endswith("_apis/git/repositories") for r in fake.requests)
    assert any("taskgroups" in p for p in paths)
    assert any("build/definitions/2" in p for p in paths)


def test_project_and_source_filters(config: Config, fake: FakeAzdo) -> None:
    summary = collect(config, client_factory=client_factory(fake, config), project_filter="Beta")
    assert summary.projects == 1
    assert not cache_of(config).item_ids(SRC, "Alpha", "build_definitions")
    assert collect(config, client_factory=client_factory(fake, config), source_filter="other").sources == 0


def test_total_auth_failure_is_reported(config: Config, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AZDO_TEST_PAT")
    summary = collect(config)
    assert summary.total_auth_failure
    assert summary.errors[0]["collector"] == "auth"


def test_repo_scan_fetches_only_manifests(config: Config, fake: FakeAzdo, collected: CollectSummary) -> None:
    fetched = [r.url.params["path"] for r in fake.requests if "git/repositories" in str(r.url) and "path" in r.url.params]
    assert "/src/Api/Api.csproj" in fetched and "/web/package.json" in fetched
    assert "/web/package-lock.json" not in fetched  # filename-only rule: no content needed
    assert "/steps/dotnet-build.yml" in fetched  # template from another repository
    legacy = [r for r in fake.requests if R_LEGACY in str(r.url) and r.url.params.get("recursionLevel") == "Full"]
    assert len(legacy) == 1


def test_doctor_reports_pass_fail_per_scope(config: Config, fake: FakeAzdo) -> None:
    results, healthy = run_doctor(config, client_factory(fake, config))
    by_check = {r.check: r for r in results}
    assert healthy
    assert by_check["Connectivity / api-version"].detail == "api-version 6.0"
    assert by_check["Build"].status == "pass"
    assert by_check["Secure Files"].status == "pass"  # needed the -preview retry
    assert by_check["Projects selected"].detail == "2 of 3"
    table = format_table(results)
    assert "PASS" in table and "Source" in table
    assert fake.non_get == []
