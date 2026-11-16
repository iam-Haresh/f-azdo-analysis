# AZDO Pipeline Analyzer — Build Spec for Claude Code

> **How to use this file.** Put it in the root of an empty repo, open Claude Code there and say:
> *"Read AZDO_PIPELINE_ANALYZER_SPEC.md. Implement Phase 0 and Phase 1 from section 19, run the tests, then stop for review."*
> Continue phase by phase. Fill in section 3 first — those answers change what gets built.

> ## ⚠️ ONLY READ OPERATIONS allowed in this project
> The tool must never create, change, delete, queue, or trigger anything in Azure DevOps or any other external system. HTTP `GET` only — no `POST`, `PUT`, `PATCH`, or `DELETE`, with no opt-in exceptions. The only things it writes are its own local cache and report files. This rule overrides anything else in this file; if a feature cannot be built with read-only calls, do not build it — raise it with the user instead.

---

## 1. Mission

Build a **read-only analysis tool** that inventories every build pipeline and release pipeline across multiple Azure DevOps (AZDO) collections and produces the information an engineer needs to **rebuild those pipelines in GitLab CI/CD** (target: GitLab Dedicated, Ultimate feature set).

The tool answers, for every pipeline and release:

1. **What is it linked to?** Collection → project → repo(s) → build pipeline(s) → release pipeline(s) → stages → deployment targets.
2. **What does it build?** Language, runtime version, framework (Angular, React, Spring Boot, ASP.NET, ...), build tool, package manager, test and scan tooling.
3. **Where does it deploy?** Target platform per stage, approvals, gates, credentials used.
4. **Is it worth migrating?** Active, dormant, disabled, never run, failing.
5. **How do I build it in GitLab?** Runner needs, image, matching shared template, variables and secrets to recreate, task-by-task GitLab equivalents, effort estimate.

## 2. Scope

**In scope**
- Azure DevOps Server (on-prem, multiple collections) and Azure DevOps Services (multiple organisations), mixed in one run.
- Classic (designer) build pipelines, YAML pipelines, classic release pipelines, YAML multi-stage deployments.
- Git and TFVC repositories (scan for technology; do not migrate).
- Supporting objects pipelines depend on: task groups, variable groups, service connections, agent pools/queues, deployment groups, environments, secure files, installed extensions, branch build-validation policies, service hooks, package feeds.
- Reports in Excel, Markdown, JSON, CSV, HTML.

**Non-goals**
- No repo, work item, or artifact migration (another tool does that).
- No writes to AZDO. HTTP `GET` only, no exceptions.
- No automatic production-ready `.gitlab-ci.yml`. The tool may emit *draft skeletons* clearly marked as drafts (Phase 8).

## 3. Assumptions to confirm before building

Claude Code: ask the user for any unchecked item that affects the phase you are about to implement. Do not guess.

- [ ] AZDO flavour and version per collection (Server 2019 / 2020 / 2022, or Services). Drives `api_version`.
- [ ] Auth method per collection: PAT (preferred), NTLM, or Kerberos/Negotiate. Corporate CA bundle or proxy needed?
- [ ] Are there TFVC repos? XAML builds (legacy TFS)?
- [ ] Rough size: number of collections, projects, pipelines (drives concurrency and cache design).
- [ ] Where will the tool run (Windows or Linux, network reach to AZDO)?
- [ ] Existing GitLab shared templates / CI components to map to (fills `gitlab.template_catalog`).
- [ ] Artifact repository in the target state (Artifactory, Nexus, GitLab package registry).
- [ ] Runner estate on GitLab: Linux tags, Windows tags, air-gapped image registry prefix.
- [ ] Target GitLab group structure (how collection/project maps to group/subgroup).

## 4. Tech stack and repo layout

- Python 3.11+, packaged with `pyproject.toml`, CLI entry point `azdo-analyzer`.
- `httpx` (sync client + thread pool, or async), `pydantic` v2 (config and data model), `ruamel.yaml` (tolerant YAML, see 9.2), `typer` (CLI), `jinja2` (Markdown/HTML reports), `openpyxl` (Excel), `lxml` or `defusedxml` (csproj/pom parsing), `tomllib` (stdlib).
- Tests: `pytest` with recorded JSON fixtures and `respx`. **No test may call a live AZDO.**
- Lint/type: `ruff`, `mypy --strict` on `src/`.

```
src/azdo_analyzer/
  cli.py
  config/          # pydantic models, loader, JSON-schema export
  client/          # http client, auth, paging, retry, api-version negotiation
  collect/         # one module per AZDO resource; writes raw JSON to cache
  model/           # normalized entities (section 15)
  normalize/       # raw JSON -> model
  link/            # linkage graph (section 8)
  parse/           # classic build, yaml pipeline, release parsers (section 9)
  detect/          # rule engine + built-in rules (sections 10, 11)
  analyze/         # activity, complexity, clustering, gitlab mapping (12, 13)
  report/          # excel, markdown, json, csv, html writers + templates
rules/             # built-in detection rules (YAML)
mappings/          # tasks.yaml, concepts.yaml, service_connections.yaml (YAML)
tests/fixtures/    # recorded API payloads, sample repos' manifest files
config.example.yaml
```

## 5. Architecture

Four stages, each runnable on its own. Stages 2–4 must work **offline** from the cache.

```
collect  ->  normalize + link  ->  analyze  ->  report
(AZDO API)   (raw JSON -> model)   (detect, score, map)   (xlsx / md / json / csv / html)
   |
   v
.cache/<source>/<project>/<resource>/<id>.json   (raw, untouched API payloads)
```

- Raw payloads are stored as received. Never re-shape before caching; analysis bugs must be fixable without re-collecting.
- Every stage is resumable and idempotent. A failure in one project is logged to an errors table and does not stop the run.
- All behaviour is driven by the config file (section 6). No hard-coded thresholds, name patterns, or mappings in code.

## 6. Configuration

Single YAML file (JSON also accepted; same schema). Validate with pydantic, fail fast with a readable error, and provide `azdo-analyzer config schema` to export JSON Schema. Secrets come from environment variables only — never from the file.

