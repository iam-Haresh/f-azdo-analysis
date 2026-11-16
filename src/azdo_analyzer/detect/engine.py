"""Rule engine for repository evidence: parse a manifest once, evaluate ``when`` and ``emit``."""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
from typing import Any

from lxml import etree
from ruamel.yaml import YAML

from ..util import as_str
from .rules import RepoRule

_PATH_TOKEN = re.compile(r"\.([A-Za-z0-9_\-@/]+|\*)|\[\s*'([^']*)'\s*\]|\[\s*\"([^\"]*)\"\s*\]|\[\s*(\d+|\*)\s*\]")


def json_path(data: Any, path: str) -> list[Any]:
    """Small JSONPath subset: ``$.a.b``, ``$.a['b/c']``, ``[0]``, ``.*`` and ``[*]``."""
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    current: list[Any] = [data]
    pos = 0
    while pos < len(path):
        match = _PATH_TOKEN.match(path, pos)
        if not match:
            return []
        pos = match.end()
        name = match.group(1) or match.group(2) or match.group(3)
        index = match.group(4)
        nxt: list[Any] = []
        for node in current:
            if name == "*" or index == "*":
                if isinstance(node, dict):
                    nxt.extend(node.values())
                elif isinstance(node, list):
                    nxt.extend(node)
            elif index is not None:
                if isinstance(node, list) and int(index) < len(node):
                    nxt.append(node[int(index)])
            elif isinstance(node, dict) and name in node:
                nxt.append(node[name])
        current = nxt
    return [node for node in current if node is not None]


def _strip_namespaces(root: Any) -> None:
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    etree.cleanup_namespaces(root)


def parse_xml(text: str) -> Any:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, remove_comments=True)
    root = etree.fromstring(text.lstrip("﻿").encode("utf-8"), parser=parser)
    if root is not None:
        _strip_namespaces(root)
    return root


def parse_properties(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#!;[":
            continue
        key, sep, value = line.partition("=")
        if not sep:
            key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip().replace("\\:", ":")
    return out


class ParsedFile:
    """A manifest parsed lazily in whichever shapes the rules ask for."""

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self._cache: dict[str, Any] = {}

    def doc(self, parser: str) -> Any:
        if parser not in self._cache:
            self._cache[parser] = self._parse(parser)
        return self._cache[parser]

    def _parse(self, parser: str) -> Any:
        try:
            if parser == "xml":
                return parse_xml(self.text)
            if parser == "json":
                return json.loads(_strip_json_comments(self.text.lstrip("﻿")))
            if parser == "toml":
                return tomllib.loads(self.text)
            if parser == "yaml":
                docs = [d for d in YAML(typ="safe").load_all(self.text) if d is not None]
                return docs[0] if len(docs) == 1 else docs
            if parser == "properties":
                return parse_properties(self.text)
        except Exception:  # noqa: BLE001 - malformed manifests simply yield no facts
            return None
        return None


def _strip_json_comments(text: str) -> str:
    """tsconfig.json / angular.json allow // comments and trailing commas."""
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _xpath_values(root: Any, expression: str) -> list[str]:
    if root is None:
        return []
    try:
        result = root.getroottree().xpath(expression)
    except etree.XPathError:
        return []
    if isinstance(result, bool):
        return ["true"] if result else []
    if isinstance(result, int | float | str):
        return [as_str(result)] if as_str(result) else []
    values = []
    for item in result:
        text = item if isinstance(item, str) else (item.text or "")
        text = text.strip()
        values.append(text if text or isinstance(item, str) else "(present)")
    return [v for v in values if v]


def _stringify(values: list[Any]) -> list[str]:
    out = []
    for value in values:
        if isinstance(value, dict | list):
            out.append("(present)")
        else:
            out.append(as_str(value))
    return [v for v in out if v]


def _first_group(match: re.Match[str]) -> str:
    """First capture group that took part in the match, else the whole match."""
    for group in match.groups():
        if group is not None:
            return group
    return match.group(0)


def extract(file: ParsedFile, rule: RepoRule, spec: Any) -> list[str]:
    """Evaluate an extractor: a literal, or a mapping naming where the value comes from."""
    if not isinstance(spec, dict):
        return [as_str(spec)] if as_str(spec) else []
    values: list[str] = []
    if "xpath" in spec:
        values = _xpath_values(file.doc("xml"), as_str(spec["xpath"]))
    elif "jsonpath" in spec:
        parser = rule.parser if rule.parser in ("json", "toml", "yaml") else "json"
        paths = spec["jsonpath"] if isinstance(spec["jsonpath"], list) else [spec["jsonpath"]]
        doc = file.doc(parser)
        values = _stringify([found for path in paths for found in json_path(doc, as_str(path))])
    elif "key" in spec:
        props = file.doc("properties") or {}
        value = props.get(as_str(spec["key"]))
        values = [value] if value else []
    elif "regex" in spec:
        flags = re.MULTILINE | (re.IGNORECASE if spec.get("ignore_case") else 0)
        for match in re.finditer(as_str(spec["regex"]), file.text, flags):
            values.append(_first_group(match))
    elif "from_extension" in spec:
        extension = posixpath.splitext(file.path)[1].lstrip(".").lower()
        mapped = as_str(spec["from_extension"].get(extension))
        values = [mapped] if mapped else []
    elif "path" in spec:
        values = [file.path]
    elif "value" in spec:
        values = [as_str(spec["value"])]

    if "pattern" in spec:  # keep only the captured part of each value
        refined = []
        for value in values:
            found = re.search(as_str(spec["pattern"]), value)
            if found:
                refined.append(_first_group(found))
        values = refined
    if "map" in spec:  # first regex key that matches wins
        mapped_values = []
        for value in values:
            for pattern, replacement in spec["map"].items():
                if re.search(as_str(pattern), value, re.IGNORECASE):
                    mapped_values.append(as_str(replacement))
                    break
        values = mapped_values
    seen: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def condition(file: ParsedFile, rule: RepoRule, when: Any) -> bool:
    if not when:
        return True
    if not isinstance(when, dict):
        return bool(when)
    if "any" in when:
        return any(condition(file, rule, sub) for sub in when["any"])
    if "all" in when:
        return all(condition(file, rule, sub) for sub in when["all"])
    if "not" in when:
        return not condition(file, rule, when["not"])
    return bool(extract(file, rule, when))
