"""Shared context and small helpers for the three pipeline parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..config.models import Config
from ..model.entities import AgentPool, AgentQueue, DeploymentGroup, Trigger, Variable, VariableGroup
from ..normalize.privacy import REDACTED, Privacy
from ..util import as_bool, as_dict, as_int, as_list, as_str, matches_any, short_branch
from .steps import StepFactory


@dataclass
class ParseEnv:
    config: Config
    source: str
    project: str
    factory: StepFactory
    privacy: Privacy
    queues: dict[str, AgentQueue] = field(default_factory=dict)  # by AZDO queue id
    pools: dict[str, AgentPool] = field(default_factory=dict)  # by AZDO pool id
    deployment_groups: dict[str, DeploymentGroup] = field(default_factory=dict)  # by AZDO id
    variable_groups: dict[str, VariableGroup] = field(default_factory=dict)  # by AZDO id
    variable_groups_by_name: dict[str, VariableGroup] = field(default_factory=dict)  # lower-case name

    def tier(self, stage_name: str) -> str:
        for tier, patterns in self.config.environments.tiers.items():
            if matches_any(patterns, stage_name):
                return tier
        return ""

    def pool_for_queue(self, queue_id: Any, queue: dict[str, Any] | None = None) -> tuple[str, str, bool]:
        """Return (pool azdo id, pool name, is_hosted) for a queue reference."""
        known = self.queues.get(as_str(queue_id)) if queue_id is not None else None
        pool_id = known.pool_id if known else as_str(as_dict(as_dict(queue).get("pool")).get("id"))
        name = (
            (known.pool_name or known.name)
            if known
            else as_str(as_dict(as_dict(queue).get("pool")).get("name") or as_dict(queue).get("name"))
        )
        pool = self.pools.get(pool_id)
        hosted = pool.is_hosted if pool else as_bool(as_dict(as_dict(queue).get("pool")).get("isHosted"))
        return pool_id, (pool.name if pool else name), hosted

    def variable(
        self,
        name: str,
        raw: Any,
        *,
        scope: Literal["definition", "stage", "group"],
        owner: str,
        source: Literal["inline", "group", "keyvault", "template"] = "inline",
    ) -> Variable:
        data = raw if isinstance(raw, dict) else {"value": raw}
        secret = as_bool(data.get("isSecret"))
        value = "" if secret else as_str(data.get("value"))
        suspected = (
            not secret and bool(value.strip()) and not value.strip().startswith(("$(", "${{", "$[")) and self.privacy.is_secret_name(name)
        )
        return Variable(
            scope=scope,
            owner=owner,
            name=name,
            # A plain variable that looks like a secret is flagged, and its value never reaches a report.
            value=REDACTED if suspected else self.privacy.value(value),
            is_secret=secret,
            allow_override=as_bool(data.get("allowOverride")),
            source=source,
            suspected_secret=suspected,
        )


def split_filters(filters: Any) -> tuple[list[str], list[str]]:
    """Classic ``+refs/heads/main`` / ``-/docs`` filter lists -> (include, exclude)."""
    include: list[str] = []
    exclude: list[str] = []
    for item in as_list(filters):
        text = as_str(item)
        if text.startswith("-"):
            exclude.append(short_branch(text[1:]))
        else:
            include.append(short_branch(text.lstrip("+")))
    return include, exclude


_DAY_FLAGS = [
    ("monday", 1, 1),
    ("tuesday", 2, 2),
    ("wednesday", 4, 3),
    ("thursday", 8, 4),
    ("friday", 16, 5),
    ("saturday", 32, 6),
    ("sunday", 64, 0),
]


def days_to_cron(days: Any) -> str:
    """AZDO schedule days (flag int or 'monday, friday' / 'all') -> cron day-of-week field."""
    selected: list[int] = []
    number = as_int(days)
    if number is not None:
        selected = [dow for _, flag, dow in _DAY_FLAGS if number & flag]
    else:
        text = as_str(days).lower()
        if text in ("all", ""):
            return "*"
        selected = [dow for name, _, dow in _DAY_FLAGS if name in text]
    if len(selected) in (0, 7):
        return "*"
    return ",".join(str(d) for d in sorted(selected))


def schedule_trigger(schedule: dict[str, Any], days_key: str) -> Trigger:
    include, exclude = split_filters(schedule.get("branchFilters"))
    hours = as_int(schedule.get("startHours")) or 0
    minutes = as_int(schedule.get("startMinutes")) or 0
    return Trigger(
        type="schedule",
        cron=f"{minutes} {hours} * * {days_to_cron(schedule.get(days_key))}",
        timezone=as_str(schedule.get("timeZoneId")),
        branches_include=include,
        branches_exclude=exclude,
        detail="only with changes" if as_bool(schedule.get("scheduleOnlyWithChanges")) else "",
    )
