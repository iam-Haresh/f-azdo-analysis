from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from azdo_analyzer.analyze.run import analyze  # noqa: E402
from azdo_analyzer.client.http import AzdoClient  # noqa: E402
from azdo_analyzer.collect.runner import CollectSummary, collect  # noqa: E402
from azdo_analyzer.config import Config, parse_config  # noqa: E402
from azdo_analyzer.config.models import SourceConfig  # noqa: E402
from azdo_analyzer.model.entities import Estate  # noqa: E402

from fake_azdo import BASE, FakeAzdo  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def base_config(tmp: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "run": {
            "output_dir": str(tmp / "out"),
            "cache_dir": str(tmp / "cache"),
            "requests_per_second": 100000,
            "concurrency": 4,
            "retries": 2,
        },
        "sources": [
            {
                "name": "DefaultCollection",
                "type": "server",
                "base_url": BASE,
                "api_version": "auto",
                "auth": {"method": "pat", "token_env": "AZDO_TEST_PAT"},
                "projects": {"include": ["*"], "exclude": ["Sandbox*"]},
            }
        ],
        "filters": {"pipeline_name_exclude": ["*-poc"]},
        "detection": {"rules_dir": str(REPO_ROOT / "rules")},
        "gitlab": {
            "task_mapping_file": str(REPO_ROOT / "mappings" / "tasks.yaml"),
            "concept_mapping_file": str(REPO_ROOT / "mappings" / "concepts.yaml"),
            "service_connection_mapping_file": str(REPO_ROOT / "mappings" / "service_connections.yaml"),
            "template_catalog": [
                {
                    "id": "maven-app",
                    "match": {"build_tool": "maven"},
                    "include": {"project": "devsecops/templates", "file": "maven/app.yml", "ref": "v1"},
                },
                {
                    "id": "dotnet-framework",
                    "match": {"language": "csharp", "runtime_family": "netframework"},
                    "include": {"project": "devsecops/templates", "file": "dotnetfx/build.yml", "ref": "v1"},
                },
            ],
            "generate_skeletons": True,
        },
    }


ConfigFactory = Callable[..., Config]


@pytest.fixture(autouse=True)
def _pat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZDO_TEST_PAT", "test-token-not-real")


@pytest.fixture
def make_config(tmp_path: Path) -> ConfigFactory:
    def factory(**sections: Any) -> Config:
        data = copy.deepcopy(base_config(tmp_path))
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            else:
                data[key] = value
        return parse_config(data)

    return factory


@pytest.fixture
def config(make_config: ConfigFactory) -> Config:
    return make_config()


@pytest.fixture
def fake() -> FakeAzdo:
    return FakeAzdo()


def client_factory(fake: FakeAzdo, config: Config) -> Callable[[SourceConfig], AzdoClient]:
    return lambda source: AzdoClient(source, config.run, transport=fake.transport(), sleep=lambda _s: None)


@pytest.fixture
def collected(config: Config, fake: FakeAzdo) -> CollectSummary:
    return collect(config, client_factory=client_factory(fake, config))


@pytest.fixture
def estate(config: Config, collected: CollectSummary) -> Estate:
    return analyze(config)
