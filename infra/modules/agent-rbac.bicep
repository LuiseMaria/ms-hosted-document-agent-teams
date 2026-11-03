// ---------------------------------------------------------------------------
// RBAC for the Foundry-managed Agent Instance Identity
//
// The runtime identity of the hosted agent is NOT the user-assigned managed
// identity from an older agent deployment. Foundry Agent Service creates a dedicated
// "Agent Instance Identity" (display name "<account>-<project>-document-agent-
// AgentIdentity", ManagedAgentIdentityBlueprint) automatically on the FIRST
// `azd deploy`. It therefore does not exist during the initial infra
// provisioning and its role assignments must be applied in a SEPARATE
// deployment run AFTER the agent has been deployed at least once.
//
// These are the roles that were verified to be required at runtime (see
// docs/infrastructure-rbac.md and infra/agent-role-assignments.json):
//   * Cognitive Services User   – non-OpenAI data-plane ops on the AIServices
//                                 account, incl. Azure AI Content Understanding
//                                 analyzer runs
//                                 (Microsoft.CognitiveServices/accounts/
//                                  MultiModalIntelligence/analyzers:analyze/action)
//   * Foundry Agent Consumer    – invoke/consume the hosted agent
//
// Deploy (see Makefile target `agent-rbac`):
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file infra/modules/agent-rbac.bicep \
//     --parameters accountName=<aif-account> agentPrincipalId=<agent-object-id>
// ---------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Foundry (AIServices) account name that hosts the agent.')
param accountName string

@description('Object (principal) ID of the Foundry-managed Agent Instance Identity.')
param agentPrincipalId string

// 'Cognitive Services User' built-in role — non-OpenAI data-plane operations on
// the AIServices account, e.g. Azure AI Content Understanding analyzer runs.
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
// 'Foundry Agent Consumer' built-in role — invoke/consume the hosted agent.
var foundryAgentConsumerRoleId = 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource agentCognitiveServicesUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, agentPrincipalId, cognitiveServicesUserRoleId)
  scope: account
  properties: {
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
  }
}

resource agentFoundryConsumerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, agentPrincipalId, foundryAgentConsumerRoleId)
  scope: account
  properties: {
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryAgentConsumerRoleId)
  }
}

output accountId string = account.id
output agentPrincipalId string = agentPrincipalId