```yaml
version: 1

run:
  output_dir: ./out
  cache_dir: ./.cache
  cache_ttl_hours: 24          # reuse cached payloads newer than this
  resume: true
  concurrency: 6
  requests_per_second: 10
  timeout_seconds: 60
  retries: 5
  log_level: INFO
  fail_on_project_error: false

sources:                        # one entry per collection or organisation
  - name: DefaultCollection
    type: server                # server | services
    base_url: https://tfs.example.com/tfs/DefaultCollection
    api_version: auto           # auto | 4.1 | 5.0 | 6.0 | 7.0 | 7.1
    auth:
      method: pat               # pat | ntlm | negotiate
      token_env: AZDO_PAT_DEFAULT
    tls:
      verify: true
      ca_bundle: /etc/ssl/certs/corp-ca.pem
    proxy: null
    projects:
      include: ["*"]            # glob; prefix "re:" for regex
      exclude: ["Sandbox*", "zz_*"]
  - name: my-org
    type: services
    base_url: https://dev.azure.com/my-org
    api_version: "7.1"
    auth:
      method: pat
      token_env: AZDO_PAT_CLOUD
    projects:
      include: ["*"]
      exclude: []

filters:
  include_disabled_pipelines: true
  include_draft_definitions: false
  include_deleted_release_definitions: false
  pipeline_name_exclude: ["*-poc", "re:^test-.*"]
  pipeline_folder_exclude: ['\Archive\*']

collectors:                     # switch any collector off
  projects: true
  repositories: true
  build_definitions: true
  build_runs: true
  release_definitions: true
  release_runs: true
  task_groups: true
  variable_groups: true
  service_connections: true
  agent_pools: true
  agent_capabilities: true
  deployment_groups: true
  environments: true
  secure_files: true
  task_catalog: true
  installed_extensions: true
  branch_policies: true
  service_hooks: true
  package_feeds: true
  xaml_builds: false

history:
  lookback_days: 365            # how far back to pull runs/deployments
  max_runs_per_definition: 50

activity:                       # classification thresholds (section 12)
  active_within_days: 90
  dormant_within_days: 365
  failing_last_n_runs: 5
  low_success_rate_pct: 60

repo_scan:
  enabled: true
  branch: default               # default | explicit branch name
  extra_branches: []            # e.g. ["release/*"] to compare
  max_tree_entries: 50000
  max_file_size_kb: 512
  max_manifest_files_per_repo: 300
  ignore_paths:
    - "**/node_modules/**"
    - "**/bin/**"
    - "**/obj/**"
    - "**/vendor/**"
    - "**/.git/**"
    - "**/packages/**"
  tfvc:
    enabled: true
    max_depth: 6

yaml_pipelines:
  resolve_templates: true
  max_template_depth: 10
  use_timeline_expansion: true  # read last successful run's timeline (GET only)

detection:
  extra_rules_dirs: []          # user rules override built-ins by rule id
  min_confidence: 0.4
  script_scan: true             # regex inline scripts for tool usage

environments:                   # normalise AZDO stage names to tiers
  tiers:
    dev:  ["re:(?i)^dev.*", "re:(?i).*\\bci\\b.*"]
    qa:   ["re:(?i)^(qa|test|sit).*"]
    uat:  ["re:(?i)^(uat|stag|pre-?prod).*"]
    perf: ["re:(?i)^perf.*"]
    prod: ["re:(?i)^(prod|prd|live).*"]
    dr:   ["re:(?i).*(-|_)dr$"]

gitlab:
  group_path_template: "{source}/{project}"     # proposed target path
  image_registry_prefix: ""     # e.g. registry.internal/ci for air-gapped
  artifact_repository: artifactory              # artifactory | nexus | gitlab
  runner_tags:
    linux: ["linux", "docker"]
    windows: ["windows", "shell"]
    macos: ["macos"]
  task_mapping_file: mappings/tasks.yaml
  concept_mapping_file: mappings/concepts.yaml
  template_catalog:             # existing shared templates / CI components
    - id: maven-app
      match: { build_tool: maven }
      include: { project: "devsecops/templates", file: "maven/app.yml", ref: "v1" }
    - id: dotnet-framework
      match: { language: csharp, runtime_family: netframework }
      include: { project: "devsecops/templates", file: "dotnetfx/build.yml", ref: "v1" }
    - id: openshift-deploy
      match: { deploy_target: openshift }
      include: { component: "$CI_SERVER_FQDN/devsecops/components/ose-deploy@1" }
  security_baseline:            # what every migrated pipeline should have
    - sast
    - secret_detection
    - dependency_scanning
    - container_scanning
  generate_skeletons: false     # draft .gitlab-ci.yml per pipeline (Phase 8)

complexity:
  weights:
    classic_definition: 2
    per_10_tasks: 1
    per_unmapped_task: 3
    per_marketplace_task: 2
    per_task_group: 1
    per_stage: 1
    per_service_connection: 1
    windows_only: 2
    deployment_group: 3
    gates: 2
    cross_project_dependency: 2
    tfvc_source: 3
    inline_script_over_50_lines: 2
  bands: { S: 5, M: 12, L: 20 }   # score <= value; above L is XL

clustering:
  enabled: true
  similarity_threshold: 0.8     # Jaccard on task sets within the same stack
  ignore_tasks: ["CopyFiles", "PublishBuildArtifacts", "CmdLine", "PowerShell", "Bash"]

reports:
  formats: [xlsx, markdown, json, csv, html]
  per_project_markdown: true
  per_pipeline_brief: true
  dependency_graph: mermaid     # mermaid | none
  excel:
    sheets: all                 # or a list of sheet ids from section 14
    freeze_header: true
    autofilter: true
  privacy:
    include_identities: false   # false = hash user names and emails
    redact_value_patterns:      # applied to non-secret variable values
      - "re:(?i)(password|secret|token|key)=\\S+"
    max_variable_value_length: 200
```

## 7. Collection — AZDO REST API reference

Base URL is `{base_url}` from the source entry (already includes the collection or organisation). Append `api-version` to every call.

**Verify each endpoint against Microsoft Learn for the detected API version before relying on it.** This table was written from knowledge of the API, not from a live test; preview suffixes and field names differ between versions.

