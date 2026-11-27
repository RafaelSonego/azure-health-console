# Every request the console sends

All requests are reads. This file lists each one, so anyone can check that
nothing changes an Azure resource or a cluster. If you add a request to the
code, add it here.

**How it's enforced:**
- Every client that talks to anything outside the console (Azure Resource
  Manager, Microsoft Graph, a container registry, a Kubernetes API server,
  ServiceNow, New Relic) extends `GuardedClient` in `healthconsole/core/guarded.py`.
  Its `send` runs the client's own allow-list (`check`) before the request
  leaves the machine, so a refused request is never sent, and logs one line
  per request. `tests/test_guarded.py` sends a forbidden request through each
  of the six and checks that nothing reached the network; turning any one
  allow-list off makes the test suite fail.
- `check_read_only` in `healthconsole/core/arm.py` checks every Azure
  request before it's sent. It allows any `GET` to `management.azure.com`
  plus exactly the four `POST`s listed below. Anything else raises
  `WriteBlocked`, so nothing is sent.
- `check_kube_read` in `healthconsole/core/explore/kube.py` does the same
  for Kubernetes: `GET` only, and only on the paths listed below.
- Both are covered by tests (`tests/test_arm.py`, `tests/test_explore.py`),
  including requests that must be refused: `PUT`, `PATCH`, `DELETE`,
  `listKeys`, app settings, `runCommand`, the admin credential, secrets,
  `exec`.

**See it for yourself:** add `--log-calls` and every request is printed as
it's sent: method, path and API version, never tokens.

```bash
.venv/bin/python -m healthconsole --log-calls serve
.venv/bin/python -m healthconsole --log-calls export <resource-id>
```

