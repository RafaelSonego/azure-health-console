"""What a resource is called on screen, and which types are noise in a dependency graph."""

# One per alert rule or deployment: they reference the resources they watch,
# which would bury the real dependencies.
NOISE_TYPES = {
    "microsoft.insights/metricalerts",
    "microsoft.insights/actiongroups",
    "microsoft.insights/activitylogalerts",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.insights/diagnosticsettings",
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.alertsmanagement/actionrules",
    "microsoft.resources/deployments",
    "microsoft.portal/dashboards",
    "microsoft.insights/workbooks",
    "microsoft.compute/galleries",
    "microsoft.compute/images",
}

DISPLAY_NAMES = {
    "microsoft.web/sites": "Web App",
    "microsoft.web/serverfarms": "App Service Plan",
    "microsoft.web/certificates": "Web Certificate",
    "microsoft.web/connections": "API Connection",
    "microsoft.insights/components": "Application Insights",
    "microsoft.insights/actiongroups": "Action Group",
    "microsoft.insights/workbooks": "Workbook",
    "microsoft.operationalinsights/workspaces": "Log Analytics",
    "microsoft.operationalinsights/querypacks": "Query Pack",
    "microsoft.managedidentity/userassignedidentities": "Managed Identity",
    "microsoft.keyvault/vaults": "Key Vault",
    "microsoft.storage/storageaccounts": "Storage Account",
    "microsoft.compute/virtualmachines": "Virtual Machine",
    "microsoft.compute/virtualmachinescalesets": "VM Scale Set",
    "microsoft.compute/disks": "Disk",
    "microsoft.compute/diskaccesses": "Disk Access",
    "microsoft.compute/virtualmachines/extensions": "VM Extension",
    "microsoft.network/networkinterfaces": "Network Interface",
    "microsoft.network/virtualnetworks": "Virtual Network",
    "microsoft.network/subnets": "Subnet",
    "microsoft.network/networksecuritygroups": "NSG",
    "microsoft.network/routetables": "Route Table",
    "microsoft.network/privateendpoints": "Private Endpoint",
    "microsoft.network/publicipaddresses": "Public IP",
    "microsoft.network/privatednszones": "Private DNS Zone",
    "microsoft.network/dnszones": "DNS Zone",
    "microsoft.network/applicationgateways": "Application Gateway",
    "microsoft.network/frontdoors": "Front Door",
    "microsoft.cdn/profiles": "Front Door / CDN",
    "microsoft.containerservice/managedclusters": "AKS Cluster",
    "microsoft.containerregistry/registries": "Container Registry",
    "microsoft.app/containerapps": "Container App",
    "microsoft.eventhub/namespaces": "Event Hub",
    "microsoft.servicebus/namespaces": "Service Bus",
    "microsoft.eventgrid/systemtopics": "Event Grid System Topic",
    "microsoft.eventgrid/topics": "Event Grid Topic",
    "microsoft.logic/workflows": "Logic App",
    "microsoft.sql/servers": "SQL Server",
    "microsoft.sql/servers/databases": "SQL Database",
    "microsoft.documentdb/databaseaccounts": "Cosmos DB",
    "microsoft.cache/redis": "Redis Cache",
    "microsoft.cognitiveservices/accounts": "AI Services",
    "microsoft.search/searchservices": "AI Search",
    "microsoft.databricks/workspaces": "Databricks Workspace",
    "microsoft.datafactory/factories": "Data Factory",
    "microsoft.batch/batchaccounts": "Batch Account",
    "microsoft.portal/dashboards": "Dashboard",
    "microsoft.authorization/roleassignments": "Role Assignment",
    "microsoft.insights/metricalerts": "Metric Alert",
    "microsoft.insights/scheduledqueryrules": "Log Alert",
    "microsoft.dashboard/grafana": "Managed Grafana",
    "microsoft.monitor/accounts": "Azure Monitor Workspace",
    "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL Flexible Server",
    "microsoft.dbformysql/flexibleservers": "MySQL Flexible Server",
    "microsoft.synapse/workspaces": "Synapse Workspace",
    "microsoft.kusto/clusters": "Data Explorer Cluster",
    "microsoft.streamanalytics/streamingjobs": "Stream Analytics Job",
    "microsoft.appconfiguration/configurationstores": "App Configuration",
    "microsoft.communication/communicationservices": "Communication Services",
    "microsoft.signalrservice/signalr": "SignalR Service",
    "microsoft.signalrservice/webpubsub": "Web PubSub",
    "microsoft.recoveryservices/vaults": "Recovery Services Vault",
    "microsoft.dataprotection/backupvaults": "Backup Vault",
    "microsoft.app/managedenvironments": "Container Apps Environment",
    "microsoft.app/jobs": "Container App Job",
    "microsoft.web/staticsites": "Static Web App",
    "microsoft.containerinstance/containergroups": "Container Instance",
    "microsoft.apimanagement/service": "API Management",
    "microsoft.botservice/botservices": "Bot Service",
    "microsoft.machinelearningservices/workspaces": "Azure ML / Foundry Hub",
    "microsoft.cognitiveservices/accounts/projects": "AI Foundry Project",
    "microsoft.automation/automationaccounts": "Automation Account",
    "microsoft.network/loadbalancers": "Load Balancer",
    "microsoft.network/azurefirewalls": "Azure Firewall",
    "microsoft.network/firewallpolicies": "Firewall Policy",
    "microsoft.network/bastionhosts": "Bastion",
    "microsoft.network/natgateways": "NAT Gateway",
    "microsoft.network/ddosprotectionplans": "DDoS Protection Plan",
    "microsoft.network/virtualnetworkgateways": "VPN / ExpressRoute Gateway",
    "microsoft.network/localnetworkgateways": "Local Network Gateway",
    "microsoft.network/trafficmanagerprofiles": "Traffic Manager",
    "microsoft.network/frontdoorwebapplicationfirewallpolicies": "WAF Policy",
    "microsoft.network/applicationgatewaywebapplicationfirewallpolicies": "App Gateway WAF Policy",
    "microsoft.network/virtualwans": "Virtual WAN",
    "microsoft.network/virtualhubs": "Virtual WAN Hub",
    "microsoft.management/managementgroups": "Management Group",
    "microsoft.resources/subscriptions": "Subscription",
    "microsoft.resources/resourcegroups": "Resource Group",
    "microsoft.authorization/policyassignments": "Policy Assignment",
    "microsoft.consumption/budgets": "Budget",
}