| Resource | Endpoint (relative to `{base_url}`) | Notes |
|---|---|---|
| Collections (optional discovery) | `{server}/_apis/projectCollections` | Server only. Explicit `sources` list is the default. |
| Projects | `_apis/projects?stateFilter=All` | Then `_apis/projects/{id}?includeCapabilities=true` for source-control type and process template. |
| Git repos | `{project}/_apis/git/repositories?includeHidden=true` | `defaultBranch`, `size`, `isDisabled`, `isFork`. |
| Repo file tree | `{project}/_apis/git/repositories/{repo}/items?recursionLevel=Full&versionDescriptor.version={branch}&versionDescriptor.versionType=branch` | Paths only; cap with `max_tree_entries`. |
| File content | `.../items?path={path}&includeContent=true` | Only for manifests matched by rules. |
| Last commit | `.../commits?searchCriteria.$top=1&searchCriteria.itemVersion.version={branch}` | Repo activity. |
| TFVC tree | `{project}/_apis/tfvc/items?scopePath=$/{project}&recursionLevel=Full` | Respect `tfvc.max_depth`. |
| Build definitions | `{project}/_apis/build/definitions?includeLatestBuilds=true` then `.../definitions/{id}` | List is shallow; always fetch each full definition. |
| Build runs | `{project}/_apis/build/builds?definitions={id}&minTime={iso}&$top={n}&queryOrder=finishTimeDescending` | Result, reason, branch, duration, requester. |
| Build timeline | `{project}/_apis/build/builds/{buildId}/timeline` | Actual jobs and tasks executed (section 9.2). |
| Release definitions | `{project}/_apis/release/definitions?$expand=Environments,Artifacts,Triggers,Variables` then `.../definitions/{id}` | **Services uses host `vsrm.dev.azure.com/{org}`**; Server uses the same base URL. |
| Releases | `{project}/_apis/release/releases?definitionId={id}&minCreatedTime={iso}&$top={n}&$expand=environments` | |
| Deployments | `{project}/_apis/release/deployments?definitionId={id}&minStartedTime={iso}&$top={n}` | Per-stage status and last success. |
| Task groups | `{project}/_apis/distributedtask/taskgroups?expanded=true` | Nested task groups possible. |
| Variable groups | `{project}/_apis/distributedtask/variablegroups` | `type` `Vsts` or `AzureKeyVault`; secret values are never returned. |
| Service connections | `{project}/_apis/serviceendpoint/endpoints?includeFailed=true` | `type`, `url`, auth scheme, shared-with projects. |
| Agent pools | `_apis/distributedtask/pools` and `pools/{id}/agents?includeCapabilities=true` | Capabilities list installed tooling on self-hosted agents. |
| Agent queues | `{project}/_apis/distributedtask/queues` | Maps definition queue id → pool. |
| Deployment groups | `{project}/_apis/distributedtask/deploymentgroups` and `.../{id}/targets` | VM-based deploy targets and tags. |
| Environments | `{project}/_apis/distributedtask/environments` and `.../{id}?expands=resourceReferences` | Server 2020+. Kubernetes and VM resources. |
| Environment checks | `{project}/_apis/pipelines/checks/configurations?resourceType=environment&resourceId={id}` | Preview. Approvals on YAML deployments. |
| Secure files | `{project}/_apis/distributedtask/securefiles` | Preview. Names only. |
| Task catalog | `_apis/distributedtask/tasks` | **Required**: classic definitions reference tasks by GUID. Gives name, version, author, contribution id. |
| Installed extensions | `_apis/extensionmanagement/installedextensions` | Services host: `extmgmt.dev.azure.com/{org}`. |
| Branch policies | `{project}/_apis/policy/configurations` | Build-validation policies link pipelines to repo+branch. Resolve type ids via `_apis/policy/types`. |
| Service hooks | `_apis/hooks/subscriptions` | Outbound integrations to recreate as GitLab webhooks. |
| Package feeds | `_apis/packaging/feeds` | Services host: `feeds.dev.azure.com/{org}`. |
| XAML builds | `{project}/_apis/build/definitions?type=xaml` | Legacy TFS only; flag, do not parse deeply. |

**Client requirements**
- Auth: PAT as HTTP Basic with empty user name. NTLM / Negotiate as optional extras for Server.
- Paging: follow the `x-ms-continuationtoken` response header; fall back to `$top`/`$skip` where the API uses them. Never assume one page.
- API version `auto`: try the highest known version; on a version-not-supported error step down (7.1 → 7.0 → 6.0 → 5.0 → 4.1) and remember the result per source. Server 2019 ≈ 5.0, 2020 ≈ 6.0, 2022 ≈ 7.0/7.1.
- Retry with exponential backoff on 429/5xx, honour `Retry-After`. Throttle to `requests_per_second`.
- A 401/403 on one resource type is recorded as "no access" for that collector and the run continues.
- Minimum PAT scopes (read): Code, Build, Release, Project and Team, Variable Groups, Service Connections, Agent Pools, Task Groups, Deployment Groups, Environment, Secure Files, Packaging, Extensions. `azdo-analyzer doctor` must test each and print a pass/fail table.

## 8. Linkage model (the mapping)

Build a directed graph. This is the core deliverable — every report is a view of it.

```
Source(collection/org)
  └─ Project
       ├─ Repo ──(scanned)──> Component(path, stack)        # monorepo-aware
       ├─ BuildPipeline ──builds──> Repo (may be in ANOTHER project or external)
       │     ├─ uses ──> TaskGroup, VariableGroup, ServiceConnection, AgentPool, SecureFile
       │     ├─ includes ──> YAML template (same repo or another repo/project)
       │     ├─ triggered by ──> BuildPipeline (build-completion / pipeline resource)
       │     └─ validates ──> Repo+branch (branch policy)
       └─ ReleasePipeline ──consumes──> Artifact(Build | Git | Feed | Container | Jenkins | External)
             └─ Stage ──deploys to──> DeploymentTarget
                   └─ uses ──> ServiceConnection, DeploymentGroup, Environment, VariableGroup, AgentPool
```

Rules:
- **Build → repo**: from `definition.repository` (`type`: `TfsGit`, `TfsVersionControl`, `GitHub`, `GitHubEnterprise`, `Bitbucket`, `Git`, `Svn`). For YAML also every `resources.repositories` entry and `checkout:` step.
- **Release → build → repo**: for each release artifact of type `Build`, read `definitionReference.project` and `definitionReference.definition`, then follow the build to its repo. Artifacts of type `Git` link straight to a repo.
- **Cross-project links are first-class**: a build in project A on a repo in project B, or a release consuming a build from another project, must be flagged `cross_project=true`. These become multi-project pipelines or `include:project` in GitLab.
- **Pipeline → component**: in a monorepo, map a pipeline to a component using trigger path filters, `workingDirectory`, and solution/project/pom paths in task inputs.
- **Orphans**: pipelines whose repo is missing/disabled; repos with no pipeline; build pipelines with no release; releases whose artifact build no longer exists; unused task groups, variable groups, service connections.
- Every edge stores `evidence` (which field of which payload produced it).

## 9. Pipeline parsing

