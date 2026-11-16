from __future__ import annotations

import httpx
import pytest
import respx

from azdo_analyzer.client.http import AccessDenied, AuthConfigError, AzdoClient, NotFound, ReadOnlyViolation
from azdo_analyzer.config import parse_config
from azdo_analyzer.config.models import Config

BASE = "https://tfs.test/tfs/Coll"


def make(api_version: str = "6.0", **source: object) -> tuple[AzdoClient, list[float], Config]:
    config = parse_config(
        {
            "run": {"requests_per_second": 100000, "retries": 3},
            "sources": [{"name": "c", "base_url": BASE, "api_version": api_version, "auth": {"token_env": "AZDO_TEST_PAT"}, **source}],
        }
    )
    sleeps: list[float] = []
    return AzdoClient(config.sources[0], config.run, sleep=sleeps.append), sleeps, config


def version_error(version: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "typeKey": "VssVersionOutOfRangeException",
            "message": f"The requested REST API version of {version} is out of range for this server.",
        },
    )


@respx.mock
def test_pat_is_sent_as_basic_auth_with_empty_user() -> None:
    route = respx.get(f"{BASE}/_apis/projects").mock(return_value=httpx.Response(200, json={"value": []}))
    client, _, _ = make()
    client.get_json("_apis/projects")
    assert route.calls.last.request.headers["authorization"] == "Basic OnRlc3QtdG9rZW4tbm90LXJlYWw="  # ":test-token-not-real"
    assert route.calls.last.request.url.params["api-version"] == "6.0"


def test_missing_token_and_unsupported_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZDO_TEST_PAT")
    with pytest.raises(AuthConfigError, match="AZDO_TEST_PAT"):
        make()
    monkeypatch.setenv("AZDO_TEST_PAT", "x")
    with pytest.raises(AuthConfigError, match="ntlm"):
        make(auth={"method": "ntlm", "token_env": "AZDO_TEST_PAT"})


@respx.mock
def test_paging_follows_continuation_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("continuationToken")
        if token is None:
            return httpx.Response(200, json={"value": [1, 2]}, headers={"x-ms-continuationtoken": "t1"})
        if token == "t1":
            return httpx.Response(200, json={"value": [3]}, headers={"x-ms-continuationtoken": "t2"})
        return httpx.Response(200, json={"value": [4]})

    respx.get(f"{BASE}/P/_apis/build/definitions").mock(side_effect=handler)
    client, _, _ = make()
    assert client.get_list("P/_apis/build/definitions") == [1, 2, 3, 4]
    assert client.calls == 3


@respx.mock
def test_paging_falls_back_to_top_and_skip_and_honours_max_items() -> None:
    data = list(range(5))

    def handler(request: httpx.Request) -> httpx.Response:
        top, skip = int(request.url.params["$top"]), int(request.url.params.get("$skip", 0))
        return httpx.Response(200, json={"value": data[skip : skip + top]})

    route = respx.get(f"{BASE}/_apis/projects").mock(side_effect=handler)
    client, _, _ = make()
    assert client.get_list("_apis/projects", page_size=2) == data
    assert route.call_count == 3
    assert client.get_list("_apis/projects", page_size=2, max_items=3) == [0, 1, 2]


@respx.mock
def test_retry_on_429_honours_retry_after_then_backs_off_on_5xx() -> None:
    route = respx.get(f"{BASE}/_apis/projects").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, json={"value": ["ok"]}),
        ]
    )
    client, sleeps, _ = make()
    assert client.get_list("_apis/projects") == ["ok"]
    assert route.call_count == 3
    assert sleeps == [7.0, 1.0]


@respx.mock
def test_retries_are_bounded() -> None:
    route = respx.get(f"{BASE}/_apis/projects").mock(return_value=httpx.Response(500, json={"message": "boom"}))
    client, _, _ = make()
    with pytest.raises(Exception, match="HTTP 500"):
        client.get_json("_apis/projects")
    assert route.call_count == 4  # first try + 3 retries


@respx.mock
def test_api_version_auto_steps_down_and_is_remembered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        version = request.url.params["api-version"]
        return httpx.Response(200, json={"value": []}) if version == "5.0" else version_error(version)

    route = respx.get(f"{BASE}/_apis/projects").mock(side_effect=handler)
    client, _, _ = make(api_version="auto")
    assert client.api_version == "5.0"
    assert [c.request.url.params["api-version"] for c in route.calls] == ["7.1", "7.0", "6.0", "5.0"]
    client.get_json("_apis/projects")
    assert route.calls.last.request.url.params["api-version"] == "5.0"
    assert route.call_count == 5


@respx.mock
def test_preview_only_resources_are_retried_with_the_preview_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "-preview" in request.url.params["api-version"]:
            return httpx.Response(200, json={"value": ["file"]})
        return httpx.Response(400, json={"typeKey": "VssInvalidPreviewVersionException", "message": "The -preview flag must be supplied"})

    route = respx.get(f"{BASE}/P/_apis/distributedtask/securefiles").mock(side_effect=handler)
    client, _, _ = make()
    assert client.get_list("P/_apis/distributedtask/securefiles") == ["file"]
    assert client.get_list("P/_apis/distributedtask/securefiles") == ["file"]
    assert route.call_count == 3  # the second call goes straight to the preview version


@respx.mock
def test_403_401_404_and_signin_pages_map_to_typed_errors() -> None:
    respx.get(f"{BASE}/a").mock(return_value=httpx.Response(403, json={"message": "no"}))
    respx.get(f"{BASE}/b").mock(return_value=httpx.Response(401))
    respx.get(f"{BASE}/c").mock(return_value=httpx.Response(404, json={"message": "gone"}))
    respx.get(f"{BASE}/d").mock(return_value=httpx.Response(203, html="<html>Sign in</html>"))
    client, _, _ = make()
    for path in ("a", "b", "d"):
        with pytest.raises(AccessDenied):
            client.get(path)
    with pytest.raises(NotFound):
        client.get("c")


@respx.mock
def test_client_is_read_only() -> None:
    """ONLY READ OPERATIONS: no write method exists and the transport hook blocks non-GET requests."""
    route = respx.route(url__startswith=BASE).mock(return_value=httpx.Response(200, json={}))
    client, _, _ = make()
    for method in ("post", "put", "patch", "delete", "request", "send"):
        assert not hasattr(client, method)
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        with pytest.raises(ReadOnlyViolation):
            client._http.request(method, f"{BASE}/_apis/projects")
    assert route.call_count == 0


@respx.mock
def test_services_use_dedicated_hosts_for_release_feeds_and_extensions() -> None:
    config = parse_config(
        {
            "sources": [
                {
                    "name": "o",
                    "type": "services",
                    "base_url": "https://dev.azure.com/org",
                    "api_version": "7.1",
                    "auth": {"token_env": "AZDO_TEST_PAT"},
                }
            ]
        }
    )
    client = AzdoClient(config.sources[0], config.run)
    assert client.url("P/_apis/release/definitions", "release") == "https://vsrm.dev.azure.com/org/P/_apis/release/definitions"
    assert client.url("_apis/packaging/feeds", "feeds") == "https://feeds.dev.azure.com/org/_apis/packaging/feeds"
    assert client.url("_apis/projects") == "https://dev.azure.com/org/_apis/projects"
    server, _, _ = make()
    assert server.url("P/_apis/release/definitions", "release") == f"{BASE}/P/_apis/release/definitions"
