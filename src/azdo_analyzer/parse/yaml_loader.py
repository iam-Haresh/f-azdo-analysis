"""Tolerant YAML loading and static template resolution for Azure Pipelines YAML.

Azure Pipelines allows repeated ``${{ if ... }}:`` keys in one mapping, which strict
parsers reject (and lenient ones silently drop). The loader composes the node tree and
converts it itself, keeping every duplicate key under a unique suffix.

Expansion is static and read-only: the pipeline preview API is a POST and is never used.
"""

from __future__ import annotations

import copy
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ..util import as_dict, as_list, as_str

DUP_MARK = "\x00dup"
_PARAM_EXACT = re.compile(r"^\$\{\{\s*parameters(?:\.(\w+)|\[['\"]([^'\"]+)['\"]\])\s*\}\}$")
_PARAM_EMBEDDED = re.compile(r"\$\{\{\s*parameters(?:\.(\w+)|\[['\"]([^'\"]+)['\"]\])\s*\}\}")


class YamlParseError(Exception):
    pass


def is_expression_key(key: Any) -> bool:
    return isinstance(key, str) and key.lstrip().startswith("${{")


def clean_key(key: str) -> str:
    return key.split(DUP_MARK, 1)[0]


def _scalar(node: ScalarNode) -> Any:
    value = node.value
    tag = node.tag or ""
    if node.style in ("'", '"', "|", ">"):
        return value
    if tag.endswith(":null"):
        return None
    if tag.endswith(":bool"):
        return value.lower() in ("true", "yes", "on")
    if tag.endswith(":int"):
        try:
            return int(value.replace("_", ""), 0)
        except ValueError:
            return value
    if tag.endswith(":float"):
        return value  # keep versions such as 3.10 verbatim
    return value


def _convert(node: Node, seen: dict[int, Any]) -> Any:
    if id(node) in seen:  # alias
        return copy.deepcopy(seen[id(node)])
    result: Any
    if isinstance(node, ScalarNode):
        result = _scalar(node)
    elif isinstance(node, SequenceNode):
        result = [_convert(child, seen) for child in node.value]
    elif isinstance(node, MappingNode):
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = as_str(_convert(key_node, seen)) if isinstance(key_node, ScalarNode) else str(key_node.value)
            unique, n = key, 1
            while unique in mapping:
                n += 1
                unique = f"{key}{DUP_MARK}{n}"
            mapping[unique] = _convert(value_node, seen)
        result = mapping
    else:  # pragma: no cover - no other node kinds exist
        result = None
    seen[id(node)] = result
    return result


