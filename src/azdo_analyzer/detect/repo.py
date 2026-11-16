"""Repository evidence -> components and repo flags (spec section 10 B)."""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field

from ..collect.cache import Cache
from ..collect.collectors import scan_branch, tfvc_relative
from ..collect.files import RepoFiles
from ..config.models import Config
from ..model.entities import Component, Detection, Estate, Repo
from ..parse.yaml_loader import RepoRef
from ..util import as_dict, as_list, as_str, path_matches
from .engine import ParsedFile, condition, extract
from .rules import RepoRule, RuleSet, manifest_globs, select_manifests

SINGLE_FACTS = (
    "language",
    "runtime_family",
    "runtime_version",
    "framework",
    "framework_version",
    "build_tool",
    "build_tool_version",
    "package_manager",
    "packaging",
    "project_kind",
)
PRECEDENCE = {"pin": 2, "manifest": 1}


@dataclass
class RepoScan:
    components: list[Component] = field(default_factory=list)
    detections: list[Detection] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)


def _component_dir(path: str, levels: int) -> str:
    directory = posixpath.dirname(path)
    for _ in range(levels):
        directory = posixpath.dirname(directory)
    return directory


def _under(path: str, directory: str) -> bool:
    return not directory or path == directory or path.startswith(directory + "/")


def _extend(target: list[Detection], new: list[Detection]) -> None:
    """Add detections, keeping one per (rule, fact, value): fifty .tf files are one fact."""
    seen = {(d.rule_id, d.fact, d.value) for d in target}
    for detection in new:
        key = (detection.rule_id, detection.fact, detection.value)
        if key not in seen:
            seen.add(key)
            target.append(detection)


def _best(detections: list[Detection]) -> Detection:
    return max(detections, key=lambda d: (d.precedence, d.confidence, d.value))


def _resolve(component: Component, multi: set[str]) -> None:
    by_fact: dict[str, list[Detection]] = {}
    for detection in component.detections:
        by_fact.setdefault(detection.fact, []).append(detection)
    for fact, found in sorted(by_fact.items()):
        if fact in multi or fact == "test_tooling":
            values = sorted({d.value for d in found})
            if fact == "test_tooling":
                component.test_tooling = values
            else:
                component.facts[fact] = ", ".join(values)
            continue
        best = _best(found)
        rivals = sorted({d.value for d in found if d.precedence == best.precedence and d.value.lower() != best.value.lower()})
        if rivals and fact in ("runtime_version", "build_tool_version", "framework_version"):
            # Sources disagree: report it instead of silently picking one.
            component.conflicts.append(f"{fact}: {best.value} vs {', '.join(rivals)}")
        if fact in SINGLE_FACTS:
            setattr(component, fact, best.value)
        else:
            component.facts[fact] = best.value
    if component.detections:
        component.confidence = round(max(d.confidence for d in component.detections), 2)