### 9.1 Classic build definitions (`process.type == 1`)
Extract per phase (job): name, agent queue/pool, demands, execution mode (multi-config/multi-agent → matrix/parallel), timeout, `dependencies`, condition. Per step: task GUID → resolve through task catalog to name/major version/author; `inputs`; `enabled`; `condition`; `continueOnError`; `timeoutInMinutes`. Steps with `definitionType == "metaTask"` are task groups — expand recursively and keep both the collapsed and expanded view.

Also extract: triggers (CI with branch and path filters, batch flag, PR, scheduled with cron/timezone, build completion, gated check-in), variables (name, value unless secret, `allowOverride`), variable groups, build number format, retention, `queueStatus`, folder path, tags, checkout options (clean, submodules, LFS, fetch depth).

### 9.2 YAML pipelines (`process.type == 2`)
1. Fetch `process.yamlFilename` from the repo at the definition's default branch. Missing file → flag `yaml_missing`.
2. Parse with `ruamel.yaml` configured to **allow duplicate keys** and keep `${{ ... }}` expressions as plain strings (repeated `${{ if ... }}:` keys are legal in AZDO and break strict parsers).
3. Resolve `template:` references and `extends:` up to `max_template_depth`, including templates in other repos declared under `resources.repositories` (record the cross-repo dependency; fetch if readable).
4. Extract: `trigger`, `pr`, `schedules`, `resources` (repositories, pipelines, containers, packages), `variables` (inline, groups, templates), `parameters`, `pool` / `vmImage` / `demands`, `container`, `services`, `stages` → `jobs` → `steps`, `strategy` (matrix, parallel, and for deployment jobs `runOnce` / `rolling` / `canary`), `environment`, `dependsOn`, `condition`, `timeoutInMinutes`, `continueOnError`.
5. **Timeline expansion** (`use_timeline_expansion`): because templates and conditionals hide the real shape, read the timeline of the last successful run and record the stages, jobs, and tasks (name + version) that actually executed, plus the agent pool used. Treat this as ground truth and note where it differs from static parsing.
6. Do **not** use the pipeline preview API (`POST .../pipelines/{id}/preview`) to expand YAML: it is a non-GET call and is excluded by the read-only rule. Static template resolution plus timeline expansion are the only sources.

### 9.3 Classic release definitions
Per definition: artifacts (alias, type, source project/definition, default version, primary flag), triggers (CD trigger with branch filters, schedules, PR), variables with scope (release vs stage), variable groups, release name format, folder path.

Per stage (`environments[]`): name, rank, **normalised tier** (config `environments.tiers`), trigger conditions (after release / after stage(s) / manual), artifact filters, schedules, pre- and post-deployment approvals (approver identities or groups, count, order, timeout, automated or not), pre- and post-deployment **gates** (type and settings), deployment queue settings (parallelism → GitLab `resource_group`), retention.

Per deploy phase: type — `agentBasedDeployment`, `machineGroupBasedDeployment` (deployment group + tags), `runOnServer` (agentless: manual intervention, REST call, delay) — pool, demands, parallel/multi-config, artifact download selection, tasks as in 9.1.

Produce a stage flow (which stage follows which) so it can be redrawn as GitLab stages with `needs`.

### 9.4 Normalised step
All three parsers emit the same `Step` shape so detection, mapping, and clustering work once:
`{task_name, task_major, task_author, is_marketplace, is_task_group, display_name, inputs, script_text, enabled, condition, continue_on_error, timeout, source: classic_build|yaml|release|timeline}`.

## 10. Technology detection

Two evidence sources, merged with confidence scoring. **Every detected fact carries evidence and confidence**; conflicts between sources are reported, not silently resolved.

**A. Pipeline evidence** — tasks used (`Maven`, `Gradle`, `Ant`, `DotNetCoreCLI`, `UseDotNet`, `VSBuild`, `MSBuild`, `VSTest`, `NuGetCommand`, `Npm`, `NodeTool`, `UsePythonVersion`, `Go`, `Docker`, `Xcode`, `XamarinAndroid`, ...), tool-installer version inputs, and — when `detection.script_scan` is on — regex over inline scripts (`dotnet `, `mvn `, `gradle`, `npm run`, `ng build`, `yarn`, `pip install`, `docker build`, `kubectl`, `helm`, `oc `, `terraform`, `ansible-playbook`, `aws `, `az `, `jf `/`jfrog`).

**B. Repository evidence** — list the tree once, match rule file patterns, fetch only matched manifests.

**Rule engine.** Rules are declarative YAML in `rules/`, overridable from `detection.extra_rules_dirs`. Example:

```yaml
- id: dotnet-sdk-csproj
  files: ["**/*.csproj", "**/*.vbproj", "**/*.fsproj"]
  parser: xml
  when: { xpath: "/Project[@Sdk]" }
  emit:
    language: { from_extension: { csproj: csharp, vbproj: vbnet, fsproj: fsharp } }
    runtime_family: dotnet
    runtime_version: { xpath: "//TargetFramework|//TargetFrameworks" }
    project_kind: { xpath: "/Project/@Sdk" }      # Web, Worker, BlazorWebAssembly...
    build_tool: dotnet-cli
  confidence: 0.9
- id: angular
  files: ["**/package.json"]
  parser: json
  when: { jsonpath: "$.dependencies['@angular/core']" }
  emit:
    framework: angular
    framework_version: { jsonpath: "$.dependencies['@angular/core']" }
  confidence: 0.95
```

Parsers to support: `xml` (XPath, namespace-tolerant), `json` (JSONPath), `toml`, `yaml`, `properties`, `regex`, `filename` (presence only).

**Built-in rule coverage (minimum)**

