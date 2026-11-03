# Document Agent hosted agent

Document Agent is a Microsoft Foundry hosted agent that creates a German credit
`Firmenanalyse` from documents attached to a chat conversation.

## Repository ownership

The three repositories are deployed in this order:

1. `central-ai-platform-infrastructure` owns the central APIM AI gateway,
   shared Foundry model deployments and central monitoring.
2. `ai-spoke-template` owns the Spoke network, private endpoints,
   Foundry account/project, Storage, Cosmos DB, AI Search and per-Spoke
   Application Insights with its project monitoring connection.
3. This repository deploys Document Agent source code into that existing Spoke project.
   Model inference goes through central APIM; OCR uses the explicitly configured
   Content Understanding endpoint, which may be a separate regional resource.

The current [azd configuration](./azure.yaml) uses `codeConfiguration`.
No agent-specific ACR, infrastructure deployment or `azd provision` is required.
There is no registry provisioning template in this repository.
The source-code deployment uses the managed Python runtime and `requirements.txt`;
it does not require a Dockerfile or Docker build configuration. The `container.resources`
settings in `azure.yaml` still specify the hosted agent's CPU and memory.

## Repository layout

| Path | Purpose |
| --- | --- |
| [src/document-agent/](./src/document-agent) | Hosted-agent code, prompts and tests |
| [infra/modules/agent-rbac.bicep](./infra/modules/agent-rbac.bicep) | Separate runtime-role onboarding template; review scopes for the target environment |
| [azure.yaml](./azure.yaml) | Existing project binding and source-code deployment |
| [azure-pipelines.yml](./azure-pipelines.yml) | Deployment to the existing Spoke |
| [docs/infrastructure-rbac.md](./docs/infrastructure-rbac.md) | Infrastructure boundaries, monitoring and runtime identity |

## Azure DevOps deployment

Create the variable group `REPLACE_WITH_AGENT_VARIABLE_GROUP` with these non-secret values:

| Variable | Value |
| --- | --- |
| `SubscriptionId` | Subscription containing the existing Spoke |
| `ResourceGroup` | Spoke resource group |
| `FoundryAccountName` | Existing Spoke Foundry account, not the central model account |
| `FoundryProjectName` | Existing Spoke Foundry project |
| `FoundryProjectEndpoint` | Existing project endpoint from the Spoke deployment |
| `AzureAiModelDeploymentName` | Central model deployment exposed through APIM |
| `AiGatewayBaseUrl` | APIM OpenAI base URL, including its API path |
| `AiGatewayAudience` | Entra audience validated by APIM |
| `AiGatewayApiVersion` | Chat Completions API version, e.g. `2024-10-21` |
| `ContentUnderstandingEndpoint` | OCR account endpoint; do not assume this is the Spoke account |

The former data-service, VNet, private-DNS-zone, project display/description,
`FoundryAccountEndpoint` and `AgentContainerRegistryName` pipeline variables
are no longer used.

Run the manual pipeline with the service connection, azd environment name and
`deploymentAgentPool` parameter. The default pool name
`REPLACE_WITH_PRIVATE_AGENT_POOL` is a placeholder for an existing self-hosted **Linux**
pool with Bash, Azure CLI and private DNS/network access to the Spoke endpoints
used by code deployment. This change does not provision that pool. The pipeline
deploys directly without a separate source-validation stage.

The deployment job uses a clean checkout, installs azd and the tested
`microsoft.foundry` extension version `1.0.0-beta.17`, checks that the target
project exists, and creates a fresh local azd environment. It configures
project/APIM/model/OCR values, runs `azd deploy` and reports the agent status.
The azd global configuration is isolated to the job's temporary directory.
No ARM infrastructure deployment or role assignments are performed.

### Tenant-wide Teams endpoint

Set the pipeline parameter `enableTeamsEndpoint` to `true` to configure the
protected M365 Activity route immediately after `azd deploy`. It defaults to
`true` for this Teams example. Set it to `false` to skip endpoint configuration;
this does not disable an existing M365 route. The private Linux deployment
runner also needs Python 3.

