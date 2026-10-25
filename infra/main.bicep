// Interview Evaluator on Azure Container Apps.
// Deployed twice per release by .github/workflows/ci-cd.yml:
//   1. without containerImage: ensures the registry and shared infra exist so the image can be pushed;
//   2. with containerImage (+ secrets): creates/updates the Container App on that image.

@description('Short prefix for resource names (lowercase letters and digits).')
@minLength(3)
@maxLength(12)
param namePrefix string = 'interveval'

param location string = resourceGroup().location

@description('Full image reference to run, e.g. myacr.azurecr.io/interview-evaluator:<sha>. Empty = infra only.')
param containerImage string = ''

@secure()
param anthropicApiKey string = ''

@secure()
@description('Clients must send this in X-API-Key.')
param serviceApiKey string = ''

@secure()
@description('Voyage AI key for embeddings (rubric library and calibration).')
param voyageApiKey string = ''

param rateLimitPerMin int = 10

param postgresAdminLogin string = 'evaluator'

@secure()
@description('Postgres admin password; required (the server is created on the first, infra-only deploy).')
param postgresAdminPassword string

var suffix = uniqueString(resourceGroup().id)
var deployApp = !empty(containerImage)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${namePrefix}${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// User-assigned so the AcrPull grant exists before the app's first image pull.
resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-pull'
  location: location
}

var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, pullIdentity.id, acrPullRoleId)
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${namePrefix}-pg-${suffix}'
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '17'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

// Allow-list pgvector so the app's first migration can CREATE EXTENSION vector.
resource pgExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: { value: 'VECTOR', source: 'user-override' }
}

resource pgDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'evaluator'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

// Consumption Container Apps have no fixed outbound IP, so allow Azure-internal traffic
// (0.0.0.0 rule). Access still needs the password, over TLS (sslmode=require).
resource pgAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// Speech-to-text for recording uploads and the live mic. The key stays server-side (browsers get
// short-lived tokens from POST /speech/token).
resource speech 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: '${namePrefix}-speech-${suffix}'
  location: location
  kind: 'SpeechServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${namePrefix}-speech-${suffix}'
    publicNetworkAccess: 'Enabled'
  }
}

// Container App secrets can't be empty, so optional keys are added only when provided.
var optionalSecrets = empty(voyageApiKey) ? [] : [{ name: 'voyage-api-key', value: voyageApiKey }]
var optionalSecretEnv = empty(voyageApiKey) ? [] : [{ name: 'VOYAGE_API_KEY', secretRef: 'voyage-api-key' }]

var databaseUrl = 'postgresql://${postgresAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres.properties.fullyQualifiedDomainName}:5432/${pgDatabase.name}?sslmode=require'

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: '${namePrefix}-app'
  location: location
  dependsOn: [acrPull, pgExtensions, pgAllowAzure]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${pullIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        { server: acr.properties.loginServer, identity: pullIdentity.id }
      ]
      secrets: concat([
        { name: 'anthropic-api-key', value: anthropicApiKey }
        { name: 'service-api-key', value: serviceApiKey }
        { name: 'database-url', value: databaseUrl }
        { name: 'azure-speech-key', value: speech.listKeys().key1 }
      ], optionalSecrets)
    }
    template: {
      containers: [
        {
          name: 'app'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat([
            { name: 'PORT', value: '8000' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'SERVICE_API_KEY', secretRef: 'service-api-key' }
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            { name: 'RATE_LIMIT_PER_MIN', value: string(rateLimitPerMin) }
            { name: 'AZURE_SPEECH_KEY', secretRef: 'azure-speech-key' }
            { name: 'AZURE_SPEECH_REGION', value: location }
            { name: 'AZURE_SPEECH_ENDPOINT', value: speech.properties.endpoint }
          ], optionalSecretEnv)
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 10 }
          ]
        }
      ]
      // Exactly one replica: rate limits (and later live sessions) are in memory.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output appFqdn string = deployApp ? app!.properties.configuration.ingress.fqdn : ''
