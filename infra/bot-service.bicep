targetScope = 'resourceGroup'

@minLength(2)
@description('Globally unique Azure Bot Service name.')
param botName string

@minLength(1)
@description('Bot display name shown to users.')
param displayName string

@minLength(36)
@maxLength(36)
@description('Foundry agent instance_identity.client_id, not the project or blueprint identity.')
param msaAppId string

@minLength(36)
@maxLength(36)
@description('Microsoft Entra tenant ID containing the agent identity.')
param tenantId string

@minLength(1)
@description('Full Foundry agent Activity URL ending in /endpoint/protocols/activityProtocol.')
param endpoint string

@description('Azure Bot Service SKU. Free tier is sufficient for the initial example deployment.')
param botServiceSku string = 'F0'

// Resource API versions follow the Foundry private-network publishing guide.
resource botService 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  kind: 'azurebot'
  location: 'global'
  sku: {
    name: botServiceSku
  }
  properties: {
    displayName: displayName
    endpoint: endpoint
    msaAppId: msaAppId
    msaAppTenantId: tenantId
    msaAppType: 'SingleTenant'
    publicNetworkAccess: 'Disabled'
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2021-03-01' = {
  parent: botService
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
  }
}

@description('Pass this resource ID as botServiceArmId to the Foundry M365 publish API.')
output botServiceArmId string = botService.id