The [endpoint script](./scripts/enable_teams_endpoint.py) reads the existing
agent endpoint, preserves all protocol settings and non-Bot authorization schemes,
enables `activity.enable_m365_public_endpoint` and configures `BotServiceTenant`.
An existing `BotServiceTenant` is preserved; `BotServiceRbac` is explicitly
replaced with `BotServiceTenant`, broadening Teams access from Azure-authorized
users to users in the tenant. Direct Responses access remains Entra/RBAC-protected.
Publish separately with `publishScope: Tenant` and obtain Microsoft 365 admin
approval; configuring the endpoint does not publish or approve the app.
It patches only when needed and reads the configuration back to verify it.
Conflicting Bot Service schemes cause an explicit failure rather than silently
changing an organization's publishing scope. Do not edit the endpoint
concurrently with deployment because the API replaces the supplied lists.

This is a deliberate, source-IP-filtered and authenticated public route for
M365 channel traffic, not public access to the Foundry account or Responses API.
Use deployment-environment approvals for this opt-in network change. The
service connection must be authorized to update the agent endpoint through the
private project API. Account network settings are not changed.
No bot, Teams channel, app package or publication is created by this option.
For the separate bot deployment, use [infra/bot-service.bicep](./infra/bot-service.bicep).
It creates the single-tenant Azure Bot and Teams channel using the agent's
`instance_identity.client_id`, keeps bot public network access disabled, and
outputs `botServiceArmId` for the publish API. Supply `botName`, `displayName`,
`msaAppId`, `tenantId` and the full Activity protocol `endpoint`; the SKU defaults
to `F0`. This template does not enable the agent's M365 route or publish the app.
Complete those as separate Teams onboarding steps and test attachments separately.
For example, the bot messaging endpoint for `document-agent` is
`https://<spoke-account>.services.ai.azure.com/api/projects/<project>/agents/document-agent/endpoint/protocols/activityprotocol?api-version=2025-11-15-preview`.
This is the agent's Teams ingress URL, not the central APIM model URL.
See [Microsoft's private-project publishing guide](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network).

Authorize the variable group, deployment pool, service connection and
`central-ai-agent` Azure DevOps environment for this pipeline. Use the
environment's approval checks where required. The service connection needs
read access to the existing account/project and the project-scoped Foundry
code-deployment permissions (Foundry User and Contributor per the
[Microsoft CI/CD guide](https://learn.microsoft.com/azure/foundry/agents/quickstarts/set-up-cicd-hosted-agent?pivots=azd)).
It does not need subscription-wide infrastructure or role-assignment rights.

Runtime permissions remain separate from deployment permissions. After the
first deployment creates the agent identity, onboard that actual identity for
APIM, OCR, project Files and component-scoped telemetry as required. Do not
grant it direct access to the central model account. Until onboarding and an
end-to-end smoke test succeed, a successful deployment alone does not establish
runtime readiness.

Application Insights is supplied by the Spoke template. Foundry injects the
reserved `APPLICATIONINSIGHTS_CONNECTION_STRING` from its project connection;
do not override it in the pipeline or the hosted-agent environment map.

## Local workflow

Create an azd environment, set the same project/APIM/model/OCR configuration
listed above using the environment keys in [azure.yaml](./azure.yaml), and run
`azd deploy --environment <environment> --no-prompt` after signing in.
Also set `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
`AZURE_AI_PROJECT_ID` and `AZURE_LOCATION` for the existing Spoke.
The former `infra-*`, `agent-sync-env` and `deploy-all` Make targets have been
removed. Configure the existing Spoke environment before using `make agent-deploy`.

See the [agent README](./src/document-agent/README.md) for runtime details.

## Local validation

The deployment helper tests use only the Python standard library and no Azure
credentials. From the repository root, run
`python -m unittest discover -s scripts/tests -p 'test_*.py'`.
Run these checks locally before deployment; the pipeline does not run them.

From `src/document-agent`, using the repository virtual environment:

```powershell
..\..\.venv\Scripts\python.exe -m compileall -q .
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -p '*unit.py'
```