# Azure's own product names where they differ from the label above, so a filter for "kubernetes" finds AKS.
ALSO_CALLED = {
    "microsoft.containerservice/managedclusters": "Kubernetes service k8s",
    "microsoft.web/sites": "App Service Function App",
    "microsoft.web/serverfarms": "App Service plan",
    "microsoft.cache/redis": "Azure Cache for Redis",
    "microsoft.documentdb/databaseaccounts": "Azure Cosmos DB account",
    "microsoft.insights/components": "App Insights",
    "microsoft.operationalinsights/workspaces": "Log Analytics workspace",
    "microsoft.network/networksecuritygroups": "Network security group",
    "microsoft.apimanagement/service": "API Management service APIM",
    "microsoft.containerregistry/registries": "ACR",
    "microsoft.cognitiveservices/accounts": "Azure OpenAI Cognitive Services",
    "microsoft.compute/virtualmachinescalesets": "Virtual machine scale set VMSS",
    "microsoft.network/applicationgateways": "App Gateway WAF",
    "microsoft.network/publicipaddresses": "Public IP address",
    "microsoft.network/virtualnetworks": "VNet",
}


def names_for(resource_type):
    """Every name a type goes by, lower case: its type ID, its label and what people also call it ("k8s")."""
    lowered = (resource_type or "").lower()
    return f"{lowered} {DISPLAY_NAMES.get(lowered, '')} {ALSO_CALLED.get(lowered, '')}".lower()


def types_called(text):
    """The resource types whose label, Azure name or type ID mentions `text` (any case)."""
    text = (text or "").strip().lower()
    if not text:
        return []
    return sorted(t for t in set(DISPLAY_NAMES) | set(ALSO_CALLED) if text in names_for(t))


# Shared containers whose properties list every consumer (a VNet names each NIC
# in its subnets); shown in the graph but never expanded from.
LEAF_TYPES = {
    "microsoft.network/virtualnetworks",
    "microsoft.network/networksecuritygroups",
    "microsoft.network/routetables",
    "microsoft.network/natgateways",
    "microsoft.network/privatednszones",
    "microsoft.network/ddosprotectionplans",
    "microsoft.network/networkwatchers",
    "microsoft.network/virtualnetworkgateways",
    "microsoft.web/serverfarms",
    "microsoft.operationalinsights/workspaces",
}


def is_noise(resource_type):
    return (resource_type or "").lower() in NOISE_TYPES


