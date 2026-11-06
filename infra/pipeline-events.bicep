// Optional example infrastructure. Supply your own existing storage account.
param location string = resourceGroup().location
param topicName string = 'coreview-pipeline-events'
param namespaceName string = 'coreview-pipeline-${uniqueString(resourceGroup().id)}'
param storageAccountName string

resource topic 'Microsoft.EventGrid/topics@2022-06-15' = {
  name: topicName
  location: location
  properties: {
    inputSchema: 'EventGridSchema'
    publicNetworkAccess: 'Enabled'
  }
}

resource bus 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: namespaceName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

var routes = [
  { queue: 'file-upload', eventType: 'CoreView.FileUpload' }
  { queue: 'ome-generated', eventType: 'CoreView.OmeGenerated' }
]

resource queues 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = [for route in routes: {
  parent: bus
  name: route.queue
  properties: {
    lockDuration: 'PT5M'
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'P1D'
    defaultMessageTimeToLive: 'P14D'
    deadLetteringOnMessageExpiration: true
    maxDeliveryCount: 20
    maxSizeInMegabytes: 1024
  }
}]

// Queue-scoped listen credentials avoid namespace-wide management credentials.
resource listeners 'Microsoft.ServiceBus/namespaces/queues/authorizationRules@2024-01-01' = [for (route, i) in routes: {
  name: '${bus.name}/${queues[i].name}/consumer'
  properties: { rights: ['Listen'] }
}]

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}
resource deadletters 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'pipeline-event-deadletters'
  properties: { publicAccess: 'None' }
}

// Standard Event Grid destination authentication works without new RBAC grants.
// Deploying the subscription requires access to the destination resources.
resource subscriptions 'Microsoft.EventGrid/topics/eventSubscriptions@2022-06-15' = [for (route, i) in routes: {
  parent: topic
  name: route.queue
  properties: {
    eventDeliverySchema: 'EventGridSchema'
    filter: { includedEventTypes: [route.eventType] }
    destination: {
      endpointType: 'ServiceBusQueue'
      properties: {
        resourceId: queues[i].id
        deliveryAttributeMappings: [{
          name: 'MessageId'
          type: 'Dynamic'
          properties: { sourceField: 'id' }
        }]
      }
    }
    deadLetterDestination: {
      endpointType: 'StorageBlob'
      properties: {
        resourceId: storage.id
        blobContainerName: deadletters.name
      }
    }
    retryPolicy: {
      maxDeliveryAttempts: 30
      eventTimeToLiveInMinutes: 1440
    }
  }
}]

output eventGridEndpoint string = topic.properties.endpoint
output serviceBusNamespace string = '${bus.name}.servicebus.windows.net'
output uploadQueue string = queues[0].name
output catalogQueue string = queues[1].name
output uploadListenRuleId string = listeners[0].id
output catalogListenRuleId string = listeners[1].id
