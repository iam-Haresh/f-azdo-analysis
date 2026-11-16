"""Task catalog lookup and the unified ``Step`` shape (spec section 9.4)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..detect.rules import PipelineRules
from ..model.entities import Step, StepSource
from ..normalize.privacy import Privacy
from ..util import as_bool, as_dict, as_int, as_list, as_str, dig, var_refs

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_TASK_GROUP_DEPTH = 10


def is_guid(text: str) -> bool:
    return bool(_GUID.match(text))


@dataclass(frozen=True)
class TaskInfo:
    id: str
    name: str
    major: int | None
    author: str
    is_marketplace: bool
    is_custom: bool
    in_catalog: bool


def _major_of(version_spec: Any) -> int | None:
    if isinstance(version_spec, dict):
        return as_int(version_spec.get("major"))
    match = re.match(r"^(\d+)", as_str(version_spec))
    return int(match.group(1)) if match else None


class TaskCatalog:
    """Resolves task GUIDs / names to name, version, author and origin."""

    def __init__(self, raw_tasks: list[Any]) -> None:
        self._by_id: dict[str, dict[int, dict[str, Any]]] = {}
        self._by_name: dict[str, str] = {}
        for task in raw_tasks:
            if not isinstance(task, dict) or not task.get("id"):
                continue
            task_id = as_str(task["id"]).lower()
            major = as_int(dig(task, "version", "major")) or 0
            self._by_id.setdefault(task_id, {})[major] = task
            name = as_str(task.get("name"))
            if name:
                self._by_name.setdefault(name.lower(), task_id)
            contribution = as_str(task.get("contributionIdentifier"))
            if contribution and name:
                # YAML may reference extension tasks as publisher.extension.task
                prefix = ".".join(contribution.split(".")[:2])
                self._by_name.setdefault(f"{prefix}.{name}".lower(), task_id)

    def __bool__(self) -> bool:
        return bool(self._by_id)

    def entries(self) -> dict[str, dict[int, dict[str, Any]]]:
        return self._by_id

    @staticmethod
    def origin(task: dict[str, Any]) -> str:
        if as_bool(task.get("serverOwned")):
            return "builtin"
        contribution = as_str(task.get("contributionIdentifier"))
        if contribution:
            return "builtin" if contribution.lower().startswith("ms.") else "extension"
        return "custom"  # uploaded directly (tfx), no extension behind it

    def _info(self, task_id: str, major: int | None, fallback_name: str) -> TaskInfo:
        versions = self._by_id.get(task_id.lower())
        if not versions:
            return TaskInfo(task_id, fallback_name or task_id, major, "", False, False, not self._by_id)
        entry = versions.get(major) if major is not None and major in versions else versions[max(versions)]
        assert entry is not None
        origin = self.origin(entry)
        return TaskInfo(
            id=task_id.lower(),
            name=as_str(entry.get("name")) or fallback_name or task_id,
            major=major if major is not None else max(versions),
            author=as_str(entry.get("author")),
            is_marketplace=origin == "extension",
            is_custom=origin == "custom",
            in_catalog=True,
        )

    def resolve_id(self, task_id: str, version_spec: Any = None, fallback_name: str = "") -> TaskInfo:
        return self._info(task_id, _major_of(version_spec), fallback_name)

    def resolve_name(self, reference: str) -> TaskInfo:
        """YAML reference: ``Name@2``, ``publisher.ext.Name@2`` or a GUID."""
        name, _, version = reference.partition("@")
        major = _major_of(version) if version else None
        name = name.strip()
        if is_guid(name):
            return self._info(name, major, "")
        task_id = self._by_name.get(name.lower())
        if task_id is None and "." in name:
            task_id = self._by_name.get(name.rsplit(".", 1)[-1].lower())
        if task_id is None:
            short = name.rsplit(".", 1)[-1]
            # Without a catalog every task is unknown, which says nothing about its origin.
            return TaskInfo("", short, major, "", "." in name, False, not self._by_id)
        info = self._info(task_id, major, name)
        return info


class StepFactory:
    """Builds ``Step`` objects from classic build, release, YAML and timeline payloads."""

    def __init__(
        self,
        catalog: TaskCatalog,
        task_groups: dict[str, list[dict[str, Any]]],
        privacy: Privacy,
        rules: PipelineRules,
    ) -> None:
        self.catalog = catalog
        self._task_groups = task_groups  # guid -> versions (raw payloads)
        self._privacy = privacy
        self._script_inputs = {name.lower() for name in rules.script_inputs}

    # -- shared ------------------------------------------------------------------

    def _finish(self, step: Step, raw_inputs: dict[str, Any], extra_script: str = "") -> Step:
        inputs: dict[str, str] = {}
        scripts: list[str] = [extra_script] if extra_script else []
        for key, value in raw_inputs.items():
            text = as_str(value) if not isinstance(value, dict | list) else str(value)
            if key.lower() in self._script_inputs and text.strip():
                scripts.append(text)
            inputs[str(key)] = self._privacy.text(text)
        raw_script = "\n".join(scripts)
        haystack = "\n".join([raw_script, *[as_str(v) for v in raw_inputs.values()], step.condition])
        step.inputs = inputs
        step.script_text = self._privacy.text(raw_script)
        step.script_lines = len([line for line in raw_script.splitlines() if line.strip()])
        step.has_inline_credential = self._privacy.has_inline_credential(haystack)
        step.variables_referenced = var_refs(haystack)
        return step

    def _apply_info(self, step: Step, info: TaskInfo) -> None:
        step.task_id = info.id
        step.task_name = info.name
        step.task_major = info.major
        step.task_author = info.author
        step.is_marketplace = info.is_marketplace
        step.is_custom = info.is_custom
        step.in_catalog = info.in_catalog

    # -- classic build / release / task group -------------------------------------

    def _pick_group(self, group_id: str, version_spec: Any) -> dict[str, Any] | None:
        versions = self._task_groups.get(group_id.lower())
        if not versions:
            return None
        major = _major_of(version_spec)
        for candidate in versions:
            if major is not None and as_int(dig(candidate, "version", "major")) == major:
                return candidate
        stable = [v for v in versions if not as_bool(dig(v, "version", "isTest"))]
        return max(stable or versions, key=lambda v: as_int(dig(v, "version", "major")) or 0)

    def classic(self, raw: dict[str, Any], source: StepSource, _trail: tuple[str, ...] = ()) -> Step:
        """Classic build step (``task: {id, versionSpec}``) or release workflow task (``taskId``)."""
        task = as_dict(raw.get("task"))
        task_id = as_str(task.get("id") or raw.get("taskId")).lower()
        version_spec = task.get("versionSpec") or raw.get("version")
        definition_type = as_str(task.get("definitionType") or raw.get("definitionType")).lower()
        display = as_str(raw.get("displayName") or raw.get("name"))
        step = Step(
            display_name=display,
            enabled=as_bool(raw.get("enabled", True)),
            condition=as_str(raw.get("condition")),
            continue_on_error=as_bool(raw.get("continueOnError")),
            timeout=as_int(raw.get("timeoutInMinutes")) or None,
            source=source,
        )
        if as_bool(raw.get("alwaysRun")) and not step.condition:
            step.condition = "always()"

        group = self._pick_group(task_id, version_spec) if task_id else None
        if definition_type == "metatask" or (group is not None and task_id not in self.catalog.entries()):
            step.is_task_group = True
            step.task_id = task_id
            step.task_group_id = task_id
            step.task_major = _major_of(version_spec)
            step.task_name = as_str(group.get("name")) if group else (display or task_id)
            step.in_catalog = group is not None
            if group is not None and task_id not in _trail and len(_trail) < MAX_TASK_GROUP_DEPTH:
                step.children = [
                    self.classic(child, source, (*_trail, task_id)) for child in as_list(group.get("tasks")) if isinstance(child, dict)
                ]
        else:
            self._apply_info(step, self.catalog.resolve_id(task_id, version_spec, display))
        return self._finish(step, as_dict(raw.get("inputs")))

    # -- YAML --------------------------------------------------------------------

    _SHORTCUTS = {
        "script": "CmdLine",
        "bash": "Bash",
        "pwsh": "PowerShell",
        "powershell": "PowerShell",
        "checkout": "Checkout",
        "download": "DownloadPipelineArtifact",
        "downloadBuild": "DownloadBuildArtifacts",
        "publish": "PublishPipelineArtifact",
        "getPackage": "DownloadPackage",
        "reviewApp": "ReviewApp",
    }

    def yaml(self, raw: dict[str, Any]) -> Step:
        conditions = [as_str(c) for c in as_list(raw.get("_conditions"))]
        condition = as_str(raw.get("condition"))
        step = Step(
            display_name=as_str(raw.get("displayName") or raw.get("name")),
            enabled=as_bool(raw.get("enabled", True)),
            condition=" && ".join([*conditions, condition] if condition else conditions),
            continue_on_error=as_bool(raw.get("continueOnError")),
            timeout=as_int(raw.get("timeoutInMinutes")) or None,
            source="yaml",
            template=as_str(raw.get("_template")),
        )
        inputs = dict(as_dict(raw.get("inputs")))
        script = ""
        if "task" in raw:
            self._apply_info(step, self.catalog.resolve_name(as_str(raw["task"])))
        elif "template" in raw:  # could not be resolved
            step.task_name = "Template"
            step.in_catalog = True
            step.template = as_str(raw["template"])
            inputs = {"template": raw["template"], **as_dict(raw.get("parameters"))}
        else:
            for key, task_name in self._SHORTCUTS.items():
                if key in raw:
                    step.task_name = task_name
                    value = as_str(raw[key])
                    if key in ("script", "bash", "pwsh", "powershell"):
                        script = value
                        inputs.setdefault("workingDirectory", as_str(raw.get("workingDirectory")))
                    else:
                        inputs[key] = value
                        for option in ("clean", "fetchDepth", "lfs", "submodules", "persistCredentials", "artifact"):
                            if option in raw:
                                inputs[option] = raw[option]
                    break
            else:
                step.task_name = "Unknown"
                step.in_catalog = False
        if not step.display_name:
            step.display_name = step.task_name
        env = as_dict(raw.get("env"))
        if env:
            inputs["env"] = ", ".join(f"{k}={as_str(v)}" for k, v in env.items())
        return self._finish(step, {k: v for k, v in inputs.items() if v not in ("", None)}, script)

    # -- timeline ----------------------------------------------------------------

    def timeline(self, record: dict[str, Any]) -> Step | None:
        task = as_dict(record.get("task"))
        if not task.get("name") and not task.get("id"):
            return None
        info = self.catalog.resolve_id(as_str(task.get("id")), task.get("version"), as_str(task.get("name")))
        step = Step(display_name=as_str(record.get("name")), source="timeline")
        self._apply_info(step, info)
        if not info.in_catalog and task.get("name"):
            step.task_name = as_str(task["name"])
        return step
