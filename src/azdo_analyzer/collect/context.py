"""Shared state for one source during ``collect``: client, cache, error table, worker pool."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, TypeVar

from ..client.http import AccessDenied, AzdoClient, AzdoHttpError, NotFound, ReadOnlyViolation
from ..config.models import Config, SourceConfig
from .cache import Cache

log = logging.getLogger(__name__)
T = TypeVar("T")


class CollectContext:
    def __init__(
        self,
        config: Config,
        source: SourceConfig,
        client: AzdoClient,
        cache: Cache,
        executor: ThreadPoolExecutor,
        *,
        refresh: bool = False,
    ) -> None:
        self.config = config
        self.source = source
        self.client = client
        self.cache = cache
        self.refresh = refresh
        self._executor = executor
        self._lock = threading.Lock()
        self.errors: list[dict[str, str]] = []

    @property
    def name(self) -> str:
        return self.source.name

    def error(self, project: str, collector: str, message: str) -> None:
        with self._lock:
            self.errors.append({"source": self.name, "project": project, "collector": collector, "message": message})
        log.warning("%s/%s [%s]: %s", self.name, project or "-", collector, message)

    @contextmanager
    def guard(self, project: str, collector: str) -> Iterator[None]:
        """Turn a failure into a row of the errors table so the run continues."""
        try:
            yield
        except ReadOnlyViolation:
            raise
        except AccessDenied as exc:
            self.error(project, collector, f"no access (HTTP {exc.status})")
        except NotFound:
            self.error(project, collector, "not available on this server (HTTP 404)")
        except AzdoHttpError as exc:
            self.error(project, collector, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad payload must not stop the run
            self.error(project, collector, f"{type(exc).__name__}: {exc}")

    def cached_list(self, project: str, resource: str, fetch: Callable[[], list[Any]], item_id: str | int = "list") -> list[Any]:
        """Return a list payload from the cache, fetching and storing it when stale."""
        path = self.cache.path(self.name, project, resource, item_id)
        if not self.cache.is_fresh(path, self.refresh):
            items = fetch()
            self.cache.write(path, {"count": len(items), "value": items})
            return items
        payload = self.cache.read(path)
        value = payload.get("value") if isinstance(payload, dict) else None
        return value if isinstance(value, list) else []

    def cached_item(self, project: str, resource: str, item_id: str | int, fetch: Callable[[], Any]) -> Any:
        path = self.cache.path(self.name, project, resource, item_id)
        if not self.cache.is_fresh(path, self.refresh):
            payload = fetch()
            self.cache.write(path, payload)
            return payload
        return self.cache.read(path)

    def pmap(self, fn: Callable[[T], None], items: Iterable[T]) -> None:
        """Run ``fn`` over items on the bounded worker pool (callers guard their own errors)."""
        for _ in self._executor.map(fn, list(items)):
            pass
