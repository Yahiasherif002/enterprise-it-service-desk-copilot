@allowed(['dev', 'uat', 'prod'])
param env string

param location string = resourceGroup().location

@description('Application (client) ID of the Entra app registration that protects the API.')
param apiClientId string

@description('Application (client) ID of the Entra app registration used by the Power Platform custom connector.')
param connectorClientId string

@description('Object ID and display name of the Entra group that administers Azure SQL.')
param sqlAdminGroupId string
param sqlAdminGroupName string

@description('ServiceNow instance URL, for example https://dev12345.service-now.com.')
param serviceNowUrl string
param serviceNowUser string

var name = 'servicedesk-${env}'
var suffix = take(uniqueString(resourceGroup().id), 4)
var blobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var keyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${name}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${replace(name, '-', '')}${suffix}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
  }

  resource blob 'blobServices' = {
    name: 'default'

    resource deployments 'containers' = {
      name: 'deployments'
    }
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${name}-${suffix}'
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource sql 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: 'sql-${name}-${suffix}'
  location: location
  properties: {
    minimalTlsVersion: '1.2'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: sqlAdminGroupName
      sid: sqlAdminGroupId
      tenantId: tenant().tenantId
      principalType: 'Group'
    }
  }

  resource azureServices 'firewallRules' = {
    name: 'AllowAllWindowsAzureIps'
    properties: {
      startIpAddress: '0.0.0.0'
      endIpAddress: '0.0.0.0'
    }
  }

  resource db 'databases' = {
    name: 'servicedesk'
    location: location
    sku: { name: 'GP_S_Gen5_2', tier: 'GeneralPurpose' }
    properties: {
      useFreeLimit: true
      freeLimitExhaustionBehavior: 'AutoPause'
      autoPauseDelay: 60
      minCapacity: json('0.5')
      maxSizeBytes: 34359738368
    }
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${name}'
  location: location
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-${name}-${suffix}'
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: { minTlsVersion: '1.2' }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}deployments'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: { maximumInstanceCount: 40, instanceMemoryMB: 2048 }
      runtime: { name: 'python', version: '3.12' }
    }
  }
}

resource storageAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, app.id, blobDataOwner)
  scope: storage
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataOwner)
  }
}

resource vaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, app.id, keyVaultSecretsUser)
  scope: vault
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUser)
  }
}

resource settings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'appsettings'
  properties: {
    AzureWebJobsStorage__accountName: storage.name
    APPLICATIONINSIGHTS_CONNECTION_STRING: insights.properties.ConnectionString
    SQL_CONNECTION_STRING: 'Driver={ODBC Driver 18 for SQL Server};Server=tcp:${sql.properties.fullyQualifiedDomainName},1433;Database=${sql::db.name};Authentication=ActiveDirectoryMsi;Encrypt=yes;Connection Timeout=60'
    SERVICENOW_URL: serviceNowUrl
    SERVICENOW_USER: serviceNowUser
    SERVICENOW_PASSWORD: '@Microsoft.KeyVault(VaultName=${vault.name};SecretName=servicenow-password)'
  }
  dependsOn: [storageAccess, vaultAccess]
}

resource auth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: apiClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [apiClientId, 'api://${apiClientId}']
          defaultAuthorizationPolicy: {
            allowedApplications: [connectorClientId]
          }
        }
      }
    }
  }
}

output functionAppName string = app.name
output apiHost string = app.properties.defaultHostName
output sqlServer string = sql.properties.fullyQualifiedDomainName
output vaultName string = vault.name
