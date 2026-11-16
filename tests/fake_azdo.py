"""An in-memory Azure DevOps Server used by the tests. No test ever talks to a real AZDO.

The payloads follow the shapes of the REST API (trimmed to the fields that matter). The
server only answers GET; anything else is recorded in ``FakeAzdo.non_get`` so tests can
assert the tool never tries.

Estate:
  Alpha    web-api (YAML, .NET 8 + Angular + Docker + Helm), legacy-app (classic, .NET Framework),
           pipeline-templates (YAML templates), unused-repo (empty); 3 release definitions
  Beta     orders-svc (Spring Boot Maven), TFVC $/Beta; service connections return 403
  Sandbox1 excluded by the project filter
  Gamma    state "deleting"
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

BASE = "https://tfs.example.com/tfs/DefaultCollection"
REPOS_DIR = Path(__file__).parent / "fixtures" / "repos"

R_WEB = "11111111-aaaa-4aaa-8aaa-000000000001"
R_LEGACY = "11111111-aaaa-4aaa-8aaa-000000000002"
R_TEMPLATES = "11111111-aaaa-4aaa-8aaa-000000000003"
R_UNUSED = "11111111-aaaa-4aaa-8aaa-000000000004"
R_ORDERS = "11111111-aaaa-4aaa-8aaa-000000000005"
REPO_DIRS = {R_WEB: "dotnet8-angular", R_LEGACY: "dotnet-framework", R_TEMPLATES: "templates", R_ORDERS: "spring-boot-maven"}

T = {
    "VSBuild": "71a9a2d3-a98a-4caa-96ab-affca411ecda",
    "VSTest": "ef087383-ee5e-42c7-9a53-ab56c98420f9",
    "NuGetCommand": "333b11bd-d341-40d9-afcf-b32d5ce6f23b",
    "PublishBuildArtifacts": "2ff763a7-ce83-4e1f-bc89-0ae63477cebe",
    "CopyFiles": "5bfb729a-a7c8-4a78-a7c3-8d717bb7c13c",
    "Maven": "ac4ee482-65da-4485-a532-7b085873e532",
    "CmdLine": "d9bafed4-0b18-4f58-968d-86655b4d2ce9",
    "PowerShell": "e213ff0f-5d5c-4791-802d-52ea3e7be1f1",
    "AzureRmWebAppDeployment": "497d490f-eea7-4f2b-ab94-48d9c1acdcb1",
    "IISWebAppDeploymentOnMachineGroup": "1b467810-6725-4b6d-accd-886174c09bba",
    "ManualIntervention": "bcb64569-d51a-4af0-9c01-ea5d05b3b622",
    "HelmDeploy": "afa7d54d-537b-4dc8-b60a-e0eeea2c9a87",
    "DotNetCoreCLI": "5541a522-603c-47ad-91fc-a4b1d163081b",
    "UseDotNet": "b0ce7256-7898-45d3-9cb5-176b752bfea6",
    "Docker": "e28912f1-0114-4464-802a-a3a35437fd16",
    "NodeTool": "31c75bbb-bcdf-4706-8d7c-4da6a1959bc2",
    "PublishTestResults": "0b0f01ed-7dde-43ff-9cbb-e48954daf9b1",
    "queryWorkItems": "f1e4b0e6-017e-4819-8a48-ef19ae96e289",
}
T_REPLACETOKENS = "a8515ec8-7254-4ffd-912c-86772e2b5962"
T_CUSTOM = "c0ffee00-0000-4000-8000-000000000001"
T_REMOVED = "dead0000-0000-4000-8000-00000000dead"
TG_BUILD = "aaaa0001-0000-4000-8000-000000000001"
TG_RESTORE = "aaaa0002-0000-4000-8000-000000000002"
TG_UNUSED = "aaaa0003-0000-4000-8000-000000000003"
SC_AZURE = "bbbb0001-0000-4000-8000-000000000001"
SC_AKS = "bbbb0002-0000-4000-8000-000000000002"
SC_ACR = "bbbb0003-0000-4000-8000-000000000003"
SC_UNUSED = "bbbb0004-0000-4000-8000-000000000004"
SF_PFX = "cccc0001-0000-4000-8000-000000000001"
P_ALPHA = "dddd0001-0000-4000-8000-000000000001"
P_BETA = "dddd0002-0000-4000-8000-000000000002"


def ago(days: float) -> str:
    moment = datetime.now(UTC) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.1234567Z")


def _step(task: str, name: str, inputs: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    task_id = T.get(task, task)
    return {
        "enabled": True,
        "continueOnError": False,
        "alwaysRun": False,
        "displayName": name,
        "timeoutInMinutes": 0,
        "condition": "succeeded()",
        "task": {"id": task_id, "versionSpec": "2.*", "definitionType": "metaTask" if task in (TG_BUILD, TG_RESTORE) else "task"},
        "inputs": inputs or {},
        **extra,
    }


def _wf(task: str, name: str, inputs: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "taskId": T.get(task, task),
        "version": "4.*",
        "name": name,
        "enabled": True,
        "definitionType": "task",
        "inputs": inputs or {},
        **extra,
    }


def _catalog() -> list[dict[str, Any]]:
    tasks = [
        {
            "id": guid,
            "name": name,
            "friendlyName": name,
            "version": {"major": 2, "minor": 0, "patch": 0},
            "author": "Microsoft Corporation",
            "serverOwned": True,
        }
        for name, guid in T.items()
    ]
    tasks.append(
        {
            "id": T["Maven"],
            "name": "Maven",
            "version": {"major": 3, "minor": 0, "patch": 0},
            "author": "Microsoft Corporation",
            "serverOwned": True,
        }
    )
    tasks.append(
        {
            "id": T_REPLACETOKENS,
            "name": "replacetokens",
            "version": {"major": 3, "minor": 1, "patch": 0},
            "author": "Guillaume Rouchon",
            "contributionIdentifier": "qetza.replacetokens.replacetokens-task",
            "serverOwned": False,
        }
    )
    tasks.append(
        {
            "id": T_CUSTOM,
            "name": "AcmeDeploy",
            "version": {"major": 1, "minor": 0, "patch": 0},
            "author": "Acme Platform Team",
            "serverOwned": False,
        }
    )
    return tasks


def _build_def(def_id: int, name: str, project: str, repo: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": def_id,
        "name": name,
        "path": "\\",
        "type": "build",
        "quality": "definition",
        "queueStatus": "enabled",
        "project": {"name": project},
        "repository": repo,
        "queue": {"id": 11, "name": "Default", "pool": {"id": 1, "name": "Default", "isHosted": False}},
        "_links": {"web": {"href": f"{BASE}/{project}/_build?definitionId={def_id}"}},
        **extra,
    }


def _git(repo_id: str, name: str, project: str) -> dict[str, Any]:
    return {
        "id": repo_id,
        "type": "TfsGit",
        "name": name,
        "url": f"{BASE}/{project}/_git/{name}",
        "defaultBranch": "refs/heads/main",
        "clean": "true",
        "checkoutSubmodules": False,
        "properties": {"fetchDepth": "1", "gitLfsSupport": "false"},
    }


def _builds(def_id: int, results: list[tuple[float, str, str]]) -> list[dict[str, Any]]:
    """(days ago, result, reason) newest first."""
    return [
        {
            "id": def_id * 1000 + i,
            "buildNumber": f"{def_id}.{i}",
            "status": "completed",
            "result": result,
            "queueTime": ago(days + 0.01),
            "startTime": ago(days + 0.005),
            "finishTime": ago(days),
            "reason": reason,
            "sourceBranch": "refs/heads/main" if i % 2 == 0 else "refs/heads/feature/x",
            "requestedFor": {"displayName": "Jane Dev", "uniqueName": "CORP\\jdev"},
            "definition": {"id": def_id},
            "queue": {"id": 12, "name": "Linux-Pool", "pool": {"id": 2, "name": "Linux-Pool", "isHosted": False}},
        }
        for i, (days, result, reason) in enumerate(results)
    ]


class FakeAzdo:
    """Routes GET requests to payloads. ``max_api_version`` mimics Azure DevOps Server 2020."""

    def __init__(self, max_api_version: str = "6.0") -> None:
        self.max_api_version = max_api_version
        self.requests: list[httpx.Request] = []
        self.non_get: list[httpx.Request] = []
        self.build_runs = {
            1: _builds(1, [(1, "succeeded", "individualCI"), (2, "failed", "pullRequest"), (5, "succeeded", "schedule")]),
            2: _builds(2, [(d, "failed", "manual") for d in (3, 4, 6, 8, 9)] + [(20, "succeeded", "individualCI")]),
            3: _builds(3, [(200, "succeeded", "manual")]),
            10: _builds(10, [(2, "succeeded", "buildCompletion"), (12, "succeeded", "individualCI")]),
        }
        self.definitions = self._build_definitions()
        self.releases = self._release_definitions()

    # -- definitions -------------------------------------------------------------

    def _build_definitions(self) -> dict[str, dict[int, dict[str, Any]]]:
        alpha = {
            1: _build_def(
                1,
                "web-api-ci",
                "Alpha",
                _git(R_WEB, "web-api", "Alpha"),
                process={"type": 2, "yamlFilename": "azure-pipelines.yml"},
                triggers=[{"triggerType": "continuousIntegration", "settingsSourceType": 2, "branchFilters": []}],
                variables={"system.debug": {"value": "false", "allowOverride": True}},
            ),
            2: _build_def(
                2,
                "legacy-app-build",
                "Alpha",
                _git(R_LEGACY, "legacy-app", "Alpha"),
                process={
                    "type": 1,
                    "phases": [
                        {
                            "name": "Build",
                            "refName": "Job_1",
                            "condition": "succeeded()",
                            "target": {
                                "type": 1,
                                "demands": ["msbuild", "visualstudio"],
                                "executionOptions": {"type": 1, "multipliers": "BuildConfiguration"},
                            },
                            "steps": [
                                _step(TG_BUILD, "Task group: Build .NET Framework", {"solution": "LegacyWeb.sln"}),
                                _step("VSTest", "Test", {"testAssemblyVer2": "**\\*Tests.dll"}),
                                _step(T_REPLACETOKENS, "Replace tokens", {"targetFiles": "**/*.config"}),
                                _step(T_CUSTOM, "Acme deploy prep", {"mode": "prep"}),
                                _step(T_REMOVED, "Old extension task", {}),
                                _step(
                                    "CmdLine",
                                    "Legacy script",
                                    {"script": "echo building\nnuget.exe push *.nupkg -ApiKey token=abcdef123456"},
                                ),
                                _step("CopyFiles", "Disabled copy", {"Contents": "**"}, enabled=False),
                                _step(
                                    "PublishBuildArtifacts",
                                    "Publish",
                                    {"PathtoPublish": "$(Build.ArtifactStagingDirectory)", "ArtifactName": "drop"},
                                ),
                            ],
                        }
                    ],
                },
                triggers=[
                    {
                        "triggerType": "continuousIntegration",
                        "branchFilters": ["+refs/heads/main", "-refs/heads/experimental/*"],
                        "pathFilters": ["+/src", "-/docs"],
                        "batchChanges": True,
                    },
                    {
                        "triggerType": "schedule",
                        "schedules": [
                            {
                                "branchFilters": ["+refs/heads/main"],
                                "timeZoneId": "W. Europe Standard Time",
                                "startHours": 3,
                                "startMinutes": 30,
                                "daysToBuild": 31,
                                "scheduleOnlyWithChanges": True,
                            }
                        ],
                    },
                ],
                variables={
                    "BuildConfiguration": {"value": "Release", "allowOverride": True},
                    "NugetApiKey": {"value": None, "isSecret": True},
                    "DeployPassword": {"value": "PlainTextPassw0rd"},
                },
                variableGroups=[{"id": 1, "name": "web-api-common"}],
                buildNumberFormat="$(date:yyyyMMdd)$(rev:.r)",
                retentionRules=[{"daysToKeep": 10, "minimumToKeep": 1}],
                tags=["legacy"],
            ),
            3: _build_def(
                3,
                "cross-proj-build",
                "Alpha",
                _git(R_ORDERS, "orders-svc", "Beta"),
                process={
                    "type": 1,
                    "phases": [
                        {
                            "name": "Agent job",
                            "refName": "Job_1",
                            "target": {"type": 1, "queue": {"id": 12}},
                            "steps": [
                                _step("Maven", "Maven package", {"mavenPomFile": "pom.xml", "goals": "package", "jdkVersionOption": "1.17"})
                            ],
                        }
                    ],
                },
            ),
            4: _build_def(4, "spike-poc", "Alpha", _git(R_WEB, "web-api", "Alpha"), process={"type": 1, "phases": []}),
            5: _build_def(
                5, "never-run", "Alpha", _git(R_LEGACY, "legacy-app", "Alpha"), queueStatus="disabled", process={"type": 1, "phases": []}
            ),
        }
        beta = {
            10: _build_def(
                10,
                "orders-ci",
                "Beta",
                _git(R_ORDERS, "orders-svc", "Beta"),
                process={
                    "type": 1,
                    "phases": [
                        {
                            "name": "Agent job",
                            "refName": "Job_1",
                            "target": {"type": 1, "queue": {"id": 22}},
                            "steps": [
                                _step("Maven", "Maven deploy", {"mavenPomFile": "pom.xml", "goals": "deploy", "jdkVersionOption": "1.17"}),
                                _step("CmdLine", "Sonar", {"script": "mvn sonar:sonar -Dsonar.host.url=$(SonarUrl)"}),
                                _step("PublishBuildArtifacts", "Publish", {"PathtoPublish": "target", "ArtifactName": "drop"}),
                            ],
                        }
                    ],
                },
                triggers=[
                    {
                        "triggerType": "buildCompletion",
                        "definition": {"id": 3, "project": {"name": "Alpha"}},
                        "requiresSuccessfulBuild": True,
                        "branchFilters": ["+refs/heads/main"],
                    }
                ],
            ),
            11: _build_def(
                11,
                "tfvc-build",
                "Beta",
                {
                    "id": "$/Beta",
                    "type": "TfsVersionControl",
                    "name": "Beta",
                    "defaultBranch": "$/Beta/Main",
                    "properties": {
                        "tfvcMapping": json.dumps({"mappings": [{"serverPath": "$/Beta/Main", "mappingType": "map", "localPath": "\\"}]})
                    },
                },
                # API 4.1 shape: flat step list instead of process.phases
                build=[_step("VSBuild", "Build solution", {"solution": "$/Beta/Main/App/App.sln"})],
                triggers=[{"triggerType": "gatedCheckIn", "pathFilters": ["+$/Beta/Main"]}],
            ),
        }
        for def_id, defn in {**alpha, **beta}.items():
            runs = self.build_runs.get(def_id)
            if runs:
                defn["latestBuild"] = runs[0]
        beta[11]["latestBuild"] = _builds(11, [(500, "succeeded", "manual")])[0]
        return {"Alpha": alpha, "Beta": beta}

    def _release_definitions(self) -> dict[int, dict[str, Any]]:
        def approvals(*names: str) -> dict[str, Any]:
            if not names:
                return {"approvals": [{"rank": 1, "isAutomated": True}], "approvalOptions": {}}
            return {
                "approvals": [
                    {
                        "rank": i,
                        "isAutomated": False,
                        "approver": {"displayName": n, "uniqueName": f"CORP\\{n}", "isContainer": n.startswith("[")},
                    }
                    for i, n in enumerate(names, 1)
                ],
                "approvalOptions": {"requiredApproverCount": 1, "executionOrder": "beforeGates", "timeoutInMinutes": 1440},
            }

        def env(
            env_id: int, name: str, rank: int, conditions: list[dict[str, Any]], phases: list[dict[str, Any]], **extra: Any
        ) -> dict[str, Any]:
            return {
                "id": env_id,
                "name": name,
                "rank": rank,
                "variables": {},
                "preDeployApprovals": approvals(),
                "postDeployApprovals": approvals(),
                "conditions": conditions,
                "deployPhases": phases,
                "executionPolicy": {"concurrencyCount": 1, "queueDepthCount": 0},
                "retentionPolicy": {"daysToKeep": 30, "releasesToKeep": 3},
                **extra,
            }

        agent_phase = {
            "rank": 1,
            "phaseType": "agentBasedDeployment",
            "name": "Agent job",
            "deploymentInput": {"queueId": 11, "demands": [], "parallelExecution": {"parallelExecutionType": "none"}},
            "workflowTasks": [
                _wf(
                    "AzureRmWebAppDeployment",
                    "Deploy web app",
                    {
                        "ConnectedServiceName": SC_AZURE,
                        "WebAppName": "legacy-web-$(Release.EnvironmentName)",
                        "enableXmlTransform": "true",
                        "DeployToSlotOrASEFlag": "true",
                    },
                ),
            ],
        }
        web_release = {
            "id": 1,
            "name": "web-release",
            "path": "\\",
            "releaseNameFormat": "Release-$(rev:r)",
            "variables": {"Region": {"value": "westeurope"}},
            "variableGroups": [2],
            "artifacts": [
                {
                    "alias": "_legacy",
                    "type": "Build",
                    "isPrimary": True,
                    "definitionReference": {
                        "project": {"id": P_ALPHA, "name": "Alpha"},
                        "definition": {"id": "2", "name": "legacy-app-build"},
                        "defaultVersionType": {"id": "latestType", "name": "Latest"},
                    },
                },
                {
                    "alias": "_orders",
                    "type": "Build",
                    "isPrimary": False,
                    "definitionReference": {
                        "project": {"id": P_BETA, "name": "Beta"},
                        "definition": {"id": "10", "name": "orders-ci"},
                        "defaultVersionType": {"id": "latestType", "name": "Latest"},
                    },
                },
            ],
            "triggers": [{"triggerType": "artifactSource", "artifactAlias": "_legacy", "triggerConditions": [{"sourceBranch": "main"}]}],
            "environments": [
                env(
                    1,
                    "Dev",
                    1,
                    [{"name": "ReleaseStarted", "conditionType": "event", "value": ""}],
                    [agent_phase],
                    variables={"ConnString": {"value": "Server=dev;Password=devpass123"}},
                ),
                env(2, "QA", 2, [{"name": "Dev", "conditionType": "environmentState", "value": "4"}], [agent_phase]),
                env(
                    3,
                    "Prod",
                    3,
                    [{"name": "QA", "conditionType": "environmentState", "value": "4"}],
                    [
                        {
                            "rank": 1,
                            "phaseType": "machineGroupBasedDeployment",
                            "name": "IIS deploy",
                            "deploymentInput": {"queueId": 5, "tags": ["web"], "healthPercent": 0},
                            "workflowTasks": [
                                _wf(
                                    "IISWebAppDeploymentOnMachineGroup",
                                    "Deploy IIS",
                                    {"WebSiteName": "LegacyWeb", "XmlVariableSubstitution": "true"},
                                )
                            ],
                        },
                        {
                            "rank": 2,
                            "phaseType": "runOnServer",
                            "name": "Agentless",
                            "deploymentInput": {},
                            "workflowTasks": [_wf("ManualIntervention", "Smoke test sign-off", {"instructions": "check"})],
                        },
                    ],
                    preDeployApprovals=approvals("Rita Release", "[Alpha]\\Release Approvers"),
                    preDeploymentGates={
                        "gatesOptions": {"isEnabled": True, "timeout": 1440, "samplingInterval": 15},
                        "gates": [{"tasks": [_wf("queryWorkItems", "No open bugs", {})]}],
                    },
                ),
            ],
            "_links": {"web": {"href": f"{BASE}/Alpha/_release?definitionId=1"}},
        }
        git_release = {
            "id": 2,
            "name": "git-release",
            "path": "\\k8s",
            "variables": {},
            "artifacts": [
                {
                    "alias": "_web-api",
                    "type": "Git",
                    "isPrimary": True,
                    "definitionReference": {
                        "project": {"id": P_ALPHA, "name": "Alpha"},
                        "definition": {"id": R_WEB, "name": "web-api"},
                        "branches": {"id": "main", "name": "main"},
                    },
                }
            ],
            "environments": [
                env(
                    1,
                    "Production",
                    1,
                    [],
                    [
                        {
                            "rank": 1,
                            "phaseType": "agentBasedDeployment",
                            "name": "Helm",
                            "deploymentInput": {"queueId": 12},
                            "workflowTasks": [
                                _wf(
                                    "HelmDeploy",
                                    "helm upgrade",
                                    {"kubernetesServiceConnection": SC_AKS, "namespace": "web", "command": "upgrade"},
                                )
                            ],
                        }
                    ],
                )
            ],
        }
        broken = {
            "id": 3,
            "name": "broken-release",
            "path": "\\",
            "artifacts": [
                {
                    "alias": "_gone",
                    "type": "Build",
                    "definitionReference": {
                        "project": {"id": P_ALPHA, "name": "Alpha"},
                        "definition": {"id": "999", "name": "deleted-build"},
                    },
                }
            ],
            "environments": [env(1, "Test", 1, [{"name": "ReleaseStarted", "conditionType": "event"}], [])],
        }
        return {1: web_release, 2: git_release, 3: broken}

    # -- static lists ------------------------------------------------------------

    def _alpha_lists(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "git/repositories": [
                {
                    "id": R_WEB,
                    "name": "web-api",
                    "defaultBranch": "refs/heads/main",
                    "size": 4096000,
                    "project": {"name": "Alpha"},
                    "webUrl": f"{BASE}/Alpha/_git/web-api",
                },
                {"id": R_LEGACY, "name": "legacy-app", "defaultBranch": "refs/heads/main", "size": 20480, "project": {"name": "Alpha"}},
                {
                    "id": R_TEMPLATES,
                    "name": "pipeline-templates",
                    "defaultBranch": "refs/heads/main",
                    "size": 1024,
                    "project": {"name": "Alpha"},
                },
                {"id": R_UNUSED, "name": "unused-repo", "size": 0, "project": {"name": "Alpha"}},
            ],
            "distributedtask/queues": [
                {"id": 11, "name": "Default", "pool": {"id": 1, "name": "Default", "isHosted": False}},
                {"id": 12, "name": "Linux-Pool", "pool": {"id": 2, "name": "Linux-Pool", "isHosted": False}},
            ],
            "distributedtask/taskgroups": [
                {
                    "id": TG_BUILD,
                    "name": "Build .NET Framework",
                    "version": {"major": 2, "minor": 0, "patch": 0, "isTest": False},
                    "tasks": [
                        {
                            "displayName": "Restore",
                            "enabled": True,
                            "task": {"id": TG_RESTORE, "versionSpec": "1.*", "definitionType": "metaTask"},
                            "inputs": {},
                        },
                        {
                            "displayName": "Build solution",
                            "enabled": True,
                            "task": {"id": T["VSBuild"], "versionSpec": "2.*", "definitionType": "task"},
                            "inputs": {"solution": "$(solution)", "vsVersion": "16.0"},
                        },
                    ],
                },
                {
                    "id": TG_RESTORE,
                    "name": "Restore NuGet",
                    "version": {"major": 1, "minor": 0, "patch": 0, "isTest": False},
                    "tasks": [
                        {
                            "displayName": "NuGet restore",
                            "enabled": True,
                            "task": {"id": T["NuGetCommand"], "versionSpec": "2.*", "definitionType": "task"},
                            "inputs": {"command": "restore", "restoreSolution": "**/*.sln"},
                        }
                    ],
                },
                {"id": TG_UNUSED, "name": "Old steps", "version": {"major": 1, "isTest": True}, "tasks": []},
            ],
            "distributedtask/variablegroups": [
                {
                    "id": 1,
                    "type": "Vsts",
                    "name": "web-api-common",
                    "variables": {
                        "ApiUrl": {"value": "https://api.example.com"},
                        "DbPassword": {"value": "hunter2hunter2"},
                        "SigningKey": {"value": None, "isSecret": True},
                    },
                },
                {
                    "id": 2,
                    "type": "AzureKeyVault",
                    "name": "kv-prod",
                    "variables": {"sql-admin": {"enabled": True}},
                    "providerData": {"vault": "kv-prod-weu", "serviceEndpointId": SC_AZURE},
                },
                {"id": 3, "type": "Vsts", "name": "unused-group", "variables": {"x": {"value": "1"}}},
            ],
            "serviceendpoint/endpoints": [
                {
                    "id": SC_AZURE,
                    "name": "azure-prod",
                    "type": "azurerm",
                    "url": "https://management.azure.com/",
                    "authorization": {"scheme": "ServicePrincipal", "parameters": {"tenantid": "t"}},
                    "isShared": True,
                    "serviceEndpointProjectReferences": [{"projectReference": {"name": n}} for n in ("Alpha", "Beta", "Sandbox1")],
                },
                {
                    "id": SC_AKS,
                    "name": "aks-prod",
                    "type": "kubernetes",
                    "url": "https://aks.example.com",
                    "authorization": {"scheme": "Kubernetes"},
                },
                {
                    "id": SC_ACR,
                    "name": "acr-prod",
                    "type": "dockerregistry",
                    "url": "https://acr.example.com",
                    "authorization": {"scheme": "UsernamePassword"},
                },
                {
                    "id": SC_UNUSED,
                    "name": "unused-conn",
                    "type": "generic",
                    "url": "https://x.example.com/path?token=abc",
                    "authorization": {"scheme": "Token"},
                },
            ],
            "distributedtask/deploymentgroups": [
                {"id": 5, "name": "IIS-Prod", "pool": {"id": 9, "name": "IIS-Prod-pool"}, "machineCount": 2}
            ],
            "distributedtask/deploymentgroups/5/targets": [
                {
                    "id": 1,
                    "tags": ["web"],
                    "online": True,
                    "agent": {"name": "IISPROD01", "osDescription": "Microsoft Windows 10.0.17763", "status": "online"},
                },
                {
                    "id": 2,
                    "tags": ["web", "db"],
                    "online": False,
                    "agent": {"name": "IISPROD02", "osDescription": "Microsoft Windows 10.0.17763", "status": "offline"},
                },
            ],
            "distributedtask/environments": [{"id": 7, "name": "web-prod"}],
            "pipelines/checks/configurations": [
                {
                    "id": 1,
                    "type": {"name": "Approval"},
                    "settings": {"approvers": [{"displayName": "Rita Release"}]},
                    "resource": {"type": "environment", "id": "7"},
                }
            ],
            "distributedtask/securefiles": [{"id": SF_PFX, "name": "signing.pfx"}],
            "policy/configurations": [
                {
                    "id": 31,
                    "isEnabled": True,
                    "isBlocking": True,
                    "type": {"displayName": "Build"},
                    "settings": {
                        "buildDefinitionId": 1,
                        "scope": [{"repositoryId": R_WEB, "refName": "refs/heads/main", "matchKind": "exact"}],
                    },
                },
                {
                    "id": 32,
                    "isEnabled": True,
                    "isBlocking": True,
                    "type": {"displayName": "Minimum number of reviewers"},
                    "settings": {"minimumApproverCount": 2, "scope": [{"repositoryId": None}]},
                },
            ],
        }

    def _beta_lists(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "git/repositories": [
                {"id": R_ORDERS, "name": "orders-svc", "defaultBranch": "refs/heads/main", "size": 51200, "project": {"name": "Beta"}}
            ],
            "distributedtask/queues": [{"id": 22, "name": "Linux-Pool", "pool": {"id": 2, "name": "Linux-Pool", "isHosted": False}}],
            "distributedtask/taskgroups": [],
            "distributedtask/variablegroups": [],
            "distributedtask/deploymentgroups": [],
            "distributedtask/environments": [],
            "distributedtask/securefiles": [],
            "policy/configurations": [],
        }

    def _collection_lists(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "distributedtask/tasks": _catalog(),
            "extensionmanagement/installedextensions": [
                {
                    "extensionId": "replacetokens",
                    "extensionName": "Replace Tokens",
                    "publisherId": "qetza",
                    "publisherName": "Guillaume Rouchon",
                    "version": "3.1.0",
                    "flags": "trusted",
                    "installState": {"flags": "none"},
                    "contributions": [
                        {
                            "id": "qetza.replacetokens.replacetokens-task",
                            "type": "ms.vss-distributed-task.task",
                            "properties": {"name": "replacetokens"},
                        }
                    ],
                },
                {
                    "extensionId": "unused-ext",
                    "extensionName": "Unused Extension",
                    "publisherId": "acme",
                    "publisherName": "Acme",
                    "version": "1.0.0",
                    "flags": "",
                    "installState": {"flags": "none"},
                    "contributions": [
                        {"id": "acme.unused-ext.unusedtask", "type": "ms.vss-distributed-task.task", "properties": {"name": "UnusedTask"}}
                    ],
                },
            ],
            "distributedtask/pools": [
                {"id": 1, "name": "Default", "isHosted": False, "poolType": "automation"},
                {"id": 2, "name": "Linux-Pool", "isHosted": False, "poolType": "automation"},
                {"id": 3, "name": "Hosted", "isHosted": True, "poolType": "automation"},
            ],
            "distributedtask/pools/1/agents": [
                {
                    "id": 1,
                    "name": "WINBUILD01",
                    "version": "2.170.1",
                    "status": "online",
                    "enabled": True,
                    "systemCapabilities": {
                        "Agent.OS": "Windows_NT",
                        "VisualStudio_16.0": "C:\\VS\\2019",
                        "MSBuild_16.0": "C:\\MSBuild",
                        "java": "C:\\jdk8",
                        "USERNAME": "svc-build",
                    },
                },
                {
                    "id": 2,
                    "name": "WINBUILD02",
                    "version": "2.170.1",
                    "status": "offline",
                    "enabled": True,
                    "systemCapabilities": {"Agent.OS": "Windows_NT", "VisualStudio_16.0": "C:\\VS\\2019"},
                },
            ],
            "distributedtask/pools/2/agents": [
                {
                    "id": 3,
                    "name": "LNXBUILD01",
                    "version": "2.170.1",
                    "status": "online",
                    "enabled": True,
                    "systemCapabilities": {"Agent.OS": "Linux", "docker": "/usr/bin/docker", "maven": "/opt/maven"},
                }
            ],
            "distributedtask/pools/3/agents": [],
            "hooks/subscriptions": [
                {
                    "id": "h1",
                    "eventType": "build.complete",
                    "consumerId": "webHooks",
                    "consumerActionId": "httpRequest",
                    "status": "enabled",
                    "publisherInputs": {"projectId": P_ALPHA},
                    "consumerInputs": {"url": "https://chat.example.com/hooks/SECRETTOKEN123"},
                }
            ],
            "packaging/feeds": [{"id": "f1", "name": "corp-nuget", "upstreamSources": [{"name": "nuget.org", "protocol": "nuget"}]}],
        }

    # -- repository content --------------------------------------------------------

    @staticmethod
    def _tree(repo_id: str) -> list[dict[str, Any]]:
        root = REPOS_DIR / REPO_DIRS[repo_id]
        items: list[dict[str, Any]] = [{"path": "/", "isFolder": True, "gitObjectType": "tree"}]
        for path in sorted(root.rglob("*")):
            rel = "/" + path.relative_to(root).as_posix()
            if path.is_dir():
                items.append({"path": rel, "isFolder": True, "gitObjectType": "tree"})
            else:
                items.append({"path": rel, "gitObjectType": "blob"})
        return items

    @staticmethod
    def _file(repo_id: str, path: str) -> dict[str, Any] | None:
        file = REPOS_DIR / REPO_DIRS[repo_id] / path.lstrip("/")
        if not file.is_file():
            return None
        return {"path": path, "gitObjectType": "blob", "content": file.read_text(encoding="utf-8")}

    # -- request handling ----------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _json(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers)

    def _list(self, items: list[Any]) -> httpx.Response:
        return self._json({"count": len(items), "value": items})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            self.non_get.append(request)
            return httpx.Response(405)
        url = str(request.url.copy_with(query=None))
        if not url.startswith(BASE + "/"):
            return httpx.Response(404, json={"message": "unknown host"})
        path = unquote(url[len(BASE) + 1 :])
        params = dict(request.url.params)
        version = params.get("api-version", "")
        number = version.replace("-preview", "")
        if not number or float(number) > float(self.max_api_version):
            return self._json(
                {
                    "typeKey": "VssVersionOutOfRangeException",
                    "message": f"The requested REST API version of {number} is out of range for this server.",
                },
                400,
            )
        if path.endswith("distributedtask/securefiles") and "-preview" not in version:
            return self._json(
                {
                    "typeKey": "VssInvalidPreviewVersionException",
                    "message": f'The requested version "{number}" of the resource is under preview. The -preview flag must be supplied in the api-version for such requests.',
                },
                400,
            )
        if path.startswith("_apis/"):
            return self._collection(path[len("_apis/") :], params)
        project, _, rest = path.partition("/_apis/")
        if project == "Alpha":
            return self._project(project, rest, params, self._alpha_lists())
        if project == "Beta":
            if rest == "serviceendpoint/endpoints":
                return self._json({"message": "TF400813: not authorized"}, 403)
            return self._project(project, rest, params, self._beta_lists())
        return httpx.Response(404, json={"message": f"project {project} not found"})

    def _collection(self, rest: str, params: dict[str, str]) -> httpx.Response:
        if rest == "projects":
            projects = [
                {"id": P_ALPHA, "name": "Alpha", "state": "wellFormed"},
                {"id": P_BETA, "name": "Beta", "state": "wellFormed"},
                {"id": "dddd0003-0000-4000-8000-000000000003", "name": "Sandbox1", "state": "wellFormed"},
                {"id": "dddd0004-0000-4000-8000-000000000004", "name": "Gamma", "state": "deleting"},
            ]
            top = int(params.get("$top", "100"))
            skip = int(params.get("$skip", "0"))
            return self._list(projects[skip : skip + top])
        if rest.startswith("projects/"):
            name = "Alpha" if P_ALPHA in rest else "Beta"
            kind = {"sourceControlType": "Git", "gitEnabled": "True", "tfvcEnabled": "True" if name == "Beta" else "False"}
            return self._json(
                {
                    "id": rest.split("/")[1],
                    "name": name,
                    "capabilities": {"versioncontrol": kind, "processTemplate": {"templateName": "Agile"}},
                }
            )
        lists = self._collection_lists()
        if rest in lists:
            return self._list(lists[rest])
        return httpx.Response(404, json={"message": "not found"})

    def _project(self, project: str, rest: str, params: dict[str, str], lists: dict[str, list[dict[str, Any]]]) -> httpx.Response:
        definitions = self.definitions[project]
        if rest == "build/definitions":
            if params.get("type") == "xaml":
                return self._list([])
            shallow = [{k: v for k, v in d.items() if k not in ("process", "build", "triggers", "variables")} for d in definitions.values()]
            # Two pages through a continuation token.
            token = params.get("continuationToken")
            half = (len(shallow) + 1) // 2
            if token is None and len(shallow) > 1:
                return self._json({"count": half, "value": shallow[:half]}, headers={"x-ms-continuationtoken": "page2"})
            return self._list(shallow[half:] if token else shallow)
        if rest.startswith("build/definitions/"):
            defn = definitions.get(int(rest.rsplit("/", 1)[1]))
            return self._json(defn) if defn else httpx.Response(404, json={"message": "no definition"})
        if rest == "build/builds":
            return self._list(self.build_runs.get(int(params.get("definitions", "0")), []))
        if rest.startswith("build/builds/") and rest.endswith("/timeline"):
            return self._json(
                {
                    "records": [
                        {"id": "s1", "type": "Stage", "name": "Build", "order": 1, "result": "succeeded"},
                        {"id": "j1", "type": "Job", "name": "api", "order": 1, "result": "succeeded"},
                        {"id": "t0", "type": "Task", "name": "Initialize job", "order": 1, "result": "succeeded", "task": None},
                        {
                            "id": "t1",
                            "type": "Task",
                            "name": "UseDotNet",
                            "order": 2,
                            "result": "succeeded",
                            "task": {"id": T["UseDotNet"], "name": "UseDotNet", "version": "2.207.2"},
                        },
                        {
                            "id": "t2",
                            "type": "Task",
                            "name": "Build",
                            "order": 3,
                            "result": "succeeded",
                            "task": {"id": T["DotNetCoreCLI"], "name": "DotNetCoreCLI", "version": "2.210.0"},
                        },
                        {
                            "id": "t3",
                            "type": "Task",
                            "name": "Prepare analysis",
                            "order": 4,
                            "result": "succeeded",
                            "task": {"id": "15b84ca1-b62f-4a2a-a403-89b77a063157", "name": "SonarQubePrepare", "version": "5.4.0"},
                        },
                        {
                            "id": "t4",
                            "type": "Task",
                            "name": "Skipped task",
                            "order": 5,
                            "result": "skipped",
                            "task": {"id": T["VSTest"], "name": "VSTest", "version": "2.0.0"},
                        },
                        {"id": "s2", "type": "Stage", "name": "Prod", "order": 2, "result": "succeeded"},
                    ]
                }
            )
        if project == "Alpha" and rest == "release/definitions":
            return self._list(
                [
                    {"id": r["id"], "name": r["name"], "path": r.get("path"), "environments": r["environments"]}
                    for r in self.releases.values()
                ]
            )
        if project == "Alpha" and rest.startswith("release/definitions/"):
            return self._json(self.releases[int(rest.rsplit("/", 1)[1])])
        if rest == "release/definitions":
            return self._list([])
        if rest == "release/releases":
            if params.get("definitionId") == "1":
                return self._list(
                    [
                        {
                            "id": 100 + i,
                            "name": f"Release-{9 - i}",
                            "status": "active",
                            "createdOn": ago(d),
                            "createdBy": {"displayName": "Rita Release", "uniqueName": "CORP\\rrelease"},
                        }
                        for i, d in enumerate((8, 30, 60))
                    ]
                )
            return self._list([])
        if rest == "release/deployments":
            if params.get("definitionId") == "1":

                def dep(stage: str, days: float, status: str) -> dict[str, Any]:
                    return {
                        "id": int(days * 10),
                        "releaseEnvironment": {"name": stage},
                        "deploymentStatus": status,
                        "reason": "automated",
                        "queuedOn": ago(days + 0.02),
                        "startedOn": ago(days + 0.01),
                        "completedOn": ago(days),
                        "requestedFor": {"displayName": "Rita Release", "uniqueName": "CORP\\rrelease"},
                    }

                return self._list(
                    [
                        dep("Dev", 8, "succeeded"),
                        dep("QA", 7.9, "succeeded"),
                        dep("Prod", 7.5, "failed"),
                        dep("Dev", 30, "succeeded"),
                        dep("QA", 29.9, "succeeded"),
                        dep("Prod", 29, "succeeded"),
                    ]
                )
            return self._list([])
        if rest.startswith("distributedtask/environments/"):
            return self._json({"id": 7, "name": "web-prod", "resources": [{"id": 1, "name": "web-ns", "type": "kubernetes"}]})
        if rest == "tfvc/changesets":
            if project != "Beta":
                return self._list([])
            return self._list([{"changesetId": 4711, "createdDate": ago(40), "author": {"displayName": "Jane Dev"}, "comment": "fix"}])
        if rest == "tfvc/items":
            if project != "Beta":
                return self._list([])
            if "path" in params:
                source = REPOS_DIR / "dotnet-framework" / "src" / "LegacyWeb" / "LegacyWeb.csproj"
                return self._json({"path": params["path"], "content": source.read_text(encoding="utf-8")})
            return self._list(
                [
                    {"path": "$/Beta", "isFolder": True},
                    {"path": "$/Beta/Main", "isFolder": True},
                    {"path": "$/Beta/Main/App", "isFolder": True},
                    {"path": "$/Beta/Main/App/App.sln"},
                    {"path": "$/Beta/Main/App/App.csproj"},
                    {"path": "$/Beta/Main/Tools/Tool.csproj"},
                    {"path": "$/Beta/Dev/App/App.csproj"},
                    {"path": "$/Beta/Main/a/b/c/d/e/f/g/too-deep.csproj"},
                ]
            )
        if rest.startswith("git/repositories/"):
            _, _, repo_id, tail = rest.split("/", 3)
            if repo_id not in REPO_DIRS:
                return httpx.Response(404, json={"message": "repository not found"})
            if tail == "commits":
                return self._list(
                    [
                        {
                            "commitId": "abc123",
                            "author": {"name": "Jane Dev", "email": "jane@example.com", "date": ago(1)},
                            "committer": {"date": ago(1)},
                        }
                    ]
                )
            if tail == "items":
                if "path" in params:
                    found = self._file(repo_id, params["path"])
                    return self._json(found) if found else httpx.Response(404, json={"message": "TF401174: item not found"})
                return self._list(self._tree(repo_id))
        if rest in lists:
            return self._list(lists[rest])
        return httpx.Response(404, json={"message": f"no route for {rest}"})