| Ecosystem | Files to read | Facts to extract |
|---|---|---|
| .NET (SDK-style) | `*.sln`, `*.csproj/.vbproj/.fsproj`, `global.json`, `Directory.Build.props`, `Directory.Packages.props`, `nuget.config` | TFM(s) (`net8.0`, `netstandard2.0`, `net48`), SDK kind (Web, Worker, Blazor, MAUI), output type, test framework (xUnit/NUnit/MSTest), key packages (EF Core, etc.), SDK pin, NuGet sources |
| .NET Framework (legacy) | old-style `*.csproj`, `packages.config`, `web.config`, `*.pubxml`, `*.nuspec` | `TargetFrameworkVersion`, `ProjectTypeGuids` (Web App, WCF, test, WPF, WinForms), MVC/WebForms/WCF, config transforms. **Sets `requires_windows=true`.** |
| .NET specials | `*.sqlproj`, `*.dtproj`, `*.rptproj`, `*.wixproj`, `*.vdproj`, `*.sfproj`, `*.btproj`, `host.json` | SSDT/dacpac, SSIS, SSRS, installer, Service Fabric, BizTalk, Azure Functions |
| Java | `pom.xml`, `build.gradle(.kts)`, `settings.gradle`, `gradle-wrapper.properties`, `.mvn/wrapper/*`, `build.xml`, `.java-version`, `.sdkmanrc` | Java version (`maven.compiler.release/source`, `java.version`, toolchain), packaging (jar/war/ear), Spring Boot / Quarkus / Micronaut + version, multi-module layout, Maven/Gradle/Ant version, repositories and `distributionManagement` |
| JavaScript / TypeScript | `package.json`, lockfiles, `.nvmrc`, `.node-version`, `angular.json`, `nx.json`, `lerna.json`, `tsconfig.json`, `vite.config.*`, `webpack.config.*`, `next.config.*`, `.npmrc` | Node version (`engines`, nvmrc), package manager (npm/yarn/pnpm + version), framework (Angular, React, Vue, Next, Nuxt, Svelte, Express, NestJS, React Native, Electron) + version, scripts (`build`, `test`, `lint`), workspaces, registry |
| Python | `pyproject.toml`, `requirements*.txt`, `setup.py/.cfg`, `Pipfile`, `poetry.lock`, `uv.lock`, `tox.ini`, `.python-version`, `runtime.txt` | Python version, build backend (setuptools/poetry/hatch/uv), framework (Django, Flask, FastAPI), test runner, index URLs |
| Go / Rust / PHP / Ruby | `go.mod`, `Cargo.toml`, `composer.json`, `Gemfile` | Language version, framework (Gin, Laravel, Symfony, Rails ...) |
| C / C++ | `CMakeLists.txt`, `*.vcxproj`, `Makefile`, `conanfile.*`, `vcpkg.json` | Toolset, platform toolset version, Windows-only flag |
| Mobile | `AndroidManifest.xml`, Android `build.gradle`, `*.xcodeproj`, `Podfile`, `pubspec.yaml`, `*.csproj` with MAUI/Xamarin | Platform, SDK levels, **`requires_macos`** for iOS |
| Containers | `Dockerfile*`, `docker-compose*.yml`, `.dockerignore` | Base images and tags, multi-stage, exposed ports |
| Kubernetes / OpenShift | `Chart.yaml`, `values*.yaml`, `kustomization.yaml`, manifests with `apiVersion`/`kind`, OpenShift `Template`/`Route`/`DeploymentConfig` | Helm vs Kustomize vs raw, OpenShift-specific kinds |
| IaC / config | `*.tf`, `*.bicep`, ARM `*.json`, `Pulumi.yaml`, Ansible playbooks/roles, `serverless.yml`, SAM `template.yaml`, `cdk.json` | Tool, providers, backend |
| Database | `*.sqlproj`, Flyway `V*__*.sql`, Liquibase changelogs | Migration tool |
| Quality / security | `sonar-project.properties`, `.snyk`, dependency-check config, `.trivyignore`, `.gitleaks.toml` | Existing scanners |
| Other CI | `Jenkinsfile`, `.gitlab-ci.yml`, `.github/workflows/*`, `bitbucket-pipelines.yml` | Prior or parallel CI definitions |
| Git features | `.gitmodules`, `.gitattributes` (LFS) | Submodules, LFS → GitLab `GIT_SUBMODULE_STRATEGY`, LFS handling |

**Output per repo**: a list of **components** (path, language, runtime family + version, framework + version, build tool + version, package manager, test tooling, packaging/output type, container build yes/no), plus repo-level flags (`monorepo`, `polyglot`, `requires_windows`, `requires_macos`, `has_dockerfile`, `has_helm`, `has_iac`). Handle any combination — a repo may be .NET API + Angular UI + Dockerfile + Helm + Terraform; each is its own component.

**Version precedence** (highest first): pipeline tool-installer input → pin files (`global.json`, `.nvmrc`, `.python-version`, wrapper properties) → manifest declaration → agent capability / hosted image default (mark `inferred`).

## 11. Deployment target detection

Derive per release stage and per YAML deployment job, from tasks, service-connection types, deployment groups, and environment resources.

| Signal | Target |
|---|---|
| `AzureWebApp`, `AzureRmWebAppDeployment`, `AzureWebAppContainer` | Azure App Service |
| `AzureFunctionApp`, `AzureFunctionAppContainer` | Azure Functions |
| `Kubernetes`, `KubernetesManifest`, `HelmDeploy`, `Kubectl`; kubernetes service connection; environment with Kubernetes resource | Kubernetes / AKS |
| `oc` in scripts, OpenShift extension tasks, OpenShift service connection | OpenShift |
| `IISWebAppDeploymentOnMachineGroup`, `IISWebAppManageOnMachineGroup`, `WindowsMachineFileCopy`, `PowerShellOnTargetMachines`; deployment-group phase | IIS / Windows VM |
| `SSH`, `CopyFilesOverSSH`; ssh service connection | Linux VM |
| `SqlAzureDacpacDeployment`, `SqlDacpacDeploymentOnMachineGroup`, `sqlpackage`, Flyway/Liquibase | Database |
| `AzureResourceManagerTemplateDeployment`, `AzureCLI`, `AzurePowerShell`, Terraform tasks | Azure infrastructure / IaC |
| AWS Toolkit tasks (`AWSCLI`, `S3Upload`, `LambdaDeployFunction`, `ECRPushImage`, `CloudFormationCreateOrUpdateStack`), `aws` in scripts, AWS service connection | AWS (Lambda, ECS, ECR, S3, CloudFormation) |
| `Docker` push, `ECRPushImage`; registry service connection | Container registry (ACR / ECR / Artifactory / Docker Hub) |
| `NuGetCommand push`, `Npm publish`, `Maven deploy`, `TwineAuthenticate`, `UniversalPackages`, JFrog/Artifactory tasks | Package repository (Azure Artifacts / Artifactory / Nexus) |
| `AzureFileCopy`, `AzureBlob` | Azure Storage / static site |
| `ServiceFabricDeploy` | Service Fabric |
| `AppCenterDistribute`, `GooglePlayRelease`, `AppStoreRelease` | Mobile distribution |
| `Ansible` task, `ansible-playbook` in scripts | Ansible-managed hosts |

Record per stage: target type, target identifier where visible (app name, namespace, cluster, host group — subject to privacy config), service connection, credential type, strategy (rolling/canary/blue-green/slot swap), config transformation in use (`FileTransform`, XML/JSON variable substitution, `replacetokens`) — these need an explicit replacement in GitLab.