def detect_components(
    repo_id: str,
    paths: list[str],
    read: Callable[[str], str | None],
    rules: RuleSet,
    config: Config,
) -> RepoScan:
    """Run every repo rule over a file tree. ``read`` returns file content or ``None``."""
    scan_config = config.repo_scan
    visible = [p for p in paths if not path_matches(scan_config.ignore_paths, p)]
    content_paths, _ = select_manifests(paths, manifest_globs(rules), scan_config)
    files: dict[str, ParsedFile | None] = {}

    def load(path: str) -> ParsedFile | None:
        if path not in files:
            text = read(path)
            files[path] = ParsedFile(path, text) if text is not None else None
        return files[path]

    result = RepoScan()
    components: dict[tuple[str, str], Component] = {}
    pins: list[tuple[str, str, list[Detection]]] = []
    multi = {fact for rule in rules.repo for fact in rule.multi}
    min_confidence = config.detection.min_confidence

    def apply(rule: RepoRule, path: str, file: ParsedFile) -> None:
        if not condition(file, rule, rule.when):
            return
        detections = []
        for fact, spec in rule.emit.items():
            for value in extract(file, rule, spec):
                detections.append(
                    Detection(
                        fact=fact,
                        value=value,
                        confidence=rule.confidence,
                        evidence=f"{path} [{rule.id}]",
                        source="repo",
                        rule_id=rule.id,
                        precedence=PRECEDENCE[rule.precedence],
                    )
                )
        for flag in rule.flags:
            if flag not in result.flags:
                result.flags.append(flag)
        directory = _component_dir(path, rule.dir_up)
        if rule.group:
            key = (directory, rule.group)
            if key not in components:
                components[key] = Component(
                    id=f"{repo_id}#{rule.group}:{directory or '.'}", repo_id=repo_id, path=directory or ".", group=rule.group
                )
            _extend(components[key].detections, detections)
        elif rule.applies_to:
            pins.append((directory, rule.applies_to, detections))
        else:
            _extend(result.detections, detections)

    for rule in rules.repo:
        if rule.confidence < min_confidence:
            continue
        candidates = visible if rule.parser == "filename" else content_paths
        for path in candidates:
            if not path_matches(rule.files, path):
                continue
            if rule.parser == "filename":
                apply(rule, path, ParsedFile(path, ""))
            else:
                file = load(path)
                if file is not None:
                    apply(rule, path, file)

    for directory, group, detections in pins:
        for (comp_dir, comp_group), component in components.items():
            if comp_group == group and _under(comp_dir, directory):
                _extend(component.detections, detections)

    containers = [directory for (directory, group) in components if group == "container"]
    for (directory, group), component in sorted(components.items()):
        component.detections.sort(key=lambda d: (d.fact, -d.precedence, -d.confidence, d.value, d.evidence))
        _resolve(component, multi)
        component.container_build = group == "container" or any(_under(directory, c) for c in containers)
        result.components.append(component)

    app = [c for c in result.components if c.group in rules.app_groups]
    if len({c.path for c in app}) > 1:
        result.flags.append("monorepo")
    if len({c.group for c in app}) > 1:
        result.flags.append("polyglot")
    groups = {c.group for c in result.components}
    for flag, flag_groups in rules.flag_groups.items():
        if groups & set(flag_groups) and flag not in result.flags:
            result.flags.append(flag)
    result.flags.sort()

    by_fact: dict[str, list[Detection]] = {}
    for detection in result.detections:
        by_fact.setdefault(detection.fact, []).append(detection)
    result.facts = {fact: ", ".join(sorted({d.value for d in found})) for fact, found in sorted(by_fact.items())}
    result.detections.sort(key=lambda d: (d.fact, d.value, d.evidence))
    return result


def detect_repos(estate: Estate, config: Config, cache: Cache, rules: RuleSet) -> None:
    """Scan every repository whose tree is in the cache (offline)."""
    if not config.repo_scan.enabled:
        return
    for repo in estate.repos:
        if repo.type == "external" or repo.disabled:
            continue
        payload = as_dict(cache.get(repo.source, repo.project, "repo_trees", repo.azdo_id))
        if not payload:
            continue
        files = RepoFiles(cache, repo.source)
        if repo.type == "tfvc":
            ref = RepoRef(repo.project, "tfvc", repo.name, "tfvc", kind="tfvc")
            full = {
                tfvc_relative(as_str(i.get("path")), repo.project): as_str(i.get("path"))
                for i in as_list(payload.get("value"))
                if isinstance(i, dict) and not i.get("isFolder")
            }
            paths = sorted(full)

            def read(path: str, ref: RepoRef = ref, full: dict[str, str] = full, files: RepoFiles = files) -> str | None:
                return files.read(ref, full[path])
        else:
            branch = scan_branch(config.repo_scan.branch, repo.default_branch)
            ref = RepoRef(repo.project, repo.azdo_id, repo.name, branch)
            paths = sorted(
                as_str(i.get("path")).lstrip("/")
                for i in as_list(payload.get("value"))
                if isinstance(i, dict) and not i.get("isFolder") and as_str(i.get("gitObjectType") or "blob") == "blob"
            )

            def read(path: str, ref: RepoRef = ref, files: RepoFiles = files) -> str | None:  # type: ignore[misc]
                return files.read(ref, path)

        _apply_scan(repo, detect_components(repo.id, paths, read, rules, config))
        _, cut = select_manifests(paths, manifest_globs(rules), config.repo_scan)
        partial = bool(payload.get("_truncated")) or cut
        repo.scan_status = "partial" if partial else "scanned"
        if partial and "partial_scan" not in repo.flags:
            repo.flags.append("partial_scan")
        repo.flags.sort()


def _apply_scan(repo: Repo, scan: RepoScan) -> None:
    repo.components = scan.components
    repo.detections = scan.detections
    repo.facts = scan.facts
    repo.flags = sorted({*repo.flags, *scan.flags})