Requests are cached in memory for 60 seconds per identity, so a repeated
view doesn't hit Azure again. A view's **Refresh** (and Browse's **Re-check**) asks Azure again instead
(`fresh=1` on the request), and its answers replace the cached ones. Nothing is written to disk except files you
export.

## Sign-in (local only, not a network call from the console)

| What | How |
|---|---|
| Azure token | `azure-identity`'s `AzureCliCredential` runs `az account get-access-token` for `https://management.azure.com/.default` (and `--tenant` when you pick another tenant) |
| AKS token, for Entra clusters | the same, for the AKS server app scope `6dae42f8-4368-4678-94ff-3960e28e3630/.default` |
| **Sign in** / **Sign in again** buttons (local console only) | `az login --allow-no-subscriptions --output none` (so an account with no usable subscription, a personal one say, is still saved and can get tokens), plus `--tenant <ID or domain>` (checked against a strict pattern) and `--use-device-code` when asked; one at a time, stopped after 5 minutes, subscription picker off for that run (`AZURE_CORE_LOGIN_EXPERIENCE_V2=off`). The Azure CLI does the sign-in and writes its own token cache; the console sends nothing to Azure itself. Code: `healthconsole/signin.py`, routes added only by `create_local_app` |

## Azure Resource Manager: `GET` (reads)

| Request | API version | Used for | Code |
|---|---|---|---|
| `GET /tenants` | 2022-12-01 | tenant picker | `core/inventory.py` |
| `GET /subscriptions` | 2022-12-01 | subscription picker | `core/inventory.py` |
| `GET /subscriptions/{sub}/providers/{namespace}` | 2021-04-01 | newest API version for a resource type (detectors, details) | `core/arm.py` `api_version` |
| `GET {resource}/providers/Microsoft.ResourceHealth/availabilityStatuses/current` | 2025-05-01 | availability signal | `core/health.py` |
| `GET {resource}/providers/Microsoft.ResourceHealth/availabilityStatuses` | 2025-05-01 | Resource Health history (Activity tab) | `core/explore/activity.py` |
| `GET {resource}/providers/Microsoft.Insights/metricDefinitions` | 2018-01-01 | which metrics and dimensions exist | `core/health.py`, `core/explore/metrics.py` |
| `GET {resource}/providers/Microsoft.Insights/metrics` | 2018-01-01 | metric values: signals, the last-week comparison, Metrics tab charts, AKS pods by phase, an Application Gateway's `CapacityUnits` (Maximum, last hour at 5 minutes) on its Scaling tab to estimate instances | `core/health.py`, `core/explore/metrics.py`, `core/explore/aks.py` |
| `GET {metric alert rule}` (`Microsoft.Insights/metricAlerts`) | 2018-03-01 | **Is this alert right?** on a fired Azure alert, when clicked: the rule's metric, aggregation, operator, threshold, window and dimensions (after one Resource Graph query of `AlertsManagementResources` for the alert's rule and resource). Then one `GET …/metrics` for that metric over `alert_check.days` + 1 at the rule's own window (with the rule's included dimensions as `$filter`). Kept 15 minutes | `core/azure_alerts.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.Insights/eventtypes/management/values` | 2015-04-01 | Activity Log for one resource, last 6 h, 24 h, 3, 7, 14 or 30 days (Timeline; Azure keeps 90), read as four time slices at once (up to 3 pages each), only the fields shown (`$select`), following `nextLink` up to 10 pages | `core/explore/activity.py` |
| `GET {subscription or resourceGroup}/providers/Microsoft.Consumption/budgets` | 2023-05-01 | the budgets on a subscription or group and what's spent against them, on its Browse **Cost** card and on Summary's Cost card (one per subscription in scope, kept an hour) | `core/cost.py` |
| `GET {resource}/detectors` and `{resource}/detectors/{name}` (each its own request from the Diagnostics tab, six at a time; opening a resource runs the same default set in the background, three at a time, once per resource per session, for the tab's count; a health check runs the same set after the other signals. Each detector's answer is kept 10 minutes per viewer and resource, so the check, the tab and its count read it once; a failed read isn't kept, and Refresh reads again) | provider's latest | list Azure's "Diagnose and solve problems" detectors | `core/explore/detectors.py` |
| `GET {resource}/detectors/{name}` | provider's latest | run one detector (it reads Microsoft's telemetry; nothing on the resource) | `core/explore/detectors.py` |
| `GET {site}/config/web` | provider's latest | App Service: runtime, Always On, health-check path. **Not** app settings or connection strings | `core/explore/details.py` |
| `GET {site}/slots` | provider's latest | deployment slots and their state | `core/explore/details.py` |
| `GET {site}/functions` | provider's latest | function list and disabled state (not keys) | `core/explore/details.py` |
| `GET {sqlDatabase}/usages` | provider's latest | size and other usage vs limit | `core/explore/details.py` |
| `GET {sqlDatabase}/automaticTuning/current` | provider's latest | automatic tuning state | `core/explore/details.py` |
| `GET {sqlDatabase}/replicationLinks` | provider's latest | geo-replication state | `core/explore/details.py` |
| `GET {redis}/patchSchedules/default` | provider's latest | maintenance window | `core/explore/details.py` |
| `GET {redis}/linkedServers` | provider's latest | geo-replication | `core/explore/details.py` |
| `GET {serviceBusNamespace}/queues`, `/topics` | provider's latest | message and dead-letter counts | `core/explore/details.py` |
| `GET {eventHubNamespace}/eventhubs` | provider's latest | partitions, status | `core/explore/details.py` |
| `GET {keyVault}/secrets`, `GET {keyVault}/keys` | 2023-07-01 | names, enabled, content type and expiry dates, to flag expired or soon-expiring secrets, keys and certificates (a certificate is the secret whose content type is PKCS#12 or PEM). Azure Resource Manager's list **never returns secret values**; the vault's own API isn't called. A vault's own page reads at most 40 pages (1,000 items) of each list and says when there are more; **Secrets & certificates** reads every page | `core/explore/keyvault.py` |
| `GET {virtualNetwork}/usages` | 2024-05-01 | IPs in use per subnet, when one VNet is opened on the Network tab, and for each AKS VNet in the readiness report | `core/network.py`, `core/readiness.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.Compute/locations/{region}/usages` | 2024-07-01 | vCPU quota per VM family and region, for the readiness report | `core/readiness.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.Compute/skus?$filter=location eq '{region}'` | 2021-07-01 | VM size → family and vCPUs, once per region (cached for a day), for the readiness report | `core/readiness.py` |
| `GET /providers/Microsoft.Authorization/roleEligibilityScheduleInstances?$filter=asTarget()` | 2020-10-01 | the PIM roles you're eligible for, for "no access" hints and Settings → PIM (cached 5 minutes) | `core/pim.py` |
| `GET /providers/Microsoft.Authorization/roleAssignmentScheduleRequests?$filter=asTarget()` | 2020-10-01 | your own PIM activations, so a role already active isn't suggested | `core/pim.py` |
| `GET /providers/Microsoft.Authorization/roleDefinitions?$filter=type eq 'BuiltInRole'` | 2022-04-01 | built-in role names and permissions, for Access → Check someone's access and *Your access here*'s "You can …" (Resource Graph holds custom roles only); one ~1.6 MB read, cached for a day | `core/user_access.py` |
| `GET {resource, group or subscription}/providers/Microsoft.Authorization/roleAssignments?$filter=assignedTo('{you}')` | 2022-04-01 | **Your access here**: your roles that apply there, direct, through groups or inherited (PIM activations matched by role and scope) | `core/user_access.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments?$filter=assignedTo('{user}')` | 2022-04-01 | that user's roles held at management groups or `/`, read once per distinct parent management group in scope (not per subscription) | `core/user_access.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleEligibilityScheduleInstances?$filter=assignedTo('{user}')` | 2020-10-01 | that user's PIM-eligible roles, directly or through groups, one per subscription in scope, 8 at a time (above 100 subscriptions the page asks first) | `core/user_access.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignmentScheduleInstances` | 2020-10-01 | **Access audit**: every role assignment at, below and inherited above the subscription, plain or through PIM (whether it's a PIM activation, and when it ends); one per subscription in scope, 8 at a time, about 0.85 s per subscription measured (above 100 subscriptions the page asks first). Without Reader on the subscription Azure answers 400 ("use `asTarget()`"), named as not readable | `core/access_audit.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleEligibilityScheduleInstances` | 2020-10-01 | **Access audit**: who is PIM-eligible for the same role, so a standing role that PIM already covers is flagged; with the read above | `core/access_audit.py` |
| `GET /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Authorization/roleAssignmentScheduleInstances` and `…/roleEligibilityScheduleInstances` | 2020-10-01 | **Access audit**, only in subscriptions that refuse the reads above: each resource group that holds assignments (from Resource Graph), 8 at a time | `core/access_audit.py` |
| `GET {virtualMachine}/instanceView` | provider's latest | power state, VM agent | `core/explore/details.py` |
| `GET /subscriptions/{sub}/providers/Microsoft.ContainerService/locations/{region}/kubernetesVersions` | 2024-02-01 | the Kubernetes versions Azure supports in a region and their support plans, for the Vulnerabilities tab (once per region, cached for a day) | `core/vulnerabilities.py` |
| `GET /providers/Microsoft.Web/webAppStacks` and `/functionAppStacks` (`stackOsType` Linux and Windows) | 2023-12-01 | Azure's runtime versions with their end-of-life dates (four reads, cached for a day) | `core/vulnerabilities.py` |
| `GET {site}/config/web` | 2023-12-01 | one App Service app's runtime, minimum TLS and FTP state, on its Vulnerabilities tab (app settings and connection strings are a separate `POST …/list`, still refused) | `core/vulnerabilities.py` |

Paging follows Azure's `nextLink`, which is also checked by
`check_read_only`, so a link to another host is refused.

## Microsoft Graph: `GET` only, four paths

| Request | Used for | Code |
|---|---|---|
| `GET https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group?$select=id,displayName` | the groups you're in (Access tab), cached 5 minutes | `core/graph.py` |
| `GET https://graph.microsoft.com/v1.0/users?$filter=startswith(displayName,'…') or startswith(userPrincipalName,'…') or startswith(mail,'…')&$top=10` | Access → Check someone's access, as you type (2 characters or more, after a 0.3 s pause), cached 60 seconds | `core/graph.py` |
| `GET https://graph.microsoft.com/v1.0/users/{object ID}` | the same, when you paste an object ID | `core/graph.py` |
| `GET https://graph.microsoft.com/v1.0/users/{object ID}/transitiveMemberOf/microsoft.graph.group?$select=id,displayName` | the picked user's groups, cached 5 minutes | `core/graph.py` |
| `GET https://graph.microsoft.com/v1.0/groups/{object ID}/transitiveMembers/microsoft.graph.user?$select=…&$top=999` | **Access audit**: the users in a group that holds a role above Reader (nested groups included), 8 at a time, cached 5 minutes | `core/graph.py` |
| `GET https://graph.microsoft.com/v1.0/users/{object ID}?$select=…` | **Access audit**: a person assigned a role directly (name, sign-in name, guest, enabled), cached 5 minutes; 404 means deleted (listed as an orphaned assignment) | `core/graph.py` |
| `GET https://graph.microsoft.com/v1.0/groups/{object ID}?$select=id,displayName` | **Access audit**: the name of a group Resource Graph gives only by ID; 404 means deleted | `core/graph.py` |
| `GET https://graph.microsoft.com/v1.0/directoryObjects/{object ID}` | **Access audit**: what a Key Vault access policy entry is (user, group, app) and its name; 404 means deleted | `core/graph.py` |

`check_graph_read` in `core/graph.py` allows only these paths, by `GET`, on `graph.microsoft.com`; anything else
raises `WriteBlocked` before it's sent. The token is the Azure CLI's, for `https://graph.microsoft.com/.default`.

## Azure Resource Manager: the only four `POST`s

Azure exposes these reads as actions, so they use `POST`. They don't change
anything.

| Request | API version | Used for | Code |
|---|---|---|---|
| `POST /providers/Microsoft.ResourceGraph/resources` | 2022-10-01 | KQL over the inventory: resource groups, resources, the dependency walk, fired alerts, resource changes, metric alert rules, autoscale settings, Service Health, Advisor, Defender for Cloud | many; all through `ArmClient.query` |
| `GET {component}/providers/microsoft.insights/metrics` | 2018-01-01 | the Triage card *App Insights: failure spikes*: `requests/count`, `requests/failed`, `exceptions/count` hourly over the last 7 days and 1 hour, one GET per component (first 50 in scope, eight at a time); the last bucket is compared with the first | `core/explore/appinsights.py` |
| `GET {component}/query` | 2018-04-20 | a resource's **Application Insights** tab (a component, a Web or Function App, an API Management service): requests by role now and a week ago and availability results, plus failing operations, exceptions and failing dependencies sent through Investigate's source, so the same queries as Investigate; six GETs per load, three at a time, and the two metrics GETs of Investigate's step 1 for its *Last hour* line (Overview's Needs attention makes only those two) | `core/explore/appinsights.py`, `core/investigate/appinsights.py` |
| `GET {component}/providers/microsoft.insights/metrics` (Investigate) | 2018-01-01 | step 1, *Which app is failing?*: the same hourly read as the Triage card (its last 24 points drawn as a trend), for the first 50 components in scope, eight at a time; then a second GET each for `requests/failed` over the last hour split by `request/resultCode` (`$filter=request/resultCode eq '*'`, `top=100`) to count 5xx and keep each code's count (shown as the top 5xx codes and the responses not sent in full); plus one Resource Graph query for the Web and Function Apps whose `hidden-link` tag names a component | `core/investigate/__init__.py` |
| `GET {component}/query` (Investigate) | 2018-04-20 | one GET per click: failed requests by HTTP code (two GETs side by side: one pass giving each code's total and its 5 busiest endpoints, and the exceptions on requests not sent in full or with code 0), failing operations, exceptions by type and failing dependencies by type and target (five GETs on picking an app); then one more for the endpoints a list shows (the failing endpoints, and a code's 5 when it's opened): their failed requests joined to failed `dependencies` by `operation_Id` for the backends, and `customDimensions['Subscription Name']` for API Management's callers. Every App Insights query may take 90 s (other reads 30 s): a first query over weeks of a busy component took over 30 s, an operation's samples (20, optionally only one code), one request's spans by `operation_Id` (`union` of requests, dependencies, exceptions, traces; up to 500), or a log search (100). A request is read in its component and in each ticked API Management logger's component, four at a time | `core/investigate/appinsights.py` |
| Resource Graph `resourcechanges` (Investigate) | — | step 2, *Recent changes*: one query on picking an app, for the component, the apps that report to it and the API Management services that log to it (up to 20 IDs), over the look-back window plus an hour, newest 50 | `core/investigate/__init__.py` |
| `GET {apiManagement}/loggers` | 2022-08-01 | Investigate: which App Insights component each API Management service in scope logs to (first 10 services, A–Z); a service's Application Insights tab and Overview line read its own | `core/investigate/gateway.py` |
| `GET {apiManagement}/providers/microsoft.insights/diagnosticSettings` | 2021-05-01-preview | Investigate: whether its gateway logs go to a workspace, and into which table | `core/investigate/gateway.py` |
| `POST {resource}/providers/microsoft.insights/logs` | 2018-03-01-preview | a Log Analytics query scoped to one resource (Container Insights on AKS: pods, restarts, OOMKilled, events (over Workloads' Look back, 1 hour to 30 days), containers near limits, opt-in logs, and `KubeNodeInventory` for the Nodes tab when the Kubernetes API can't be reached). A workload's details add, when opened: `Perf` memoryWorkingSetBytes per pod and hour over 7 days (averaged per container, then added up) with `KubePodInventory` restart counts and last statuses for its pods. Workloads → Memory & scaling adds, when opened: `KubePodInventory` restarts and OOMKilled pods per workload over 7 days. Investigate adds, per click: `KubePodInventory` for one pod name on each cluster in scope (first 20, six at a time), the pod's `ContainerLogV2` / `ContainerLog` within 2 minutes of a request, `KubeEvents` warnings in its namespace, and on API Management `ApiManagementGatewayLogs` (then `AzureDiagnostics`) within 2 minutes | `core/explore/aks.py`, `core/investigate/` |
| `POST {subscription or resourceGroup}/providers/Microsoft.CostManagement/query` | 2023-11-01 | `ActualCost` (as invoiced), or `AmortizedCost` once the viewer picks Amortized (a store of its own, read the same way), into the viewer's cost store (kept on disk in `cache_dir`), from which Browse's subscription and group **Cost** cards, Summary and the **Cost** view are answered. Per subscription, at the subscription: cost by resource group, `ChargeType` and day (the last 30 days first, then back to the first of last month; Purchase and Refund are kept apart from usage), and this month by resource ID and service; four at a time in the background, started only once a cost page is opened (Triage never reads costs), the scope in use first. A page waiting on a subscription not stored yet reads it at once in two queries (the whole window by resource group and day, and this month by resource and service). After 6 hours only the last 3 days are read again. A resource's **Cost** tab: one query at its group, filtered to its ID, by day and meter for 30 days, kept an hour. Refusals are kept 6 hours (until Refresh access); Refresh reads again. Summary first tries one query at the management group holding the whole scope (by subscription, charge type and day, since the first of last month), when the viewer can read costs there, and falls back to the per-subscription read. `…/forecast` is allowed but not used; never exports or scheduled actions | `core/cost.py` |
| `POST {aksCluster}/listClusterUserCredential` | provider's latest | **Only when no local kubeconfig context matches the cluster** (and `aks.fetch_credentials` is on): the **user** kubeconfig, to reach the Kubernetes API. Kept in memory, never written to disk. The **admin** credential is refused | `core/explore/kube.py` |

### What the Triage, Search, Secrets and Network pages add

Nothing new in kind:
- **Triage** runs ten Resource Graph queries (`AlertsManagementResources`,
  `ServiceHealthResources`, `Resources`) scoped to the chosen subscriptions, then
  one `resourcechanges` query for just the resources those cards list as
  problems (none when nothing is wrong).
  With auto-refresh on, the page repeats them on its interval while Triage is open
  and visible, the same as pressing Refresh.
- **Search** is one Resource Graph query (`Resources | union (ResourceContainers)`).
- **Browse's filter** (2 characters or more, after a pause in typing) is one Resource Graph query over the scope's
  subscriptions and groups: `Resources | where name contains … or type contains … or type in~ (types named so)`,
  without alert rules and similar, at most 500 rows.
- **Secrets** lists each vault's secrets and keys (above), once per vault in scope,
  plus one Resource Graph query for App Service certificates' expiry dates.
- **Network** is two Resource Graph queries (VNets, and their subnets with a count of IP
  configurations); opening a VNet adds its `usages` list.
- **Security → Exposure & configuration** is four Resource Graph queries: `Resources` with
  something to report (public network access, firewall default action, AKS API server access, local
  auth, shared keys, admin users, TLS, HTTPS only, purge protection; everything else is left out in
  the query, up to 10,000), NSG inbound Allow rules from the internet, network interfaces with a
  public IP, and `resourcechanges` for the last 14 days joined on the server to the resources with
  findings (a tenant's 14 days hold hundreds of thousands of creates). Triage's *Exposed to the
  internet* card is the first three queries without the change history. A resource's Overview
  *Security* card runs the settings query for that one resource (plus its NSG rules for an NSG, or
  its network interfaces for a VM). Browse's resource list reads the same properties in its existing
  query, so its *public* chips cost nothing extra. When the results include Web Apps with public
  network access on, all three add one `appserviceresources` query (500 sites per batch) for their
  `config/web` access restrictions (site and SCM), projecting only those fields.
- **Check pods and detectors** on Triage's AKS card repeats the per-resource reads above (the AKS pod-phase metric, detectors),
  once per cluster in scope.
- **A graph on screen checks again by itself** every `graph.refresh_minutes` (default 5; 0 is off): the same calls as
  **Check again** for that resource with its dependencies, or that group, only while the graph is visible and no
  other check runs. Measured live, cold: a 320-resource group (161 checked) 62.7 s, a resource with 2 hops 20.8 s.
- **`bench`** runs the same calls as the page, nothing else.
- **Cancel** (and starting another check) stops a check's remaining requests; nothing is
  sent to Azure to cancel, the console just stops asking.
- **Your PIM roles** (Access tab, hints) also read the roles you already hold, so they aren't listed as
  eligible: your and your groups' role assignments in `authorizationresources` (in batches of 400 principals,
  every subscription you can read), plus the management-group read in the table above, once per parent
  management group. Cached with the eligibility list for 5 minutes.
- **Check someone's access** (Access tab) is two Resource Graph queries (that
  user's and their groups' role assignments in `authorizationresources`, and each
  subscription's parent management group from `resourcecontainers`), plus the
  Graph and ARM reads listed above. Nothing about the user is kept beyond the
  5-minute caches in memory.
- **Access audit** (Security) adds these Resource Graph queries to the ARM and Graph reads above: every role
  assignment to a person or group in scope (`authorizationresources`, one query, up to 200,000 rows), which fills in subscriptions without Reader and the assignments the PIM records leave out; role
  assignments that carry a condition (tenant-wide, because only Resource Graph keeps an assignment's condition,
  management groups included); AKS clusters in scope without Entra ID (`resources`), only when someone holds a role
  with the AKS user credential; and Key Vaults on the access policy model with their policies (`resources`,
  `mv-expand` over `accessPolicies`).
- **What the console needs** (Access) reads nothing new about anyone else: your own roles and PIM eligibility (the
  reads above, cached), what those roles allow (built-in definitions, custom ones from Resource Graph), one Resource
  Graph query for the AKS clusters and registries in scope, Defender's plan settings (`securityresources`
  pricings, as Vulnerabilities reads them), and one Graph read of one of your own groups' members to see whether
  Graph lets you list members. **Download the custom role** is a file the console writes; it creates nothing.
- **Vulnerabilities** is a handful of Resource Graph queries over `securityresources`
  (Defender's assessments and plan settings) and `Resources` (which of the affected
  resources are reachable, AKS clusters, apps, TLS settings), plus the version and
  runtime lists above. Nothing about packages or resources leaves the machine.
- **The Watchlist** runs a depth-0 snapshot per pinned item. Its only write
  is to your own file, `watchlist_file`, on your machine.
- **Alert rules** repeat calls listed above on their interval, in the background
  while the console runs: a scope rule is the subscriptions list, the resource
  groups query when the saved scope is narrowed, and Triage's cards; a Watchlist
  rule is the Watchlist check. Each rule's last check shows how many calls it made.
  With the ServiceNow or monitoring signals, a scope rule also reads the resources in
  scope (the matching query above) and the saved scope's connections exactly as their
  tabs do, sharing their 60-second caches; nothing is written to either.

- **Fired alerts** (signals, Timeline, Triage) read the same `AlertsManagementResources` rows as before, now
  projecting each alert's rule and its first fired criterion (`properties.context`), which is what the page shows
  as "what fired"; no extra request. *Your access here* adds one `authorizationresources` query for custom roles'
  permissions, only when you hold or could activate one.

Cost → **Billed while idle** is one Resource Graph query (six `union`ed checks on `resources`: unattached disks, unattached public IPs, App Service plans with no apps, VMs stopped but not deallocated, snapshots older than 90 days, Standard load balancers with no backend pool), no Cost Management read.

### Resource Graph tables queried

`Resources`, `ResourceContainers`, `AlertsManagementResources`,
`resourcechanges`, `ServiceHealthResources`, `AdvisorResources`,
`securityresources`. Every query is a KQL read. Resource Graph only returns
rows the signed-in identity can read.

## Kubernetes API: `GET` only (AKS Workloads tab, opt-in logs)

Sent to the cluster's API server, and only when it's reachable from this
machine. List requests carry `resourceVersion=0`, so the API server answers
from its cache instead of reading etcd.

| Request | Used for |
|---|---|
| `GET /version` | reachability check |
| `GET /api/v1/pods`, `/api/v1/nodes` | pod phases, restarts, OOMKilled, requests and limits; node readiness and allocatable capacity; the **Nodes** tab (nodes, the pods placed on each, node metrics: three reads) |
| `GET /apis/apps/v1/deployments`, `statefulsets`, `daemonsets`, `replicasets` | workloads and replica counts; mapping pods to workloads |
| `GET /apis/batch/v1/jobs`, `cronjobs` | mapping job pods to cron jobs |
| `GET /apis/autoscaling/v2/horizontalpodautoscalers` | HPA min, max, current and targets |
| `GET /apis/policy/v1/poddisruptionbudgets` | PDBs that block evictions |
| `GET /apis/metrics.k8s.io/v1beta1/pods`, `/nodes` | current CPU and memory usage (metrics-server) |
| `GET /api/v1/namespaces/{ns}/pods/{pod}/log?tailLines=N` | opt-in logs for one pod, only when you click "logs" |
| `GET /api/v1/namespaces/{ns}/pods/{pod}/log?sinceTime=…&timestamps=true&limitBytes=2000000` | Investigate: one pod's lines from 2 minutes before a request; the console keeps those up to 2 minutes after |
| `GET /api/v1/namespaces/{ns}/pods` | **Running image** on a vulnerable container (Vulnerabilities): the `image:tag` it runs, one read per namespace with findings, only when you click it |

Not on the list, so refused: secrets, configmaps, `exec`, `attach`,
`port-forward`, and every write.

## Container registries: sign-in and one read (Vulnerabilities → **find tags**)

Only when you click **find tags** on a registry image, and only to `<registry>.azurecr.io` (`.cn`, `.us`), the
registry's address from Resource Graph. `check_registry_request` in `core/acr.py` allows exactly these three requests
and refuses anything else before it's sent:

| Request | Used for |
|---|---|
| `POST /oauth2/exchange` | exchanges your Azure token for a registry token (a sign-in: reads and changes nothing) |
| `POST /oauth2/token` with `scope=repository:{repo}:metadata_read` | a token that can only read that repository's metadata |
| `GET /acr/v1/{repo}/_manifests/sha256:{digest}` | which tags point at the vulnerable image, and when it was pushed (cached 5 minutes) |

It needs **AcrPull** (or *Container Registry Repository Reader* on registries using repository permissions) and a
network the registry's firewall allows. A refusal is shown as an answer: the registry's firewall (VPN needed) or the
missing role (with your PIM roles that would cover it). Registry credentials (`listCredentials`, an ARM `POST`) are
refused by the ARM guard, and image layers and the catalog aren't on the list.

## Local files read (never written)

| File | Why |
|---|---|
| `$KUBECONFIG`, or `~/.kube/config` | find a context for the selected AKS cluster |
| AKS desktop's `kubeconfigs` folder (`AKS desktop`, `aks-desktop` or `Headlamp` under `~/Library/Application Support`, `~/.config` or `%APPDATA%`), plus `aks.kubeconfig_paths` | the same, for clusters registered in AKS desktop |
| `~/.config/azure-health-console/config.yaml` and `profiles/` | your settings |

A context's `exec` command (`kubelogin`, AKS desktop's bundled `az`) is
**not** run. Entra contexts use the console's own Azure CLI token.

## ServiceNow and monitoring tools (only when turned on in Settings → Integrations)

Off by default. Each connection has its own guard, refused before sending, and is covered by
tests (`tests/test_integrations.py`).

| Request | Used for | Guard (`check_read_only` in …) |
|---|---|---|
| `GET https://<instance>/api/now/table/<table>` for each table selected in Settings (`incident`, `change_request`, `em_alert`, `em_event`) | the rows shown, and Test connection (one row per table) | `core/integrations/servicenow.py`: `GET` only, to the configured instance's host only, only those tables |
| `GET https://<instance>/api/now/table/sys_user?sysparm_query=user_name=<you>` | Test connection says who you're signed in as (basic sign-in) | same |
| `POST https://<instance>/oauth_token.do` | an OAuth token (client credentials, refresh token, or the code from the browser sign-in). The only ServiceNow `POST` | same |
| Browser → `https://<instance>/oauth_auth.do` | ServiceNow's own login page, for "sign in with your own login" (PKCE) | opened by you, not sent by the console |
| What the ServiceNow tab reads, each a `GET` above | `incident` with your incident filter (ordered by priority); `change_request` twice: `active=true` (windows filtered on your machine: in progress, starting within 24 hours, or past its planned end and still open) and `active=false` with a start in the last 30 days (closed, with its close code); `em_alert` not closed; `em_event` from the last 24 hours. Up to "Most records per query" rows each, cached for 60 seconds. Matching to Azure resources happens on your machine, against one Resource Graph query of the resource, resource group and subscription names in scope | `core/integrations/sn_data.py` |
| `POST https://api.newrelic.com/graphql` (EU: `api.eu.newrelic.com`) | NerdGraph **queries** only | `core/integrations/newrelic.py`: the query text is checked and any `mutation` or `subscription` is refused |
| What the New Relic tab reads (from the connection picked in the Scope bar), each a query above | `entitySearch` for entities in critical or warning in the configured accounts (up to 1,000, 200 a page); `aiIssues` activated issues from the last 7 days, per account (up to 500); for one resource, `entitySearch` by name (up to 50); **Load logs** on a resource, or **Logs** on a row of the New Relic tab, runs one NRQL query per account, `SELECT * FROM Log, Log_<partition>… WHERE (entity.guid IN (…) OR allColumnSearch('<name>', insensitive: true) [OR \`<attribute>\` = '<name>'] [OR lower(\`resourceId\`) IN ('<azure id>', …)]) [AND message LIKE '%…%'] SINCE n minutes ago LIMIT 100` (shown on the page as *Will send* before the read and *Query sent* after; the name search only for a distinctive name and while ticked; the Azure ID match, for forwarded Azure diagnostic logs, while ticked: the resource's own ID, or on a row the resources it matched (up to 20); **Load older** sends the same query with `SINCE <ms> UNTIL <ms>`, ending at the oldest line shown), after `SHOW EVENT TYPES SINCE 1 week ago` once an hour per account to find the log partitions. Picking a resource on the Query sub-tab, or opening a resource's New Relic tab, asks only for its plan: the entity searches above and `SHOW EVENT TYPES`, no log or APM query. The tab's query bar runs the same logs query, or `SELECT … FROM TransactionError` / `FROM Transaction WHERE (entityGuid IN (…) OR lower(appName) = '<name>') [AND … LIKE '%…%'] FACET … SINCE n minutes ago LIMIT 50`, one per account, after a name search over the resources in scope (on your machine, from the cached Resource Graph list) and an `entitySearch` with `name LIKE '<text>'` (up to 30, kept to the configured accounts) for entities picked by name. Cached for 60 seconds. Matching to Azure resources happens on your machine, against the same Resource Graph query as ServiceNow | `core/integrations/nr_data.py` |
| What a workload's memory and Memory & scaling read from New Relic, for a cluster without Container Insights | A workload's details: `SELECT average(memoryWorkingSetBytes) FROM K8sContainerSample WHERE clusterName = … AND deploymentName = … FACET podName, containerName TIMESERIES 1 hour SINCE 7 days ago LIMIT MAX` and `SELECT sum(restartCountDelta), filter(count(*), WHERE reason = 'OOMKilled') … FACET podName TIMESERIES 1 hour SINCE 7 days ago`, one account at a time until one has data. Memory & scaling: `SELECT sum(restartCountDelta), filter(uniqueCount(podName), WHERE reason = 'OOMKilled'), max(memoryWorkingSetUtilization), average(requestedMemoryWorkingSetUtilization) FROM K8sContainerSample WHERE clusterName = … FACET namespaceName, deploymentName, statefulsetName, daemonsetName SINCE 7 days ago LIMIT MAX` and `SELECT percentage(count(*), WHERE currentReplicas >= maxReplicas) FROM K8sHpaSample WHERE clusterName = … FACET namespaceName, displayName SINCE 7 days ago LIMIT MAX`. Kept 10 minutes. Measured live: 3.0 s for a 205-pod workload, 0.8 s for a cluster of 122 workloads and 88 HPAs | `core/integrations/nr_data.py`, `core/explore/workload_memory.py` |
| What **Is this alert right?** reads from New Relic, when clicked on an open issue | `nrqlConditionsSearch(searchCriteria: {name})` in the issue's account (the condition's name, query, terms and signal), then the condition's own NRQL with `SINCE <ms> UNTIL <ms> TIMESERIES <bucket> minutes LIMIT MAX` appended, in that account: up to 8 queries, 4 at a time, together covering `alert_check.days` + 1 (the bucket is the condition's own window when 8 queries of 366 buckets, NRQL's limit, hold the days; else the smallest that does). Kept 15 minutes. The query is shown on the page | `core/integrations/nr_data.py`, `core/integrations/newrelic.py` |

Secrets (passwords, client secrets, refresh tokens, API keys) are kept in the OS keychain, or read
from `AHC_…` environment variables, never written to `config.yaml`, logs or exports, and never sent
back to the page. `--log-calls` shows these requests as `SN GET /api/now/table/incident …` and
`NR POST /graphql …`, without query values or credentials.

## Outside Azure

| Request | Why |
|---|---|
| `GET https://azure.status.microsoft/en-us/status/feed/` | Triage's *Azure service status* card: the public Azure status feed (Azure-wide outages). No token, no cookie, nothing about you or your tenant; redirects not followed, at most 1 MB read, cached 2 minutes (a failure 1 minute). Any other host, path or method is refused before sending. Off with `triage.azure_status_feed: false` (`core/status_feed.py`) |
| `GET https://pypi.org/pypi/azure-health-console/json`, `GET https://pypistats.org/api/packages/azure-health-console/…` | whether a newer version is out (an installed copy only, every 6 hours, off with `updates.check: false`) and About's download numbers. No token, nothing about you; any other host, path or method is refused before sending (`core/pypi_stats.py`) |
| `GET https://github.com/RafaelSonego/azure-health-console/releases/…` and the asset host it redirects to | only when you run `azure-health-console upgrade`: the release's `SHA256SUMS.txt`, its signature and `constraints.txt`. Every redirect is checked against the same list; nothing else is fetched (`upgrade.py`) |
| Browser → `127.0.0.1:8765` | the local web UI. It only answers requests addressed to `127.0.0.1`/`localhost`, and its `POST`s are for local jobs (start a snapshot, drop caches), never forwarded to Azure as writes |

The graph and chart libraries ship with the console (`web/static/vendor/`), so the
page loads nothing from the internet. No telemetry is sent anywhere.
The ↗ links on the service status card open the Azure portal's Service Health or the public status post in your browser.
