"""``analyze`` stage: normalize, link, detect, score and map. Offline: reads only the cache."""

from __future__ import annotations

import logging
from pathlib import Path

from ..collect.cache import Cache
from ..config.models import Config
from ..detect.deploy import detect_targets
from ..detect.pipeline import detect_pipelines
from ..detect.repo import detect_repos
from ..detect.rules import RuleSet, load_rules
from ..link.graph import build_mapping, find_orphans, link
from ..model.entities import Estate
from ..normalize.build import normalize
from ..util import json_dumps, write_text
from .activity import analyze_activity
from .clustering import cluster_estate
from .complexity import score_estate
from .gitlab import Mappings, plan_estate
from .security import analyze_security
from .waves import plan_waves

log = logging.getLogger(__name__)

MODEL_FILE = "model.json"


def analyze(config: Config, rules: RuleSet | None = None) -> Estate:
    rules = rules or load_rules(config)
    cache = Cache(config.run.cache_dir, config.run.cache_ttl_hours, config.run.resume)
    estate, runs = normalize(config, cache, rules)
    detect_repos(estate, config, cache, rules)
    link(estate)
    detect_pipelines(estate, rules, config)
    detect_targets(estate, rules)
    analyze_activity(estate, runs, config)
    plan_estate(estate, Mappings(config), rules, config)
    score_estate(estate, config)
    cluster_estate(estate, config)
    analyze_security(estate, rules, config)
    plan_waves(estate, config)
    build_mapping(estate)
    find_orphans(estate)
    estate.errors.sort(key=lambda e: (e.source, e.project, e.collector, e.message))
    log.info(
        "analyzed %d projects, %d repos, %d build and %d release pipelines",
        len(estate.projects),
        len(estate.repos),
        len(estate.builds),
        len(estate.releases),
    )
    return estate


def model_path(config: Config) -> Path:
    return Path(config.run.output_dir) / MODEL_FILE


def save_model(estate: Estate, config: Config) -> Path:
    path = model_path(config)
    write_text(path, json_dumps(estate.model_dump(mode="json")))
    return path


def load_model(config: Config) -> Estate | None:
    path = model_path(config)
    if not path.is_file():
        return None
    return Estate.model_validate_json(path.read_text(encoding="utf-8"))
