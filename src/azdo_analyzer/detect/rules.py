"""Declarative rule files (``rules/*.yaml``) and their loaders.

Built-in rules live in ``rules/``; ``detection.extra_rules_dirs`` override them by id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

from ..config.models import Config, RepoScanConfig
from ..util import find_data_dir, path_matches


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoRule(_Rule):
    """One repository-evidence rule (section 10 B)."""

    id: str
    files: list[str]
    parser: Literal["xml", "json", "toml", "yaml", "properties", "regex", "filename"] = "filename"
    when: dict[str, Any] | None = None
    emit: dict[str, Any] = {}
    confidence: float = Field(0.8, ge=0, le=1)
    group: str = ""  # component group (dotnet, java, node, container, ...); empty = repo-level only
    dir_up: int = 0  # component directory is this many levels above the file
    precedence: Literal["pin", "manifest"] = "manifest"
    applies_to: str = ""  # pin rules: component group the fact is pushed down to
    flags: list[str] = []  # repo flags set when the rule matches
    multi: list[str] = []  # facts that accumulate instead of competing


class TaskRule(_Rule):
    """Pipeline evidence from a task (section 10 A)."""

    task: str  # exact name or re: pattern
    emit: dict[str, str] = {}
    version_inputs: dict[str, list[str]] = {}  # fact -> candidate input names
    when_input: dict[str, str] = {}  # input name -> regex that must match
    confidence: float = 0.8


class ScriptRule(_Rule):
    id: str
    regex: str
    emit: dict[str, str] = {}
    confidence: float = 0.6


class TargetRule(_Rule):
    """Deployment target signal (section 11)."""

    type: str
    label: str
    tasks: list[str] = []
    task_inputs: list[dict[str, str]] = []  # {task, input, regex}
    script_regex: list[str] = []
    connection_types: list[str] = []
    phase_types: list[str] = []
    environment_resource_types: list[str] = []
    identifier_inputs: list[str] = []
    strategy_inputs: dict[str, str] = {}  # input name -> strategy label when truthy


class ScannerRule(_Rule):
    name: str
    tasks: list[str] = []
    script_regex: list[str] = []
    covers: list[str] = []  # baseline items (sast, dependency_scanning, ...)
    gitlab_equivalent: str = ""


class ImageRule(_Rule):
    match: dict[str, str]
    image: str  # may contain {version}
    default_version: str = ""
    version_pattern: str = "(\\d+(?:\\.\\d+)?)"
    version_map: dict[str, str] = {}
    tools: list[str] = []


class RunnerRules(_Rule):
    windows_demands: list[str] = []
    macos_demands: list[str] = []
    linux_demands: list[str] = []
    windows_vm_images: list[str] = []
    macos_vm_images: list[str] = []
    windows_runtime_families: list[str] = []
    capability_keys: list[str] = []  # agent capabilities worth summarising
    default_image: str = "alpine:latest"
    images: list[ImageRule] = []


class DeployRules(_Rule):
    targets: list[TargetRule] = []
    config_transform_tasks: list[str] = []
    config_transform_inputs: list[str] = []
    strategy_patterns: dict[str, list[str]] = {}  # strategy -> task/script patterns
    credential_types: dict[str, str] = {}  # auth scheme -> credential label


class PipelineRules(_Rule):
    tasks: list[TaskRule] = []
    scripts: list[ScriptRule] = []
    path_inputs: list[str] = []  # inputs that point at a solution / project / working dir
    script_tasks: list[str] = []  # tasks whose inputs carry inline script text
    script_inputs: list[str] = []


class RuleSet(_Rule):
    repo: list[RepoRule] = []
    pipeline: PipelineRules = PipelineRules()
    deploy: DeployRules = DeployRules()
    scanners: list[ScannerRule] = []
    runner: RunnerRules = RunnerRules()
    app_groups: list[str] = []  # component groups that count as application stacks
    flag_groups: dict[str, list[str]] = {}  # repo flag -> component groups that set it
    baseline_requires: dict[str, str] = {}  # baseline item -> evidence needed (container | iac)


def _load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def load_rules(config: Config, base: Path | None = None) -> RuleSet:
    """Merge every YAML file in the rules dir, then let extra dirs override by rule id."""
    builtin = find_data_dir(config.detection.rules_dir, base)
    dirs = [builtin, *[Path(d) for d in config.detection.extra_rules_dirs]]
    merged: dict[str, Any] = {}
    repo_rules: dict[str, dict[str, Any]] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.y*ml")):
            data = _load_yaml(file)
            if isinstance(data, list):  # a bare list is a list of repo rules
                data = {"repo": data}
            if not isinstance(data, dict):
                continue
            for rule in data.pop("repo", None) or []:
                repo_rules[rule["id"]] = rule
            for key, value in data.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    for sub, sub_value in value.items():
                        existing = merged[key].get(sub)
                        if isinstance(sub_value, list) and isinstance(existing, list):
                            existing.extend(sub_value)
                        else:
                            merged[key][sub] = sub_value
                elif isinstance(value, list) and isinstance(merged.get(key), list):
                    merged[key].extend(value)
                else:
                    merged[key] = value
    merged["repo"] = list(repo_rules.values())
    return RuleSet.model_validate(merged)


def manifest_globs(rules: RuleSet) -> list[str]:
    """Globs of files whose content is needed (``filename`` rules only need the tree)."""
    return sorted({glob for rule in rules.repo if rule.parser != "filename" for glob in rule.files})


def select_manifests(paths: list[str], globs: list[str], scan: RepoScanConfig) -> tuple[list[str], bool]:
    """Pick the files worth fetching: rule matches outside ignored paths, shallowest first.

    Returns the selection and whether it was cut short by ``max_manifest_files_per_repo``.
    Collect and analyze both call this, so the offline stage asks for exactly the cached files.
    """
    matched = [p for p in paths if not path_matches(scan.ignore_paths, p) and path_matches(globs, p)]
    matched.sort(key=lambda p: (p.count("/"), p.lower()))
    limit = scan.max_manifest_files_per_repo
    return matched[:limit], len(matched) > limit