## 12. Activity and health

All thresholds from `activity` and `history` config.

| Status | Rule |
|---|---|
| `active` | last run within `active_within_days` |
| `dormant` | last run between `active_within_days` and `dormant_within_days` |
| `stale` | last run older than `dormant_within_days` |
| `never_run` | no runs at all |
| `disabled` | `queueStatus` disabled/paused, or release definition disabled |
| `failing` | last `failing_last_n_runs` runs all failed |
| `orphaned` | repo or artifact source missing or disabled |

Per pipeline metrics: last run date, result, branch, requester; runs in lookback window and runs/month; success rate; median and p90 duration; distinct branches built; trigger mix (CI / PR / schedule / manual). Per release: last deployment and last *successful* deployment per stage, deployments/month, time since last production deployment. Per repo: last commit date, default branch, size. Per project: roll-up counts by status, and a derived `migration_candidate` flag (has at least one active pipeline or release).

## 13. GitLab mapping (primary purpose of the reports)

### 13.1 Concept mapping (seed for `mappings/concepts.yaml`)

| Azure DevOps | GitLab CI/CD |
|---|---|
| Stage / job / step | `stages` / job / `script` lines |
| Classic phase with demands; agent pool | Job with runner `tags` |
| `vmImage` (hosted) | `image:` on a Linux runner; Windows/macOS need matching runners |
| `container:` job, `services:` | `image:`, `services:` |
| Task group; YAML template; `extends` | `include:` (`project`/`local`/`component`), `extends`, CI/CD components |
| `resources.repositories` templates | `include:project` with pinned `ref` |
| Variables; variable groups | CI/CD variables at project/group level (masked, protected), environment-scoped |
| Key Vault-linked variable group; `AzureKeyVault` task | External secrets (`secrets:` with Vault / Azure Key Vault / AWS Secrets Manager) or ID-token-based fetch |
| Secure files | Project-level Secure Files or file-type variables |
| Service connection (cloud) | OIDC with `id_tokens:` where possible, else masked variables |
| Service connection (Kubernetes) | GitLab agent for Kubernetes or kubeconfig variable |
| CI trigger branch filters | `workflow:rules`, `rules:if` on `$CI_COMMIT_BRANCH` |
| Path filters | `rules:changes` |
| PR trigger; build-validation branch policy | Merge request pipelines + "Pipelines must succeed" |
| Scheduled trigger | Pipeline schedules |
| Build-completion trigger; pipeline resource | Multi-project pipeline (`trigger:project`), `needs:project` |
| `dependsOn`, stage conditions | `needs`, `rules` |
| `condition`, `continueOnError`, `timeoutInMinutes`, retry | `rules`/`when`, `allow_failure`, `timeout`, `retry` |
| Multi-config / matrix | `parallel:matrix` |
| Batch CI / auto-cancel | `interruptible` + auto-cancel redundant pipelines |
| Publish/download build artifacts | `artifacts:paths`, `needs:artifacts`, `expire_in` |
| Azure Artifacts feed | Artifactory / Nexus / GitLab package registry (per config) |
| `PublishTestResults` | `artifacts:reports:junit` (TRX must be converted) |
| `PublishCodeCoverageResults` | `coverage:` regex + `artifacts:reports:coverage_report` |
| Release stage / YAML environment | `environment:` with `deployment_tier` |
| Pre-deployment approval; environment checks | Protected environments + deployment approvals; `when: manual` |
| Gates | Manual job, external status checks, or a scripted check job |
| Deployment queue settings; exclusive lock | `resource_group` |
| Deployment group (agents on target VMs) | **No direct equivalent** — runner on target with env tags, or WinRM/SSH/Ansible from a runner. Flag as design decision. |
| Agentless: manual intervention / delay / REST call | `when: manual` / `when: delayed` + `start_in` / `curl` job |
| Release variables scoped to stage | Environment-scoped CI/CD variables |
| Service hooks | Webhooks / integrations |
| Retention policy | `artifacts:expire_in`, pipeline/artifact retention settings |

### 13.2 Task mapping (`mappings/tasks.yaml`)
One entry per AZDO task: `{task, versions, gitlab: {approach, image, script_hint, keywords, notes}, runner_os, confidence}`. Ship a seed covering at least the tasks named in sections 10, 11 and 13.1, plus: `CopyFiles`, `ArchiveFiles`, `ExtractFiles`, `CmdLine`, `Bash`, `PowerShell`, `BatchScript`, `PythonScript`, `DownloadSecureFile`, `InstallSSHKey`, `NuGetAuthenticate`, `npmAuthenticate`, `MavenAuthenticate`, `PipAuthenticate`, `SonarQubePrepare/Analyze/Publish`, `FileTransform`, `ManualIntervention`, `ManualValidation`, `InvokeRESTAPI`, `Delay`, Terraform tasks, JFrog tasks.

Any task used anywhere in the estate but absent from the mapping file is listed in the **Unmapped tasks** report with usage count, author, and marketplace flag — this is the backlog for extending the mapping. Custom/in-house extension tasks are always flagged.

### 13.3 Runner and image requirements
Per pipeline: required OS (Linux / Windows / macOS), reason (e.g. `.NET Framework 4.8 + VSBuild`), tools and versions needed, suggested image (prefixed with `gitlab.image_registry_prefix`), suggested tags from `gitlab.runner_tags`. For self-hosted pools, summarise agent capabilities (Visual Studio version, MSBuild, JDKs, Node, etc.) so equivalent runners can be planned. Do not assume hosted Windows/macOS runners exist on the target — report the requirement and leave provisioning as a decision.

### 13.4 Shared-template fit
Match each pipeline's detected stack and deploy target against `gitlab.template_catalog`. Report: matching template(s), what the template does not cover (extra tasks left over after the match), or `no template — gap`. Roll up gaps by stack so missing templates can be prioritised.

### 13.5 Pattern clustering (template candidates)
Fingerprint each pipeline: stack signature + set of normalised tasks (minus `clustering.ignore_tasks`). Cluster within the same stack by Jaccard similarity ≥ threshold. Output clusters with member count, representative pipeline, common tasks, and per-member deviations. Purpose: build one GitLab template per cluster instead of one pipeline per AZDO definition.

