# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.2] - 2026-11-18

### Added

- Security → Exposure: Service Bus and Event Hubs namespaces and SQL servers with public access are judged by their
  firewall rules (one read each, in parallel, only on Security and a resource's Overview, not Triage) instead of
  "firewall rules aren't read here". A namespace whose default action is Allow is open even with rules stored
  (Azure keeps them but doesn't apply them); a SQL rule for every address is a warning, *Allow Azure services* is named.
- AI Search is judged by its IP rules; Recovery Services vaults say they have no IP firewall; Arc private link
  scopes are no longer listed (they aren't endpoints).
- **A new version is out**: an installed copy (pip, pipx or brew) asks PyPI every 6 hours; a newer release puts a dot
  on the account button and a line in its menu, and About gives the upgrade command for how it was installed
  (`brew upgrade`, `pipx upgrade` or `pip install -U`, from where its Python lives) and links to the release notes.
  **Not now** hides the dot until the next version (per browser). A checkout never asks. Setting `updates.check`
  (on); copies older than this one can't tell.
- About → **On PyPI**: the latest release (and "you have …" when it's newer than yours), downloads for the last day,
  week and month and in all (pypistats.org, without mirrors), with links to the project page and pypistats. Three
  public GETs, guarded to those URLs, only when About opens; kept 6 hours (a failure 10 minutes), **Read again**
  reads now. About's "Stays on your machine" now names the public pages the console reads.
- Diagnostics: for a type without detectors (SQL databases, say) or whose detectors API doesn't answer (Storage), the
  tab shows Microsoft's troubleshooting guides: service, then problem type, as in the portal's form, then that
  problem's self-help guides, read on the page, and its solutions and checks, listed with the portal link (Microsoft
  opens those by creating a Help resource, a write). Tenant-level reads of Microsoft.Support and Microsoft.Help, kept for a day.

### Changed

- Graph and a group's Table: **Minimize** in the bar folds it to that bar (room for the tabs below); **Show graph** /
  **Show table** brings it back. Each view keeps its own choice until a reload; a folded panel is outlined and
  tinted in the accent colour and says it's minimized.
- Browse → a group's page: each type tile opens just that type (or the types behind "other" / "supporting") in the
  tree on the left, the others folded.
- Diagnostics: a type whose detectors API times out or answers 5xx (Storage, Event Hubs, Service Bus, Container
  Registry: 503 after ~41 s on every API version) counts as having no detectors, instead of "didn't answer, tries
  again in 15 minutes", which never came true. A 401 is still named.
- Scope bar: the tag filter's key and value pickers show at most 32 characters (the full text on hover) and stay
  18em wide, like the Cost view's tag picker; a long tag key no longer stretches the list across the screen.
- Subscriptions picker: with a filter typed, **All shown (N)** ticks just the subscriptions it shows (keeping the
  others ticked) and **Clear shown** unticks just those; with no filter they work on every subscription as before.

- Graph: a group is a diagram drawn by the console (`core/diagram.py`, inline SVG): bands by role (Ingress,
  Compute, Hosting, Data & messaging, Identity & secrets, Observability, Network), each box near what it links to,
  right-angle connectors with lanes for long jumps, Azure's icons (`healthconsole/diagram_icons`), a health colour and
  dot per box, names cut in the middle. Drawn at 100% in a taller, scrolling box with zoom, Fit, full screen and
  **Open in new tab**; click a box for its details. Hovering or clicking a box lights its connectors and what they
  reach and fades the rest (also in the new tab). An identity one resource signs in as folds into it as 🔑 with its
  role arrows starting there (an AKS cluster's AcrPull on its registry is one arrow); unused identities sit in a small
  row; container registries are drawn with Hosting; a legend explains the marks; boxes sit near what they link to
  in both directions. A single resource's dependencies are laid out by ELK's layered
  layout (bundled, `static/vendor`), left to right.
- Graph: role assignments are drawn as dashed amber arrows, from an identity (or the resource that signs in as it) to
  a resource it holds a role on, labelled with the role, e.g. an AKS kubelet identity's AcrPull on its registry. One
  Resource Graph query per check; roles on a whole group or subscription aren't drawn.
- Graph: plumbing is left out unless it joins two other resources (network interfaces, private endpoints, Event Grid
  system topics, API connections, autoscale settings, registry replications; links to the Network band don't count);
  a private endpoint shows as 🔒 on what it serves and an AKS cluster's node pools as +N on it; anything critical or
  warning is always drawn; a line names what was left out, with **Show everything**. On an 81-resource group: 34
  boxes. The Table, exports and Browse still list every resource.
- A group's summary lists its workloads first, by role (compute, hosting, data…, the diagram's bands) then by count,
  up to 8 types; network interfaces, private endpoints, system topics and the like share one "supporting" tile at the
  end (hover lists them).
- A group's summary offers **Show last check** (its kept check, without checking again) beside **Check again**.

- Diagram: a filter box lights the boxes whose name or type matches every word typed, with what they connect to,
  and scrolls to the first; it uses search's type names, so "k8s" finds an AKS cluster. In the new tab too.
- A group check opens on Table each time (Graph with `graph.auto`); the Table/Graph choice is no longer remembered.
- Diagram: connectors run along the gaps between boxes on a grid, so none passes behind a box (no side lanes);
  **Problems** lights every critical or warning box and what it connects to; a problem box shows its worst signal on
  a third line, the hover text up to three; connector labels sit beside the arrow into a box, the role only (the
  identity it goes through is in the hover text).
- A single resource's dependencies are the same diagram (bands, icons, focus, the resource outlined) and include
  what its identities hold a role on, e.g. an AKS cluster's registry. Cytoscape and ELK are no longer bundled.
- Roles held on a whole group, subscription or management group are listed in the box's hover text and in the
  resource's Overview (**Roles it holds**, with roles on resources and which identity holds them).
- The printable report draws the same diagram (PDF and Markdown keep their simpler graph).
- Security → Exposure: API Management is judged by its virtual network mode, Data Explorer (Kusto) by its allowed IP
  ranges, Synapse by its firewall rules (read like SQL's); Databricks and Data Factory say they have no IP firewall
  in ARM instead of "not read". Measured: "not read" went from 38 to 8 resources.

### Fixed

- Overview → Needs attention: a monitoring tool's entity in critical or warning with no matched open issue (New
  Relic's alert state on the entity itself) is listed, as Triage's alerting card already did; it said "Nothing needs
  attention" before. The tool's tab badge counts it too. An entity an open issue already covers (by guid, or New
  Relic's entity for the issue itself, same name) isn't listed twice.
- ServiceNow → **OAuth sign-in** is now offered in Settings (it was hidden). On SSO instances that block basic auth
  over the REST API, pick *OAuth: sign in with your own ServiceNow login*, enter the Application Registry client ID
  and secret, Save, then **Sign in with ServiceNow**: the browser does the login (MFA included) and the console keeps
  a refresh token. One app registration by an admin, then every user self-serves; the backend already existed. The
  app's **Scope Restriction** must be **Broadly scoped** (the default Securely scoped issues a token that 403s every
  table, even as admin), and its Redirect URL must match the console's host:port exactly. See the README.
- ServiceNow and monitoring matches: a name glued to more text by an underscore (`my-rg_prod query result…`, a
  condition name pasted into the incident) matches the resource; the whole word is still tried for names with `_`.
- Graph and Dependencies: a nested resource is linked to its parent by its ID ("part of"), so a SQL database whose
  `managedBy` is empty is no longer drawn alone; a parent's Dependencies list its children.
- A resource group check stopped at `graph.max_nodes` (60) resources and dropped the rest from the Table and the
  graph, in type order (storage accounts and scale sets went first), saying so only in Notes. It now reads every
  resource in the group; `max_nodes` limits only what's added from outside it.
- Security → Exposure & configuration showed at most 1,000 resources with findings, a different 1,000 each time,
  and didn't say so: a Resource Graph query ending in `take` gets no next page. The queries now page up to the
  10,000 cap, and a query cut that way is an error instead of a silent cut.

## [1.1.1] - 2026-11-16

### Changed

- README: Homebrew is the recommended install on macOS, with when to use brew, pipx or pip, and how to remove
  everything (settings, cache, Keychain secrets) for a fresh start.
- README: Prerequisites per OS (Azure CLI, Python 3.11+, pipx on macOS, Windows and Linux), pipx as the way for
  Windows and Linux (not tested there yet), and Uninstall and Remove everything for each OS.
- `serve` no longer prints a line for every page request; `-v` shows them again. Warnings and errors still show.

### Fixed

- Homebrew: the formula no longer depends on `azure-cli`, which Homebrew moved to a cask (a formula can't depend
  on one), so `brew install` failed. Install it with `brew install --cask azure-cli`; the formula's notes say so.
- pipx/pip on the newest Python (3.15 on Windows): the install failed building PyYAML, which has no ready-built
  package for it yet. PyYAML is gone; every YAML file is read and written with ruamel.yaml in pure Python
  (`core/yamlio.py`), with the same rules as before (`yes`/`on` are booleans). Files are now read as UTF-8 on every OS.

### Removed

- Chocolatey and the Windows executable: never published. `packaging/chocolatey/`, `scripts/build.py`,
  `scripts/launcher.py` and the `build` (PyInstaller) extra are gone; Windows users install with pipx.
- A stray file named `0` at the repo root, left by a shell redirect.

## [1.1.0] - 2026-11-16

### Security

- **The console's page and API need your key.** The command prints (and with `--open`, opens) an address carrying
  it; it becomes a private cookie for 30 days and the address is cleaned at once, so after opening it once per
  browser `http://127.0.0.1:8765` works as before, restarts included. Another user or program reaching 127.0.0.1 gets
  nothing (before, the loopback and JSON checks were the only guard). The key is in `cache_dir/launch-key` (0600).
- Redirects are no longer followed blindly: every client follows one only for a read, and only after its own host and
  path checks allow the new address; a redirect answering anything else (the ServiceNow OAuth token call) is refused.
- Test connection sends saved or environment secrets only to the instance they were saved for; testing another
  instance needs its secrets typed on the form.
- Kubernetes requests go only to the cluster's own server over https; a kubeconfig context with an http server is
  skipped with its reason.
- CSV exports can't carry a live spreadsheet formula: text starting with `=`, `+`, `-` or `@` gets a leading `'`;
  numbers stay numbers.
- An alert rule's dimension values are quoted safely in the metric filter *Is this alert right?* reads, and a dimension
  name that could carry filter syntax is refused.
- Settings, their backup and the watchlist are written readable by you only (0600) whatever your umask; a symlink or a
  non-file at those paths is refused rather than written through; a cache folder others could write to loses that.

### Fixed

- **Is this alert right?** on New Relic failed on every condition with "syntax error before:": the condition lookup
  was missing a closing brace. A test now checks every NerdGraph query the console builds.
- **Is this alert right?** on New Relic now reads the condition's own window (several queries when 366 buckets
  aren't enough) instead of hourly buckets, so short spikes count. A `rate()` over a level (`rate(latest(…), 1
  minute)`) is scaled to the window when a bigger bucket is used. Before, such a rule read about 12× too low and
  showed "never true" while its issue was open. The function is found when the query starts with `FROM` too.
- Last week's value is the nearest bucket within one bucket when the exact one is empty.
- A failed history query says the NRQL it sent.
- The New Relic tab showed "Can't find variable: kind" when it had an open issue.
- The Cost view could finish while a subscription's last month was still being read, leaving its comparison at
  *reading…* until a reload; it now finishes once last month is in too (refused or failed subscriptions don't hold it).
- A change under 1% read "−0%"; it now shows one decimal (−0.3%), and 0% when nothing moved.
- Cost grouped by tag counted only the groups with costs ("1 group" for a value with two); it now counts every group
  with the value, and says how many had costs.
- A resource's Overview showed the same New Relic or ServiceNow item up to three times. Now it's shown once: an
  urgent one in Needs attention (labelled "New Relic issue" or "ServiceNow incident"), and Recent events and Open
  elsewhere leave it out; Open elsewhere says how many each tool has in Needs attention and lists only the rest.
- A New Relic issue from a query condition (its entity is "Metric query", with no ID) didn't show on the resource
  it's about, so it couldn't be checked there. Its condition name and title are now matched too, by full distinctive
  name or Azure ID only.
- *Is this alert right?* names `filter(max(…), WHERE …)` as a max.

### Changed

- **Cost compares like with like.** This month is compared with the same days of last month (Nov 1–16 against Oct
  1–16), on the Cost view, Summary and the Cost card. The Cost view's **Last month** column shows all of last month,
  with this month's change against the same days under it.
- **Purchases are kept apart from usage.** A purchase (a reservation's monthly payment, billed on one day outside any
  resource group) counts in This month, but not in the 7-day change or the spikes, so a payment no longer reads as a
  spike and then a −100% fall. Where the month is heading counts each purchase once, and expects last month's later
  ones again.
- **A shorter Cost view:** this month, last month, the last 30 and 7 days, and spikes. Costs outside any resource group
  are named **Subscription-level charges** (hover for what they are). The Spike column is gone: the Spikes card lists
  them, and a row with one has a ⚠ that names it on hover.
- **The cost store keeps cost by charge type**, so it's read again once after updating. The scope in use is read
  first, last month included; until a subscription's last month is in, its comparison says *reading…*, not 0.

### Added

- **Summary's Cost card reads at a glance:** how many subscriptions it covers (and how many aren't readable) first,
  then one labelled line each for this month (against the same days last month), where the month is heading (against
  last month), the last 7 days and the spikes (named); the biggest subscriptions under a heading; budgets list only
  those that need attention. The Cost view's header says how many subscriptions its figures cover. Every cost chart
  draws today, still filling in, lighter and labelled "today, so far".
- **Cost view subscription filter:** with several subscriptions in scope, pick one to narrow the counters, spikes and
  table to it (Summary keeps the whole scope).
- **Amortized cost:** an Actual / Amortized switch on the Cost view (remembered), used by every cost page.
  Amortized spreads reservations and savings plans over their days and charges them to the resources that use them;
  it's kept in a store of its own, read the first time it's picked.
- **Logs** finds Azure diagnostic logs forwarded to New Relic with no entity: lines whose `resourceId` is the
  resource's Azure ID, in any case. A tick box in the log panel, on by default, includes them; untick it to leave
  them out. An Alerts row's
  **Logs** reads the Azure resources the row matched.
- **Open New Relic ↗** and **Open ServiceNow ↗** on the New Relic and ServiceNow tabs, and on a resource's tabs for
  them: the connection's own console (on its region) or instance, in a new tab.
- More links to the tools themselves: a New Relic issue's title opens New Relic's own link for it (the condition's
  query charted around when it fired), so an issue with no entity has a link too; *Is this alert right?* links the
  rule in its tool (the condition in New Relic and its runbook, or the alert rule in the Azure portal); each list on
  the ServiceNow tab opens in ServiceNow with the very query the console sent, read with your own access there; a
  resource's ServiceNow tab links each record's configuration item to its CMDB record.

## [1.0.0] - 2026-11-16

1.0.0 is the first release meant for teams. It turns the console from a health checker into an on-call workspace:
end-to-end request tracing (**Investigate**), a **Security** view, **Cost** kept on disk, role-based **Personas** with a
one-page **Summary**, a wider **Azure service status** on Triage, and **Is this alert right?**, which checks any alert
against the history of what it watches. It also reworks the layout and navigation, makes slow Azure reads stop holding
up the page, and was tested end to end, live, before release. Everything stays read-only and uses the viewer's own
access. (It was developed as 0.6.0, which was never released.)

### Highlights

- **Is this alert right?** For an Azure metric alert or a New Relic condition: how much of the last week the rule was
  true, now against last week and against the usual, and a verdict whose rule and setting are shown with it. Tells a
  new problem from a threshold set inside normal values, for any resource and any source that can give its rule.
- **Investigate:** follow one failing request end to end, across API Management, the app, its dependencies and the
  pod that served it, and copy a ready-made summary into the incident.
- **Security:** exposure and configuration checks across every resource in scope, with Vulnerabilities alongside.
- **Cost:** subscription, resource group and resource costs in Browse, a Cost view across the scope, and a list of
  resources billed while idle.
- **Personas and Summary:** Manager, SRE, DevOps and Security views of the same data, and a short summary to copy
  into a status update.
- **Azure service status:** Azure-wide outages from the public status page, together with your subscriptions'
  incidents, maintenance and advisories.
- **Faster pages:** costs are kept on disk and answered in milliseconds, a resource's health shows in seconds, a read Azure throttles stops waiting after 20 s, and a read
  your access can't make returns at once.

### Public release

- **Open source** under the MIT license, on GitHub (`RafaelSonego/azure-health-console`).
- **Install with Homebrew** (`brew install rafaelsonego/tools/azure-health-console`), **Chocolatey**
  (`choco install azure-health-console`) or **pipx from PyPI** (`pipx install azure-health-console`); the Windows
  executable and checksums are attached to each GitHub release.
- `azure-health-console --version`.

### Upgrade notes

- **Favourites and default tag filters become saved scopes.** Existing configs keep working: the old keys show up as
  a saved scope called *Favourites* until **Move them into saved scopes** (on Triage, in Settings or in the Saved
  scopes menu) rewrites them. Moving keeps a `.bak` of the config file and its comments. `triage.scope` is replaced by
  the tenant's `open_with` (**Settings → General → Open with**: nothing, every subscription, or a saved scope). No
  saved scope can be called `none` or `all`.
- **New settings:** `persona` (the persona to open with), `personas:` (define or override personas),
  `triage.azure_status_feed` (on by default), `cache_dir` (where costs are kept, `~/.cache/azure-health-console`) and
  `alert_check.*` (the limits behind *Is this alert right?*'s verdicts; README → Is this alert right?) and
  `summary.*` (items per card, Readiness's quick checks, the budget warning share, what What changed compares with
  and how long summaries are kept). A persona can take a `summary` list of cards.
- **New outbound requests,** both listed in `AZURE-CALLS.md`:
  - Cost Management's query, the fourth read-only `POST` the guard allows. It runs only when a cost page is opened.
  - The public Azure status feed (`azure.status.microsoft`): no token and nothing about the viewer is sent. Turn it
    off with `triage.azure_status_feed: false`.
- **Signing in from the page** now passes `--allow-no-subscriptions` to `az login`, so accounts without a
  subscription (a personal tenant, say) can sign in.

### Added

**Is this alert right?**
- A button under a fired Azure metric alert (a resource's Needs attention) and under every open New Relic issue (the
  New Relic tab, and a resource's Needs attention). It reads the alert's rule and the last `alert_check.days` (7) of
  what the rule watches, then shows: how many buckets the rule was true in, now against the same time last week,
  the usual (median) value and the threshold against it, a chart with the threshold, and verdicts (*not new*,
  *unusual*, *rare and cleared*, *higher or lower than last week*), each with its numbers, its rule and the setting
  that sets it. *How it was read* shows the rule as the source defines it, the query sent and any scaling; **Copy**
  puts the whole check in a ticket.
- Tool-agnostic: Azure Monitor and New Relic both implement one interface (`AlertHistory`: the rule, and its
  history), and the arithmetic is the same for both. A source that can't answer is named on the page.
- New Relic: the condition is found by name in the issue's account (its issues don't carry a condition ID); when
  several share the name, the check says so rather than guessing.
- `azure-health-console alert-check` runs the same check from a terminal.
- Measured live: 1 to 1.5 s per Azure alert. Runs only when clicked, and is kept 15 minutes.

**Investigate (new view)**
- Step 1 lists every Application Insights component in scope, worst first. Each row shows its last hour against the
  same hour last week, a 24-hour trend of failed requests, and its server errors (5xx) with their share of all 5xx
  in scope.
- Step 2 shows the failing endpoints, exceptions by type, failing dependencies and recent changes to the component,
  its apps and its gateways.
- **Follow** a request (or paste an operation ID) to see every span, dependency, exception (with its stack) and log
  line of that request, nested under its parent. When API Management logs to Application Insights, the gateway span
  is shown on top.
- **Gateway logs** shows the gateway's rows in a Log Analytics workspace. **Pod** finds the AKS pod that served the
  request and shows its state, its logs around that minute, warning events and the cluster's recent changes.
  **Search logs** searches a component's logs and exceptions.
- **Copy for incident** copies a plain-text summary for the ticket. **Refresh** rereads every open step, and every
  query is shown as KQL.
- Opens from a failing operation on a resource's Application Insights tab. Web Apps, Function Apps and API Management
  services get that tab with the same *Last hour* line.

**Security (new view)**
- *Exposure & configuration* checks every resource in scope by setting:
  - open to the internet: public network access with no firewall, open AKS API servers, anonymous blob access,
    inbound NSG rules (risky ports flagged), public IPs;
  - keys used instead of Entra ID: local auth, shared keys, admin users, SQL logins;
  - TLS below 1.2 or HTTP allowed;
  - Key Vault purge protection;
  - Web App and SCM (Kudu) access restrictions.
- Each finding says what to do and who changed the setting in the last 14 days. Findings are grouped by resource
  type, or listed one per row.
- Vulnerabilities moved here as the second tab, and old links still work. A resource's Overview gets a Security card,
  Browse marks public resources, and Triage's *Exposed to the internet* card matches this view.

**Cost**
- In Browse:
  - Subscriptions and resource groups get a **Cost** card: month to date against last month by the same day, the
    projected month, 30 days by day, cost by service, the top groups or resources, and budgets.
  - Resources get a **Cost** tab.
- **Cost** view across the scope: read as a background job that fills in as it goes. Group by subscription, resource
  group or tag, sort by spend or by the biggest weekly change, see spikes, and export to Markdown or CSV.
- **Billed while idle:** resources that are billed while doing nothing, grouped by kind, with what to do about each.
- **Cost store:** every subscription's costs are read once and kept on disk (`cache_dir`, one file per tenant and
  account, readable only by you), so subscriptions, resource groups, Summary and the Cost view answer in milliseconds
  instead of asking Azure each time. The first read runs in the background once a cost page is opened: the scope in
  use first, every subscription's last 30 days before the rest. After 6 hours only the last 3 days are reread, and
  **Clear cached data** (account menu) forgets everything. The page says what's happening: a card read for the
  first time says so (5–15 s, then instant), a card updated by Refresh says "updated just now", a Cost view Refresh
  counts the subscriptions actually reread with the time left, and figures older than 6 hours say they're being
  updated in the background.
- Cost is read only once a cost page is opened, never by Triage or a timer. A missing cost role is named.

**Personas and Summary**
- **Personas** (View as, top right): Manager / lead, SRE / on-call, DevOps / platform, Security and Everything. Each
  persona sets the home page, which views lead the menu, the order of Triage cards and resource tabs, and how much
  detail is shown. It never changes what you can read. The persona is picked on first run or set in Settings, and a
  team can define its own.
- **Summary:** each view's headline for the scope in use, written for someone who won't open the views:
  - A **status line**, one colour per card, also the first line of **Copy summary**. Clicking a chip outlines its
    card for a moment (scrolling to it when needed) and keeps the chip marked.
  - **Problems now** with the worst items, each one opening the resource.
  - **What changed** since the summary from at least a day ago: new or worse, and resolved, per card. Summaries are
    kept on this machine for a week, readable only by you.
  - **Cost:** where the month is heading at its rate so far against last month, 30 days by day, and every budget in
    scope (spent and forecast). A 7-day change that includes charges booked on the 1st of the month says so.
  - **Security:** a tile per area, the first warnings, and Defender's vulnerability totals with the resources to fix
    first.
  - **Readiness:** its quick checks run on their own (no Kubernetes or Key Vault reads), or the last full report.
  - The persona picks the cards and their order (`summary` in `personas:`); Manager swaps Your access for What
    changed.

**ServiceNow incidents**
- Incidents are read a page at a time up to **Incidents: most records** (`incident_max_records`, 2,000; per
  connection), instead of one read of 500. Measured on a large instance: 4.9 s instead of 1.9 s, and every P1–P3
  incident the account can see, where 500 left most of them out. The tab says when the cap is reached and how many
  of the incidents read ServiceNow's read rules hid from the account.

**ServiceNow changes**
- Changes cover the last 30 days (closed, with their outcome), in progress, past their window, scheduled in the next
  14 days, and open with no dates; both spans are per connection (`change_history_days`, `change_ahead_days`).
  ServiceNow filters to the span itself, and changes are read a page at a time (four pages at once) up to
  `change_max_records` (5,000); the tab says "Only 5,000 of 7,812 closed changes were read" when that's reached.
  Closed changes are kept for 15 minutes, open ones for a minute.
  Triage's card is **ServiceNow: changes** over that span (it showed only changes in progress).
- A change names Azure resources through its **affected CIs** (`task_ci`) and its **change tasks** (`change_task`) as
  well as its own fields, and through more of them (`change_match_fields`: justification, implementation, backout
  and test plans, business service). The ServiceNow tab lists each change's affected CIs and tasks, and a resource
  chip says where it was found. An affected CI with the resource's Azure ID counts as the CMDB mapping it.

**Triage and alerts**
- **Azure service status** (replaces the Service Health card):
  - Azure-wide outages from the public status page come first. The card also shows your subscriptions' incidents,
    planned maintenance, security advisories, incidents resolved in the last 24 h, and a count of health advisories.
  - Each line links to Service Health in the portal. A line under the card's description says what the public feed
    showed and when it was read.
  - Links to the public Azure status page and to Microsoft 365 service health sit under the card. The console
    doesn't read Microsoft 365 health, because the Azure CLI's sign-in can't.
- Fired alerts are explained in words, for example "Average CapacityUnits was 92.3, alert when > 80 (over 5 min)",
  with links to the alert, its rule and, for log alerts, the query results.
- A problem that changed recently shows what changed and who changed it.

**Access**
- **My access** lists the Azure roles you hold now, as well as the ones you can activate.
- **Your access here** says what you can do ("read everything · change some things · can't manage access"), and
  which PIM role to activate for more.
- **Check someone's access** has the same layout. Roles are grouped by subscription on both tabs.

**Navigation and lists**
- Every table sorts by its headers. Long lists show 100 rows, then *show more*, and large tables get a filter.
- **Back and Forward** move between views and tabs, and **Copy link** includes the open resource and tab.
- Links to the Azure portal (↗) appear next to resource names across Triage, Security, Vulnerabilities and
  Investigate.
- **Sign in / out** to another tenant or account. The tenant menu lists every tenant the Azure CLI is signed in to.
- Every view says how old its data is. A notice appears when a background read finishes while you're elsewhere.
- New Relic: Query starts from an entity or app name, shows the exact NRQL before it runs, and can **Load older**
  logs.
- Browse: a group's type headings sort **busiest first** or **by service**. By service keeps related types together
  (SQL Server before SQL Database, App Service plan beside Web App, a VM beside its disks and extensions). The browser
  remembers the choice.

### Changed

**Layout**
- New look: a dark header, a views band, a scope strip, and content cards on a canvas, in light and dark themes. The
  alerts bar sits at the very top of the page.
- The layout follows the window size. The header and menus wrap, and Browse fills the height, stacking below 900 px.
- Refresh access, Sign in / out, the theme and About moved into an account menu under your name.

**Browse**
- The filter finds resources by name or type across the whole scope.
- Resources fold under one heading per type, busiest first. Alert rules and similar resources are listed last.
- Each group's last check is kept when you come back to it. The breadcrumb opens the group and subscription pages.

**Resource pages**
- A resource's Overview leads with *Needs attention*, then the last 24 hours of events with their days.

**Scope bar**
- The Scope bar labels each control and names what's in the scope. It also shows how many groups a tag filter hides,
  and **Clear filters** resets the filters.

**Triage**
- Cards with something to show come first, including cards that load late.
- Triage's Recent changes card was folded into the problems it explains.

**ServiceNow**
- ServiceNow shows changes from the last 30 days, including closed ones with their outcome.

**Speed**
- A resource's health shows in 3–6 s, while Azure's detectors fill in afterwards.
- Access and the Activity Log read in parallel. Secrets scans only when opened.
- A read shown on screen stops waiting on Azure throttling after 20 s and says when to try again.
- A refused read (403) is remembered for 5 minutes, and an unreachable cluster for 2, so reopening them is instant.
  **Refresh access** asks again.
- **Refresh** always reads Azure afresh instead of a cached answer from the last minute.
- Buttons use one word per action: **Refresh**, **Check again**.

### Changed (ServiceNow sign-in)

- Settings offers only **Username and password** for a new or changed ServiceNow connection. The OAuth sign-ins need
  an OAuth app on the instance, so they're hidden for now; one already saved keeps working.

### Removed

- Favourite subscriptions and resource groups, and default tag filters (replaced by saved scopes; see Upgrade notes).
- Triage's *Recent changes* card (its information now sits on the problems it explains).

### Fixed

**Alerts and times**
- A resource's **Needs attention** said "✓ Nothing needs attention" while a monitoring tool or ServiceNow had an
  alert open on it. Their open alerts are listed there now (with *Is this alert right?*), and the line reads
  *Azure's own signals: nothing needs attention*.
- Times written into item text (fired alerts, Service Health, "Checked …") were in UTC while the rest of the page used
  your local time. They now read in local time, with the UTC original on hover; exports keep UTC.

**Sign-in and access**
- Signing in from the page with an account that has no subscription appeared to work, but every read then failed.
- Signing out while a read was running showed the Azure CLI's error text instead of the sign-in screen.
- In a tenant without Entra ID P2, My access showed no roles. Roles you already hold are no longer offered as
  "eligible".
- A missing cost role read as a bad sign-in.

**Page responsiveness and links**
- The page stayed locked for up to 25 s after opening a resource while Azure's detectors ran.
- Clicking a group check's row seemed to do nothing, because its details opened off screen.
- The breadcrumb's group link did nothing.
- Cost lines and pod results looked clickable but weren't.

**Scope and settings**
- Apply right after changing subscriptions could apply the previous selection's resource groups.
- Browse could get stuck on "Loading resource groups…".
- Investigate ignored a new scope until the view was reopened.
- A new saved scope was missing from Settings → Alerts until the next alert check.
- Saving a scope named `all` or `none` (reserved for Open with) seemed to do nothing; the name prompt now says why
  and suggests another.

**Investigate and integrations**
- Investigate's queries failed on real Application Insights components (reserved column names).
- Search logs didn't say it needs a component first.
- New Relic's partition list stayed empty or stale.
- "Gateway logs" was offered for API Management services with no gateway logs.

**Display**
- Times showed "NaN h NaN min ago" on Application Insights, ServiceNow and monitoring tabs.
- The **logs** buttons on AKS Workloads didn't appear.
- Failed pods were only counted; they're now listed with their reason.
- The header, settings rows and Browse's pane overflowed in narrow windows or with long names.
- After an update, the browser could keep an old script or stylesheet.
- A card's list could stop scrolling mid-line, cutting text.
- A refused request showed "400 BAD REQUEST" instead of the reason.
- Cost's tiles put the currency on a line of its own, and on a zoomed or narrow page the amount ran past the tile.
  The figure now stays on one line, shrinks with its tile, and shows the currency as a small prefix.
- Settings in a mid-width window (about 760–1000 px) squeezed each setting's help into a column one letter wide;
  the help now moves under its control when the pane is narrow.
- The Cost and Vulnerabilities headers could split their Export buttons and Refresh across two lines.
- Settings listed *Is this alert right?*'s settings under Files; they're under Thresholds now.
- A Summary item could open a page that didn't show it: a vulnerable resource opened on Overview, whose Security card
  said "✓ Nothing in its settings". Each item now opens where its finding is listed (a vulnerable resource on its
  Vulnerabilities tab, a Readiness finding on Readiness, which shows Summary's quick report), and Overview's Security
  card gives the resource's vulnerability totals with a link to the tab.
- A resource's Vulnerabilities tab counted the findings it listed (at most 300) instead of all of them; it now gives
  the total and says the list is the highest CVSS.
- Overview's Recent events could draw a status over its time ("Yesterday 03:03 PM").
- A ServiceNow instance that stopped answering (a developer instance asleep or reclaimed answers 500 with a web
  page) looked like an empty one: the ServiceNow tab, the Triage cards and a resource's ServiceNow tab said "✓ Nothing"
  while each table's error sat in grey text. It's now one notice everywhere, "ServiceNow (…) isn't answering (500
  Internal Server Error)", with the hint that a developer instance sleeps, and Test connection says the same. A single
  table that can't be read is a warning in its place, never followed by a ✓.
- ServiceNow changes were read at most 500 at a time, without saying so: on a busy instance the newest 500 closed
  changes hid the rest, and open changes were read oldest first, so long-forgotten open ones could push current ones
  out. Closed changes were also picked by planned start, which can be empty; they're now picked by when they closed.
- An account that ServiceNow's read rules keep from seeing a table's records looked like an empty table: ServiceNow
  counts the matches before its read rules, then returns no rows, so the tabs said "✓ No changes" and Test connection
  said "readable". It's now "ServiceNow has 7,511 matching change_request records but shows none to <account>…", with
  the roles that would allow it, on the tab and in Test connection, and once for the whole connection when no table
  is readable. Some rows hidden is said too ("read rules hid 4,100 of 6,363"). A fully hidden table is judged on its
  first few pages and remembered for 15 minutes, so it doesn't cost a full read each time. "Only N of M were read"
  now appears only when the cap was reached. README → Permissions lists what a ServiceNow account needs.
- A personal Microsoft account (or a guest from another directory) was named `live.com#me@example.com` in the top
  bar, reports and the CLI; it's now just the address.
- On Python 3.11 the console failed to start: one line used syntax only Python 3.12 accepts. The tests now also pass
  on 3.11, the oldest version supported.

**Found by the release's end-to-end test** (every view, tab and route, live and read-only, on a large tenant)
- Opening a resource that no longer exists in Azure, though Resource Graph still lists it, said "✓ Nothing needs
  attention" and its recent events spun forever. It now says the resource can't be read and may no longer exist.
- A Key Vault was listed twice each time its page opened (the same path in two letter cases missed the cache, now
  case-insensitive for every Azure read), and a vault with thousands of secrets never finished. Its page now reads the
  first 1,000 of each list and says when there are more; Secrets & certificates still reads them all.
- When Azure answered with a web page instead of an error (a gateway's 502 page), the page printed its HTML. It now
  names the page by its title.
- PIM's **Activate in the portal** links had no tenant, so with two tenants signed in the portal could open PIM in the
  other one.
- Narrow windows: the Settings paths, the "as of" line, Triage's cards and wide tables pushed the page sideways at
  390–1024 px. They now wrap or scroll inside their own box.
- Snoozing an alert rule by a name that doesn't exist reported success. It now says nothing was snoozed.
- The browser asked for a site icon on every page load and got a 404; the page now has one.

### Known issues

- **Is this alert right? doesn't read everything yet:** Azure log alert rules, New Relic APM and infrastructure
  conditions, and other monitoring tools say they can't be checked. The New Relic part was built from New Relic's
  documentation and tested with recorded answers; its first live run is on the release checklist.
- **The cost store's first read is limited by Azure.** About 20 subscriptions a minute for their last 30 days, then about
  as long for the rest, in the background; while it runs, a resource's Cost tab can be told to try again in a few
  seconds. A billing-account query (one query for every subscription) would remove the wait but isn't used yet.
- **Azure's detectors are slow for some types** (Function Apps' Diagnostics about 50 s, one Logic App detector timing
  out at 30 s). The page shows each detector as it answers and the rest of the page doesn't wait.
- **At phone width** the header, menu and Scope bar take most of the first screen; the console is built for desktop.

### Security

- Every outbound request still goes through one guarded client per service, and that client refuses anything but a
  read before sending. The two new requests (Cost Management's query and the public status feed) are allow-listed by
  host, path and method. Tests fail if either guard is loosened.
- *Is this alert right?* adds no new host and no new kind of request: Azure `GET`s (the alert rule, its metric) and
  New Relic queries (a condition search, one NRQL query), which the existing guards already allow; a NerdGraph
  mutation is still refused before it is sent.
- Secrets stay in the OS keychain or `AHC_…` environment variables. Tests never touch the real keychain, Azure or the
  public status feed.

### Internal

- Shared core: one base class or helper per repeated idea (outside-service errors, "connection unavailable"
  answers, browser storage, notices, list sorting), and Investigate behind a trace-source interface that New Relic
  can implement next.
- A test fails if `app.js` declares a top-level name twice, which caused two of the bugs fixed above.
- 529 tests, plus an end-to-end test of every view, tab and route against a live tenant.

## [0.5.0] - 2026-11-10

### Added

- **Sign in from the page** (local console only): **Sign in** runs `az login` (your browser opens), **Use a code
  instead** shows the device code to enter at microsoft.com/devicelogin, with progress, Cancel and what `az login`
  said if it fails. **Sign in again** (top right) does the same for this tenant or your default one, to switch account
  or after activating a PIM group. The command is fixed; only a validated tenant ID or domain is passed in. A hosted
  app never gets these routes.
- **About** (top right, a popup): what the console is and how it works, its version, where your settings and Watchlist are kept, and who made it (Rafael Sonego da Silva, rafaelsonego.com).
- **What each alert check saw**, in one line per rule in Settings → Alerts and in the heartbeat's tooltip: "nothing
  new · tracking 2 red, 4 yellow · 1 open incident". An empty History no longer looks the same as a rule that isn't
  working.

- **Triage auto-refresh and "what's new":** Settings → General → Triage → *Refresh Triage by itself* (off, or every 1, 2,
  5, 10 or 15 minutes; `triage.auto_refresh_minutes`), and the same choice in the Triage header for that tab. It runs
  only while Triage is visible. After any refresh of the same scope, new rows are marked **new** and cards that got
  worse are outlined; Refresh now keeps the cards up while it loads.

- **Vulnerabilities tab:** Microsoft Defender for Cloud's findings for the scope (summary, by resource ranked by
  reachable / running / fixable / CVSS, by software, filters, CVEs), what Defender isn't scanning, and platform checks
  that need no Defender: AKS version support and patches against Azure's list, node image age, App Service and
  Functions runtimes against Azure's end-of-life dates, TLS below 1.2 and plain HTTP. Export to Markdown, CSV or PDF,
  and a Vulnerabilities tab on each resource.
- **Nodes tab on AKS clusters:** each node's pool, VM size, zone, readiness (with the reason), pressure, cordoned state,
  taints, CPU, memory and pods requested and used against what it holds, kubelet and node image versions with the
  image's age, and the pools in total; worst first, filterable, a click for conditions and OS details. Falls back to
  Container Insights' node inventory when the Kubernetes API can't be reached.
- **Your access here** on a resource's Overview, a resource group's summary and a subscription's (click its name in
  Browse): PIM roles active there with time left, standing roles and where they come from (direct or through a
  group), and the PIM roles you could activate there. One `roleAssignments?$filter=assignedTo(<you>)` read.
- **Check pods and detectors on an AKS cluster's Overview:** Triage's AKS check, for that one cluster.
- **Search vulnerabilities** by CVE (an exact match for a full ID), package, resource, container or image, or version,
  with an **only running** filter; a CVE search says where it is and what fixes it, and highlights it in drill-downs.
  Findings without a package name show Defender's title instead of "(unnamed)".
- **Which image a vulnerability is in:** registry findings show their repository and digest (with copy), **find tags**
  asks the registry which tags point at it, and **running image** reads an AKS cluster's pods for the `image:tag` a
  vulnerable container runs. A refusal is an answer: the registry's firewall (VPN needed) or a missing role (AcrPull,
  a Kubernetes read role), with any PIM role that would cover it. The registry is reached through its own allow-list
  (two sign-in POSTs, one manifest GET); Kubernetes gains one read, a namespace's pods.
- **README → Requirements:** what the console needs to run, and for each feature what it needs on top (Defender plans
  for vulnerability findings, Container Insights, connections…) and what you see without it.

### Changed

- **One core for what every feature shares** (no behaviour change beyond the fixes above): every outbound client
  extends `GuardedClient` (one allow-list check before sending, one log line for each request, including Kubernetes,
  Graph, ServiceNow and New Relic, which `--log-calls` now shows too); one `TtlCache` instead of seven hand-written
  caches; one card and item builder; one way to name scopes and roles; in the page, one way to wait for a job and to
  copy text. Tests now check each client refuses before sending, and the suite fails if any allow-list is switched off.
- **Theme switch** (top right: ☀ light, ◐ system, ☾ dark), like rafaelsonego.com: it follows the system theme
  until you pick one, which this browser remembers. The graph and an open chart repaint with it.
- **Tabs and menu items show they're clickable:** on hover the text brightens, a blue tint and a soft underline
  appear (the selected tab keeps the solid one), with a pointer cursor and a visible focus ring for the keyboard.

### Fixed

- **Refresh access now clears everything that depends on your access**, not only PIM and Graph: the scope's inventory,
  ServiceNow and New Relic records, Kubernetes clients, registry tags and reused jobs, so a PIM activation shows at once.
  Reference data (built-in role names, runtime lists, AKS versions, VM sizes) is kept.
- **A refused ServiceNow or New Relic request** is handled like a refused Azure one (it was an unhandled error).
- **The Vulnerabilities tab said "✓ No open findings" where Defender isn't on** (a personal tenant, for example). It now
  says Defender's scanning plans are off, or don't answer at all, and that only the platform checks apply.
- **A resource's Overview cards overflowed** (seen on AKS: "Why it's critical" ran into the next card) when a detail
  held a long name or ID with no spaces. The cards wrap such text now, and each "why" line stops at four lines, with
  the full text on hover and in the Signals table.

## [0.4.0] - 2026-11-09

### Added

- **Alerts** (phase 13a): Settings → Alerts holds rules per tenant (`alert_rules` in config.yaml), each watching a
  saved scope or the Watchlist on an interval (2–30 min; 5 or more for a scope). The server runs them in the
  background, from start and with no page open. A rule alerts on a change only: a status that gets worse, a new red
  resource, a new Service Health event. A bar on every tab shows new alerts until dismissed, a heartbeat and the rules
  that can't run (and why); History keeps 24 hours in memory; browser notifications are optional per rule. A check
  that starts failing raises one alert, and one more when it works again. An alert resolves itself once what it was
  about is back to how it was (leaving the bar, marked resolved in History), with a "Recovered" entry and an optional
  notification per rule (`notify_recovered`).
- **Alerts from ServiceNow and monitoring tools** (phase 13b): scope rules can alert on a new ServiceNow incident at or
  above a priority (`servicenow_priority`, P1–P2 by default) and on a New Relic issue opening, when either names a
  resource in scope by ID or a distinctive name. They use the saved scope's connections and say when it has none.
- **Phase 13c:** Watchlist rules can alert when a metric doubles or halves against last week; **Acknowledge** and
  **Snooze 1 h** per alert; **Snooze 1 h** per rule (still checking, quiet); **Export** of the alert history as
  Markdown, CSV or PDF.

- **Access → Check someone's access:** search the directory as you type and pick a user to see their groups and their
  active and PIM-eligible roles in the scope's subscriptions, with the group each comes through and roles held at
  management groups. Built for few requests: one Resource Graph query for active roles, one ARM read per parent
  management group, one per subscription for eligibility (8 in parallel, asked first above 100), built-in role names
  read once a day. Microsoft Graph's allow-list gains the user search and a user's groups, `GET` only.

### Changed

- **The title "Azure Health Console" goes back to Triage**, keeping the scope; Ctrl/Cmd-click opens a fresh console.
- **Search ranks the scope in use:** exact name, then in scope, then names starting with the text, then favourites,
  with an *in scope* / *outside scope* mark on each result and an **only in scope** toggle. Ranking now happens in the
  query, before the result cap, so a common word can't push an exact match out.
- **The Watchlist's Auto-check is an alert rule** with the Watchlist as its scope, instead of a timer in the page
  stored in the browser. The page offers to set one up from an old Auto-check setting.

### Fixed

- **Settings → Save failed with a 500** when only general settings changed (not favourites or tag filters): the
  tenant ID the page sends with every request was read as the tenant's settings.
- **Diagnostics said "Azure doesn't offer detectors" when Azure only failed to answer** (Event Hubs, Service Bus,
  Storage, Container Registry answer 500/503 on every API version) and remembered it for an hour. Now a type is
  "not offered" only on 400/404/405; anything else says what Azure answered and is retried after 15 minutes. For types
  without detectors in the read API (API Management), the tab explains that the portal's page runs through Microsoft's
  Help service, a write, and links to it.
- **Recommendations missed everything on resources inside the one opened:** an API Management service's APIs, a SQL
  server's databases. They're included now, grouped by finding with a count, after the resource's own.
- **The Timeline was buried by routine operations:** on API Management, ~100 "Get subscription keys" per page pushed
  everything else out. The Activity Log is read up to ~2,000 events, and repeats (and alerts that keep firing) fold
  into one row with a count.
- **A resource's Diagnostics tab failed with a 500** (seen on Redis) when a detector answered an error with a bare JSON
  string instead of an object. Any error body shape is read now, so that detector shows its error and the rest run.

## [0.3.0] - 2026-11-05

### Added

- **Pipeline** (`.azuredevops/pipeline.yaml`, shared template `python_ci` 1.1.0): tests and lint on every run;
  on `main`, the wheel is published to the `platform/platform-tools` feed and the commit tagged with `VERSION`.
  The Windows executable is still built by hand (`scripts/build.py` on Windows).

### Changed

- **Interface rework** (from a review of the pages):
  - Top bar grouped: Triage, Browse, Readiness, Sweeps ▾ (Secrets, Network), Tools ▾ (ServiceNow, monitoring
    tools), Watchlist, Access, and Settings on the right.
  - Browse is one tree (groups open in place, resources by type, one filter); a group shows a summary with
    **Check this group**; the toolbar is Re-check, Dependencies ▾, Export ▾ (with Copy link) and Watch.
  - A resource's Overview leads with why it has its colour, the latest timeline events and what's open in
    ServiceNow and monitoring tools; tabs reordered (health first, configuration last) with red counts.
  - New Relic (any monitoring tool) has Alerts and Query sub-tabs; Query is three steps beside its results,
    suggesting the resources the tool is alerting on, and an alert row's **Query** opens it with that resource.
  - Settings has a section menu, plain names (config keys in small print), starred favourite subscriptions
    (patterns under Advanced) and a save bar with Discard.
- **Access tab** (PIM moved out of Settings): active roles with time left and the group they came through, roles
  refused this session with the role that would open them, eligible roles filtered to the scope in use, and the
  groups you're in (one new read-only Graph `GET`), with a link to the portal for PIM groups.
- **Scope changes wait for Apply.** Subscriptions, resource groups, tag filters and connections picked in the
  Scope bar are a draft until **Apply**, which reloads every tab once; **Discard** goes back to the scope in use.
  Before, each tick reloaded Triage, ServiceNow, New Relic, Secrets and Network. A saved scope still applies in
  one pick.

### Added

- **Timeline** tab on a resource (replaces Activity): alerts fired and resolved, changes, Activity Log operations,
  Resource Health, ServiceNow incidents and changes and the monitoring tool's issues in one list, newest first,
  over 6 h, 24 h or 7 days, with each source switchable. Activity Log events of one operation are one row.
- **Against last week** on a resource group check: the metrics that doubled or halved against the same window a
  week earlier, as a panel above the table, a column per resource, and a section in its Markdown and PDF export.
  No extra calls.
- **PIM hints**: wherever a read is refused, the eligible PIM roles that cover that resource, with a link to
  activate in the portal; **Settings → PIM** lists every eligible role. Two read-only GETs on your own
  eligibilities and activations; the console never activates anything.

- **Pre-peak readiness** (Triage → Pre-peak readiness, or the `readiness` command): one report over the scope,
  judged against the day the peak ends: scaling headroom (autoscale, AKS pools, App Service plans), vCPU quota per
  subscription, region and VM family against what the scope could add at its maximums, subnet IPs including
  whether each AKS subnet holds its pools at their maximum, single points of failure, HPAs, and certificates that
  expire before the peak ends. Kubernetes and Key Vault are an optional deep part. Each section names what it
  couldn't check and why; exports to Markdown and PDF. Two new GETs (`Microsoft.Compute` usages and skus).

- **Query a resource** bar on a monitoring tool's tab: find a resource in scope by name, pick **Logs**,
  **Errors** or **Slow transactions**, and run; the query is built from the pick and shown with the results.
  Errors and slow transactions are new provider reports, so another tool can answer them later.
- **Application Insights** tab on a component (Browse): requests by role against the same window last week,
  failing operations, exceptions, failing dependencies and availability tests, over 1 h, 6 h or 24 h. GET-only
  queries on the component's own endpoint, so the read-only guard needed no new exception.
- The same tab on **Web and Function Apps**, through the app's component: its `hidden-link` tag, else a related
  name in its resource group, else your pick (remembered per app in the browser).
- Triage card **App Insights: failure spikes**: components whose last hour fails or throws more than the same hour
  last week, from metrics, loaded after the other cards.
- **Load logs** reads lines from the matching entities **or** mentioning the resource's name in any attribute
  (`allColumnSearch`), with no setup, so logs with no entity ID (e.g. AKS container logs, labelled `cluster_name`)
  show too; each line says whether it came from an entity or only mentions the name. Generic names aren't
  searched. Optional `log_attributes` per connection in config.yaml adds an exact attribute match.
- New Relic entities with the same name in the same account (an AKS cluster seen by the Azure and by the
  Kubernetes integration) are one row, listing each source with its own type, state and link.
- Logs read every NRDB data partition (`Log`, `Log_<name>`) by default, found per account; a **Partition**
  picker narrows it. Before, only `Log` was read, so partitioned logs never showed.
- **New Relic** tab (one tab per monitoring tool; Datadog and Grafana get theirs when they connect), showing the
  connection picked in the Scope bar: activated issues (last 7 days) and entities in critical or warning, each
  with the Azure resources it matches: exact through any tag holding an Azure resource ID, name through the
  entity's name; no tag names assumed. A connection that can't be read says why. Filters for text, critical
  only, and only what matches resources in scope (on by default).
- A Triage *alerting* card per tool, and a tab per tool on each resource: the entities that match it, alerting
  or not, and the open issues touching it.
- **Logs** on a resource's tool tab, only on request: New Relic lines from the matching entities
  (`entity.guid`), last 15 min to 6 h, optional text, with every attribute and the NRQL sent. Also a **Logs**
  button on every open-issue and alerting-entity row of the New Relic tab, opening that entity's logs under the row.
- **Several ServiceNow connections** per tenant (e.g. prod and non-prod instances), each named, like monitors.
- **Connection pickers** in the Scope bar: which ServiceNow connection and which connection of each monitoring
  tool the tabs and Triage use, remembered per tenant.
- **Saved scopes** per tenant (Scope bar → Saved scopes): subscriptions, resource groups, tag filters and
  connections under a name, applied in one pick; save, rename and delete from the same menu.
- Settings lists what each tool answers (alerts, entities, logs).

- ServiceNow tables sort by any column (click the header).

### Changed

- The page opens with **no scope selected** and queries nothing until you pick one; Triage offers your saved
  scopes as buttons. `triage.scope` gains `none` (the new default); `favourites` and `all` still work.
- Browse's left side follows the resource opened from the Watchlist, search or a link: its group opens with
  the resource highlighted, a breadcrumb sits above it, and one outside the scope is marked as such, with **Show
  its resource group** and **Back to scope**. The scope is never changed for you.
- The Triage ServiceNow card lists P3 incidents too (P1–P3); it stays red with any P1, amber otherwise.
- Keychain items are named for what they hold (*Azure Health Console - NR Prod (API key)*) instead of all being
  `azure-health-console`, which made separate keys look like one being overwritten. Existing items are renamed
  on first read, nothing to re-enter.
- **Integrations are per tenant.** ServiceNow and monitoring connections live under
  `tenants.<domain or ID>.integrations`; Settings → Integrations edits the current tenant's, and switching tenant
  switches the ServiceNow and Monitors tabs and Triage cards. Never taken from `"*"`. Monitoring keys are kept in
  the keychain per tenant. A global `integrations:` block from 0.2.0 is used by no tenant until Settings moves it
  (with its keys) to the tenant you choose.
- `integrations.servicenow` is a list of named connections; a single block from before is read as one connection
  called "ServiceNow" and written as a list on the next save.
- The default incident filter is `active=true^state!=6`: ServiceNow keeps Resolved incidents active until they
  auto-close, so `active=true` alone listed incidents already fixed. Saved settings keep their own filter.
- Monitoring tools sit behind one provider interface (`core/integrations/provider.py`); New Relic is the first
  provider (`nr_data.py`), so another tool adds a provider, not page changes.
- Matching records to Azure resources moved to `core/integrations/matching.py`, shared by ServiceNow and the
  monitoring tools.

### Fixed

- Matching missed a resource named only inside a hostname (`myapp-prod.azurewebsites.net`) and anything that
  named a subscription. A dotted word's parts are now tried too, and the subscriptions in scope are matched like
  resources (an ID exactly, a name by the name rule), without a CMDB note. Applies to ServiceNow and New Relic.
- Updating a saved scope took Save current as… with the same name and a confirm. While the applied scope shows
  "(changed)", the Saved scopes menu now offers **Update "<name>"** and **Revert**.
- Service Health listed one row per Azure tracking ID, and Azure often raises several with the same title, so
  Browse, the Triage card and the reports looked duplicated. Events with the same type and title are now one row
  naming its tracking IDs (Triage: "N events"). The Browse banner collapses to one summary line, remembered.
- The ServiceNow tab could show incidents matched against an earlier scope: a Triage ServiceNow card still loading
  when the scope changed replaced the tab's results, so a resource in the new scope showed no match.
- New Relic entities showed only their broad class ("Generic Infrastructure"), so two different entities with
  the same name looked like a duplicate; they now show the specific type, with the class underneath.
- Connection names are checked on Save: a name that clashes with another ignoring case, spaces or punctuation
  ("NR Prod" / "nr-prod", which share an environment variable), contains `|` (the keychain account separator),
  or runs past 40 characters is refused with a plain message on the card that was edited, not one carrying the
  config file path.

- After `az logout` / `az login` to another account or tenant in a terminal, the page kept the old tenant: it
  wrote the first session's tenant into the address bar and kept its cached token. It now follows the CLI on
  reload (unless a tenant was picked or linked), and notices a changed session while open (tab focus, every
  2 minutes) with a **Switch to it** bar.

- Links in tables were the browser's dark blue in dark mode; they now use the accent colour.

## [0.2.0] - 2026-11-03

### Added

- **Settings → Integrations**: read-only connections to ServiceNow and monitoring
  tools, off until enabled. ServiceNow signs in with a username and password, OAuth
  with your own login (PKCE, works with single sign-on), or OAuth client
  credentials; tables `incident`, `change_request`, `em_alert`, `em_event`. New
  Relic connects through NerdGraph queries (mutations refused); Datadog and Grafana
  can be configured and connect later.
- **ServiceNow** tab: active incidents (priority first), changes in progress or
  starting within 24 hours, Event Management alerts and events when the instance
  has them, each with the Azure resources it names (exact, name or possible
  match; no CMDB needed) and a ⚠ CMDB note when its configuration item doesn't
  map them. Triage cards for P1/P2 incidents and changes in progress, and a
  ServiceNow tab on each resource. A table the instance lacks is reported, not fatal.
  By default the tab and the cards show only records naming Azure resources in
  scope (an exact or name match), and count the rest.
- Secrets live in the OS keychain (`keyring`) or `AHC_…` environment variables,
  never in config.yaml, logs, exports or the page.
- Each integration card is one form: connection, sign-in with the password or key
  inline, then Test connection (checks what's typed, saves nothing) and Save. A
  status on every card says what's still missing. The instance URL is accepted as typed (`dev123`,
  `dev123.service-now.com`); Save also stores a secret typed on the same card.
- **Secrets** tab: every Key Vault secret, key and certificate in scope, plus App
  Service certificates, with Expired / Critical / Warning / Healthy counters that
  filter a sortable table (subscription, resource group, vault, type, name, expiry,
  days left), a red-count badge on the tab, and Markdown, CSV and PDF export.
- **Network** tab, from azure-subnet-check: every VNet in scope ranked by its
  fullest subnet; per VNet, address space bars, subnet IP usage (Azure's own
  usage list), free ranges and the subnet sizes that still fit; exports.
- Triage card **Subnets running low**; `network.subnet_warning_percent` (80) and
  `subnet_critical_percent` (90).
- `health.expiry_critical_days` (7): secrets, keys and certificates turn red this
  many days before expiry, not only once expired.
- Cancel for a running check, in the page and on the server: the check's
  remaining Azure requests stop. Starting another check, or closing the tab,
  cancels the previous one.
- `bench` command: times Triage, Search, a resource with and without
  dependencies, its resource group, the AKS fleet and Network (secrets opt-in),
  and reports the slowest kinds of Azure call. `--log-calls` lines now include
  status and duration.

### Changed

- One check at a time: while a check runs, whatever would start another is
  dimmed and says what is still running.
- Clicking a resource checks only that resource; **Show dependencies** draws the
  graph (`graph.auto: true` restores the graph on every click). A resource group
  check opens as a table, worst first, with a Table | Graph switch.
- Secrets and certificates moved from Triage and Checks to the Secrets tab.
- The Checks tab is gone: its AKS sweep (pods by phase, optional detectors) is the
  **Check pods and detectors** button on Triage's AKS card.
- Triage no longer loads twice when the page opens.
- Signed out of the Azure CLI (or never signed in): one sign-in screen with the command
  to run and a Copy button, for signed out, not installed or wrong tenant, which
  carries on by itself once `az login` has worked, instead of an empty Browse page
  with a raw error. Loading the page re-checks the CLI session, so `az logout` takes
  effect on refresh.
- AKS Workloads: findings split into **Happening now** and **Configuration risks**
  (yellow at most). A Kubernetes list the identity can't read (e.g. nodes) no longer
  hides the whole layer or silently skips checks: a note names the checks that didn't
  run and the role they need.
- Key Vault certificates are recognised from their backing secret and listed once.
- The page always opens on Triage: the address bar no longer carries the last
  resource, so a reload doesn't reopen Browse. **Copy link** still builds the full link.
- Favourite subscriptions are listed first in the Subscriptions dropdown (as resource
  groups already were), with a divider after the last favourite in both dropdowns.
- Secrets: a **Needs action** counter (expired, critical and warning together) is the
  default view and the way back after filtering by another counter.
- Secrets: expired is red and critical (within 7 days, still valid) is orange, on the
  tab, its badge and in the exports, so the two read apart at a glance.
- Network: a VNet's subnets are listed by name (exports too); click a column header to
  sort by status, CIDR or usage.

## [0.1.0] - 2026-10-30

### Added

- Local web UI: tenant, subscription, resource group and resource pickers,
  an interactive dependency graph (Cytoscape.js) and a detail panel for each
  node.
- Read-only ARM client: GETs and Resource Graph queries only, rejected
  before sending otherwise; throttling retries; a per-identity cache.
- Dependency discovery from one resource, both outbound and inbound, using
  Resource Graph. Shared network containers are treated as leaves, and AKS
  links to its node pools and load balancer.
- Layered health: provisioning state, Resource Health, fired alerts,
  existing metric alert rules, curated profiles for 18 resource types, a
  heuristic fallback, recent changes, Service Health and a last-week
  comparison.
- Markdown (Mermaid graph) and PDF (fpdf2: pure Python, so it installs the same
  on Windows, macOS and Linux) export, plus a printable HTML report, from the UI or the
  `export` command.
- Resource group filters by tag: keys and values are discovered from the
  loaded resource groups, filters stack, and per-tenant defaults are
  optional. Select shown / Clear for subscriptions and resource groups.
- Explore panel for every resource type: metrics charts with dimension
  splits, type-specific details, scaling (autoscale and node pools vs max),
  Azure "Diagnose and solve problems" detectors, activity (alerts, changes,
  Activity Log, Resource Health history), and Advisor/Defender
  recommendations.
- AKS workloads: pods by phase (platform metrics), Container Insights
  findings, aks-check's checks as a current-status snapshot through a
  GET-only Kubernetes client, and opt-in pod logs.
- Snapshot signals for autoscale/node pool at max, pending or failed AKS
  pods, and Critical health/risk detectors (`health.detectors`).
- Access errors name what was being read, the role that grants it and
  Azure's own message, plus a PIM hint (`core/access.py`).
- `--log-calls` prints every Azure and Kubernetes request as it's sent.
  `AZURE-CALLS.md` lists every request the console can make.
- Kubernetes list calls use `resourceVersion=0`, served from the API
  server's cache rather than etcd.
- AKS uses a matching local kubeconfig context first (`$KUBECONFIG`,
  `~/.kube/config`, AKS desktop's folder, `aks.kubeconfig_paths`), falling back
  to the cluster user credential (`aks.fetch_credentials`).
- Key Vault: secret and key expiry (expired = critical, within
  `health.expiry_warning_days` = warning) in the snapshot and the Details tab.
  Read from Azure Resource Manager's metadata list, which never returns values.
- Loading indicators: a spinner with elapsed seconds wherever data loads, and a
  "checking" panel in the graph area that follows the snapshot's progress.
- Triage-first interface: a **Triage** page ("What's wrong now": fired alerts,
  Service Health, failed deployments, AKS, autoscale near max, private endpoints,
  certificates, recent changes), **search** across every subscription, a
  **resource group graph**, **Checks** (Key Vault expiry scan, AKS fleet) and a
  **Watchlist** with auto-check, plus links that reopen any view.
- Favourites per tenant (`favourite_subscriptions`, `favourite_resource_groups`,
  `*` patterns; tenant keyed by ID, domain or `"*"`) drive the default scope.
  `config` command shows what they resolve to.
- Graphs appear as soon as discovery finishes and colour in as checks complete.
- One **Scope bar** for every page (subscriptions with Favourites/All/Clear,
  resource groups, discovered tag filters), replacing Browse's pickers; Browse
  lists the resource groups in scope and the resources of the one you open.
- **Settings** tab: every setting with its help and default, a live preview of
  favourite patterns, and Save and apply. It validates, edits `config.yaml` in
  place keeping comments (`ruamel.yaml`), keeps `config.yaml.bak`, and applies
  without a restart. Settings now have their types checked.
- The graph and chart libraries ship in `web/static/vendor/`, so the page loads
  nothing from the internet (corporate proxies blocked the CDN).
- Triage **Secrets & keys** card: a background vault scan (limit
  `triage.secret_scan_max_vaults`, reused for 10 minutes) listing what expires
  soon and what expired recently; long-expired secrets are folded away. Vaults
  whose firewall blocks trusted services are named as such.
- Resource group favourites: ☆ in the Resource groups list and a ★ Favourites
  shortcut; no longer applied automatically when the page opens.
- Watchlist: a row turns red and a browser notification fires when an item
  gets worse during auto-check; one Watchlist per tenant.
- Diagnostics shows each finding's **View details** like the portal
  (sections, troubleshooting, data tables), rendered from plain text only.
  Detector findings colour a resource yellow at most, and say where they come
  from (Diagnose and solve problems > category, last 24 h).
- Fixed: tables inside detector details showed as a stray "0" (the
  placeholder used a NUL character, which HTML parsing replaces), and
  line-separated names in a table cell ran together.
- Fixed: values in HTML attributes weren't quote-escaped, which broke the
  Watch buttons and clicking Triage/Checks/Watchlist rows ("JSON Parse error").
- `scripts/build.py`: a single-file executable for the current OS (PyInstaller;
  no Python needed to run it) and a wheel for `pipx install`. Run with no
  arguments, the console starts and opens the browser; a busy port falls back
  to a free one.
- Fixed: connections to Azure are pooled (32, reused) instead of opened and
  discarded past 10; this also caps concurrent requests per session.
- `profile-draft` and `whoami` commands. User config and profiles live under
  `~/.config/azure-health-console/`.
