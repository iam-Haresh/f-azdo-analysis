from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from azdo_analyzer.cli import app
from azdo_analyzer.config import ConfigError, config_json_schema, load_config, parse_config

from conftest import REPO_ROOT

EXAMPLE = REPO_ROOT / "config.example.yaml"
SPEC_EXAMPLE_SOURCE = {
    "name": "DefaultCollection",
    "type": "server",
    "base_url": "https://tfs.example.com/tfs/DefaultCollection",
    "api_version": "auto",
    "auth": {"method": "pat", "token_env": "AZDO_PAT_DEFAULT"},
    "tls": {"verify": True, "ca_bundle": "/etc/ssl/certs/corp-ca.pem"},
    "proxy": None,
    "projects": {"include": ["*"], "exclude": ["Sandbox*", "zz_*"]},
}


def test_example_config_is_valid() -> None:
    config = load_config(EXAMPLE)
    assert config.sources[0].name == "DefaultCollection"
    assert config.complexity.bands == {"S": 5, "M": 12, "L": 20}
    assert config.reports.formats == ["xlsx", "markdown", "json", "csv", "html"]


def test_tls_verification_is_off_by_default_and_can_be_enabled() -> None:
    assert parse_config({"sources": [{"name": "a", "base_url": "https://x/y"}]}).sources[0].tls.verify is False
    assert load_config(EXAMPLE).sources[0].tls.verify is False
    assert parse_config({"sources": [SPEC_EXAMPLE_SOURCE]}).sources[0].tls.verify is True


def test_spec_source_shapes_are_accepted() -> None:
    cloud = {"name": "my-org", "type": "services", "base_url": "https://dev.azure.com/my-org", "api_version": 7.1}
    config = parse_config({"version": 1, "sources": [SPEC_EXAMPLE_SOURCE, cloud]})
    assert config.sources[1].api_version == "7.1"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"sources": []}, "sources"),
        ({}, "sources"),
        ({"sources": [{"name": "a", "base_url": "ftp://x"}]}, "base_url must start with http"),
        ({"sources": [{"name": "a", "base_url": "https://x", "api_version": "9.9"}]}, "sources.0.api_version"),
        ({"sources": [{"name": "a", "base_url": "https://x"}], "run": {"concurency": 3}}, "run.concurency"),
        ({"sources": [{"name": "a", "base_url": "https://x"}], "run": {"concurrency": 0}}, "run.concurrency"),
        ({"sources": [{"name": "a", "base_url": "https://x"}, {"name": "a", "base_url": "https://y"}]}, "duplicate source names: a"),
        ({"sources": [{"name": "a", "base_url": "https://x"}], "filters": {"pipeline_name_exclude": ["re:("]}}, "invalid regex"),
        ({"sources": [{"name": "a", "base_url": "https://x", "auth": {"token": "secret-in-file"}}]}, "sources.0.auth.token"),
        ({"version": 2, "sources": [{"name": "a", "base_url": "https://x"}]}, "version"),
    ],
)
def test_bad_configs_are_rejected_with_clear_messages(data: dict[str, object], expected: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse_config(data)
    assert expected in str(error.value)


def test_json_config_and_schema(tmp_path: Path) -> None:
    file = tmp_path / "config.json"
    file.write_text('{"version": 1, "sources": [{"name": "a", "base_url": "https://x/y"}]}')
    assert load_config(file).sources[0].base_url == "https://x/y"
    schema = config_json_schema()
    assert {"run", "sources", "collectors", "gitlab", "complexity"} <= set(schema["properties"])


def test_cli_validate_and_schema(tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["config", "validate", "-c", str(EXAMPLE)]).exit_code == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nsources: []\n")
    result = runner.invoke(app, ["config", "validate", "-c", str(bad)])
    assert result.exit_code == 2
    assert "sources" in result.output
    assert '"properties"' in runner.invoke(app, ["config", "schema"]).output
