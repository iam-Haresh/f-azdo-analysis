"""Migration waves (spec section 13.7): active first, low complexity first, providers before consumers."""

from __future__ import annotations

from ..config.models import Config
from ..model.entities import Estate, Wave


def plan_waves(estate: Estate, config: Config) -> None:
    waves_config = config.waves
    pipelines = {p.id: p for p in estate.pipelines()}
    # Upstream = what must exist first: triggering builds, builds consumed by releases.
    upstream: dict[str, set[str]] = {pid: set() for pid in pipelines}
    for build in estate.builds:
        upstream[build.id].update(u for u in build.upstream_ids if u in pipelines)
    for release in estate.releases:
        upstream[release.id].update(b for b in release.build_ids if b in pipelines)

    numbers: dict[str, int] = {}
    max_wave = max(waves_config.band_wave.values(), default=1) + 1

    def wave_of(pid: str, trail: tuple[str, ...] = ()) -> int:
        if pid in numbers:
            return numbers[pid]
        pipeline = pipelines[pid]
        base = waves_config.band_wave.get(pipeline.effort.band, max_wave)
        if pid not in trail:  # guard against trigger cycles
            for dependency in sorted(upstream[pid]):
                if pipelines[dependency].stats.status in waves_config.migrate_statuses:
                    base = max(base, wave_of(dependency, (*trail, pid)))
        numbers[pid] = base
        return base

    buckets: dict[str, list[str]] = {}
    for pid in sorted(pipelines):
        pipeline = pipelines[pid]
        status = pipeline.stats.status
        if status in waves_config.migrate_statuses:
            name = f"Wave {wave_of(pid)}"
        elif status in waves_config.review_statuses:
            name = "Review (dormant)"
        else:
            name = "Do not migrate (inactive)"
        pipeline.wave = name
        buckets.setdefault(name, []).append(pid)

    def order(name: str) -> tuple[int, str]:
        return (0, name.rjust(12)) if name.startswith("Wave ") else (1 if name.startswith("Review") else 2, name)

    estate.waves = [
        Wave(name=name, pipelines=sorted(buckets[name], key=lambda p: (pipelines[p].effort.score, p)))
        for name in sorted(buckets, key=order)
    ]
