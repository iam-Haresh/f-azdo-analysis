"""``doctor``: connectivity, api-version and PAT read-scope check per source (GET only)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..client.http import AccessDenied, AuthConfigError, AzdoClient, AzdoHttpError, NotFound, quote_segment
from ..config.models import Config, SourceConfig
from ..filters import project_included
from ..util import as_str

# (PAT scope, path, params, area). {p} = first selected project.
CHECKS: list[tuple[str, str, dict[str, str], str]] = [
    ("Project and Team", "_apis/projects", {"$top": "1"}, "core"),
    ("Code", "{p}/_apis/git/repositories", {}, "core"),
    ("Build", "{p}/_apis/build/definitions", {"$top": "1"}, "core"),
    ("Release", "{p}/_apis/release/definitions", {"$top": "1"}, "release"),
    ("Variable Groups", "{p}/_apis/distributedtask/variablegroups", {}, "core"),
    ("Service Connections", "{p}/_apis/serviceendpoint/endpoints", {}, "core"),
    ("Agent Pools", "_apis/distributedtask/pools", {}, "core"),
    ("Task Groups", "{p}/_apis/distributedtask/taskgroups", {}, "core"),
    ("Deployment Groups", "{p}/_apis/distributedtask/deploymentgroups", {}, "core"),
    ("Environment", "{p}/_apis/distributedtask/environments", {}, "core"),
    ("Secure Files", "{p}/_apis/distributedtask/securefiles", {}, "core"),
    ("Packaging", "_apis/packaging/feeds", {}, "feeds"),
    ("Extensions", "_apis/extensionmanagement/installedextensions", {}, "extmgmt"),
    ("Task catalog", "_apis/distributedtask/tasks", {}, "core"),
    ("Policy", "{p}/_apis/policy/configurations", {"$top": "1"}, "core"),
    ("Service Hooks", "_apis/hooks/subscriptions", {}, "core"),
]


@dataclass
class CheckResult:
    source: str
    check: str
    status: str  # pass | fail | n/a
    detail: str = ""


def run_doctor(config: Config, client_factory: Callable[[SourceConfig], AzdoClient] | None = None) -> tuple[list[CheckResult], bool]:
    """Return the check table and whether every source could at least connect and authenticate."""
    factory = client_factory or (lambda source: AzdoClient(source, config.run))
    results: list[CheckResult] = []
    healthy = True
    for source in config.sources:
        try:
            client = factory(source)
        except AuthConfigError as exc:
            results.append(CheckResult(source.name, "Token", "fail", str(exc)))
            healthy = False
            continue
        try:
            try:
                version = client.api_version
                results.append(CheckResult(source.name, "Connectivity / api-version", "pass", f"api-version {version}"))
            except AccessDenied as exc:
                results.append(CheckResult(source.name, "Connectivity / api-version", "fail", f"authentication failed (HTTP {exc.status})"))
                healthy = False
                continue
            except AzdoHttpError as exc:
                results.append(CheckResult(source.name, "Connectivity / api-version", "fail", str(exc)[:200]))
                healthy = False
                continue

            project = ""
            try:
                listed = client.get_list("_apis/projects", {"stateFilter": "WellFormed"}, page_size=100)
                names = sorted(
                    as_str(p.get("name"))
                    for p in listed
                    if isinstance(p, dict) and as_str(p.get("state") or "wellFormed").lower() == "wellformed"
                )
                selected = [n for n in names if project_included(source.projects, n)]
                project = selected[0] if selected else ""
                results.append(
                    CheckResult(source.name, "Projects selected", "pass" if selected else "fail", f"{len(selected)} of {len(names)}")
                )
            except AzdoHttpError as exc:
                results.append(CheckResult(source.name, "Projects selected", "fail", str(exc)[:200]))

            for scope, path, params, area in CHECKS:
                if "{p}" in path and not project:
                    results.append(CheckResult(source.name, scope, "n/a", "no project to test against"))
                    continue
                try:
                    client.get(path.replace("{p}", quote_segment(project)), params, area=area)
                    results.append(CheckResult(source.name, scope, "pass"))
                except AccessDenied as exc:
                    results.append(CheckResult(source.name, scope, "fail", f"no access (HTTP {exc.status})"))
                except NotFound:
                    results.append(CheckResult(source.name, scope, "n/a", "endpoint not available on this server version"))
                except AzdoHttpError as exc:
                    results.append(CheckResult(source.name, scope, "fail", str(exc)[:200]))
        finally:
            client.close()
    return results, healthy


def format_table(results: list[CheckResult]) -> str:
    headers = ("Source", "Check", "Result", "Detail")
    rows = [(r.source, r.check, r.status.upper(), r.detail) for r in results]
    widths = [max(len(str(row[i])) for row in [headers, *rows]) for i in range(3)]
    lines = ["  ".join(str(h).ljust(widths[i]) if i < 3 else h for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths) + "  ------")
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) if i < 3 else c for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)
