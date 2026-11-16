# Decisions, assumptions and deviations

Recorded while implementing `AZDO_PIPELINE_ANALYZER_SPEC.md` (phases 0-8, built in one pass at the user's request).

## Answers from the user (spec section 3)

| Question | Answer | Effect |
|---|---|---|
| Scope of the first session | All phases 0-8 | Open questions for later phases got the defaults listed below |
| AZDO flavour | Azure DevOps Server (on-prem) only | `api_version: auto` negotiation is the default. Services host switching (vsrm / feeds / extmgmt) is implemented but has only a unit test |
| Auth | PAT only | `ntlm` / `negotiate` are accepted by the schema but rejected at client creation with a clear message |
| Legacy | TFVC repos exist; XAML unknown | TFVC scan on by default, `collectors.xaml_builds` off by default |
| TLS | "Set the ssl verify to false as I will run only from local" | `sources[].tls.verify` defaults to `false` in the model and in `config.example.yaml`. Set `verify: true` (+ `ca_bundle`) to turn it back on |

## Still open (defaults in use)

- Size of the estate: defaults `concurrency: 6`, `requests_per_second: 10`.
- Existing GitLab shared templates: `gitlab.template_catalog` in the example holds the three entries from the spec; replace with the real catalogue.
- Artifact repository: `artifactory` (spec default). Only used as a label so far.
- Runner estate, image registry prefix, target group structure: spec defaults.

## Not verified against a live server

**No endpoint has been exercised against a real Azure DevOps Server.** The payload shapes in
`tests/fake_azdo.py` were written from knowledge of the REST API, like the table in spec section 7.
Run `azdo-analyzer doctor` first; expect to adjust a field name or a preview flag for your server version.
Things most likely to differ:

- preview-only resources (secure files, environment checks): handled generically - a 400 that says
  the `-preview` flag is required is retried once with `<version>-preview` and remembered;
- TFVC `items?recursionLevel=Full` on a very large project (may time out; the error is recorded and the run continues);
- `extensionmanagement/installedextensions` on older servers (404 -> "not available" in the errors table).

## Deviations from the spec

- **Collectors are not one module per resource.** Simple list endpoints are rows in a table
  (`collect/collectors.py`: `COLLECTION_LISTS`, `PROJECT_LISTS`); resources that need follow-up calls have a function.
- **Policy types are not fetched** (`_apis/policy/types`). A policy configuration is treated as build validation
  when `settings.buildDefinitionId` is present, which needs no extra call.
- **Cache layout for lists.** List endpoints are stored as one `list.json` (`{"count", "value"}`, all pages merged) per
  resource; per-item payloads are stored as received. Repository files live under `files/<repo>/<hash>.json`
  (the API response, or `{"_missing": true}` / `{"_too_large": true}`). Trees are cut at `repo_scan.max_tree_entries` and marked `_truncated`.
- **Test fixtures** are an in-memory fake server (`tests/fake_azdo.py`, `httpx.MockTransport`) plus manifest files under
  `tests/fixtures/repos/`, rather than recorded JSON files; `respx` is used for the HTTP client tests.
- **Markdown reports are built in Python** (`report/markdown.py`); Jinja2 renders the HTML summary only.
- **`repo_scan.extra_branches` is accepted but not used yet** (comparing branches was not specified further).
- **TFVC depth** is applied after one `recursionLevel=Full` listing rather than by walking level by level.
- **Line length** is 140 for ruff (the report tables are wide); `E501` is left to the formatter.

## Behaviour worth knowing

- **Deterministic "now".** Activity status is computed against the newest `collected_at` in the cache, not the wall clock.
- **Run history shortcut.** Definitions without a `latestBuild` are not queried for runs. A pipeline whose last run is
  older than `history.lookback_days` is classified from the definition's `latestBuild`.
- **Timeline expansion** is fetched for YAML pipelines only, from the newest succeeded run in the collected history.
  Tasks seen only in the timeline are added to the pipeline's task set (mapping, clustering, scanners).
- **Static YAML expansion keeps every branch** of `${{ if }}` / `${{ each }}` and records the expression as the
  step's condition. Template parameters are substituted where the value is known. The preview API (a POST) is never used.
- **Status precedence** for the single `status` column: disabled, orphaned, never_run, stale, failing, active, dormant.
  `all_statuses` keeps every match (for example `active; failing`).
- **Service connections are matched** by GUID or exact name in step inputs. Names shorter than three characters are ignored.
- **Template "coverage"** is an approximation: a matched template is assumed to cover the tasks that produce the facts it
  matches on, plus `clustering.ignore_tasks`. Everything else is listed as uncovered.
- **Plain variables that look like secrets** (name matches `security.secret_name_patterns`) are flagged and their value is
  replaced by `***REDACTED***` in the model, so it cannot reach any report.
- **Config additions beyond section 6:** `detection.rules_dir`, `environments.production_tiers`,
  `environments.deployment_tiers`, `gitlab.service_connection_mapping_file`, `complexity.inline_script_line_threshold`,
  `clustering.min_cluster_size`, `security.*`, `waves.*`.
