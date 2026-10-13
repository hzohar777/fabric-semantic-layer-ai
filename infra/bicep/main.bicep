// TeraSky Fabric Semantic Layer AI — Azure Infrastructure
// Deploys: Container Apps Environment, MCP Server ACA, Azure AI Search, Azure OpenAI, ACR
//
// Usage:
//   az deployment group create \
//     --resource-group rg-fabric-semantic-layer-ai \
//     --template-file infra/bicep/main.bicep \
//     --parameters @infra/bicep/parameters.json

@description('Location for all resources')
param location string = resourceGroup().location

@description('Name prefix for all resources')
param prefix string = 'fabric-ai'

@description('Azure Container Apps image (from ACR)')
param mcpServerImage string

@description('Azure Entra ID tenant ID')
param tenantId string

@description('Azure Entra ID app client ID')
@secure()
param clientId string

@description('Azure Entra ID app client secret')
@secure()
param clientSecret string

@description('Power BI access token (PoC only — rotate hourly in production)')
@secure()
param powerBiAccessToken string = ''

// ── Container Registry ────────────────────────────────────────────────────────
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${replace(prefix, '-', '')}acr'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

// ── Log Analytics ─────────────────────────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

// ── Container Apps Environment ────────────────────────────────────────────────
resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ── Azure OpenAI ──────────────────────────────────────────────────────────────
resource openAI 'Microsoft.CognitiveServices/accounts@2024-04-01-preview' = {
  name: '${prefix}-openai'
  location: location
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: { customSubDomainName: '${prefix}-openai' }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-04-01-preview' = {
  parent: openAI
  name: 'text-embedding-3-small'
  sku: { name: 'GlobalStandard', capacity: 120 }
  properties: {
    model: { format: 'OpenAI', name: 'text-embedding-3-small', version: '1' }
  }
}

// ── Azure AI Search ───────────────────────────────────────────────────────────
resource aiSearch 'Microsoft.Search/searchServices@2024-03-01-preview' = {
  name: '${prefix}-search'
  location: location
  sku: { name: 'basic' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'Enabled'
    semanticSearch: 'standard'
  }
}

// ── MCP Server — Azure Container App ─────────────────────────────────────────
resource mcpServer 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-mcp-server'
  location: location
  properties: {
    managedEnvironmentId: acaEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: [{
        server: acr.properties.loginServer
        username: acr.name
        passwordSecretRef: 'acr-password'
      }]
      secrets: [
        { name: 'acr-password', value: acr.listCredentials().passwords[0].value }
        { name: 'client-secret', value: clientSecret }
        { name: 'pbi-token', value: powerBiAccessToken }
        { name: 'search-key', value: aiSearch.listAdminKeys().primaryKey }
        { name: 'openai-key', value: openAI.listKeys().key1 }
      ]
    }
    template: {
      containers: [{
        name: 'mcp-server'
        image: mcpServerImage
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'AZURE_TENANT_ID',          value: tenantId }
          { name: 'AZURE_CLIENT_ID',          value: clientId }
          { name: 'AZURE_CLIENT_SECRET',      secretRef: 'client-secret' }
          { name: 'AZURE_SEARCH_ENDPOINT',    value: 'https://${aiSearch.name}.search.windows.net' }
          { name: 'AZURE_SEARCH_API_KEY',     secretRef: 'search-key' }
          { name: 'AZURE_OPENAI_ENDPOINT',    value: openAI.properties.endpoint }
          { name: 'AZURE_OPENAI_API_KEY',     secretRef: 'openai-key' }
          { name: 'POWERBI_ACCESS_TOKEN',     secretRef: 'pbi-token' }
          { name: 'PORT',                     value: '8080' }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output mcpServerUrl string = 'https://${mcpServer.properties.configuration.ingress.fqdn}/mcp'
output acrLoginServer string = acr.properties.loginServer
output searchEndpoint string = 'https://${aiSearch.name}.search.windows.net'
output openAIEndpoint string = openAI.properties.endpoint
