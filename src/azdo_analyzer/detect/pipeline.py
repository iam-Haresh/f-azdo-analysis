"""Pipeline evidence (spec section 10 A) and pipeline -> component linkage."""

from __future__ import annotations

import posixpath
import re

from ..config.models import Config
from ..model.entities import BuildPipeline, Component, Detection, Estate, ReleasePipeline, Repo, pipeline_steps
from .match import _rx, task_matches
from .rules import RuleSet

_PREFIXES = re.compile(
    r"^(\$\((Build\.SourcesDirectory|System\.DefaultWorkingDirectory|Build\.Repository\.LocalPath|Pipeline\.Workspace)\)|\$\{\{[^}]*\}\})[\\/]*",
    re.IGNORECASE,
)


def detect_pipeline(pipeline: BuildPipeline | ReleasePipeline, rules: RuleSet, config: Config) -> None:
    found: dict[tuple[str, str, str], Detection] = {}

    def add(fact: str, value: str, confidence: float, evidence: str, precedence: int, rule_id: str, context: str = "") -> None:
        if confidence < config.detection.min_confidence or not value:
            return
        key = (fact, value, rule_id)
        if key not in found:
            found[key] = Detection(
                fact=fact,
                value=value,
                confidence=confidence,
                evidence=evidence,
                source="pipeline",
                rule_id=rule_id,
                context=context,
                precedence=precedence,
            )

    for step in pipeline_steps(pipeline):
        label = f"{step.task_name}@{step.task_major}" if step.task_major is not None else step.task_name
        inputs = {k.lower(): v for k, v in step.inputs.items()}
        for rule in rules.pipeline.tasks:
            if not task_matches(rule.task, step.task_name):
                continue
            if any(not _rx(rx).search(inputs.get(name.lower(), "")) for name, rx in rule.when_input.items()):
                continue
            for fact, value in rule.emit.items():
                add(fact, value, rule.confidence, f"task {label}", 1, f"task:{rule.task}")
            for fact, names in rule.version_inputs.items():
                for name in names:
                    value = inputs.get(name.lower(), "").strip()
                    if value and value.lower() not in ("default", "latest"):
                        # Tool-installer inputs are the strongest version evidence.
                        add(fact, value, 0.95, f"task {label} input {name}", 3, f"task:{rule.task}", rule.emit.get("runtime_family", ""))
                        break
        if config.detection.script_scan and step.script_text:
            for script_rule in rules.pipeline.scripts:
                if _rx(script_rule.regex).search(step.script_text):
                    for fact, value in script_rule.emit.items():
                        add(
                            fact,
                            value,
                            script_rule.confidence,
                            f"inline script in '{step.display_name or step.task_name}'",
                            1,
                            f"script:{script_rule.id}",
                        )
    pipeline.detections = sorted(found.values(), key=lambda d: (d.fact, -d.precedence, d.value, d.evidence))


def _clean_path(value: str, is_dir: bool = False) -> str:
    """Normalise a task input to a repo-relative directory hint ('' = repo root / unknown)."""
    value = value.strip().strip("'\"").replace("\\", "/")
    value = _PREFIXES.sub("", value)
    if not value or value.startswith(("$(", "$[", "${{", "http")):
        return ""
    value = re.sub(r"^(\./)+", "", value).lstrip("/")
    parts = []
    for part in value.split("/"):
        if any(ch in part for ch in "*?$"):
            break
        parts.append(part)
    if not parts:
        return ""
    cleaned = "/".join(parts)
    if not is_dir and len(parts) == len(value.split("/")) and "." in parts[-1]:
        cleaned = posixpath.dirname(cleaned)  # a file: use its directory
    return cleaned.strip("/")


def path_hints(pipeline: BuildPipeline, rules: RuleSet) -> list[str]:
    wanted = {name.lower() for name in rules.pipeline.path_inputs}
    hints: set[str] = set()
    for step in pipeline_steps(pipeline):
        for name, value in step.inputs.items():
            if name.lower() in wanted:
                is_dir = any(word in name.lower() for word in ("dir", "cwd", "context", "folder"))
                for part in re.split(r"[\n;]", value):
                    hint = _clean_path(part, is_dir)
                    if hint:
                        hints.add(hint)
    for trigger in pipeline.triggers:
        if trigger.type in ("ci", "pr"):
            for path in trigger.paths_include:
                hint = _clean_path(path, is_dir=True)
                if hint:
                    hints.add(hint)
    return sorted(hints)


def _related(component_path: str, hint: str) -> bool:
    path = "" if component_path == "." else component_path
    return path == hint or path.startswith(hint + "/") or hint.startswith(path + "/") if path else True


def link_components(pipeline: BuildPipeline, repo: Repo | None, rules: RuleSet) -> list[Component]:
    """Components a build pipeline works on: narrowed by path hints, then by pipeline evidence."""
    if repo is None or not repo.components:
        return []
    components = list(repo.components)
    hints = path_hints(pipeline, rules)
    if hints:
        narrowed = [c for c in components if c.path != "." and any(_related(c.path, h) for h in hints)]
        if narrowed:
            # Root-level components (Dockerfile, pipeline config) still belong to the build.
            components = narrowed + [c for c in components if c.path == "." and c.group not in rules.app_groups]
    evidence = {d.value.lower() for d in pipeline.detections if d.fact in ("build_tool", "language", "runtime_family")}
    app = [c for c in components if c.group in rules.app_groups]
    if evidence and app:
        matching = [c for c in app if {c.build_tool.lower(), c.language.lower(), c.runtime_family.lower()} & evidence]
        if matching:
            keep = {c.id for c in matching}
            components = [c for c in components if c.group not in rules.app_groups or c.id in keep]
    return components


def _label(component: Component) -> str:
    return "/".join(x for x in (component.language, component.framework or component.runtime_family, component.build_tool) if x)


def detect_pipelines(estate: Estate, rules: RuleSet, config: Config) -> None:
    repos = {r.id: r for r in estate.repos}
    builds = {b.id: b for b in estate.builds}
    for build in estate.builds:
        detect_pipeline(build, rules, config)
        components = link_components(build, repos.get(build.repo_id), rules)
        build.component_ids = [c.id for c in components]
        labels = sorted({_label(c) for c in components if c.group in rules.app_groups and _label(c)})
        if not labels:
            best: dict[str, str] = {}
            for detection in build.detections:
                if detection.fact in ("language", "runtime_family", "build_tool"):
                    best.setdefault(detection.fact, detection.value)
            label = "/".join(best[k] for k in ("language", "runtime_family", "build_tool") if k in best)
            labels = [label] if label else []
        build.stack = labels
        build.stack_signature = " + ".join(labels) if labels else "unknown"

    for release in estate.releases:
        detect_pipeline(release, rules, config)
        labels = sorted({label for b in release.build_ids if b in builds for label in builds[b].stack})
        release.stack = labels
        release.stack_signature = " + ".join(labels) if labels else "unknown"
