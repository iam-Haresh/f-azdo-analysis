"""Configuration schema (spec section 6). Every threshold, pattern and mapping lives here."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunConfig(_Model):
    output_dir: str = "./out"
    cache_dir: str = "./.cache"
    cache_ttl_hours: float = Field(24, ge=0)
    resume: bool = True
    concurrency: int = Field(6, ge=1, le=64)
    requests_per_second: float = Field(10, gt=0)
    timeout_seconds: float = Field(60, gt=0)
    retries: int = Field(5, ge=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    fail_on_project_error: bool = False


class AuthConfig(_Model):
    method: Literal["pat", "ntlm", "negotiate"] = "pat"
    token_env: str = "AZDO_PAT"


class TlsConfig(_Model):
    # Default is false: the tool is run from a local workstation against internal servers.
    verify: bool = False
    ca_bundle: str | None = None


class ProjectFilter(_Model):
    include: list[str] = ["*"]
    exclude: list[str] = []


ApiVersion = Literal["auto", "4.1", "5.0", "6.0", "7.0", "7.1"]


class SourceConfig(_Model):
    name: str
    type: Literal["server", "services"] = "server"
    base_url: str
    api_version: ApiVersion = "auto"
    auth: AuthConfig = AuthConfig()
    tls: TlsConfig = TlsConfig()
    proxy: str | None = None
    projects: ProjectFilter = ProjectFilter()

    @field_validator("api_version", mode="before")
    @classmethod
    def _version_as_str(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int | float) else value

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value.rstrip("/")


class FiltersConfig(_Model):
    include_disabled_pipelines: bool = True
    include_draft_definitions: bool = False
    include_deleted_release_definitions: bool = False
    pipeline_name_exclude: list[str] = []
    pipeline_folder_exclude: list[str] = []


class CollectorsConfig(_Model):
    projects: bool = True
    repositories: bool = True
    build_definitions: bool = True
    build_runs: bool = True
    release_definitions: bool = True
    release_runs: bool = True
    task_groups: bool = True
    variable_groups: bool = True
    service_connections: bool = True
    agent_pools: bool = True
    agent_capabilities: bool = True
    deployment_groups: bool = True
    environments: bool = True
    secure_files: bool = True
    task_catalog: bool = True
    installed_extensions: bool = True
    branch_policies: bool = True
    service_hooks: bool = True
    package_feeds: bool = True
    xaml_builds: bool = False


class HistoryConfig(_Model):
    lookback_days: int = Field(365, ge=1)
    max_runs_per_definition: int = Field(50, ge=1)


class ActivityConfig(_Model):
    active_within_days: int = Field(90, ge=1)
    dormant_within_days: int = Field(365, ge=1)
    failing_last_n_runs: int = Field(5, ge=1)
    low_success_rate_pct: float = Field(60, ge=0, le=100)


class TfvcScanConfig(_Model):
    enabled: bool = True
    max_depth: int = Field(6, ge=1)


class RepoScanConfig(_Model):
    enabled: bool = True
    branch: str = "default"
    extra_branches: list[str] = []
    max_tree_entries: int = Field(50000, ge=1)
    max_file_size_kb: int = Field(512, ge=1)
    max_manifest_files_per_repo: int = Field(300, ge=1)
    ignore_paths: list[str] = [
        "**/node_modules/**",
        "**/bin/**",
        "**/obj/**",
        "**/vendor/**",
        "**/.git/**",
        "**/packages/**",
    ]
    tfvc: TfvcScanConfig = TfvcScanConfig()


class YamlPipelinesConfig(_Model):
    resolve_templates: bool = True
    max_template_depth: int = Field(10, ge=0)
    use_timeline_expansion: bool = True


class DetectionConfig(_Model):
    rules_dir: str = "rules"
    extra_rules_dirs: list[str] = []
    min_confidence: float = Field(0.4, ge=0, le=1)
    script_scan: bool = True


class EnvironmentsConfig(_Model):
    tiers: dict[str, list[str]] = {
        "dev": ["re:(?i)^dev.*", "re:(?i).*\\bci\\b.*"],
        "qa": ["re:(?i)^(qa|test|sit).*"],
        "uat": ["re:(?i)^(uat|stag|pre-?prod).*"],
        "perf": ["re:(?i)^perf.*"],
        "prod": ["re:(?i)^(prod|prd|live).*"],
        "dr": ["re:(?i).*(-|_)dr$"],
    }
    production_tiers: list[str] = ["prod", "dr"]
    # tier -> GitLab deployment_tier (production | staging | testing | development | other)
    deployment_tiers: dict[str, str] = {
        "dev": "development",
        "qa": "testing",
        "uat": "staging",
        "perf": "testing",
        "prod": "production",
        "dr": "production",
    }


class TemplateCatalogEntry(_Model):
    id: str
    match: dict[str, str]
    include: dict[str, str]


class RunnerTags(_Model):
    linux: list[str] = ["linux", "docker"]
    windows: list[str] = ["windows", "shell"]
    macos: list[str] = ["macos"]


class GitLabConfig(_Model):
    group_path_template: str = "{source}/{project}"
    image_registry_prefix: str = ""
    artifact_repository: Literal["artifactory", "nexus", "gitlab"] = "artifactory"
    runner_tags: RunnerTags = RunnerTags()
    task_mapping_file: str = "mappings/tasks.yaml"
    concept_mapping_file: str = "mappings/concepts.yaml"
    service_connection_mapping_file: str = "mappings/service_connections.yaml"
    template_catalog: list[TemplateCatalogEntry] = []
    security_baseline: list[str] = ["sast", "secret_detection", "dependency_scanning", "container_scanning"]
    generate_skeletons: bool = False


class ComplexityWeights(_Model):
    classic_definition: float = 2
    per_10_tasks: float = 1
    per_unmapped_task: float = 3
    per_marketplace_task: float = 2
    per_task_group: float = 1
    per_stage: float = 1
    per_service_connection: float = 1
    windows_only: float = 2
    deployment_group: float = 3
    gates: float = 2
    cross_project_dependency: float = 2
    tfvc_source: float = 3
    inline_script_over_50_lines: float = 2


class ComplexityConfig(_Model):
    weights: ComplexityWeights = ComplexityWeights()
    bands: dict[str, float] = {"S": 5, "M": 12, "L": 20}
    inline_script_line_threshold: int = Field(50, ge=1)

    @field_validator("bands")
    @classmethod
    def _bands(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("at least one band is required")
        return value


class ClusteringConfig(_Model):
    enabled: bool = True
    similarity_threshold: float = Field(0.8, ge=0, le=1)
    ignore_tasks: list[str] = ["CopyFiles", "PublishBuildArtifacts", "CmdLine", "PowerShell", "Bash"]
    min_cluster_size: int = Field(2, ge=1)


class SecurityConfig(_Model):
    """Patterns behind the section 13.6 findings."""

    secret_name_patterns: list[str] = ["re:(?i)(password|passwd|pwd|secret|token|apikey|api_key|private_?key)"]
    inline_credential_patterns: list[str] = [
        "re:(?i)(password|passwd|pwd|secret|token|api_?key)\\s*[=:]\\s*['\"]?[^\\s'\"$%{(]{6,}",
        "re:(?i)-p(assword)?\\s+[^\\s$%{(-][^\\s]{5,}",
        "re:AKIA[0-9A-Z]{16}",
    ]
    shared_connection_project_threshold: int = Field(3, ge=1)


class WavesConfig(_Model):
    """Migration wave planning (section 13.7)."""

    migrate_statuses: list[str] = ["active", "failing"]
    review_statuses: list[str] = ["dormant"]
    band_wave: dict[str, int] = {"S": 1, "M": 2, "L": 3, "XL": 4}


class ExcelConfig(_Model):
    sheets: Literal["all"] | list[str] = "all"
    freeze_header: bool = True
    autofilter: bool = True


class PrivacyConfig(_Model):
    include_identities: bool = False
    redact_value_patterns: list[str] = ["re:(?i)(password|secret|token|key)=\\S+"]
    max_variable_value_length: int = Field(200, ge=0)


ReportFormat = Literal["xlsx", "markdown", "json", "csv", "html"]


class ReportsConfig(_Model):
    formats: list[ReportFormat] = ["xlsx", "markdown", "json", "csv", "html"]
    per_project_markdown: bool = True
    per_pipeline_brief: bool = True
    dependency_graph: Literal["mermaid", "none"] = "mermaid"
    excel: ExcelConfig = ExcelConfig()
    privacy: PrivacyConfig = PrivacyConfig()


class Config(_Model):
    version: Literal[1] = 1
    run: RunConfig = RunConfig()
    sources: list[SourceConfig] = Field(min_length=1)
    filters: FiltersConfig = FiltersConfig()
    collectors: CollectorsConfig = CollectorsConfig()
    history: HistoryConfig = HistoryConfig()
    activity: ActivityConfig = ActivityConfig()
    repo_scan: RepoScanConfig = RepoScanConfig()
    yaml_pipelines: YamlPipelinesConfig = YamlPipelinesConfig()
    detection: DetectionConfig = DetectionConfig()
    environments: EnvironmentsConfig = EnvironmentsConfig()
    gitlab: GitLabConfig = GitLabConfig()
    complexity: ComplexityConfig = ComplexityConfig()
    clustering: ClusteringConfig = ClusteringConfig()
    security: SecurityConfig = SecurityConfig()
    waves: WavesConfig = WavesConfig()
    reports: ReportsConfig = ReportsConfig()

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, value: list[SourceConfig]) -> list[SourceConfig]:
        names = [s.name for s in value]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate source names: {', '.join(dupes)}")
        return value
