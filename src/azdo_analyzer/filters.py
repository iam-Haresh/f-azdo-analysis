"""Definition filters from the ``filters`` config section, shared by collect and normalize."""

from __future__ import annotations

from typing import Any

from .config.models import FiltersConfig, ProjectFilter
from .util import as_bool, as_str, matches_any


def project_included(filters: ProjectFilter, name: str) -> bool:
    return matches_any(filters.include, name) and not matches_any(filters.exclude, name)


def definition_included(filters: FiltersConfig, defn: dict[str, Any], *, release: bool = False) -> bool:
    name = as_str(defn.get("name"))
    folder = as_str(defn.get("path")) or "\\"
    if matches_any(filters.pipeline_name_exclude, name):
        return False
    if matches_any(filters.pipeline_folder_exclude, folder) or matches_any(
        filters.pipeline_folder_exclude, folder.rstrip("\\") + "\\" + name
    ):
        return False
    if release:
        if as_bool(defn.get("isDeleted")) and not filters.include_deleted_release_definitions:
            return False
        if as_bool(defn.get("isDisabled")) and not filters.include_disabled_pipelines:
            return False
        return True
    if as_str(defn.get("quality")).lower() == "draft" and not filters.include_draft_definitions:
        return False
    if as_str(defn.get("queueStatus")).lower() in ("disabled", "paused") and not filters.include_disabled_pipelines:
        return False
    return True