# Plumbing the graph leaves out unless it joins two other resources (app.js graphElements); the Table still lists it.
GRAPH_PLUMBING = {"microsoft.network/networkinterfaces", "microsoft.network/privateendpoints",
                  "microsoft.eventgrid/systemtopics", "microsoft.web/connections",
                  "microsoft.insights/autoscalesettings", "microsoft.containerregistry/registries/replications"}


# A group's graph is drawn in bands by role, top to bottom in this order (app.js bandLayout).
TIERS = [
    ("ingress", "Ingress"),
    ("compute", "Compute"),
    ("platform", "Hosting"),
    ("data", "Data & messaging"),
    ("security", "Identity & secrets"),
    ("observability", "Observability"),
    ("network", "Network"),
    ("other", "Other"),
]
TIER_RANK = {key: index for index, (key, _label) in enumerate(TIERS)}
_TIER_BY_TYPE = {
    "microsoft.cdn/profiles": "ingress",
    "microsoft.network/frontdoors": "ingress",
    "microsoft.network/applicationgateways": "ingress",
    "microsoft.network/publicipaddresses": "ingress",
    "microsoft.network/dnszones": "ingress",
    "microsoft.network/trafficmanagerprofiles": "ingress",
    "microsoft.network/frontdoorwebapplicationfirewallpolicies": "ingress",
    "microsoft.apimanagement/service": "ingress",
    "microsoft.web/serverfarms": "platform",
    "microsoft.containerregistry/registries": "platform",  # where workloads pull their images from
    "microsoft.compute/disks": "platform",
    "microsoft.app/managedenvironments": "platform",
    "microsoft.network/networksecuritygroups": "network",  # "security" in the name, but a network control
    "microsoft.keyvault/vaults": "security",
    "microsoft.managedidentity/userassignedidentities": "security",
    "microsoft.web/certificates": "security",
    "microsoft.cognitiveservices/accounts": "compute",
    "microsoft.botservice/botservices": "compute",
    "microsoft.machinelearningservices/workspaces": "compute",
    "microsoft.automation/automationaccounts": "compute",
    "microsoft.search/searchservices": "data",
    "microsoft.synapse/workspaces": "data",
    "microsoft.kusto/clusters": "data",
    "microsoft.streamanalytics/streamingjobs": "data",
    "microsoft.appconfiguration/configurationstores": "data",
    "microsoft.signalrservice/signalr": "data",
    "microsoft.signalrservice/webpubsub": "data",
    "microsoft.recoveryservices/vaults": "data",
    "microsoft.dataprotection/backupvaults": "data",
    "microsoft.dashboard/grafana": "observability",
}
_TIER_BY_KEYWORD = [
    ("observability", ("insights", "operationalinsights", "alertsmanagement", "monitor", "portal/dashboards")),
    ("security", ("keyvault", "managedidentity", "security", "authorization")),
    ("network", ("microsoft.network/", "privatednszones")),
    ("data", ("storage", "sql", "documentdb", "cache/redis", "eventhub", "servicebus", "eventgrid",
              "datafactory", "databricks")),
    ("compute", ("microsoft.web/", "microsoft.compute/", "container", "microsoft.app/", "logic/workflows", "batch")),
]


def tier(resource_type):
    """The band a type is drawn in: an exact type first, then a keyword in it, else "other"."""
    lowered = (resource_type or "").lower()
    if lowered in _TIER_BY_TYPE:
        return _TIER_BY_TYPE[lowered]
    return next((name for name, words in _TIER_BY_KEYWORD if any(w in lowered for w in words)), "other")


def graph_role(resource_type):
    """"plumbing" or "": how the page's graph treats a type (app.js graphElements)."""
    return "plumbing" if (resource_type or "").lower() in GRAPH_PLUMBING else ""


def is_leaf(resource_type):
    return (resource_type or "").lower() in LEAF_TYPES


def display_name(resource_type, kind=None):
    """`kind` tells apart the three products Azure files under microsoft.web/sites."""
    lowered = (resource_type or "").lower()
    kind_lowered = (kind or "").lower()

    if lowered == "microsoft.web/sites":
        if "workflowapp" in kind_lowered:
            return "Logic App (Standard)"
        if "functionapp" in kind_lowered:
            return "Function App"
        return "Web App"

    if lowered in DISPLAY_NAMES:
        return DISPLAY_NAMES[lowered]
    tail = lowered.split("/")[-1]
    return tail or lowered
