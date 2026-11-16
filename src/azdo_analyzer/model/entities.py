"""Normalized data model (spec section 15). Stable ids: ``<source>/<project>/<kind>/<azdo id>``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .. import SCHEMA_VERSION


class Entity(BaseModel):
    model_config = ConfigDict(extra="ignore")


def make_id(source: str, project: str, kind: str, azdo_id: Any) -> str:
    return f"{source}/{project}/{kind}/{azdo_id}"


class Detection(Entity):
    fact: str
    value: str
    confidence: float
    evidence: str
    source: Literal["repo", "pipeline", "agent"] = "repo"
    rule_id: str = ""
    context: str = ""  # runtime family the fact belongs to (pipeline evidence)
    precedence: int = 1  # 3 tool-installer input, 2 pin file, 1 manifest, 0 inferred
    inferred: bool = False


StepSource = Literal["classic_build", "yaml", "release", "timeline"]


class Step(Entity):
    task_name: str = ""
    task_id: str = ""
    task_major: int | None = None
    task_author: str = ""
    is_marketplace: bool = False
    is_custom: bool = False
    in_catalog: bool = True
    is_task_group: bool = False
    task_group_id: str = ""
    display_name: str = ""
    inputs: dict[str, str] = {}
    script_text: str = ""
    script_lines: int = 0
    enabled: bool = True
    condition: str = ""
    continue_on_error: bool = False
    timeout: int | None = None
    source: StepSource = "classic_build"
    template: str = ""
    variables_referenced: list[str] = []
    has_inline_credential: bool = False
    children: list[Step] = []  # expanded task group


class Job(Entity):
    name: str = ""
    display_name: str = ""
    kind: Literal["agent", "server", "deployment_group", "deployment"] = "agent"
    pool: str = ""
    pool_is_hosted: bool = False
    vm_image: str = ""
    demands: list[str] = []
    container: str = ""
    services: list[str] = []
    strategy: str = ""  # matrix | parallel | runOnce | rolling | canary | multi-config ...
    matrix: list[str] = []
    depends_on: list[str] = []
    condition: str = ""
    timeout: int | None = None
    environment: str = ""
    environment_resource: str = ""
    deployment_group: str = ""
    deployment_group_tags: list[str] = []
    skip_artifact_download: bool = False
    template: str = ""
    steps: list[Step] = []


class Approval(Entity):
    approvers: list[str] = []
    automated: bool = True
    required_count: int | None = None
    order: str = ""
    timeout_minutes: int | None = None


class Gate(Entity):
    tasks: list[str] = []
    timeout_minutes: int | None = None
    sampling_minutes: int | None = None


class DeploymentTarget(Entity):
    type: str
    label: str = ""
    identifier: str = ""
    service_connection: str = ""
    credential_type: str = ""
    strategy: str = ""
    evidence: str = ""


class StageStats(Entity):
    last_deployment: str = ""
    last_status: str = ""
    last_success: str = ""
    deployments: int = 0


class Variable(Entity):
    scope: Literal["definition", "stage", "group"] = "definition"
    owner: str = ""  # id of the pipeline / stage / group
    name: str
    value: str = ""
    is_secret: bool = False
    allow_override: bool = False
    source: Literal["inline", "group", "keyvault", "template"] = "inline"
    suspected_secret: bool = False


class Stage(Entity):
    name: str = ""
    display_name: str = ""
    rank: int = 0
    tier: str = ""
    depends_on: list[str] = []
    condition: str = ""
    trigger: str = ""  # after_release | after_stage | manual | scheduled
    artifact_filters: list[str] = []
    schedules: list[str] = []
    pre_approval: Approval | None = None
    post_approval: Approval | None = None
    pre_gates: Gate | None = None
    post_gates: Gate | None = None
    parallelism: str = ""
    retention: str = ""
    variables: list[Variable] = []
    variable_group_ids: list[str] = []
    jobs: list[Job] = []
    targets: list[DeploymentTarget] = []
    service_connection_ids: list[str] = []
    config_transforms: list[str] = []
    stats: StageStats = StageStats()
    template: str = ""


class Trigger(Entity):
    type: str  # ci | pr | schedule | build_completion | gated | artifact | pipeline_resource | manual
    branches_include: list[str] = []
    branches_exclude: list[str] = []
    paths_include: list[str] = []
    paths_exclude: list[str] = []
    batch: bool = False
    cron: str = ""
    timezone: str = ""
    upstream: str = ""
    detail: str = ""


class VariableGroup(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    type: str = "Vsts"
    key_vault: str = ""
    variables: list[Variable] = []
    secret_count: int = 0
    used_by: list[str] = []


class TaskGroup(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    version: str = ""
    is_draft: bool = False
    task_names: list[str] = []
    used_by: list[str] = []


class TaskDefinition(Entity):
    id: str
    source: str
    name: str
    friendly_name: str = ""
    majors: list[int] = []
    author: str = ""
    contribution_id: str = ""
    origin: Literal["builtin", "extension", "custom"] = "builtin"


class ServiceConnection(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    type: str = ""
    url: str = ""
    auth_scheme: str = ""
    is_shared: bool = False
    shared_projects: list[str] = []
    used_by: list[str] = []
    gitlab_replacement: str = ""


class Agent(Entity):
    name: str
    os: str = ""
    version: str = ""
    status: str = ""
    enabled: bool = True
    capabilities: dict[str, str] = {}


class AgentPool(Entity):
    id: str
    source: str
    azdo_id: str
    name: str
    is_hosted: bool = False
    pool_type: str = ""
    agents: list[Agent] = []
    os: list[str] = []
    key_capabilities: dict[str, str] = {}
    used_by: list[str] = []


class AgentQueue(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    pool_id: str = ""
    pool_name: str = ""


class DeploymentGroupTarget(Entity):
    name: str
    tags: list[str] = []
    os: str = ""
    online: bool = False


class DeploymentGroup(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    pool_name: str = ""
    targets: list[DeploymentGroupTarget] = []
    tags: list[str] = []
    used_by: list[str] = []


class EnvironmentResource(Entity):
    name: str
    type: str = ""


class Environment(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    resources: list[EnvironmentResource] = []
    checks: list[str] = []
    used_by: list[str] = []


class SecureFile(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    used_by: list[str] = []


class Extension(Entity):
    id: str
    source: str
    publisher: str
    name: str
    version: str = ""
    builtin: bool = False
    disabled: bool = False
    tasks: list[str] = []
    used: bool = False


class BranchPolicy(Entity):
    id: str
    source: str
    project: str
    repo_id: str = ""
    repo_name: str = ""
    branch: str = ""
    match_kind: str = ""
    build_id: str = ""
    build_name: str = ""
    blocking: bool = False
    enabled: bool = True
    path_filters: list[str] = []


class ServiceHook(Entity):
    id: str
    source: str
    project: str = ""
    event_type: str = ""
    consumer: str = ""
    action: str = ""
    target: str = ""
    status: str = ""


class Feed(Entity):
    id: str
    source: str
    project: str = ""
    name: str
    upstreams: list[str] = []


class Artifact(Entity):
    alias: str = ""
    type: str = ""
    source_project: str = ""
    source_definition_id: str = ""
    source_definition_name: str = ""
    default_version: str = ""
    branch: str = ""
    is_primary: bool = False
    build_id: str = ""  # resolved stable id of the build pipeline
    repo_id: str = ""  # resolved stable id of the repo (Git artifacts, or via the build)
    cross_project: bool = False
    missing: bool = False


class RunStats(Entity):
    status: str = "never_run"
    statuses: list[str] = []  # every matching status (e.g. active + failing)
    last_run: str = ""
    last_result: str = ""
    last_branch: str = ""
    last_requester: str = ""
    runs_in_window: int = 0
    runs_per_month: float = 0
    success_rate: float | None = None
    median_duration_s: float | None = None
    p90_duration_s: float | None = None
    distinct_branches: int = 0
    trigger_mix: dict[str, int] = {}
    low_success: bool = False
    last_prod_deployment: str = ""


class StepMapping(Entity):
    task: str
    major: int | None = None
    display_name: str = ""
    mapped: bool = False
    approach: str = ""
    image: str = ""
    script_hint: str = ""
    keywords: list[str] = []
    notes: str = ""
    runner_os: str = "any"
    stage: str = ""
    job: str = ""


class GitLabPlan(Entity):
    runner_os: str = "linux"
    runner_reasons: list[str] = []
    tools: list[str] = []
    image: str = ""
    tags: list[str] = []
    templates: list[str] = []
    includes: list[dict[str, str]] = []
    uncovered_tasks: list[str] = []
    gap_reason: str = ""
    stages: list[str] = []
    step_mappings: list[StepMapping] = []
    unmapped_tasks: list[str] = []
    notes: list[str] = []


class EffortScore(Entity):
    score: float = 0
    band: str = ""
    factors: list[str] = []


class SecurityPosture(Entity):
    scanners: list[str] = []
    gitlab_equivalents: list[str] = []
    baseline_gaps: list[str] = []
    findings: list[str] = []


class TimelineView(Entity):
    build_id: str = ""
    stages: list[str] = []
    jobs: list[str] = []
    tasks: list[str] = []
    pool: str = ""
    only_in_timeline: list[str] = []
    only_in_static: list[str] = []


class Component(Entity):
    id: str
    repo_id: str
    path: str
    group: str = ""
    language: str = ""
    runtime_family: str = ""
    runtime_version: str = ""
    framework: str = ""
    framework_version: str = ""
    build_tool: str = ""
    build_tool_version: str = ""
    package_manager: str = ""
    test_tooling: list[str] = []
    packaging: str = ""
    project_kind: str = ""
    container_build: bool = False
    confidence: float = 0
    facts: dict[str, str] = {}
    conflicts: list[str] = []
    detections: list[Detection] = []


class Repo(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    type: Literal["git", "tfvc", "external"] = "git"
    external_type: str = ""
    url: str = ""
    default_branch: str = ""
    size_kb: int | None = None
    last_commit: str = ""
    disabled: bool = False
    is_fork: bool = False
    scan_status: str = "not_scanned"  # scanned | partial | not_scanned | empty | external
    components: list[Component] = []
    flags: list[str] = []
    facts: dict[str, str] = {}
    detections: list[Detection] = []
    pipelines: list[str] = []


class PipelineBase(Entity):
    id: str
    source: str
    project: str
    azdo_id: str
    name: str
    folder: str = ""
    url: str = ""
    stages: list[Stage] = []
    triggers: list[Trigger] = []
    variables: list[Variable] = []
    variable_group_ids: list[str] = []
    task_group_ids: list[str] = []
    service_connection_ids: list[str] = []
    secure_file_ids: list[str] = []
    pool_ids: list[str] = []
    deployment_group_ids: list[str] = []
    environment_ids: list[str] = []
    flags: list[str] = []
    stats: RunStats = RunStats()
    detections: list[Detection] = []
    stack: list[str] = []
    stack_signature: str = "unknown"
    deploy_targets: list[str] = []
    plan: GitLabPlan = GitLabPlan()
    effort: EffortScore = EffortScore()
    security: SecurityPosture = SecurityPosture()
    cluster_id: str = ""
    wave: str = ""
    cross_project: bool = False


class BuildPipeline(PipelineBase):
    kind: Literal["classic", "yaml", "xaml"] = "classic"
    repo_id: str = ""
    repo_type: str = ""
    repo_name: str = ""
    repo_project: str = ""
    default_branch: str = ""
    yaml_path: str = ""
    queue_status: str = "enabled"
    is_draft: bool = False
    build_number_format: str = ""
    retention: str = ""
    tags: list[str] = []
    checkout: dict[str, str] = {}
    tfvc_mappings: list[str] = []
    resource_repos: list[str] = []  # extra repos (resources.repositories / checkout)
    templates: list[str] = []  # template files included (repo-qualified)
    resources: dict[str, list[str]] = {}  # containers, packages, pipelines, parameters
    component_ids: list[str] = []
    timeline: TimelineView | None = None
    timeline_steps: list[Step] = []
    release_ids: list[str] = []
    upstream_ids: list[str] = []
    downstream_ids: list[str] = []


class ReleasePipeline(PipelineBase):
    kind: Literal["release"] = "release"
    artifacts: list[Artifact] = []
    release_name_format: str = ""
    is_disabled: bool = False
    is_deleted: bool = False
    stage_flow: list[str] = []  # "Dev -> QA"
    build_ids: list[str] = []
    repo_ids: list[str] = []


class Project(Entity):
    id: str
    source: str
    azdo_id: str
    name: str
    state: str = ""
    source_control: list[str] = []
    process_template: str = ""
    included: bool = True
    gitlab_path: str = ""
    migration_candidate: bool = False
    status_counts: dict[str, int] = {}
    last_activity: str = ""


class Source(Entity):
    name: str
    type: str
    base_url: str
    api_version: str = ""
    collected_at: str = ""


class Edge(Entity):
    from_id: str
    to_id: str
    kind: str
    evidence: str = ""
    cross_project: bool = False


class MappingRow(Entity):
    source: str
    project: str
    repo: str = ""
    repo_project: str = ""
    build: str = ""
    build_project: str = ""
    release: str = ""
    stage: str = ""
    tier: str = ""
    target: str = ""
    cross_project: bool = False
    repo_id: str = ""
    build_id: str = ""
    release_id: str = ""


class Orphan(Entity):
    object_id: str
    kind: str
    name: str
    project: str
    reason: str


class CrossProjectDep(Entity):
    from_id: str
    to_id: str
    kind: str  # repo | template | artifact | trigger
    evidence: str = ""


class Cluster(Entity):
    id: str
    stack: str
    members: list[str] = []
    representative: str = ""
    common_tasks: list[str] = []
    deviations: dict[str, list[str]] = {}


class Finding(Entity):
    object_id: str
    kind: str
    severity: Literal["info", "low", "medium", "high"] = "medium"
    message: str


class CollectorError(Entity):
    source: str
    project: str = ""
    collector: str = ""
    message: str = ""


class Wave(Entity):
    name: str
    pipelines: list[str] = []


class Estate(Entity):
    schema_version: int = SCHEMA_VERSION
    as_of: str = ""
    sources: list[Source] = []
    projects: list[Project] = []
    repos: list[Repo] = []
    builds: list[BuildPipeline] = []
    releases: list[ReleasePipeline] = []
    variable_groups: list[VariableGroup] = []
    task_groups: list[TaskGroup] = []
    task_definitions: list[TaskDefinition] = []
    service_connections: list[ServiceConnection] = []
    agent_pools: list[AgentPool] = []
    agent_queues: list[AgentQueue] = []
    deployment_groups: list[DeploymentGroup] = []
    environments: list[Environment] = []
    secure_files: list[SecureFile] = []
    extensions: list[Extension] = []
    branch_policies: list[BranchPolicy] = []
    service_hooks: list[ServiceHook] = []
    feeds: list[Feed] = []
    edges: list[Edge] = []
    mapping: list[MappingRow] = []
    orphans: list[Orphan] = []
    cross_project_deps: list[CrossProjectDep] = []
    clusters: list[Cluster] = []
    findings: list[Finding] = []
    waves: list[Wave] = []
    errors: list[CollectorError] = []

    def pipelines(self) -> list[BuildPipeline | ReleasePipeline]:
        return [*self.builds, *self.releases]


def iter_steps(steps: list[Step], *, expand: bool = True, enabled_only: bool = True) -> list[Step]:
    """Flatten steps; task groups are replaced by their children when ``expand`` is set."""
    out: list[Step] = []
    for step in steps:
        if enabled_only and not step.enabled:
            continue
        if step.is_task_group and expand and step.children:
            out.extend(iter_steps(step.children, expand=True, enabled_only=enabled_only))
        else:
            out.append(step)
    return out


def pipeline_steps(pipeline: BuildPipeline | ReleasePipeline, *, expand: bool = True, enabled_only: bool = True) -> list[Step]:
    """Every step of a pipeline (all stages and jobs), plus timeline-only steps for builds."""
    out: list[Step] = []
    for stage in pipeline.stages:
        for job in stage.jobs:
            out.extend(iter_steps(job.steps, expand=expand, enabled_only=enabled_only))
    if isinstance(pipeline, BuildPipeline):
        out.extend(pipeline.timeline_steps)
    return out
