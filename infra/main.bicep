// Interview Evaluator on Azure Container Apps, sized for near-zero monthly cost.
// Deployed once per release by .github/workflows/ci-cd.yml with containerImage (+ secrets). The image
// comes from a public ghcr.io package (anonymous pull) and the database is external (e.g. Neon), so
// there is no registry, database server or role assignment here.

@description('Short prefix for resource names (lowercase letters and digits).')
@minLength(3)
@maxLength(12)
param namePrefix string = 'interveval'

param location string = resourceGroup().location

@description('Public image to run, e.g. ghcr.io/<owner>/interview-evaluator:<sha>. Empty = shared infra only.')
param containerImage string = ''

@secure()
param anthropicApiKey string = ''

@secure()
@description('Clients must send this in X-API-Key.')
param serviceApiKey string = ''

@secure()
@description('Postgres connection string with pgvector (e.g. Neon). Empty = persistence off.')
param databaseUrl string = ''

@secure()
@description('Voyage AI key for embeddings (rubric library and calibration).')
param voyageApiKey string = ''

param rateLimitPerMin int = 10

@allowed(['F0', 'S0'])
@description('Tier of the Speech resource created here. F0 is free but one per subscription and may lack fast transcription (recording uploads).')
param speechSku string = 'F0'

@secure()
@description('Key of an existing Speech resource (e.g. the F0 one used for local development). Set = no Speech resource is created here.')
param existingSpeechKey string = ''

@description('Region of that existing Speech resource, e.g. westeurope. Required with existingSpeechKey.')
param existingSpeechRegion string = ''

var suffix = uniqueString(resourceGroup().id)
var deployApp = !empty(containerImage)
var createSpeech = empty(existingSpeechKey)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
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

// Speech-to-text for recording uploads and the live mic. The key stays server-side (browsers get
// short-lived tokens from POST /speech/token). Skipped when an existing resource's key is passed in.
resource speech 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (createSpeech) {
  name: '${namePrefix}-speech-${suffix}'
  location: location
  kind: 'SpeechServices'
  sku: { name: speechSku }
  properties: {
    customSubDomainName: '${namePrefix}-speech-${suffix}'
    publicNetworkAccess: 'Enabled'
  }
}

// Container App secrets can't be empty, so optional ones are added only when provided.
var voyageSecret = empty(voyageApiKey) ? [] : [{ name: 'voyage-api-key', value: voyageApiKey }]
var voyageEnv = empty(voyageApiKey) ? [] : [{ name: 'VOYAGE_API_KEY', secretRef: 'voyage-api-key' }]
var databaseSecret = empty(databaseUrl) ? [] : [{ name: 'database-url', value: databaseUrl }]
var databaseEnv = empty(databaseUrl) ? [] : [{ name: 'DATABASE_URL', secretRef: 'database-url' }]

// An existing resource gets no endpoint: the app derives the regional one from the region.
var speechKey = createSpeech ? speech!.listKeys().key1 : existingSpeechKey
var speechEnv = createSpeech ? [
  { name: 'AZURE_SPEECH_REGION', value: location }
  { name: 'AZURE_SPEECH_ENDPOINT', value: speech!.properties.endpoint }
] : [
  { name: 'AZURE_SPEECH_REGION', value: existingSpeechRegion }
]

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: '${namePrefix}-app'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      secrets: concat([
        { name: 'anthropic-api-key', value: anthropicApiKey }
        { name: 'service-api-key', value: serviceApiKey }
        { name: 'azure-speech-key', value: speechKey }
      ], databaseSecret, voyageSecret)
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
            { name: 'RATE_LIMIT_PER_MIN', value: string(rateLimitPerMin) }
            { name: 'AZURE_SPEECH_KEY', secretRef: 'azure-speech-key' }
          ], speechEnv, databaseEnv, voyageEnv)
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 10 }
          ]
        }
      ]
      // Scales to zero when idle (no cost), and to at most one replica because rate limits, live
      // sessions and transcription jobs are in memory. They reset when the app scales to zero,
      // which is acceptable for a demo.
      scale: { minReplicas: 0, maxReplicas: 1 }
    }
  }
}

output appFqdn string = deployApp ? app!.properties.configuration.ingress.fqdn : ''
