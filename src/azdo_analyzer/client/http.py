"""Read-only Azure DevOps REST client.

ONLY READ OPERATIONS: this client exposes GET and nothing else. A request hook on the
underlying transport raises ``ReadOnlyViolation`` for any other HTTP method, and there
is no configuration switch to relax that.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..config.models import RunConfig, SourceConfig

log = logging.getLogger(__name__)

API_VERSIONS = ["7.1", "7.0", "6.0", "5.0", "4.1"]

# Azure DevOps Services serves some areas from dedicated hosts.
_SERVICES_HOSTS = {"release": "vsrm.dev.azure.com", "extmgmt": "extmgmt.dev.azure.com", "feeds": "feeds.dev.azure.com"}

_VERSION_ERROR_MARKERS = (
    "vssversionoutofrangeexception",
    "is out of range for this server",
    "vssversionnotspecifiedexception",
    "the latest rest api version this server supports",
    "invalid api version",
)
_PREVIEW_ERROR_MARKERS = ("vssinvalidpreviewversionexception", "-preview flag must be supplied", "is under preview")


class ReadOnlyViolation(RuntimeError):
    """Raised when anything tries to send a non-GET request."""


class AzdoHttpError(Exception):
    def __init__(self, status: int, url: str, message: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {message}")
        self.status = status
        self.url = url
        self.message = message


class AccessDenied(AzdoHttpError):
    """401/403 (or a sign-in redirect): recorded as 'no access' by collectors."""


class NotFound(AzdoHttpError):
    """404: resource or endpoint does not exist on this server version."""


class AuthConfigError(Exception):
    """Missing token or unsupported auth method."""


def _enforce_read_only(request: httpx.Request) -> None:
    if request.method.upper() != "GET":
        raise ReadOnlyViolation(f"Blocked {request.method} {request.url}: this tool is read-only (HTTP GET only)")


class RateLimiter:
    """Spaces requests evenly to stay under ``requests_per_second`` across threads."""

    def __init__(self, per_second: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval = 1.0 / per_second
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            self._sleep(delay)


def quote_segment(value: str) -> str:
    return quote(value, safe="")


class AzdoClient:
    """GET-only client with auth, throttling, retry, paging and api-version negotiation."""

    def __init__(
        self,
        source: SourceConfig,
        run: RunConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.source = source
        self._retries = run.retries
        self._sleep = sleep
        self._limiter = RateLimiter(run.requests_per_second, sleep)
        self._lock = threading.Lock()
        self._version_lock = threading.Lock()
        self._api_version: str | None = None if source.api_version == "auto" else source.api_version
        self._preview_paths: set[str] = set()
        self.calls = 0

        if source.auth.method != "pat":
            raise AuthConfigError(
                f"source {source.name}: auth method '{source.auth.method}' is not available in this build (PAT only; see docs/decisions.md)"
            )
        token = os.environ.get(source.auth.token_env, "")
        if not token:
            raise AuthConfigError(f"source {source.name}: environment variable {source.auth.token_env} is not set (PAT required)")
        basic = base64.b64encode(f":{token}".encode()).decode()

        verify: bool | str = source.tls.verify
        if source.tls.verify and source.tls.ca_bundle:
            verify = source.tls.ca_bundle
        kwargs: dict[str, Any] = {}
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = verify
            if source.proxy:
                kwargs["proxy"] = source.proxy
        self._http = httpx.Client(
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
            timeout=run.timeout_seconds,
            follow_redirects=False,
            event_hooks={"request": [_enforce_read_only]},
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    # -- URL helpers -------------------------------------------------------------

    def _base(self, area: str) -> str:
        base = self.source.base_url
        if self.source.type == "services" and area in _SERVICES_HOSTS:
            parts = urlsplit(base)
            if parts.netloc.lower() == "dev.azure.com":
                return urlunsplit((parts.scheme, _SERVICES_HOSTS[area], parts.path, "", ""))
        return base

    def url(self, path: str, area: str = "core") -> str:
        return f"{self._base(area)}/{path.lstrip('/')}"

    @staticmethod
    def _preview_key(path: str) -> str:
        idx = path.find("_apis/")
        segments = path[idx:].split("/") if idx >= 0 else path.split("/")
        return "/".join(segments[:3]).lower()

    # -- api version -------------------------------------------------------------

    @property
    def api_version(self) -> str:
        if self._api_version is None:
            with self._version_lock:
                if self._api_version is None:
                    self._api_version = self._negotiate()
        return self._api_version

    def _negotiate(self) -> str:
        """Try the highest known version and step down until the server accepts one."""
        last: AzdoHttpError | None = None
        for version in API_VERSIONS:
            try:
                self._send(self.url("_apis/projects"), {"$top": "1", "api-version": version})
            except (AccessDenied, NotFound):
                raise
            except AzdoHttpError as exc:
                if exc.status == 400 and _is_version_error(exc.message):
                    log.debug("api-version %s rejected by %s, stepping down", version, self.source.name)
                    last = exc
                    continue
                raise
            log.info("source %s: negotiated api-version %s", self.source.name, version)
            return version
        raise last or AzdoHttpError(400, self.source.base_url, "no supported api-version found")

    # -- requests ----------------------------------------------------------------

    def _send(self, url: str, params: Mapping[str, str]) -> httpx.Response:
        attempt = 0
        while True:
            self._limiter.wait()
            with self._lock:
                self.calls += 1
            try:
                response = self._http.get(url, params=dict(params))
            except ReadOnlyViolation:
                raise
            except httpx.TransportError as exc:
                if attempt >= self._retries:
                    raise AzdoHttpError(0, url, f"transport error: {exc}") from exc
                self._sleep(_backoff(attempt, None))
                attempt += 1
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                if attempt >= self._retries:
                    raise AzdoHttpError(status, url, _error_text(response))
                self._sleep(_backoff(attempt, response.headers.get("Retry-After")))
                attempt += 1
                continue
            content_type = response.headers.get("content-type", "")
            if status in (401, 403) or status in (203, 302) or (status == 200 and "text/html" in content_type):
                # 203/302/HTML are sign-in pages: the token was not accepted.
                raise AccessDenied(status, url, _error_text(response) or "access denied")
            if status == 404:
                raise NotFound(status, url, _error_text(response) or "not found")
            if status >= 400:
                raise AzdoHttpError(status, url, _error_text(response))
            return response

    def get(self, path: str, params: Mapping[str, Any] | None = None, *, area: str = "core") -> httpx.Response:
        """GET ``{base_url}/{path}`` with api-version handling. The only request method."""
        url = self.url(path, area)
        query = {k: _param(v) for k, v in (params or {}).items() if v is not None}
        key = self._preview_key(path)
        version = self.api_version
        query["api-version"] = f"{version}-preview" if key in self._preview_paths else version
        try:
            return self._send(url, query)
        except AzdoHttpError as exc:
            if exc.status == 400 and _is_preview_error(exc.message) and key not in self._preview_paths:
                self._preview_paths.add(key)
                query["api-version"] = f"{version}-preview"
                return self._send(url, query)
            raise

    def get_json(self, path: str, params: Mapping[str, Any] | None = None, *, area: str = "core") -> Any:
        return self.get(path, params, area=area).json()

    def get_list(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        area: str = "core",
        page_size: int | None = None,
        max_items: int | None = None,
        list_key: str = "value",
    ) -> list[Any]:
        """Collect every page: follow ``x-ms-continuationtoken``, else ``$top``/``$skip``."""
        query: dict[str, Any] = dict(params or {})
        if page_size is not None:
            query["$top"] = page_size
        items: list[Any] = []
        skip = 0
        seen_tokens: set[str] = set()
        while True:
            response = self.get(path, query, area=area)
            body = response.json()
            page = body.get(list_key, []) if isinstance(body, dict) else body
            if not isinstance(page, list):
                page = []
            items.extend(page)
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            token = response.headers.get("x-ms-continuationtoken")
            if token:
                if token in seen_tokens:
                    break
                seen_tokens.add(token)
                query["continuationToken"] = token
                continue
            if page_size is not None and len(page) >= page_size and "continuationToken" not in query:
                skip += len(page)
                query["$skip"] = skip
                continue
            break
        return items


def _param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 300.0))
        except ValueError:
            pass
    return float(min(0.5 * (2**attempt), 30.0))


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        return f"{body.get('typeKey', '')} {body.get('message', '')}".strip() or response.text[:300]
    return response.text[:300]


def _is_version_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _VERSION_ERROR_MARKERS)


def _is_preview_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _PREVIEW_ERROR_MARKERS)
