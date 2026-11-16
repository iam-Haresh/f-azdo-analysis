"""Load and validate the config file (YAML or JSON), with readable errors."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .models import Config


class ConfigError(Exception):
    """Raised for anything wrong with the config file; message is user-readable."""


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def _check_patterns(config: Config) -> None:
    """Fail fast on regex patterns that do not compile."""
    groups: dict[str, list[str]] = {
        "filters.pipeline_name_exclude": config.filters.pipeline_name_exclude,
        "filters.pipeline_folder_exclude": config.filters.pipeline_folder_exclude,
        "reports.privacy.redact_value_patterns": config.reports.privacy.redact_value_patterns,
        "security.secret_name_patterns": config.security.secret_name_patterns,
        "security.inline_credential_patterns": config.security.inline_credential_patterns,
    }
    for tier, patterns in config.environments.tiers.items():
        groups[f"environments.tiers.{tier}"] = patterns
    for source in config.sources:
        groups[f"sources[{source.name}].projects.include"] = source.projects.include
        groups[f"sources[{source.name}].projects.exclude"] = source.projects.exclude
    problems = []
    for where, patterns in groups.items():
        for pattern in patterns:
            if pattern.startswith("re:"):
                try:
                    re.compile(pattern[3:])
                except re.error as exc:
                    problems.append(f"  {where}: invalid regex {pattern!r}: {exc}")
    if problems:
        raise ConfigError("Invalid config:\n" + "\n".join(problems))


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("Invalid config: top level must be a mapping")
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError("Invalid config:\n" + _format_errors(exc)) from exc
    _check_patterns(config)
    return config


def load_config(path: str | Path) -> Config:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Config file not found: {file}")
    text = file.read_text(encoding="utf-8")
    try:
        if file.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = YAML(typ="safe").load(text)
    except (json.JSONDecodeError, YAMLError) as exc:
        raise ConfigError(f"Cannot parse {file}: {exc}") from exc
    return parse_config(data)


def config_json_schema() -> dict[str, Any]:
    return Config.model_json_schema()
