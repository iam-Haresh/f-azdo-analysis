"""Activity and health (spec section 12). Every threshold comes from the config."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

from ..config.models import Config
from ..model.entities import BuildPipeline, Estate, ReleasePipeline, RunStats
from ..normalize.build import RunData, RunRecord
from ..util import iso, parse_dt

_REASONS = {
    "individualci": "ci",
    "batchedci": "ci",
    "pullrequest": "pr",
    "validateshelveset": "pr",
    "checkinshelveset": "ci",
    "schedule": "schedule",
    "scheduled": "schedule",
    "manual": "manual",
    "buildcompletion": "trigger",
    "resourcetrigger": "trigger",
    "triggered": "trigger",
    "automated": "ci",
    "continuousintegration": "ci",
}
_SUCCESS = {"succeeded", "partiallysucceeded"}
_FAILED = {"failed"}


def _recency(last: datetime | None, now: datetime, config: Config) -> str:
    if last is None:
        return "never_run"
    age_days = (now - last).total_seconds() / 86400
    if age_days <= config.activity.active_within_days:
        return "active"
    if age_days <= config.activity.dormant_within_days:
        return "dormant"
    return "stale"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _stats(records: list[RunRecord], fallback: RunRecord | None, now: datetime, config: Config) -> RunStats:
    dated = [(parse_dt(r.finished), r) for r in records]
    runs = sorted(((d, r) for d, r in dated if d is not None), key=lambda item: item[0], reverse=True)
    stats = RunStats()
    last = runs[0] if runs else None
    if last is None and fallback is not None and parse_dt(fallback.finished) is not None:
        last = (parse_dt(fallback.finished), fallback)  # type: ignore[assignment]
    if last is None:
        stats.status = "never_run"
        stats.statuses = ["never_run"]
        return stats

    stats.last_run = iso(last[0])
    stats.last_result = last[1].result
    stats.last_branch = last[1].branch
    stats.last_requester = last[1].requester
    stats.runs_in_window = len(runs)
    if runs:
        limit = config.history.max_runs_per_definition
        window_days = float(config.history.lookback_days)
        if len(runs) >= limit:  # history was capped: measure over the span we actually have
            window_days = max(1.0, (runs[0][0] - runs[-1][0]).total_seconds() / 86400)
        stats.runs_per_month = round(len(runs) / (window_days / 30.0), 2)
        results = [r.result.lower() for _, r in runs]
        decided = [x for x in results if x in _SUCCESS or x in _FAILED]
        if decided:
            stats.success_rate = round(100.0 * sum(1 for x in decided if x in _SUCCESS) / len(decided), 1)
            stats.low_success = stats.success_rate < config.activity.low_success_rate_pct
        durations = []
        for finished, record in runs:
            started = parse_dt(record.started)
            if started is not None and finished >= started:
                durations.append((finished - started).total_seconds())
        if durations:
            stats.median_duration_s = round(statistics.median(durations), 1)
            stats.p90_duration_s = round(_percentile(durations, 0.9), 1)
        stats.distinct_branches = len({r.branch for _, r in runs if r.branch})
        mix: dict[str, int] = {}
        for _, record in runs:
            kind = _REASONS.get(record.reason.lower(), "other") if record.reason else "other"
            mix[kind] = mix.get(kind, 0) + 1
        stats.trigger_mix = dict(sorted(mix.items()))

    statuses = [_recency(last[0], now, config)]
    n = config.activity.failing_last_n_runs
    recent = [r.result.lower() for _, r in runs[:n]]
    if len(recent) >= n and all(result in _FAILED for result in recent):
        statuses.append("failing")
    stats.statuses = statuses
    return stats


def _primary(statuses: list[str]) -> str:
    for status in ("disabled", "orphaned", "never_run", "stale", "failing", "active", "dormant"):
        if status in statuses:
            return status
    return "never_run"


def _build(build: BuildPipeline, runs: RunData, now: datetime, config: Config) -> None:
    stats = _stats(runs.builds.get(build.id, []), runs.latest_build.get(build.id), now, config)
    if build.queue_status.lower() in ("disabled", "paused"):
        stats.statuses.append("disabled")
    if any(flag in build.flags for flag in ("repo_missing", "repo_disabled")):
        stats.statuses.append("orphaned")
    stats.status = _primary(stats.statuses)
    build.stats = stats


def _release(release: ReleasePipeline, runs: RunData, now: datetime, config: Config) -> None:
    deployments = runs.deployments.get(release.id, [])
    created = runs.releases.get(release.id, [])
    stats = _stats(deployments or created, None, now, config)
    if created:
        created_at = [d for d in (parse_dt(r.finished) for r in created) if d is not None]
        newest = max(created_at, default=None)
        if newest and (not stats.last_run or newest > (parse_dt(stats.last_run) or newest)):
            stats.last_run = iso(newest)
            stats.statuses = [_recency(newest, now, config), *[s for s in stats.statuses if s == "failing"]]

    production = set(config.environments.production_tiers)
    last_prod: datetime | None = None
    for stage in release.stages:
        mine = sorted(
            ((parse_dt(d.finished), d) for d in deployments if d.stage == stage.name and parse_dt(d.finished)),
            key=lambda item: item[0],  # type: ignore[arg-type,return-value]
            reverse=True,
        )
        stage.stats.deployments = len(mine)
        if mine:
            stage.stats.last_deployment = iso(mine[0][0])
            stage.stats.last_status = mine[0][1].result
            good = next((when for when, d in mine if d.result.lower() in _SUCCESS), None)
            stage.stats.last_success = iso(good)
            if good and stage.tier in production and (last_prod is None or good > last_prod):
                last_prod = good
    stats.last_prod_deployment = iso(last_prod)
    if release.is_disabled:
        stats.statuses.append("disabled")
    if any(artifact.missing for artifact in release.artifacts):
        stats.statuses.append("orphaned")
    stats.status = _primary(stats.statuses)
    release.stats = stats


def analyze_activity(estate: Estate, runs: RunData, config: Config) -> None:
    now = parse_dt(estate.as_of) or datetime.now(UTC)
    for build in estate.builds:
        _build(build, runs, now, config)
    for release in estate.releases:
        _release(release, runs, now, config)

    repos_by_project: dict[tuple[str, str], list[str]] = {}
    for repo in estate.repos:
        if repo.last_commit:
            repos_by_project.setdefault((repo.source, repo.project), []).append(repo.last_commit)
    for project in estate.projects:
        counts: dict[str, int] = {}
        latest: list[str] = list(repos_by_project.get((project.source, project.name), []))
        candidate = False
        for pipeline in estate.pipelines():
            if pipeline.source != project.source or pipeline.project != project.name:
                continue
            counts[pipeline.stats.status] = counts.get(pipeline.stats.status, 0) + 1
            if pipeline.stats.last_run:
                latest.append(pipeline.stats.last_run)
            if "active" in pipeline.stats.statuses and "disabled" not in pipeline.stats.statuses:
                candidate = True
        project.status_counts = dict(sorted(counts.items()))
        project.migration_candidate = candidate
        dates = [d for d in (parse_dt(x) for x in latest) if d is not None]
        project.last_activity = iso(max(dates)) if dates else ""