### 13.6 Security posture
Detect existing scanners in pipelines (SonarQube, Mend/WhiteSource, Checkmarx, Fortify, Veracode, Snyk, Black Duck, OWASP Dependency-Check, ZAP, Trivy, CredScan, gitleaks). For each pipeline compare against `gitlab.security_baseline` and report: scanner in use → GitLab equivalent (SAST, Secret Detection, Dependency Scanning, Container Scanning, DAST, IaC Scanning), and baseline items with no coverage today. Also flag: secrets in plain (non-secret) variables, inline credentials in scripts (pattern match, redacted in output), service connections shared across many projects, pipelines deploying to prod without approval.

### 13.7 Complexity and effort
Score each pipeline/release with `complexity.weights`, band into S/M/L/XL, and list the contributing factors. Aggregate per project. Suggest **migration waves**: order by active status, low complexity first, and dependency order (template providers and upstream pipelines before consumers).

### 13.8 Draft skeletons (optional, Phase 8)
When `gitlab.generate_skeletons` is true, write `out/skeletons/<source>/<project>/<pipeline>.gitlab-ci.yml` with stages, jobs, tags, image, `rules`, `needs`, `environment`, variables placeholders, template `include` where matched, and a `# TODO(azdo:<task>)` comment for every unmapped step. First line: `# DRAFT generated by azdo-analyzer — review before use`.

## 14. Reports

All under `output_dir`. Every table also exported as CSV; the full model as `model.json`.

### 14.1 `inventory.xlsx` — one sheet per view

| Sheet id | Grain | Key columns |
|---|---|---|
| `summary` | estate | counts by source, status, stack, deploy target, effort band |
| `projects` | project | source, project, source-control type, repos, build defs, release defs, active/dormant/stale counts, last activity, stacks, deploy targets, effort total, migration candidate, proposed GitLab path |
| `repos` | repo | project, type (Git/TFVC), default branch, size, last commit, disabled, components, flags, pipelines attached |
| `components` | component | repo, path, language, runtime + version, framework + version, build tool, package manager, test tooling, packaging, confidence, evidence |
| `build_pipelines` | build def | project, folder, name, type (classic/YAML/XAML), repo (+project), YAML path, status, last run + result, runs/month, success %, median duration, triggers, pool/OS, stacks, task count, template match, effort band, URL |
| `release_pipelines` | release def | project, name, artifacts, source builds, stage count, stage flow, status, last release, last prod deploy, deploy targets, approvals, gates, effort band, URL |
| `release_stages` | stage | release, stage, tier, trigger, approvers, gates, phase types, pool/deployment group, target type, service connections, last deployment + status |
| `mapping` | edge | **project ↔ repo ↔ build ↔ release ↔ stage ↔ target**, one row per path, with cross-project flag |
| `tasks_usage` | task | task, major version, author, marketplace, used in N builds / N releases, mapped (y/n), GitLab approach |
| `unmapped_tasks` | task | as above for unmapped only, with example pipelines |
| `task_groups` | task group | project, name, tasks inside, used by, unused flag |
| `variable_groups` | group | project, name, type (plain/Key Vault), variable names, secret count, used by |
| `variables` | variable | scope (definition/stage/group), name, is secret, value (redacted/truncated), overridable |
| `service_connections` | connection | project, name, type, auth scheme, shared, used by, GitLab replacement |
| `agent_pools` | pool | name, hosted/self-hosted, agents, OS, key capabilities, used by N pipelines |
| `deployment_targets` | target | type, identifier, tier, releases/stages deploying to it |
| `environments` | environment / deployment group | project, name, resources/targets, checks, used by |
| `triggers` | trigger | pipeline, type, branches, paths, schedule, upstream pipeline |
| `branch_policies` | policy | repo, branch, build-validation pipeline, blocking |
| `cross_project_deps` | dependency | from, to, kind (repo / template / artifact / trigger) |
| `template_fit` | pipeline | matched template, uncovered tasks, gap reason |
| `clusters` | cluster | stack, members, common tasks, representative |
| `security` | pipeline | scanners present, baseline gaps, findings |
| `runner_requirements` | pipeline | OS, tools + versions, suggested image, tags |
| `effort` | pipeline | score, band, factors |
| `orphans` | object | kind, reason |
| `extensions` | extension | publisher, name, version, tasks contributed, used (y/n) |
| `errors` | error | source, project, collector, message |

### 14.2 `summary.md` and `summary.html`
Executive view: estate totals, active vs inactive, stack distribution, deploy-target distribution, classic vs YAML split, effort by band, top unmapped tasks, template gaps, risks/blockers (TFVC, XAML, deployment groups, Windows-only, custom extensions), suggested waves. HTML is a single self-contained file.

### 14.3 Per-project summary — `projects/<source>/<project>.md`
Overview, repos and components, build pipelines table, release pipelines table with stage flow, a Mermaid dependency graph, shared objects (variable groups, service connections, task groups), runner needs, security posture, effort, open questions.

### 14.4 Per-pipeline brief — `briefs/<source>/<project>/<pipeline>.md` (+ `.json`)
This is the document to hand to an engineer (or to Claude Code) when writing the GitLab pipeline. Template:

````markdown
# <pipeline name>  (<classic build | YAML | release>)
Source: <collection>/<project>  |  Status: <active>  |  Effort: <M (9)>  |  AZDO link: <url>

## Linkage
Repo: <project>/<repo> @ <branch>  |  Component path: <src/Api>
Feeds release(s): <names>  |  Triggered by / triggers: <pipelines>

## Stack
| Component | Language | Runtime | Framework | Build tool | Package manager | Tests |
Evidence and confidence per fact.

## Current flow
Stage/job/step table: task, version, key inputs, condition. Task groups shown expanded.

## Triggers
CI branches, path filters, PR, schedules, upstream pipelines, build-validation policies.

## Variables and secrets to recreate
Name, scope, secret (y/n), source (inline / group / Key Vault). Values redacted.

## Credentials and connections
Service connection → type → suggested GitLab replacement.

## Deployment (release / deployment jobs)
Stage → tier → target → strategy → approvals → gates → config transforms.

## GitLab build plan
Runner OS and tags, image, matching template + include line, proposed stages and jobs,
step-by-step mapping table (AZDO task → GitLab approach), `rules`, `needs`, `environment`.

## Gaps and decisions
Unmapped tasks, deployment-group replacement, Windows runner need, security baseline gaps.
````

## 15. Data model (pydantic)