def load_yaml(text: str) -> Any:
    """Parse YAML, keeping duplicate keys and ``${{ }}`` expressions as plain strings."""
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = True
    try:
        node = yaml.compose(text.lstrip("﻿"))
    except YAMLError as exc:
        raise YamlParseError(str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML") from exc
    except Exception as exc:  # ruamel raises assorted errors on malformed input
        raise YamlParseError(f"{type(exc).__name__}: {exc}") from exc
    return None if node is None else _convert(node, {})


# -- template resolution -----------------------------------------------------------


@dataclass(frozen=True)
class RepoRef:
    project: str
    repo_id: str
    name: str
    branch: str
    kind: str = "git"  # git | tfvc

    @property
    def label(self) -> str:
        return f"{self.project}/{self.name}"


class FileProvider(Protocol):
    def read(self, repo: RepoRef, path: str) -> str | None: ...

    def find_repo(self, project: str, name: str, ref: str | None) -> RepoRef | None: ...


@dataclass
class ResolvedPipeline:
    doc: dict[str, Any] = field(default_factory=dict)
    templates: list[str] = field(default_factory=list)  # "project/repo:path"
    cross_repos: list[RepoRef] = field(default_factory=list)
    external_repos: list[str] = field(default_factory=list)  # resources we cannot read (GitHub ...)
    unresolved: list[str] = field(default_factory=list)
    missing: bool = False
    error: str = ""


@dataclass(frozen=True)
class _Frame:
    repo: RepoRef
    path: str
    depth: int


def _flatten_expressions(mapping: dict[str, Any]) -> dict[str, Any]:
    """Merge ``${{ if }}:`` / ``${{ each }}:`` mapping bodies into their parent (all branches kept)."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if is_expression_key(key):
            if isinstance(value, dict):
                for sub_key, sub_value in _flatten_expressions(value).items():
                    out.setdefault(sub_key, sub_value)
        else:
            out[clean_key(key)] = value
    return out


def _param_defaults(doc: dict[str, Any]) -> dict[str, Any]:
    params = doc.get("parameters")
    defaults: dict[str, Any] = {}
    if isinstance(params, dict):
        defaults.update({clean_key(k): v for k, v in params.items()})
    for entry in as_list(params) if isinstance(params, list) else []:
        if isinstance(entry, dict) and "name" in entry:
            defaults[as_str(entry["name"])] = entry.get("default")
    return defaults


def _substitute(node: Any, params: dict[str, Any]) -> Any:
    """Replace ``${{ parameters.x }}`` where the value is known; leave everything else verbatim."""
    if isinstance(node, str):
        exact = _PARAM_EXACT.match(node.strip())
        if exact:
            name = exact.group(1) or exact.group(2)
            if params.get(name) is not None:
                return copy.deepcopy(params[name])
            return node

        def repl(match: re.Match[str]) -> str:
            value = params.get(match.group(1) or match.group(2))
            if isinstance(value, str | int | float | bool):
                return as_str(value)
            return match.group(0)

        return _PARAM_EMBEDDED.sub(repl, node)
    if isinstance(node, list):
        out: list[Any] = []
        for item in node:
            replaced = _substitute(item, params)
            if isinstance(item, str) and isinstance(replaced, list):
                out.extend(replaced)  # stepList / jobList parameter spliced in place
            else:
                out.append(replaced)
        return out
    if isinstance(node, dict):
        return {key: _substitute(value, params) for key, value in node.items()}
    return node


_NESTED = {"stages": "jobs", "jobs": "steps"}
_HOOKS = ("preDeploy", "deploy", "routeTraffic", "postRouteTraffic")


class TemplateResolver:
    """Loads a pipeline file and inlines ``template:`` / ``extends:`` references."""

    def __init__(self, files: FileProvider, *, resolve_templates: bool = True, max_depth: int = 10) -> None:
        self._files = files
        self._resolve = resolve_templates
        self._max_depth = max_depth
        self._result = ResolvedPipeline()
        self._root: RepoRef | None = None
        self._aliases: dict[str, RepoRef] = {}

    def resolve(self, repo: RepoRef, path: str) -> ResolvedPipeline:
        self._root = repo
        text = self._files.read(repo, path)
        if text is None:
            self._result.missing = True
            return self._result
        try:
            doc = load_yaml(text)
        except YamlParseError as exc:
            self._result.error = str(exc)
            return self._result
        if not isinstance(doc, dict):
            self._result.error = "pipeline file is not a mapping"
            return self._result
        doc = _flatten_expressions(doc)
        frame = _Frame(repo, path.lstrip("/"), 0)
        self._register_resources(doc, repo)

        guard = 0
        while "extends" in doc and guard <= self._max_depth:
            guard += 1
            extends = as_dict(doc.pop("extends"))
            loaded = self._load(as_str(extends.get("template")), frame)
            if loaded is None:
                break
            template, frame = loaded
            self._register_resources(template, frame.repo)
            params = {**_param_defaults(template), **as_dict(extends.get("parameters"))}
            body = as_dict(_substitute({k: v for k, v in template.items() if k != "parameters"}, params))
            for key, value in body.items():
                if key in ("stages", "jobs", "steps", "extends") or key not in doc:
                    doc[key] = value

        for kind in ("stages", "jobs", "steps"):
            if kind in doc:
                doc[kind] = self._expand(doc[kind], kind, frame)
        if "variables" in doc:
            doc["variables"] = self._expand_variables(doc["variables"], frame)
        self._result.doc = doc
        return self._result

    # -- helpers -----------------------------------------------------------------

    def _register_resources(self, doc: dict[str, Any], owner: RepoRef) -> None:
        for entry in as_list(as_dict(doc.get("resources")).get("repositories")):
            if not isinstance(entry, dict):
                continue
            alias = as_str(entry.get("repository"))
            name = as_str(entry.get("name"))
            repo_type = as_str(entry.get("type") or "git").lower()
            if not alias or alias in self._aliases:
                continue
            if repo_type != "git":
                self._result.external_repos.append(f"{repo_type}:{name}")
                continue
            project, _, repo_name = name.rpartition("/")
            found = self._files.find_repo(project or owner.project, repo_name, entry.get("ref"))
            if found is None:
                self._result.unresolved.append(f"repository:{name}")
                continue
            self._aliases[alias] = found
            if found not in self._result.cross_repos and found.repo_id != owner.repo_id:
                self._result.cross_repos.append(found)

    def _load(self, reference: str, frame: _Frame) -> tuple[dict[str, Any], _Frame] | None:
        if not reference:
            return None
        if not self._resolve or frame.depth >= self._max_depth or "${{" in reference:
            self._result.unresolved.append(reference)
            return None
        path, _, alias = reference.partition("@")
        repo = frame.repo
        if alias and alias != "self":
            found = self._aliases.get(alias)
            if found is None:
                self._result.unresolved.append(reference)
                return None
            repo = found
        elif alias == "self" and self._root is not None:
            repo = self._root
        if path.startswith("/") or repo != frame.repo:
            full = path.lstrip("/")
        else:
            full = posixpath.normpath(posixpath.join(posixpath.dirname(frame.path), path))
        text = self._files.read(repo, full)
        if text is None:
            self._result.unresolved.append(reference)
            return None
        try:
            doc = load_yaml(text)
        except YamlParseError:
            self._result.unresolved.append(reference)
            return None
        if not isinstance(doc, dict):
            return None
        label = f"{repo.label}:{full}"
        if label not in self._result.templates:
            self._result.templates.append(label)
        return _flatten_expressions(doc), _Frame(repo, full, frame.depth + 1)

    def _expand(self, items: Any, kind: str, frame: _Frame, conditions: tuple[str, ...] = ()) -> list[Any]:
        out: list[Any] = []
        for item in as_list(items):
            if not isinstance(item, dict):
                continue
            if "template" in item:
                reference = as_str(item["template"])
                loaded = self._load(reference, frame)
                if loaded is None:
                    out.append({**item, "_unresolved": True, "_conditions": list(conditions)})
                    continue
                template, sub_frame = loaded
                params = {**_param_defaults(template), **as_dict(item.get("parameters"))}
                body = as_dict(_substitute({k: v for k, v in template.items() if k != "parameters"}, params))
                for expanded in self._expand(body.get(kind), kind, sub_frame, conditions):
                    expanded.setdefault("_template", reference)
                    out.append(expanded)
                continue
            expression_keys = [key for key in item if is_expression_key(key)]
            if expression_keys and len(expression_keys) == len(item):
                # "- ${{ if ... }}:" / "- ${{ each ... }}:" inserting list items; keep every branch
                for key in expression_keys:
                    out.extend(self._expand(item[key], kind, frame, (*conditions, clean_key(key).strip())))
                continue
            entry = _flatten_expressions(item)
            if conditions:
                entry["_conditions"] = [*conditions, *as_list(entry.get("_conditions"))]
            nested = _NESTED.get(kind)
            if nested and nested in entry:
                entry[nested] = self._expand(entry[nested], nested, frame)
            if kind == "jobs":
                self._expand_deployment(entry, frame)
            if "variables" in entry:
                entry["variables"] = self._expand_variables(entry["variables"], frame)
            out.append(entry)
        return out

    def _expand_deployment(self, job: dict[str, Any], frame: _Frame) -> None:
        strategy = as_dict(job.get("strategy"))
        for name in ("runOnce", "rolling", "canary"):
            body = as_dict(strategy.get(name))
            for hook in _HOOKS:
                section = as_dict(body.get(hook))
                if "steps" in section:
                    section["steps"] = self._expand(section["steps"], "steps", frame)
            for outcome in ("success", "failure"):
                section = as_dict(as_dict(body.get("on")).get(outcome))
                if "steps" in section:
                    section["steps"] = self._expand(section["steps"], "steps", frame)

    def _expand_variables(self, variables: Any, frame: _Frame) -> list[dict[str, Any]]:
        """Normalise mapping and list forms to a list of ``{name, value}`` / ``{group}`` entries."""
        out: list[dict[str, Any]] = []
        if isinstance(variables, dict):
            for key, value in _flatten_expressions(variables).items():
                out.append({"name": key, "value": value})
            return out
        for item in as_list(variables):
            if not isinstance(item, dict):
                continue
            if "template" in item:
                reference = as_str(item["template"])
                loaded = self._load(reference, frame)
                if loaded is None:
                    continue
                template, sub_frame = loaded
                params = {**_param_defaults(template), **as_dict(item.get("parameters"))}
                body = as_dict(_substitute({k: v for k, v in template.items() if k != "parameters"}, params))
                for entry in self._expand_variables(body.get("variables"), sub_frame):
                    entry.setdefault("_template", reference)
                    out.append(entry)
            elif all(is_expression_key(key) for key in item):
                for value in item.values():
                    out.extend(self._expand_variables(value, frame))
            else:
                out.append(_flatten_expressions(item))
        return out
