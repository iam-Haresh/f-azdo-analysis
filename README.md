# azdo-analyzer

Read-only inventory of Azure DevOps build and release pipelines, with everything an engineer
needs to rebuild them in GitLab CI/CD: what each pipeline is linked to, what it builds, where
it deploys, whether it is still alive, and how to build it in GitLab.

> **ONLY READ OPERATIONS.** The tool sends HTTP `GET` requests only. The HTTP client has no
> other method and a transport hook raises on anything else; there is no switch to relax it.
> The only things it writes are its own cache (`.cache/`) and reports (`out/`).

## Install

Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick start

```bash
cp config.example.yaml config.yaml          # edit sources[].base_url and project filters
export AZDO_PAT_DEFAULT='<your PAT>'        # the name comes from sources[].auth.token_env

azdo-analyzer config validate -c config.yaml
azdo-analyzer doctor   -c config.yaml       # connectivity, api-version, PAT scope pass/fail table
azdo-analyzer run      -c config.yaml       # collect + analyze + report
```

Or stage by stage (`analyze` and `report` work offline from the cache):

```bash
azdo-analyzer collect  -c config.yaml [--source X] [--project Y] [--refresh]
azdo-analyzer analyze  -c config.yaml       # writes out/model.json
azdo-analyzer report   -c config.yaml [--format xlsx,markdown]
azdo-analyzer brief    -c config.yaml --pipeline "<source>/<project>/<name>"
azdo-analyzer config schema                 # JSON Schema of the config file
```

Exit code is non-zero only for config errors, when no source can be reached or authenticated,
or when `run.fail_on_project_error` is set and a collector reported an error.

### TLS

`sources[].tls.verify` defaults to **false** (the tool is meant to be run from a workstation
against internal servers). To verify certificates set `verify: true` and, for a corporate CA,
`ca_bundle: /path/to/ca.pem`.

## PAT scopes (all read)

Code, Build, Release, Project and Team, Variable Groups, Service Connections, Agent Pools,
Task Groups, Deployment Groups, Environment, Secure Files, Packaging, Extensions.
`azdo-analyzer doctor` tests each one. A missing scope does not stop a run: the collector is
recorded as "no access" in the `errors` sheet and everything else continues.

The PAT is read from an environment variable only. It is never logged, cached or written to a report.

## Sensitive output

`.cache/` holds raw API payloads (identities, URLs, non-secret variable values, manifest files)
and `out/` holds the reports. Both are git-ignored - treat them as sensitive.

Reports apply the `reports.privacy` settings: user names are hashed unless
`include_identities` is true, values matching `redact_value_patterns` and inline credentials
are redacted, plain variables that look like secrets are flagged and their value withheld,
webhook and service-connection URLs are cut to scheme and host. Azure DevOps never returns
secret variable values; only their names are reported.

## Config reference

`config.example.yaml` documents every key. Sections:

| Section | Purpose |
|---|---|
| `run` | output/cache dirs, cache TTL, resume, concurrency, rate limit, timeout, retries, log level |
| `sources` | one entry per collection: URL, api-version (`auto` negotiates 7.1 -> 4.1), PAT env var, TLS, proxy, project include/exclude |
| `filters` | disabled / draft / deleted definitions, name and folder excludes |
| `collectors` | switch any collector off |
| `history` | lookback window and runs per definition |
| `activity` | active / dormant / failing / low-success thresholds |
| `repo_scan` | branch, tree and file limits, ignored paths, TFVC depth |
| `yaml_pipelines` | template resolution depth, timeline expansion |
| `detection` | rules dirs, minimum confidence, inline-script scan |
| `environments` | stage name -> tier patterns, production tiers, GitLab deployment tiers |
| `gitlab` | target path template, image registry prefix, runner tags, mapping files, template catalog, security baseline, skeletons |
| `complexity` | effort weights and S/M/L bands |
| `clustering` | similarity threshold, ignored glue tasks |
| `security` | secret-name and inline-credential patterns, shared-connection threshold |
| `waves` | which statuses migrate, band -> wave |
| `reports` | formats, per-project / per-pipeline output, Excel options, privacy |

Detection rules (`rules/*.yaml`) and GitLab mappings (`mappings/*.yaml`) are data, not code.
Add your own rules in a directory listed under `detection.extra_rules_dirs`; a rule with the
same `id` replaces the built-in one. Extend `mappings/tasks.yaml` from the *Unmapped tasks* report.

## Report catalogue (`out/`)

| File | Content |
|---|---|
| `inventory.xlsx` | one sheet per view (below) |
| `csv/<sheet>.csv` | the same tables as CSV |
| `model.json` | the full normalized model (`schema_version` 1) |
| `summary.md`, `summary.html` | executive view: totals, active vs inactive, stacks, deploy targets, classic vs YAML, effort, unmapped tasks, template gaps, risks, waves |
| `projects/<source>/<project>.md` | per-project summary with a Mermaid dependency graph |
| `briefs/<source>/<project>/<pipeline>.<kind>-<id>.md` + `.json` | the document to hand to whoever writes the GitLab pipeline |
| `skeletons/.../<pipeline>.gitlab-ci.yml` | draft skeletons, only when `gitlab.generate_skeletons` is true |
| `azdo-analyzer.log` | structured (JSON lines) log |

Sheets: `summary`, `projects`, `repos`, `components`, `build_pipelines`, `release_pipelines`,
`release_stages`, `mapping` (project <-> repo <-> build <-> release <-> stage <-> target),
`tasks_usage`, `unmapped_tasks`, `task_groups`, `variable_groups`, `variables`,
`service_connections`, `agent_pools`, `deployment_targets`, `environments`, `triggers`,
`branch_policies`, `cross_project_deps`, `template_fit`, `clusters`, `security`,
`runner_requirements`, `effort`, `orphans`, `extensions`, `errors`.

The same cache and config always produce byte-identical JSON, CSV and Markdown: "now" for
activity classification is the collection time stored in the cache.

## How it works

```
collect  ->  normalize + link  ->  analyze  ->  report
(AZDO API)   (raw JSON -> model)   (detect, score, map)   (xlsx / md / json / csv / html)
   |
   v
.cache/<source>/<project>/<resource>/<id>.json   (raw, untouched API payloads)
```

`collect` is resumable: cache entries younger than `run.cache_ttl_hours` are skipped,
`--refresh` forces. A failure in one project or collector becomes a row in the errors table.

## Development

```bash
pytest -q        # no test talks to a real Azure DevOps (tests/fake_azdo.py is an in-memory server)
ruff check .
mypy src
```

Decisions and deviations from the spec are recorded in `docs/decisions.md`.