`Source`, `Project`, `Repo`, `Component`, `BuildPipeline`, `ReleasePipeline`, `Stage`, `Job`, `Step`, `Trigger`, `Variable`, `VariableGroup`, `TaskGroup`, `TaskDefinition`, `ServiceConnection`, `AgentPool`, `Agent`, `DeploymentGroup`, `Environment`, `SecureFile`, `Artifact`, `DeploymentTarget`, `RunStats`, `Detection` (fact, value, confidence, evidence), `Edge` (from, to, kind, evidence), `GitLabPlan`, `EffortScore`, `Cluster`, `Finding`, `CollectorError`.

Stable ids: `<source>/<project>/<kind>/<azdo id>`. `model.json` carries a `schema_version`.

## 16. CLI

```
azdo-analyzer doctor   -c config.yaml     # connectivity, api version, PAT scope check per source
azdo-analyzer collect  -c config.yaml [--source X] [--project Y] [--refresh]
azdo-analyzer analyze  -c config.yaml     # offline: normalize, link, detect, score, map
azdo-analyzer report   -c config.yaml [--format xlsx,markdown]
azdo-analyzer run      -c config.yaml     # collect + analyze + report
azdo-analyzer brief    -c config.yaml --pipeline "<source>/<project>/<name>"
azdo-analyzer config schema | validate -c config.yaml
```

Exit code non-zero only on config errors, total auth failure, or when `fail_on_project_error` is set.

## 17. Non-functional requirements

- **Read-only.** Enforce in the HTTP client: the client exposes `GET` only and raises on any other method. Add a test that asserts this. There is no config switch to relax it.
- **Secrets.** PATs only from env vars; never logged, never cached. Secret variable values are not returned by AZDO — report names only. Apply `privacy.redact_value_patterns` to everything written to reports. The cache contains raw payloads (identities, URLs, non-secret values): document that `.cache/` and `out/` are sensitive and add both to `.gitignore`.
- **Scale.** Thousands of definitions: bounded concurrency, streaming writes, no full-estate in memory during collect.
- **Resumable.** Re-running `collect` skips fresh cache entries; `--refresh` forces.
- **Deterministic.** Same cache + same config → byte-identical JSON/CSV (sorted keys, stable ordering).
- **Observability.** Progress per source/project, structured log file, end-of-run summary with API call count and errors.
- **Portable.** Runs on Windows and Linux; no shell-outs.

## 18. Edge cases to handle explicitly

- YAML pipeline whose file is missing on the default branch, or definition pointing at a deleted/disabled repo.
- Classic build on GitHub / Bitbucket / external Git / SVN — cannot scan repo; fall back to pipeline evidence only and say so.
- TFVC: no YAML pipelines; workspace mappings on the definition identify the built path; flag `tfvc_source`.
- XAML builds: inventory and flag as legacy; no parsing.
- Task groups nested in task groups; task group versions; draft task groups.
- Same repo built by many pipelines (monorepo) and one pipeline checking out several repos.
- Release with multiple artifacts from different projects/collections, or non-build artifacts (container image, package feed, Jenkins).
- Draft definitions, definition folders, renamed/deleted definitions still referenced by releases.
- Disabled steps (report but exclude from mapping and fingerprint).
- Runtime expressions `$(var)`, `$[ ... ]`, and `${{ ... }}` in inputs — keep verbatim, list variables referenced.
- Pipelines using marketplace tasks whose extension is no longer installed.
- Large repos exceeding `max_tree_entries` — partial scan, flagged.
- Projects in `wellFormed` vs `deleting`/`new` state.
- Clock/timezone on scheduled triggers.

## 19. Build phases and acceptance criteria

Implement in order. Each phase ends with passing tests and a short note of what was built and what was assumed.

| Phase | Deliver | Done when |
|---|---|---|
| 0. Scaffold | Package, CLI skeleton, config models + loader + schema export, `config.example.yaml` | `config validate` accepts the example in section 6 and rejects bad configs with clear messages |
| 1. Client + core collectors | HTTP client (auth, paging, retry, version negotiation, read-only guard), cache, collectors for projects, repos, build defs, release defs, task catalog; `doctor` | Fixture-driven tests cover paging, 429 retry, version step-down, per-collector 403; `collect` fills cache from mocked API |
| 2. Remaining collectors | Runs, deployments, timelines, task groups, variable groups, service connections, pools, queues, deployment groups, environments, secure files, extensions, policies, hooks, feeds | All toggles in `collectors` honoured; errors land in `errors` table |
| 3. Normalize + link | Data model, normalizers, linkage graph, orphans, cross-project edges | Given fixtures, `mapping` rows are correct for: same-project, cross-project build, release with two artifacts, Git artifact |
| 4. Parsers | Classic build, YAML (templates, duplicate `${{ if }}` keys, cross-repo templates), release, unified `Step`, timeline expansion | Golden-file tests for each parser; task groups expand recursively |
| 5. Detection | Rule engine, built-in rules (section 10), deploy-target rules (section 11), components, version precedence | Fixture repos for .NET Framework, .NET 8 + Angular, Spring Boot Maven, Gradle, Node/React, Python, Docker+Helm, Terraform, polyglot monorepo all detect correctly with evidence |
| 6. Analysis | Activity, metrics, complexity, clustering, security posture, runner requirements, template fit | Thresholds and weights change results when config changes; no constants in code |
| 7. Reports | Excel, CSV, JSON, Markdown (summary, per-project, per-pipeline brief), HTML, Mermaid | All sheets in 14.1 present; outputs deterministic; privacy settings applied |
| 8. GitLab mapping extras | Seed `mappings/*.yaml`, unmapped-task backlog, migration waves, optional draft skeletons | Skeletons are valid YAML and every unmapped step has a TODO |

## 20. Working rules for Claude Code

- Read this whole file before writing code. If a requirement is ambiguous, ask; record the answer in `docs/decisions.md`.
- **ONLY READ OPERATIONS allowed in this project.** Never add a non-GET call to AZDO or any external system, even behind a flag.
- Never call a real AZDO instance from tests. Never print or persist a PAT.
- Config first: any new threshold, pattern, or mapping goes in the config or a `rules/`/`mappings/` YAML file, with a default and a line in `config.example.yaml`.
- Keep raw payload handling tolerant: unknown fields are ignored, missing fields do not crash (older Server versions omit many).
- When an endpoint or field in section 7 does not match the documentation for the target API version, follow the documentation and note the deviation in `docs/decisions.md`.
- Add fixtures for every new payload shape; prefer golden-file tests for parsers and reports.
- Commands: `pip install -e ".[dev]"`, `pytest -q`, `ruff check .`, `mypy src`. All must pass before a phase is called done.
- Keep `README.md` current: install, PAT scopes, quick start, config reference, report catalogue.
