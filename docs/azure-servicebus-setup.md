# Azure Service Bus + Event Grid (Graph) provisioning

Ingress chain: Outlook → Microsoft Graph → Event Grid Partner Topic → Service Bus queue → mailflow.
No Event Hubs. No public webhook.

## 1. Service Bus (you likely already have this)
az servicebus namespace create -g <RG> -n <SB_NAMESPACE> --sku Standard
az servicebus queue create -g <RG> --namespace-name <SB_NAMESPACE> -n mailflow-graph \
  --max-delivery-count 10 --enable-duplicate-detection true
# RECORD: fqns = <SB_NAMESPACE>.servicebus.windows.net ; queue = mailflow-graph

## 2. Register Event Grid + authorize Graph as a partner
az provider register --namespace Microsoft.EventGrid
# Authorize the Microsoft Graph partner to create a partner topic in <RG>
# (portal: Event Grid → Partner Configurations → add Microsoft Graph; or per Learn:
#  https://learn.microsoft.com/azure/event-grid/subscribe-to-partner-events )

## 3. Create the Graph subscription that targets the partner topic
#  notificationUrl uses the EventGrid: scheme (NOT the EventHub: scheme).
POST https://graph.microsoft.com/v1.0/subscriptions
{
  "changeType": "created,updated",
  "notificationUrl": "EventGrid:?azuresubscriptionid=<SUB>&resourcegroup=<RG>&partnertopic=mailflow-graph-topic&location=<REGION>",
  "lifecycleNotificationUrl": "EventGrid:?azuresubscriptionid=<SUB>&resourcegroup=<RG>&partnertopic=mailflow-graph-topic&location=<REGION>",
  "resource": "users/<MAILBOX-UPN>/mailFolders('inbox')/messages",
  "expirationDateTime": "<now+6days, RFC3339>",
  "clientState": "mailflow"
}
# NOTE (CD-2): if 'created' is rejected for messages via Event Grid, use "updated"
# and rely on the timer sweep for new mail (see runtime). Confirm in Task A2.

## 4. Activate the partner topic + route it to the Service Bus queue
az eventgrid partner topic activate -g <RG> -n mailflow-graph-topic
az eventgrid partner topic event-subscription create \
  -g <RG> --partner-topic-name mailflow-graph-topic -n to-sb \
  --endpoint-type servicebusqueue \
  --endpoint /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.ServiceBus/namespaces/<SB_NAMESPACE>/queues/mailflow-graph

## 5. Consumer access
# Give the mailflow identity "Azure Service Bus Data Receiver" on the queue
# (and "Data Sender" if it also emits to a Service Bus queue for egress).
