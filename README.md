# Azure Health Console

A local, **read-only** view of Azure health for incidents and busy periods. Open it in your browser and see:

- **what's wrong right now** across the subscriptions you care about,
- **any resource or resource group**, its health, what it depends on, and what happened to it lately,
- **what would stop you scaling** before a peak (quota, subnets, single points of failure, certificates),
- **ServiceNow incidents and New Relic alerts** matched to the Azure resources they name.

It runs on your machine with your own `az login` session, so it sees exactly what you can see (PIM roles
included) and **never changes anything**. Every request is a read: [`AZURE-CALLS.md`](AZURE-CALLS.md) lists each
one, and `--log-calls` prints them as they go out.

What changed in each version is in [`CHANGELOG.md`](CHANGELOG.md). MIT licensed ([`LICENSE`](LICENSE)).

## Contents

- [Requirements](#requirements): what it needs to run, and what each feature needs on top
- [Install](#install)
- [First run](#first-run)
- [Set up your saved scopes](#set-up-your-saved-scopes)
- [Using it](#using-it)
  - [The Scope bar](#the-scope-bar) · [The tabs](#the-tabs) · [Triage](#triage-whats-wrong-now) · [Investigate](#investigate) ·
    [Readiness](#readiness-before-a-peak) · [Security](#security) · [Cost](#cost) · [Search](#search) · [Browse](#browse) ·
    [A resource](#a-resource) · [Secrets](#secrets) · [Network](#network) · [ServiceNow](#servicenow) ·
    [New Relic](#new-relic-and-other-monitoring-tools) · [Watchlist](#watchlist) · [Alerts](#alerts) · [Is this alert right?](#is-this-alert-right) · [Access](#access-pim) ·
    [Settings](#settings) · [Links](#links)
- [Connecting ServiceNow and monitoring tools](#connecting-servicenow-and-monitoring-tools)
- [Permissions](#permissions): what each feature needs, the recommended access, a custom role
- [How far back you can look](#how-far-back-you-can-look): each history source's limit, measured
- [Configuration reference](#configuration-reference)
- [How health is decided](#how-health-is-decided)
- [Commands](#commands)
- [Troubleshooting](#troubleshooting)
- [For maintainers](#for-maintainers): development, what it's built with, releasing

---

## Requirements

**To run it:**

- The **Azure CLI** (`az`), signed in with `az login` (or the console's **Sign in** button runs it for you).
- **Python 3.11 or later** and **pipx**, unless you install with Homebrew, which brings its own Python. See
  [Prerequisites](#prerequisites) for the commands on macOS, Windows and Linux.
- An Azure account with **Reader** on what you want to look at (see [Permissions](#permissions)). PIM roles work:
  activate them, then click **Refresh access** (in your account menu, top right).
- A browser. The console runs on `127.0.0.1` and sends nothing about you anywhere else; besides Azure and the tools
  you connect, it reads only public pages: Azure's status feed, and this app's PyPI page (whether a newer version is
  out, every 6 hours, `updates.check`; download numbers when About opens).

That's enough for most of the console. **What each feature needs** (roles, the exact actions for custom roles, and
what the page shows without it) is in [Permissions](#permissions); **Access → What the console needs** checks your own
access against it, in the scope you picked. A personal tenant usually has none of the paid parts (Defender, Container
Insights, ServiceNow, New Relic); the console works there too, with the platform checks, health, timeline and the rest.

## Install

| Way | Use it when | You need first |
|---|---|---|
| [Homebrew](#homebrew-macos) | **macOS: the recommended way** | Homebrew, the Azure CLI |
| [pipx](#pipx-windows-linux-or-a-mac-without-homebrew) | Windows, Linux, or a Mac without Homebrew | Python 3.11+, pipx, the Azure CLI |
| [From the source](#from-the-source) | changing the console | Python 3.11+, this repo, the Azure CLI |

Windows and Linux are expected to work but haven't been tested yet; please open an issue if something breaks.

### Prerequisites

The console signs in through the **Azure CLI** (`az`), and no install method installs it for you: Homebrew ships
it as a cask, which a formula can't depend on, and pipx installs Python packages only. Without it the console
still starts, and its page says *Install the Azure CLI to continue* with a link to Microsoft's instructions.

| | macOS | Windows (PowerShell) | Linux (Debian/Ubuntu) |
|---|---|---|---|
| Azure CLI | `brew install --cask azure-cli` | `winget install --exact --id Microsoft.AzureCLI` | `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash` |
| Python 3.11+ (pipx only) | `brew install python` | `winget install --exact --id Python.Python.3.13` | `sudo apt install python3` |
| pipx (pipx only) | `brew install pipx` | `py -m pip install --user pipx` | `sudo apt install pipx` |

Other Linux distributions: see [Microsoft's Azure CLI instructions](https://learn.microsoft.com/cli/azure/install-azure-cli)
and [pipx's](https://pipx.pypa.io/stable/installation/). Check `python3 --version` (`py --version` on Windows) says
3.11 or later; older Ubuntu releases ship 3.10. After installing pipx, run `pipx ensurepath` once (`py -m pipx
ensurepath` on Windows) and open a new terminal, so the commands pipx installs are on your `PATH`.

### pip, pipx or brew?

- **brew** installs the console with its own Python, and upgrades and removes it as one package.
- **pipx** does the same for any system with Python: the console gets its own virtual environment and its command
  goes on your `PATH`.
- **pip** is only for a virtual environment you made yourself, such as the one [from the source](#from-the-source)
  uses. Outside one, macOS has no `pip` command (`command not found`) and Homebrew's Python refuses
  (`externally-managed-environment`); inside one that already has the console, `pip install azure-health-console`
  says "Requirement already satisfied" and installs nothing new. Deactivate a virtual environment (`deactivate`)
  before installing with brew or pipx.

Your settings, saved scopes and Watchlist live in `~/.config/azure-health-console/` (`%USERPROFILE%\.config\azure-health-console\`
on Windows), outside the install, so upgrades and an uninstall keep them; [Remove everything](#remove-everything)
clears them too.

### Homebrew (macOS)

```bash
brew install --cask azure-cli    # once, if you don't have az yet
brew install rafaelsonego/tools/azure-health-console
az login
azure-health-console             # starts it and opens your browser
```

**Upgrade** with `brew upgrade azure-health-console`; **uninstall** with `brew uninstall azure-health-console`.

### pipx (Windows, Linux, or a Mac without Homebrew)

With the [prerequisites](#prerequisites) installed. These steps check every file before it's installed: the release's
list of files is signed, and pip installs only files whose fingerprint (SHA-256) is on that list.

**macOS or Linux** (Terminal):

```bash
V=1.2.0   # the version to install: the newest is at the top of the Releases page
mkdir -p ~/Downloads/azure-health-console-$V && cd ~/Downloads/azure-health-console-$V
R=https://github.com/RafaelSonego/azure-health-console/releases/download/v$V
curl -fsSL -O $R/SHA256SUMS.txt -O $R/SHA256SUMS.txt.sig -O $R/constraints.txt
curl -fsSL -o release-signing.pem https://raw.githubusercontent.com/RafaelSonego/azure-health-console/v$V/healthconsole/release-signing.pem
openssl dgst -sha256 -verify release-signing.pem -signature SHA256SUMS.txt.sig SHA256SUMS.txt   # must say "Verified OK"
shasum -a 256 -c SHA256SUMS.txt --ignore-missing                                                # must say "constraints.txt: OK"
pipx upgrade-shared
pipx install "azure-health-console==$V" --pip-args="--require-hashes -c constraints.txt"
az login
azure-health-console
```

**Windows** (PowerShell; `openssl` comes with [Git for Windows](https://git-scm.com/download/win)):

```powershell
$V = "1.2.0"   # the version to install: the newest is at the top of the Releases page
New-Item -ItemType Directory -Force "$HOME\Downloads\azure-health-console-$V" | Set-Location
$R = "https://github.com/RafaelSonego/azure-health-console/releases/download/v$V"
foreach ($f in "SHA256SUMS.txt", "SHA256SUMS.txt.sig", "constraints.txt") { Invoke-WebRequest "$R/$f" -OutFile $f }
Invoke-WebRequest "https://raw.githubusercontent.com/RafaelSonego/azure-health-console/v$V/healthconsole/release-signing.pem" -OutFile release-signing.pem
& "$env:ProgramFiles\Git\usr\bin\openssl.exe" dgst -sha256 -verify release-signing.pem -signature SHA256SUMS.txt.sig SHA256SUMS.txt   # must say "Verified OK"
$listed = (Select-String '  constraints\.txt$' SHA256SUMS.txt).Line.Split(' ')[0]
if ((Get-FileHash constraints.txt).Hash.ToLower() -ne $listed) { throw "constraints.txt isn't the released file: stop here" }
pipx upgrade-shared
pipx install "azure-health-console==$V" --pip-args="--require-hashes -c constraints.txt"
az login
azure-health-console
```

If a check doesn't say what's shown, stop: don't install, and [open an issue](https://github.com/RafaelSonego/azure-health-console/issues).

**Upgrade:** run the first four lines above (macOS/Linux) or three (Windows) with the new version, into a new folder.
Then check the files with the version you already have, and install:

```bash
azure-health-console verify        # must end with "Verified": it checks with the key inside your installed version
pipx upgrade-shared
pipx install --force "azure-health-console==$V" --pip-args="--require-hashes -c constraints.txt"
```

Don't use `pipx upgrade`: it doesn't check the files (and stops, because it reuses the last version's
`constraints.txt`). **Uninstall** with `pipx uninstall azure-health-console`.

### Check what you install

The release pipeline lists every file of a release with its SHA-256 in `SHA256SUMS.txt` and signs that list with a key
that never leaves Azure Key Vault: `SHA256SUMS.txt.sig` on the
[GitHub release](https://github.com/RafaelSonego/azure-health-console/releases). `constraints.txt`, on that list,
names the exact version and SHA-256 of every package pip installs, the console's own wheel included, so
`--require-hashes -c constraints.txt` refuses anything else, from PyPI or a company mirror alike.

- **First install:** the public key comes from this repository. Its fingerprint is
  `873cc6a6353d51196dfa4fcb6b87fed1556897ec2cc79be0477e33429a65e8ab`; check it with
  `openssl pkey -pubin -in release-signing.pem -outform DER | openssl dgst -sha256`.
- **Upgrades:** `azure-health-console verify [folder]` checks the signature with the key built into the version you
  already have, so a key swapped on GitHub later wouldn't pass, then each file in the folder against the list.
- **Homebrew** installs the dependencies from the release's `requirements-release.txt`, each one checked by its hash.
  To check the formula: download and verify `SHA256SUMS.txt` as above, then compare `grep tar.gz SHA256SUMS.txt` with
  `brew cat rafaelsonego/tools/azure-health-console | grep sha256`.

### From the source

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m healthconsole serve --open
```

Every dependency is plain Python (PDF export included), so nothing else needs installing. The version `pip show`
reports for this editable install is the one it was first installed with; the code is your working tree.

### Uninstall

Remove it the way you installed it:

| Installed with | Uninstall |
|---|---|
| Homebrew | `brew uninstall azure-health-console`, then `brew untap rafaelsonego/tools` if you don't use the tap for anything else |
| pipx | `pipx uninstall azure-health-console` |
| From the source | delete the repo folder (the `.venv` is inside it) |

This keeps your settings, cache and saved secrets, so a reinstall picks up where you left off. The Azure CLI, Python
and pipx stay too; remove them with the same tool you installed them with (for example `brew uninstall --cask
azure-cli`, `winget uninstall --exact --id Microsoft.AzureCLI`, `sudo apt remove azure-cli`) if nothing else uses
them.

### Remove everything

For a fresh start, uninstall it as above, then delete its settings, cache and the secrets it saved.

**macOS:**

```bash
brew uninstall azure-health-console 2>/dev/null
pipx uninstall azure-health-console 2>/dev/null
rm -rf ~/.config/azure-health-console ~/.cache/azure-health-console
security dump-keychain | grep -E '"svce"<blob>="(Azure Health Console|azure-health-console)' \
  | sed 's/.*="//; s/"$//' | sort -u \
  | while read -r s; do while security delete-generic-password -s "$s" >/dev/null 2>&1; do :; done; done
hash -r
```

**Windows (PowerShell):**

```powershell
pipx uninstall azure-health-console
Remove-Item -Recurse -Force "$HOME\.config\azure-health-console", "$HOME\.cache\azure-health-console" -ErrorAction SilentlyContinue
```

Then remove the secrets in **Credential Manager** → **Windows Credentials**: every entry named *Azure Health
Console - …* (or *azure-health-console*).

**Linux:**

```bash
pipx uninstall azure-health-console
rm -rf ~/.config/azure-health-console ~/.cache/azure-health-console
```

Then remove the secrets from your Secret Service (for example Seahorse, *Passwords and Keys*): every item named
*Azure Health Console - …*. Without a Secret Service the console keeps no secrets, only the `AHC_…` environment
variables you set yourself.

Exported reports stay in the `exports` folder wherever you ran the console.

---

## First run

1. **Sign in:** `az login` (or `az login --tenant <your-tenant-domain>`). If you skip this, the page shows a
   "Sign in to Azure" screen: **Sign in** runs `az login` for you (your browser opens), **Use a code instead** shows a
   code to enter at microsoft.com/devicelogin on any device, or run the command it shows yourself. Its **Tenant** box
   adds `--tenant`: a personal account, or a tenant that asks for multi-factor sign-in, needs it. It carries on by
   itself once you're signed in. Later, **Switch account or tenant** (in your account menu, top right) does the same
   to switch account or tenant (pick **another tenant** and type its ID or domain, e.g. a personal tenant on another
   account), or after activating a PIM group. **Sign out** (same menu, under your account's name) runs
   `az logout --username <account>` after asking: only that account is signed out, others stay. Both change the Azure CLI's sign-in on your machine, so terminals see it too. Only the
   local console has these buttons.
2. **Start it:** `azure-health-console` (or `python -m healthconsole serve --open`). It listens
   on `http://127.0.0.1:8765`, your machine only (another port if that one is busy). It prints (and with `--open`,
   opens) an address carrying your key: open it once per browser and `http://127.0.0.1:8765` keeps working there,
   restarts included. The page keeps the key for its own address and sends it with every request; another user,
   program or website (a page on another port included) can't use the console without it. The key travels after a
   `#` in that address, which the browser never sends anywhere. It's kept in `cache_dir/launch-key`, readable only by
   you; delete it to make a new one. A key file anyone else could read or change is replaced, never used.
3. **Pick a scope:** the page opens on **Triage** with nothing selected, so it hasn't queried anything yet.
   In the Scope bar, choose **Subscriptions ▾** (or a saved scope), then press **Apply**.
4. **Look around:** Triage fills in within seconds. Click anything listed to open it.

Before your first incident, take two minutes to [save a scope or two](#set-up-your-saved-scopes) and open with
one, so step 3 is done before you look.

---

## Set up your saved scopes

A **saved scope** is a name for a whole Scope bar: subscriptions, resource groups, tag filters and the ServiceNow
and New Relic connections, applied in one pick, e.g. *Prod* and *NonProd*. It's the one way to remember what you
look at: alert rules watch one, the `readiness` command takes one, and **Open with** picks what the page opens on.

1. Set up the Scope bar (subscriptions, tag filters, resource groups) and **Apply**.
2. **Saved scopes ▾ → Save current as…** and give it a name.
3. **Settings → General → Open with:** nothing (no queries until you pick), every subscription, or one of your
   saved scopes. It's saved as soon as you change it.

They're kept in a small file on your machine, never in the repo, so subscription names aren't committed anywhere.

**Coming from favourites (before 1.0.0)?** `favourite_subscriptions`, `favourite_resource_groups`, `tag_filters` and
`triage.scope` still work, as a saved scope called *Favourites* (and *Favourite groups* for the groups). Triage,
Settings and the Saved scopes menu say so, with **Move them into saved scopes**: one click writes them as saved
scopes and Open with under this tenant and removes the old keys (the previous file is kept as `config.yaml.bak`).

### Editing the file yourself

| System | File |
|---|---|
| macOS / Linux | `~/.config/azure-health-console/config.yaml` |
| Windows | `C:\Users\<you>\.config\azure-health-console\config.yaml` |

Create it if it doesn't exist. To keep it elsewhere, set `AHC_CONFIG` to its path, or pass `--config <path>`
before any command. The console uses settings, health profiles, the Watchlist and its own data only from files and
folders no other account can change (on macOS and Linux: yours or root's, without write access for others, folders
above included). Yours are tightened for you; anything else stops with the `chmod` to run. A settings or profile file
over 1 MB (256 KB for a profile) isn't read. Everything is per **tenant**, because the same person often works in more than one; name the
tenant whichever way is easiest:

| Key | Example | When to use |
|---|---|---|
| `"*"` | `"*":` | you use one tenant; applies to every tenant |
| the tenant's domain | `contoso.onmicrosoft.com:` | readable; `az account show --query tenantDefaultDomain -o tsv` |
| the tenant ID | `00000000-0000-0000-0000-000000000000:` | if you already have it |

```yaml
# ~/.config/azure-health-console/config.yaml
tenants:
  contoso.onmicrosoft.com:
    open_with: Prod              # none | all | a saved scope's name
    saved_scopes:
      - name: Prod
        subscriptions:
          - "app-*-002"          # app-dev-002, app-qa-002, app-stage-002, app-prod-002…
          - "app-*-003"
        resource_groups: ["*-checkout-*"]   # optional: only these groups
        tag_filters: {costcenter: "1234567"}  # optional: only groups with this tag
```

Saved scopes belong to one tenant (they name its connections), so they need its domain or ID, not `"*"`. No scope
can be called `none` or `all`.

| Pattern | Matches |
|---|---|
| `app-prod-002` | exactly that subscription |
| `app-*-002` | every environment of `app-…-002` |
| `*-002` | every subscription ending in `-002` |

Matching ignores case. Quote patterns that start with `*`, as YAML requires: `"*-002"`.

**Check it:** `azure-health-console config` (or `python -m healthconsole config`) prints the file in use, the
tenant entry that matched, each saved scope with how many subscriptions its patterns pick, and Open with. A typo in a setting name is rejected with
a clear message instead of being ignored. Then click **Reload from file** in Settings (or restart the console).

---

## Using it

### The Scope bar

The bar under the tabs says **what every page looks at**. Each control is labelled; read it left to right:

- **Using ▾:** a saved scope, which sets everything to its right in one pick ("None picked (8 saved)" until you pick
  one, "None saved yet" before your first). After you change anything it says "Prod (changed)" and offers **Update "Prod"** (save the bar over it) and
  **Revert**.
- **Subscriptions ▾:** a searchable checklist with **All** and **Clear**.
- **Tags (+ filter by tag):** narrow resource groups by any tag (keys and values are discovered from your resource groups).
  Filters stack; remove one with ×. While one is on, the bar says how many groups it hides ("12 hidden by tag"), and
  **Clear filters** drops every tag filter and resource group pick at once (Apply to use it).
- **Resource groups ▾:** every group (after tag filters) is in until you untick some; the panel says how many are in.
  **All** puts every group back; type in its search box and **Only these N** keeps just the groups it shows.
- **ServiceNow ▾, New Relic ▾** (after the divider): which connection the ServiceNow and tool tabs, the Triage cards
  and a resource's tabs use (when connected; remembered per tenant).

**Changes wait for Apply.** Ticking things changes nothing on the page yet: the bar shows
**Not applied yet: subscriptions +2, tag filters · Apply · Discard**. **Apply** reloads every tab once;
**Discard** puts the bar back. Picking a saved scope applies straight away. On the right, the bar shows the scope
in use ("In use: 2 subscriptions · 14 resource groups").

**Another account or tenant:** the page follows your Azure CLI. Run `az login` (or `az login --tenant …`) in any
terminal; the open page notices within a couple of minutes and offers **Switch to it**. The Tenant menu lists every
tenant the CLI is signed in to, whichever account, with the account's name beside another account's tenants. A tenant picked in the
Tenant menu, or opened from a link with `?tenant=`, stays put instead.

### The tabs

**Summary · Triage · Browse · Investigate · Readiness · Security · Cost · Sweeps ▾** (Secrets & certificates, Network) **· Tools ▾** (ServiceNow and each
monitoring tool, when connected) **· Watchlist · Access**, and **Settings** on the right; your account menu (your name, top right) holds **Refresh access**, **Switch account or tenant**, **Sign out**, the ☀ ◐ ☾ theme switch (light, system, the default, or dark), the **Layout** switch (**Classic**: the whole page scrolls; **Panes**: the bars stay put and each part of the window scrolls once, nothing scrolling inside it, like the Azure portal; windows 700 px wide or less stay classic) and **About** (what it is, its version, its PyPI numbers: latest release and downloads from pypistats.org without mirrors, read when it opens and kept 6 hours, and where your files are). A menu shows which of its
pages is open ("Tools: New Relic"); Sweeps carries the Secrets badge, and Access shows how many PIM roles are
active. Which views lead the band depends on your persona (below); the rest are under **More ▾**.

### Personas: View as

**View as** (top right) lays the console out for how you work. It never changes what you can read: your Azure roles
decide that, and every view stays one click away under **More ▾**. The first run asks which fits; change it any time.
To open with the same persona at every start, set **Settings → General → Persona → Open as** (`persona` in the
config file); empty keeps the first-run question and then this browser's last pick, and **View as** still switches it for
the session.

| Persona | Opens on | Leads the menu | Triage cards first | A resource opens on |
|---|---|---|---|---|
| **Manager / lead** | Summary | Summary, Triage, Cost, Readiness, Security | alerts, Service Health, failed deployments, exposure | Overview, then Cost |
| **SRE / on-call** | Triage | Triage, Investigate, Browse, Watchlist, Tools | alerts, App Insights spikes, Service Health, failed deployments, AKS | Overview, Timeline, Application Insights |
| **DevOps / platform** | Triage | Triage, Browse, Investigate, Readiness, Network, Secrets, Cost | failed deployments, autoscale, subnets, AKS | Overview, Details, Scaling, Diagnostics |
| **Security** | Security | Security, Access, Browse, Secrets, Triage | exposure, private endpoints, failed deployments | Security, Vulnerabilities, Details |
| **Everything** | Triage | every view | Triage's own order | Overview |

Manager shows summaries: Triage cards list their first three items (**Show N more** for the rest) and queries are
folded away. A team can change a built-in persona or add its own in the config file (`personas:`, see
`config.example.yaml`); the console checks the names and says what's wrong.

### Summary

The headline of each view for the scope in use, written for someone who won't open the views. A **status line** on top
gives each card a colour (click one to jump to its card); **Copy summary** puts it first, then one line per card, ready
for an email or a chat.

- **Problems now:** Triage's red and yellow, the worst first; click one to open it.
- **What changed:** new or worse and resolved items since the summary from at least a day ago, per card. Each summary
  of a scope is kept on this machine (`cache_dir`, readable only by you); the first comparison is ready a day after the
  first one. **Clear cached data** removes them.
- **Cost:** this month, where it's heading at its rate so far against last month, the last 7 days against the 7 before,
  30 days by day, the top subscriptions and every budget on them (spent and forecast). When the 7-day change includes
  the 1st of the month and that day stands out, the card says so: reservations, Marketplace and similar monthly
  charges are booked on the 1st.
- **Security:** a tile per area of Security's checks, the first warnings, and Defender's vulnerability totals with the
  resources to fix first (reachable from the internet, then running, then CVSS).
- **Readiness:** the last full report for the scope, or Readiness's quick checks (scaling, vCPU quota, subnets, single
  instances; no Kubernetes or Key Vault reads) for the peak date last used on Readiness.
- **Your access:** what you can read, the roles you hold, the PIM roles you could activate.

Which cards show, and in what order, is the persona's `summary` list (Manager: Problems now, What changed, Cost,
Security, Readiness; empty shows every card). **Settings → General → Summary** sets how many items each card lists,
whether Readiness's quick checks run, when a budget turns yellow, and what What changed compares with.

### Guides by role

- **SRE, an incident in five minutes:** Triage (or the alert in the bar) → the resource's **Timeline** (what fired and
  changed) → **Investigate** on its App Insights → **Samples** → **Follow** a failing request → its gateway span and
  **Pod** → **Copy for incident**. Watchlist the resources you're watching during the incident.
- **DevOps, before a release or a peak:** **Readiness** with the peak's last day (what stops scaling, what expires),
  **Security** (exposure, keys, TLS), **Secrets** (expiry), **Network** (subnets running low), then **Billed while idle**.
- **Manager, the weekly review:** **Summary** → **Copy summary**; open **Cost** for the month by subscription or by a
  tag, and **Readiness** before a busy period. Every view's **Refresh** reads again; every number says how old it is.
- **Security, a posture review:** **Security** (exposure, keys, transport, deletion protection; Vulnerabilities), a
  resource's **Timeline** for who changed what, and **Access → Check someone's access** for a person's roles.

### Triage: "What's wrong now"

A few seconds, across every subscription in scope. Each card lists the affected resources; click one to open it.

**Refresh** reloads it in place. **Auto-refresh** (in the header; the default is in Settings → General → Triage) does
the same every 1 to 15 minutes while Triage is open and the browser tab is visible, never on top of a load still
running. After a refresh of the same scope, anything not listed the time before has a **new** mark, and a card whose
worst status went up is outlined as **worse**. The page compares its own two results, so the marks cost nothing; each
refresh costs what pressing Refresh does.

| Card | Shows |
|---|---|
| Fired alerts | Sev0–2 alerts still fired, grouped by resource |
| Azure service status | Azure-wide outages from the public status feed (`azure.status.microsoft`), then your subscriptions' Service Health grouped by title: incidents, planned maintenance, security advisories, incidents resolved in the last 24 h, and one line counting the health advisories (retirements, action required). ↗ opens Service Health in the portal or the status post. An alert rule's *new Service Health event* counts active outages, incidents and maintenance only. A line under the description says what the public feed showed and when; **Azure status ↗** and **Microsoft 365 service health ↗** under the card opens the admin center (not read by the console) |
| Failed deployments | resources whose provisioning failed or was cancelled |
| AKS clusters | stopped, failed, or node pools at their maximum. **Check pods and detectors** asks every cluster for pending, failed or unknown pods (one request per cluster), optionally with Azure's detectors |
| Autoscale near max | resources at 80% or more of their autoscale maximum |
| Private endpoints | connections pending, rejected or disconnected |
| Subnets running low | 80% (yellow) or 90% (red) of usable IPs in use; details under Sweeps → Network |
| Public network access | resources open to all networks, as information (it never turns Triage red); the full list, with what's restricted and who created or changed each, under Security → Exposure & configuration |
| App Insights: failure spikes | components whose last hour has clearly more failed requests or exceptions than the same hour a week ago; red at 20%+ failed |
| ServiceNow, New Relic | P1–P3 incidents, changes (recent, in progress and scheduled), and what the tool is alerting on, when connected |

A resource a card lists that changed lately (`triage.changes_hours`, people and pipelines, not Azure itself) says so
under it: **↻ Changed 2× (Update) in the last 1 h, latest by …**. There's no list of every change: those are on
each resource's Timeline.

Narrow it to a team with the Scope bar, e.g. **+ tag filter** → `costcenter` → the team's value.

### Investigate

One failing request end to end, starting from the app that's failing. Nothing is picked for you; each step is a read
of its own, with its KQL under **Query sent**:

1. **Pick the app.** The Application Insights components that **need a look**: a red or amber status, or any server
   errors (5xx) in the last hour. The quiet ones are counted ("4 quiet, hidden") and **Show all** lists them; **Go to
   a component or app by name** searches every component and the apps that report to them. Each row has the Web and
   Function Apps that report to it (their `hidden-link` tag; AKS apps don't say; a long list shows three and "+N
   more"), its last hour (failed of all requests, exceptions) against the same hour last week, worst first: the same metrics read as Triage's failure-spike card, one GET per
   component for every one in scope. The list shows at once, A–Z; each component is then measured, 25 at a time, and
   the rows re-sort as their numbers arrive (a few hundred components take about 15 s). **Server errors (5xx)** also lists the top three 5xx codes (*502 7,938 · 500 6,968 ·
   503 2,392*) and, in amber, failures without a usable status: responses **not sent in full** (API Management couldn't
   send the whole response, usually because the caller hung up while the backend was slow) and **failed runs (no HTTP
   status)** (code 0: a function or job started by a queue or timer, not over HTTP). Neither is 5xx. Hovering **Last
   hour** lists the 4xx codes, mostly the caller's side. Always the last hour, whatever
   Look back says. **Last 24 h** is failed requests per
   hour, grey, with its peak beside it (each line is scaled to its own peak); the newest point is red only when the
   last hour had failures. **Investigate** on a row picks it ("Investigating shop-appi · pick another app").
   **Investigate** on a failing operation of a resource's App Insights tab jumps straight to step 3. An operation ID
   from a log or an alert can be pasted here once the app is picked.

   **What the status means**, written under it with a *change* link to its setting (*Settings → Thresholds → App
   Insights*):

   | Status | When (defaults) |
   |---|---|
   | **critical** | failing more than last week (below) at a 20% failure rate or more, with 20 failed or more |
   | **warning** | failing more than last week: 5 failed or more and a rate over 1 point above last week's; or exceptions 10 or more and at least double last week's; or **failing a lot, as last week** (the critical rate and count, with no change) |
   | – | none of those |

   Triage's spike card uses only the "more than last week" rules: it lists what got worse.
2. **By HTTP code:** every failed request in the window by its code: server errors (5xx), then not sent in full,
   then failed runs (code 0), then 4xx, with each code's count and share of the failures, how many endpoints return it
   and when it was last seen. Not sent in full and code 0 add **Why**: the exceptions logged on those requests, by
   reason (*ClientConnectionFailure*: the caller closed the connection; *Timeout*; a failed policy expression…) and
   where it broke (the API Management policy step, or the code location), with **Follow**. Opening a code lists its 5
   busiest endpoints; **Samples** there reads only that code's failures, and
   **Follow one** opens the latest request that failed with it end to end.
   **Its failing endpoints:** each endpoint (operation) and the service (cloud role) that answered, failed requests
   out of all, failure rate, HTTP status codes, last failure, over the **Look back** window (one picker at the top of
   Investigate for steps 2 to 4 and Search logs, with a *Showing:* line saying what each step covers; 1 hour to 30 days;
   Application Insights keeps 30 days by default, some components 90 or more, so a longer window finds nothing older;
   30 days of a busy app takes 10 to 15 seconds).
3. **Its latest failures:** that endpoint's latest failed requests (tick *successful ones too* for the rest), with
   when, HTTP status, how long it took, the instance (pod or host) and URL. Samples you've followed are marked
   **✓ followed**.
4. **One request end to end** (**Follow** on a sample, or paste an operation ID at the top): every request,
   dependency, exception (click for the stack) and log line with that operation ID, in time order, indented under the
   span that made it. **API Management** services in scope that log to Application Insights are read too (ticked at
   the top; the gateway forwards the trace ID, so its span sits on top, marked *gateway*). A service whose gateway
   logs go to a workspace gets **Gateway logs**: rows within 2 minutes whose URL contains the request's path, marked
   probable, since they share no ID with it (gateway logs keep no trace header unless the service logs headers). The
   line at the top names which services offer which, and which offer neither.
5. **The pod that served it** (**Pod …**, from the request's role instance): the cluster in scope whose Container
   Insights saw that pod name, its state then, when it started, restarts, owner and node; its logs within 2 minutes of
   the request (the Kubernetes API when reachable, else Container Insights); warning events in its namespace 15
   minutes either side; and what changed on the cluster from 6 hours before. When no cluster knows the name, pick
   the cluster and namespace yourself.

**Finding your way.** A trail in the bar at the top says where you are (app › what you picked › latest 500s ›
request › pod); each part jumps back to its step. Going deeper folds the step above to one line saying what you picked
("you picked 500 · GetItem", "you're following 01:41 · 500 · 1.1 s"), so the next step opens right under it.
**Show all again** reopens step 2 at the row you came from; **Back to the samples** (on steps 3 and 4) reopens the
sample list to pick the next one.

**Counts** name their window ("500s in the last 30 days", "Failed in the last 7 days") and include sampling: where
Application Insights keeps one row for many requests, each row counts as the requests it stands for (`itemCount`), so
totals and failure rates match the component's own metrics.

**Recent changes** (in step 2) lists what Azure recorded changing on the component, its reporting apps and its
gateways in the window, each field before → after ("sku.capacity 3 → 4" is API Management scaling). An empty window
says when the last change was. Azure keeps 14 days; code deployments and API Management APIs and policies aren't
recorded there.

**Search logs** (sub-tab) searches the component's log lines and exceptions for text and a level; **Follow** on a
line opens its request. Nothing is read until you click, and each read is one query per component.

### Security

Three tabs, all over the scope in use, read-only and with your own access: **Exposure & configuration**,
**Vulnerabilities** and **Access audit**.

**Exposure & configuration** is a handful of Resource Graph queries for the whole scope, checked by setting rather
than by resource type, so a new type that has the setting is covered too:

| Area | Warning | Info |
|---|---|---|
| Exposure | public network access with no firewall; AKS API server with no authorized ranges; anonymous blob access; NSG inbound rules from the internet to SSH, RDP, WinRM or database ports (or all ports); public IPs on network interfaces | public access whose firewall sits in child resources the console doesn't read (PostgreSQL, Redis…); Recovery Services vaults and Data Factories with public access (they have no IP firewall); Databricks workspaces with public access (their IP access lists are a workspace setting, not in ARM); other ports open to the internet; disks exportable by SAS from any network |
| Keys & local auth | | local (key or SAS) auth, storage shared keys, admin users, SQL logins |
| Transport | | minimum TLS below 1.2, HTTP allowed, Redis's non-TLS port |
| Deletion protection | | Key Vault purge protection or soft delete off |

Web Apps are judged by their access restrictions (site and SCM), AI Search by its IP rules, Data Explorer (Kusto)
by its allowed IP ranges, API Management by its virtual network mode (internal is restricted). Service Bus and Event
Hubs namespaces, SQL servers and Synapse workspaces with public access get one extra ARM read each for their firewall
rules, in parallel, only when this tab or a resource's Overview opens (Triage's card doesn't wait for them): a
namespace's rules count only when its default action is Deny, and a SQL or Synapse rule from 0.0.0.0 to
255.255.255.255 is a warning.

A public endpoint behind a firewall or authorized ranges is listed as *restricted* (hidden unless ticked). Each row
says what to do and who created the resource, or last changed one of those settings, in the last 14 days (all
Resource Graph keeps); **only created or changed in the last 14 days** shows just those: the usual mistake at
creation. The area tiles filter the list, grouped **by resource** (one row each, worst first, every finding with
what to do) or **by finding**.

#### Vulnerabilities

What's vulnerable in the scope, what to fix first, and what nobody is scanning.

- **Findings from Microsoft Defender for Cloud:** one per vulnerable package on a VM, a running container or a registry
  image, with the CVEs, CVSS, installed and fixed versions. A summary (by severity, how many have a fix, where they
  are), then **By resource**, ranked by what to fix first (reachable from the internet, then running rather than just
  an image, then fixable, then CVSS), **By software** ("openssl on 15 resources, fixed in 3.0.15"), or **By CVE**
  (one row per CVE, however many packages, images and image versions have it). Filters for severity, *has a fix*,
  CVSS and type; the page opens on **only running + has a fix + High**, what to act on now, **Show everything**
  clears them, and your own choice is kept in this browser (exports follow the filters). Click a row for its
  findings and CVEs; each finding links to its page in Defender (**Defender ↗**), and **Open Defender for Cloud ↗**
  sits at the top. *Reachable* means a VM's public IP,
  public network access left on, or a public AKS API server (a container's own exposure isn't read).
- **Search** findings by CVE (`CVE-2026-1234` matches exactly, not `CVE-2026-12345`), package, resource, container
  or image, or version; several words must all match. **only running** keeps VMs and containers running in AKS (not
  images only stored in a registry). A CVE search first says where it is and what fixes it ("9 resources, 3 running in
  AKS, fixed in 2.39.3-9ubuntu6.6"), and drill-downs put the packages with that CVE first. Search, filters and export
  all apply together.
- **Which image exactly:** a registry finding shows its repository and digest (copy it), and **find tags** asks the
  registry which tags point at it ("1.8.0, latest · pushed 14 Sep"). A container running in AKS has **running
  image**, which reads the pods and shows the `image:tag` each one runs. Both need more than Reader (below); without
  it, the page says what refused and why, with any PIM role that would open it, and works as soon as you have it.
- **Platform checks, no Defender needed:** AKS clusters on a Kubernetes version Azure no longer supports (or only
  under Long Term Support, which the cluster isn't on) or behind on patches, against Azure's own list for the region;
  node images over 60 days old; Linux Web and Function apps on a runtime past or near its end of life, by Azure's own
  dates; storage, SQL and Redis accepting TLS below 1.2, and storage or apps allowing plain HTTP.
- **Not scanned:** machines without a vulnerability scanner and subscriptions with Defender's scanning plans off, so
  "no findings" never reads as "safe".
- **Export** as Markdown, CSV or PDF. Each resource also has a **Vulnerabilities** tab (an App Service app's runtime,
  TLS and FTP are checked there, Windows apps included).

Defender refreshes its findings up to once a day. The console never sends package or resource data anywhere.

#### Access audit

Who can change things or **see data without PIM** in the scope: people only (not apps or managed identities).

- **Standing access:** a role assignment that isn't a PIM activation: assigned outright, or through PIM as *active*
  with no end (time-limited active ones show their end date). Assigned to the person, or to a group they're in
  (nested groups included). A PIM activation, which ends by itself, isn't listed.
- **What it lets them do,** worked out from the role's permissions rather than its name, so custom roles count, widest
  first: *Manages access* (Owner, User Access Administrator), *Changes everything* (Contributor), *Gets keys or
  secrets, or signs in* (storage keys and SAS, connection strings, Cosmos DB read-only keys, a secret's value, signing
  in to a machine), *Changes data*, *Sees data* (blobs, files, queues, tables, configuration values…), *Changes some
  things* (start or stop a VM, restart an app).
- **Reader roles that show no data don't count.** Reads Azure names as actions (`checkNameAvailability`,
  `getBgpPeerStatus`, `backendHealth`, `exportTemplate`…) are told apart by their verb. `audit.ignore_actions` lists
  the rest, each explained in `config.example.yaml`: harmless writes (support requests, deployments, which run with
  the caller's own rights, operation results, provider registration), metadata (Key Vault Reader sees names and
  dates, never values), logs and metrics (Log Analytics Reader, Monitoring Data Reader: people need them during an
  investigation, so they aren't standing access to remove) and Kubernetes objects other than secrets. Remove an entry
  to count it.
- **AKS Cluster User Role** only hands out the kubeconfig on a cluster with Entra ID (what you can do there comes from
  Kubernetes RBAC or the AKS RBAC roles, which count), so it's left out; on a cluster **without** Entra ID that
  credential is admin, so there it counts, naming the clusters. The Cluster **Admin** Role always counts.
- **Conditions:** an assignment with a condition (e.g. User Access Administrator allowed to assign only some roles)
  shows it, so a limited one can be told from a full one.
- **Could use PIM instead:** someone who holds a role standing and can also activate the same role through PIM (at
  that scope or above, directly or through a group) doesn't need the standing assignment, so it can be removed; those
  are counted and flagged. Everything a person can activate, not only these matches, is on
  **Access (PIM) → Check someone's access**.
- One row per person, widest access first; **Show** lists each role, where it applies (with a link to its Access
  control in the portal), what **type** it's on (Key Vault, Storage Account, Subscription, Resource group…) and how
  they got it. Tiles filter by what they can do; filter by **name** (or sign-in name), **role**, **group** and
  **type** (lists from the results, with how many people each), *assigned directly*, *could use PIM instead* and *guests*.
  The type is where the role is assigned: one on a subscription or resource group also covers every resource in it,
  so *Type = Key Vault* shows roles given on vaults themselves. **Export** as Markdown, CSV or PDF.
- **Two sources, so nothing is missed:** Azure's PIM records per subscription (they tell a PIM activation from
  standing access) and Resource Graph's role assignments, which fill in the subscriptions where you lack Reader at
  subscription level and the few assignments the PIM records leave out. In those subscriptions each resource group
  holding assignments is read on its own (Reader on the group is enough); a group that refuses too is marked
  *standing, or a PIM activation (can't tell here)*. Checked against an independent rebuild per person on a large
  tenant: every role the audit shows exists in Azure, and nothing readable is missing.
- **Key Vault access policies:** vaults still on the access policy model give secrets through their policies, not
  roles. Each entry that reads a secret's value (*get*), changes data or uses keys is listed as **Key Vault access
  policy** with its permissions; access policies have no PIM, so they're always standing. *list* alone is metadata.
- **Assignments to deleted users or groups** (*Identity not found* in the portal) give nobody access but are worth
  removing: listed under the people, with where each sits.
- **What it can't tell:** group memberships made through PIM for Groups look the same as standing ones (Microsoft
  Graph needs a permission the Azure CLI sign-in doesn't have); the page says so.
- **Saved on this machine:** the last result per scope is kept in `cache_dir` (readable only by you) for
  `audit.keep_days` (7; 0 keeps nothing), so the audit opens at once, even for a whole tenant, under a *Saved result
  from …* line with its age. Access changes at any moment and can't be read as "what changed since", so a saved
  result is never patched: **Refresh** (or *Read it again from Azure*) reads everything and replaces it. A different
  `audit.ignore_actions` is a different saved result. **Clear cached data** removes them.

### Cost

From Azure Cost Management, with your own access, as **Actual** cost (as invoiced: a purchase on the day it's billed)
or **Amortized** (reservations and savings plans spread over their days and charged to the resources that use them),
picked at the top of the Cost view and remembered. Azure updates costs a few times a day and throttles cost reads hard
(about two dozen subscriptions a minute), so the console keeps a **cost store**: each subscription's cost by resource
group, charge type and day since the first of last month, and this month by resource and service, read once and kept
on disk in `cache_dir` (one file per tenant, account and cost type, readable only by you; Amortized is read the first
time you pick it). Browse's cards, Summary and the Cost view are answered from it in milliseconds.

- **This month against last month:** so far this month (to the latest day Azure has costs for, usually yesterday)
  against the same days of last month, e.g. Nov 1–16 against Oct 1–16, never against all of last month.
- **Purchases** (charge type Purchase or Refund: a reservation's monthly payment, billed on its purchase day, outside any
  resource group) count in This month but not in the 7-day figures, the spikes or the usage line, where one payment
  would read as a spike and then a fall. *Where this month is heading* is usage at its daily rate so far, plus this
  month's purchases once, plus last month's purchases on later days expected again.

- **The first read** runs in the background once you open anything that shows cost: the scope in use first, whole
  (its last 30 days, then the rest of last month, which the month comparison needs), then every other subscription
  you can read, the last 30 days of each before the rest of last month, then this month by resource. Until a
  subscription's last month is in, its comparison says *reading…* rather than showing 0. A card you open for a subscription not stored yet is read at once (two queries side by side) and goes
  before the background read.
- **Keeping it current:** a subscription older than 6 hours is served as stored and has its last 3 days read again in
  the background (older days don't change). **Refresh** rereads now. A subscription whose costs are refused is asked
  again after 6 hours or **Refresh access**. **Clear cached data** (account menu) forgets everything kept.

- **Browse:** a subscription's **cost** link (beside its name in the tree, or the crumb above a group) opens its page
  with a **Cost** card: this month, last month by the same day, all of last month, where this month is heading, its
  purchases, the last 30 days of usage by day, by service, its top resource groups and its budgets. A group's summary has the same card with
  its top resources. A resource has a **Cost** tab: 30 days by day and by meter. A few seconds each.
- **The Cost view** reads every subscription in scope (one query each, about 20 a minute before Azure throttles, so a
  big scope takes a few minutes the first time; the page fills in as they're read, and **Stop** keeps what's read).
  Group by subscription, resource group or any tag key on your resource groups (a tag's row counts every group with
  that value, e.g. "2 groups, 1 with costs"); with several subscriptions in scope, a **subscription filter** narrows the
  counters and the table to one. Columns this month, all of last month
  with this month's change against the same days of it, the last 30 days, and the last 7 days against the 7 before
  (purchases left out of those two). **Subscription-level charges** is what Azure bills to the subscription rather than
  to a resource group: reservations, savings plans, Defender plans, support, Marketplace; sort by any of them, including the biggest rise against last month; export Markdown or CSV. **Spikes** are
  resource groups whose latest full day of usage cost at least twice their usual day.
- **Billed while idle** lists resources billed while doing nothing (unattached disks and public IPs, empty App
  Service plans, VMs stopped but not deallocated, old snapshots, load balancers with nothing behind them), from one
  Resource Graph query.
- Nothing reads costs until you open one of these: Triage (which refreshes on a timer) has no cost card, so it never
  spends the cost quota a Browse card or the Cost view needs. A resource's Cost tab is the one cost page not kept in
  the store: one query of its own (kept an hour).

### Readiness: before a peak

Answers "if traffic doubles before the peak ends, what stops us?" for the whole scope. Pick the day the peak ends
and **Run**; it runs in the background with progress and **Cancel**. The peak end date is only used for expiry
(certificates, and secrets and keys if ticked): anything expiring before it is red. Every other section is the
headroom as it stands now. The page's *How Readiness works* panel says the same in more detail.

| Section | What it checks |
|---|---|
| Scaling headroom | autoscale settings, AKS node pools and App Service plans against their maximum; user pools with autoscaling off; (deep) Pending pods and pools near their allocatable |
| vCPU quota | free vCPUs per subscription, region and VM family (plus the regional total and Spot) against what the pools and scale sets in scope could add at their maximum |
| Subnet IPs | subnets running low, and whether each AKS subnet can hold its pools at their maximum (Azure CNI: maxPods + 1 IPs per node; overlay, kubenet or a pod subnet: 1) |
| Single points of failure | single-instance App Service plans, pools that can't go above 1 node; (deep) one-replica workloads, HPAs with minReplicas 1, PDBs that block drains |
| HPAs (deep) | every HPA's replicas against its maximum, and HPAs that can't scale |
| Certificates (deep) | red if it expires before the peak ends, yellow soon after; secrets and keys optional |

"Deep" is the Kubernetes and Key Vault part (one request per cluster and vault), on by default. Anything that
couldn't be checked is listed with the reason and the role it needs. **Export Markdown / PDF** writes the report
for the team; the `readiness` [command](#commands) does the same from a terminal.

### Search

The box at the top searches **every** subscription you can read, by name, for resources and resource groups, so it
finds what an incident or a colleague names even outside the scope. Type part of a name, pick a result (arrow keys
and Enter work), and it opens. The exact name comes first, then what's **in the scope in use**, then names starting
with what you typed; each result says *in scope* or *outside scope*. Tick **only in
scope** to search just the scope (remembered in this browser). Still one Resource Graph query.

### Browse

**On the left, one tree** of the resource groups in scope. Click a group to open it in place and to see its summary
on the right; the arrow closes it. Its resources sit under one heading per type with a count, busiest first
("Function App 15", "Key Vault 10"); a heading folds its type, **fold all** and **open all** do every one, and a group
of more than 20 resources opens folded (the type of the resource you're on stays open). Alert rules, action groups,
deployments and similar settings come last, under their own folded heading. One filter box finds groups by name and
resources by name or type across every group in scope: "kubernetes" lists each AKS cluster (Azure's own product names
count too, such as Kubernetes service or Azure Cache for Redis), each group with a match opened to just its matches
(its arrow folds them; **collapse all** and **expand all** above). A dot next to a resource shows its last check; **+ Watch** appears when you point at a
row.

**A resource group's summary** shows what's in it and when it was last checked. Once a group is checked, going back
to it (in the tree or the breadcrumb) shows that check again, kept for the last 10 groups until each is checked anew;
**‹ Group page (cost, access)** beside its results returns to this page, where **Show last check** goes back to it without
checking again (**Check again** reads it anew). **Check this group** reads the
health of every resource in it, as a table, worst first (**Table | Graph** switches view for that check; the next
check opens on Table again, or on Graph when `graph.auto` is on). A group check reads every
resource in the group; `graph.max_nodes` limits only what's added from outside it. The graph draws the group in bands
by role (Ingress, Compute, Hosting, Data & messaging, Identity & secrets, Observability, Network), each box near what
it links to. Dashed amber arrows are role assignments: an identity, or the resource that signs in as it, to a
resource it holds a role on (an AKS kubelet identity's AcrPull on its registry), read in one Resource Graph query;
roles on a whole group or subscription are listed in the box's hover text and the resource's Overview (**Roles it
holds**), not drawn. Plumbing is left out unless it joins two other resources (network
interfaces, private endpoints, Event Grid system topics, API connections, autoscale settings, registry replications;
links to the Network band don't count): a private endpoint shows as 🔒 on what it serves, and what runs inside a
resource as +N on it (an AKS cluster's node pools and load balancer). Anything critical or warning is always drawn, a
line names what was left out, and **Show everything** draws it all; the Table lists every resource either way. The
diagram is drawn at 100% with right-angle connectors that run along the gaps between boxes (none passes behind one),
Azure's icons, a health dot and colour per box, a critical or warning box's worst signal on its third line (the hover
text lists up to three), and names cut in the middle so names that share a prefix stay apart. Hovering a box lights
its connectors and what they reach and fades the rest; a click keeps that and opens its details. An identity only one
resource signs in as is drawn as 🔑 on that resource, its roles starting there ("AcrPull (via …)"); identities nothing
in the group uses sit in a small row at the bottom. A legend under the title explains the marks. Its bar's
**Problems** lights every critical or warning box and what it connects to (press again to clear); its filter box
lights the boxes whose name or type matches every word typed (the same type names as search, so "k8s" finds an AKS
cluster) and scrolls to the first; it also zooms,
fits the width, goes full screen, or **Opens in new tab** (the same picture, view-only, no Azure call). The printable
report draws the same diagram. Above the table,
**Against last week** lists the metrics that doubled or halved against the same window seven days earlier, and a
column shows each resource's biggest moves. It costs no extra calls, and it shows what's *different*, not what's
wrong: more traffic isn't bad.

**A resource** opens in a few seconds. The toolbar above it:
- **Check again** reads it again.
- **Dependencies ▾ → Show dependencies** draws what it uses and what points at it, plus what its identities hold a
  role on, as the same kind of diagram with the resource outlined (**Hops** and **also what points at it** are in the
  same menu). `graph.auto` in Settings draws it on every click.
- **Export ▾:** Markdown, PDF, a printable page, and **Copy link** (a URL that reopens this exact view).
- **Watch** adds it to the Watchlist.

The tree follows whatever is open (from search, the Watchlist or a link), with a breadcrumb
*subscription › resource group › resource*. When it's **outside the scope**, a note says so and offers
**Show its resource group** or **Back to scope**; the scope itself never changes.

**One check at a time.** While a check runs, whatever would start another is dimmed, and **Cancel** stops it,
including its requests to Azure.

### A resource

| Colour | Meaning |
|---|---|
| 🔴 critical | broken or about to be (expired secret, Sev0–1 alert, failed state, at max capacity) |
| 🟡 warning | worth a look (Sev2 alert, near a limit, expiring soon) |
| 🟢 healthy | everything Azure reports is fine |
| ⚪ unknown | not readable with your access, or nothing reported for this type. Never assumed healthy |

Its header has **Refresh** (reads the open tab again from Azure instead of the cache), **Open in portal**, **Check
from here** and the Watchlist star. Elsewhere in the console a row or card that leads to a resource opens it only
from its **Open in Browse** button (Summary's view links say **Show**), so selecting text or a stray click never
jumps away: Triage cards, Secrets, Readiness, Summary, Watchlist and the alert history.

Its tabs, in order (a red count means something is open there):

- **Overview:** **Needs attention** lists only what's critical or warning, each saying what it is: for a fired
  alert, its severity, how often it fired and when, what fired in words ("Average CapacityUnits was 92.3, alert when
  > 80 (over 5 min)"), and links to the alert, its rule and (log alerts) the query results in the portal, plus
  **Is this alert right?** (see [below](#is-this-alert-right)). What ServiceNow or a monitoring tool has open on the
  resource is listed here too, so the page never says "nothing needs attention" while another tool is alerting on it
  (it then says *Azure's own signals: nothing needs attention*). Under it,
  **Recent events** from the last 24 hours with their day and time, leaving out the alerts already listed. Then
  *Your access here*, what's open in ServiceNow and monitoring tools, and every signal. If you can't read it, the
  roles you could activate through PIM are listed with a link.
- **Timeline:** what happened in the last 6 h, 24 h, 3, 7, 14 or 30 days, newest first (changes stop at 14 days,
  which is all Azure keeps, and the page says so): alerts fired and resolved, changes,
  Activity Log operations, Resource Health, plus ServiceNow incidents and changes and the monitoring tool's issues.
  Tick sources on or off. Built to answer "what came first": the deploy, the alert, or the incident. The same
  operation by the same caller with the same outcome 3 or more times is one row with a count (API Management logs
  hundreds of "Get subscription keys" a day), and so is an alert that keeps firing; up to ~2,000 Activity Log events
  are read, and the tab says when the window held more.
- **Metrics:** any metric over 1 h to 90 days (Azure keeps platform metrics 93 days), split by any dimension
  (status code, instance, namespace…).
- **Nodes** (AKS): every node with its pool, VM size and zone, Ready or NotReady (with the reason), memory, disk or
  PID pressure, cordoned and taints, CPU, memory and pods requested and used against what it holds, kubelet version,
  node image and its age, and the pools in total; click a node for its conditions, OS, kernel and runtime. Worst
  first, with a filter and "only nodes with something to look at". Without the Kubernetes API (a private cluster off
  VPN, a missing role), it falls back to Container Insights' node inventory (readiness, pool, versions) and says so.
- **Workloads** (AKS): pods by phase, Container Insights, and checks through the Kubernetes API, with pod logs on
  request. **Happening now** (HPAs at max, replicas not ready, pending pods, OOMKilled, nodes NotReady) and
  **Configuration risks** (single replicas, missing requests or limits, HPA and PDB settings). A part your role
  can't read is named with the role it needs. Each section folds (ones with problems open, your own open/close kept
  in this browser; empty ones are one grey line). A toolbar that stays at the top holds one filter over every
  section, a **namespace** picker (namespaces with problems first, with their counts; kept per cluster), **Go to
  workload** (type part of a name; its details open under the toolbar), **Look back** (1 hour to 30 days, for restarts, OOMKilled and warning events; the workspace keeps 30 days
  by default), *include system namespaces* and **Expand all / Collapse all**, with a *Showing:* line (and Reset) and a
  jump bar to each section with its count. **logs** opens under the row you clicked, one at a time (✕, Esc), JSON
  lines as time · level · message, with a filter, errors-and-warnings only, wrap and copy. **OOMKilled** lists each kill with its pod, node and time, and whether the container hit its own memory
  limit or the node ran out (the kernel's message on the node, else memory just before), with the killed instance's
  logs around the kill. With a monitoring tool picked in the Scope bar (New Relic's Kubernetes integration), **OOM
  kills from** that tool lists the same for clusters without Container Insights, matched by the cluster's name: every
  kill in the look-back is counted (in the section's header and in What's wrong now), the newest 200 are listed and
  the newest 25 get memory readings. **Warning events** (BackOff, FailedMount, a node's OOMKilling…) come from
  Container Insights over the look-back; without it, from the cluster itself (what Kubernetes still keeps, about an
  hour), and from the monitoring tool over the look-back. A read that reaches a tool's own limit says so.
  **Dependencies of a workload** (namespace and Deployment, StatefulSet or DaemonSet, picked from the cluster's own, or for one namespace from Container Insights or the monitoring tool; also Investigate → pod): the
  Azure resources it uses, from the host names in its env and ConfigMaps (scanned on your machine, values never
  shown, Secrets never read) and from what its workload identity holds roles on, Cosmos DB data roles included.
  Without the Kubernetes API it shows the identity part alone and says so. **Path** (same pickers; also Investigate →
  pod and an App Service's App Insights tab): each hop in front of the workload and the workload itself, the last hour
  against the same hour last week, per minute on one 60-minute strip, and one sentence on where it breaks (the hop
  furthest back whose errors rose, or whose traffic fell below half). *App Gateway* routes and *API Management* APIs
  are read from their logs in Log Analytics (5xx, 502/504, the gateway's own errors against the backend's, p95, top
  reasons); *requests* from the monitoring tool picked in the Scope bar (an app's transactions) or an App Insights
  component; *pods* from Container Insights, else the monitoring tool. Nothing ties a gateway route to a workload on
  its own, so **Link front doors** suggests them from data (a backend pool that is the cluster's internal load
  balancer or in its subnets; APIs sharing words with the workload or a path its app serves; its app and component
  by name), each with why, and keeps what you tick for this tenant (`workload_links` in the config file). A hop it
  can't read says why, never zero. **Workload details**: click a workload on any line that names one (HPAs, Happening
  now, Configuration risks, Pods not running; or pick it and press **Details**) and it opens under that line, read
  then: rollout and images; the checks for it; its **HPA** with each metric now against its target and how many pods
  that metric alone asks for (so *held at its maximum by memory* is shown, not guessed), its conditions and behaviour;
  each container's requests and limits against use now, probes, scratch volumes, and its runtime's memory settings
  (`-Xmx`, `MaxRAMPercentage`, `--max-old-space-size`, `GOMEMLIMIT`… picked out of its env and arguments; the rest of a
  value is never kept); its pods with node, zone, restarts and last exit; its events. Below that, its CPU and memory
  over 7 and 30 days (Container Insights, else the monitoring tool) with the pod request each HPA target needs to be
  reachable (memory from its 30-day max, CPU from its 7-day p99), and, when the monitoring tool reports it, the app's
  heap and process memory against the pod's, which shows memory held outside the runtime. **Would a change fit?**
  takes new requests per container (today's, or *what its HPA targets need*) and, on **Check**, reads the nodes and
  every pod on them: the replicas its HPA would then ask for (its own arithmetic with the new request, or a number
  you give), whether they fit node by node on the pools its pods run on, each pool's requests before and after, the
  nodes missing when they don't (and whether the pool's autoscaler can add them), and the room a rolling update has
  next to the old pods. Taints, affinity and topology spread aren't modelled. The ⓘ next to a section shows the kubectl command for the same list, with copy. A Failed pod is a
  warning, dated by when it stopped; one that stopped over a day ago is marked *leftover* (Kubernetes keeps Failed
  pods until someone deletes them).
- **kubectl** (AKS): the commands to run in your own terminal, this cluster's name, group and subscription filled
  in, in ten folding sections (remembered in this browser): connect, what the console shows, find your way, look
  closer at one workload, network, config and storage, jobs, debug inside a container, the cluster in Azure, and
  making changes (restart, roll back, scale, clean up Failed pods, cordon and drain…), under a warning that the next
  deploy undoes a change made by hand. The console itself still only reads.
- **Application Insights** (a component, or a Web or Function App through its component): the last 1 hour to 30
  days, by role, against the same window a week ago for windows up to 7 days; failing operations, exceptions, failing dependencies,
  availability tests, and the queries sent. On an app it says which component it used and how it found it.
- **ServiceNow** and one tab per **monitoring tool:** the records and entities that name this resource.
- **Details:** type-specific facts, e.g. Key Vault expiry (names only), SQL usage and replication, Redis patch
  schedule, Service Bus dead letters, private endpoint state.
- **Scaling:** autoscale and AKS node pools against their maximum.
- **Diagnostics:** Azure's "Diagnose and solve problems" checks over the last 24 hours, with the same details as
  the portal. A detector alone only turns a resource yellow. Azure's read API has detectors for App Service, AKS,
  Redis, Logic Apps and Batch; for other types (API Management among them) the portal runs its checks through
  Microsoft's Help service, which starts a diagnostic run, a write the console doesn't make, so the tab links to the
  portal's page. A type whose detectors API never answers (Event Hubs, Service Bus, Storage and Container Registry
  answer 503 after ~41 s on every API version) counts as having none; only a refusal (401) is named, with Azure's
  reason. Either way the tab then offers
  Microsoft's **troubleshooting guides** for the type: pick a service and a problem type, as in the portal's form, and
  read its self-help guides there; its solutions and checks are listed with the portal link, since opening one is a write.
- **Recommendations:** Azure Advisor and Defender for Cloud, for the resource and the resources inside it (an API
  Management service's APIs, a SQL server's databases): the resource's own first, then one row per finding with how
  many it's on and a few examples.

### Secrets

Every Key Vault secret, key and certificate in scope, plus App Service certificates: **names and dates only;
values are never read.**

- **Counters:** **Needs action** (the default view) · Expired (red) · Critical, within 7 days (orange) ·
  Warning, within 30 days (yellow) · Healthy · No expiry date. Click one to list only those.
- **Table:** subscription, resource group, vault, type, name, expiry and days left, most urgent first. Click a
  header to sort, a row to open the vault. Items expired over 90 days ago are hidden unless you ask.
- **Export** as Markdown, CSV or PDF.
- The scan starts in the background with the page and when the scope changes (about 2 minutes for 150 vaults),
  and is reused for 10 minutes; **Refresh** forces a new one. It runs only when you open Secrets. Above 200 vaults
  (`triage.secret_scan_max_vaults`) it waits for you. Unreadable vaults are listed with the reason.

### Network

Address space and subnet IP usage for every VNet in scope, worst first; a VNet without subnets (info) comes last.

- **The list:** each VNet's address space, how much is allocated, and its three fullest subnets. Yellow at 80%,
  red at 90% (`network.subnet_warning_percent` / `subnet_critical_percent`).
- **Click a VNet** for a usage bar per address space, every subnet (usage, NSG, route table, delegations), the
  free ranges, and which subnet sizes still fit. Export as Markdown, CSV or PDF.
- Azure reserves 5 addresses in every IPv4 subnet; "usable" already excludes them. Delegated subnets don't report
  usage and show grey.

### ServiceNow

Appears once this tenant has a turned-on ServiceNow connection ([setup](#connecting-servicenow-and-monitoring-tools)),
showing the one picked in the Scope bar. Read-only, cached for a minute (**Refresh** reads again).

- **Incidents** (P1 first), **changes** and **Event Management** alerts and events when the instance has them, each
  with **the Azure resources it names**. Changes cover the last 30 days (closed, with their outcome), what's in
  progress or past its window, what's scheduled in the next 14 days, and open ones with no dates (both spans are
  per connection). A change names resources through its own fields, its **affected CIs** and its **change tasks**.
- **Only records naming Azure resources in scope** is on by default (a real instance holds every team's
  incidents); **Show them** lists the rest.
- **Matching needs no CMDB:** *exact* (an Azure resource ID on the configuration item or in the text, or a
  subscription ID), *name* (a distinctive resource, group or subscription name in the fields you choose; a
  hostname counts by its parts), *possible* (a short or plain name, shown dashed).
- **⚠ CMDB** marks a record that names an Azure resource while its configuration item doesn't: useful feedback
  for whoever owns the CMDB.

### New Relic (and other monitoring tools)

Each connected tool gets its own page under **Tools ▾**, showing the connection picked in the Scope bar
(e.g. *NR Prod · US · account 1*). Read-only, cached for a minute. Two sub-tabs:

- **Alerts:** open issues (last 7 days) and alerting entities, each with the Azure resources it matches. Entities
  with the same name in one account (an AKS cluster seen by two integrations) are one row. **Logs** on a row opens
  that entity's logs right under it; **Query** opens the Query sub-tab with its resource picked.
- **Query:** **1 Resource or entity** (search the resources in scope; before you type it suggests the ones the
  tool is alerting on). The same search also lists the tool's own entities with that in their name (*In New Relic, by
  name*), for an app that matches no Azure resource: pick one and the plan, logs and reports read that entity, plus
  APM apps named like it. **2 What New Relic has for it**, before anything is read: the entities that are this resource and
  how each matched (APM apps marked), the name search, an attribute saved for the type, and the log partitions.
  **3 What to read**, then the panel: window, partition, text, and the query it **will send** (Copy, Open New
  Relic) until you run it:
  - *Logs:* lines from the matching entities, plus (ticked by default) lines mentioning the resource's name in any
    attribute, so AKS container logs that carry `cluster_name` but no entity ID show too; untick it to leave out
    lines that may come from other apps. Short or common names aren't searched; the plan says so. Also ticked by
    default: lines whose `resourceId` is the resource's Azure ID in any case, which is how Azure diagnostic logs
    sent by New Relic's Azure log forwarder arrive, with no entity linked. 100 lines a read, newest first;
    **Load older** reads the 100 before them in the same window.
  - *Errors:* `TransactionError` by app, transaction and error class, with the last message.
  - *Slow transactions:* `Transaction` by app and transaction, slowest p95 first. Both need an APM agent: when no
    matched entity is an APM app, the button says so (and still looks for apps named like the resource).
- **Logs** on an Alerts row and the **Logs** section of a resource's New Relic tab use the same panel. A row's
  **Logs** also reads the Azure IDs of the resources the row matched.
- **Cost** follows how much New Relic scans (the window × the log partitions), not how many lines are shown: a
  shorter window or one **Partition** keeps a query cheap.
- **Matching** assumes no tag names: *exact* (a tag holding an Azure resource ID), *name*, *possible* (dashed).

### Watchlist

Pin what matters for an event: **+ Watch** sits next to every resource and resource group (Triage cards, search,
the Browse tree, Secrets, a resource's page). On the **Watchlist** tab, **Check now** gives each item a one-line
status; an item that got worse since the last check turns red and says what it was. **Auto-check** is an
[alert rule](#alerts) that watches the Watchlist (**set up** next to Check now), so it keeps checking with the tab
closed. Each tenant has its own list, saved in `~/.config/azure-health-console/watchlist.yaml`.

### Alerts

Leave the console running and be told when something stops being normal. **Settings → Alerts** holds rules; each
watches a **saved scope** or **the Watchlist** and checks on an interval (2 to 30 minutes; 5 or more for a scope,
since each check queries all of it). The server runs the checks, not the page: a closed tab misses nothing, and
two tabs don't double the calls. A sleeping computer stops them, and the heartbeat says so.

- **Alerts on a change, never on a state.** The first check records a starting point, so what's already red
  doesn't alert. After that a scope rule alerts when a resource's status gets worse, a new red resource appears,
  or a new Azure Service Health event starts (Triage's cards, so about 7 Resource Graph queries per check, whatever
  the scope's size). A Watchlist rule alerts when an item's status gets worse.
- **ServiceNow and monitoring tools** (scope rules): a new incident at or above a priority you pick (P1 and P2 by
  default), or a monitoring tool's alert (a New Relic issue) opening, when it names a resource in scope by its ID or
  a distinctive name; a short or plain name isn't enough. They use the connections the saved scope names (Saved
  scopes → Update after picking them in the Scope bar), and read what their tabs read, sharing the 60-second cache.
  A rule that asks for one its saved scope doesn't have says so. They resolve when the incident or alert closes.
- **Against last week** (Watchlist rules): a metric that doubles or halves against the same window a week earlier,
  from values each check already reads. It doesn't judge direction; it resolves when the metric is back within 2x.
- **An alert resolves itself** when what it was about is back to how it was before (a red resource back to its
  earlier status or gone from the card, a Service Health event over), at the next check that could read it: a check
  that couldn't read never counts as a recovery. It leaves the bar, History marks it **✓ resolved**, and a
  **Recovered** entry says how long it lasted. Red again later is a new alert.
- **History lists alerts, not checks.** An empty History with a recent "last check" means nothing changed. Each
  rule in Settings → Alerts (and the heartbeat's tooltip) says what its last check saw, for example "nothing new ·
  tracking 2 red, 4 yellow · 1 open incident": known problems it's watching, which alert again only if they change.
- **The bar at the very top of the page** shows unresolved alerts on every tab until **Dismiss**, how many recovered, a heartbeat
  ("last check 2 min ago, next in 3 min"), and how many rules can't run, with a link to why. **History** lists the
  last 24 hours.
- **Acknowledge** an alert to take it off the bar while it stays open (it still resolves, and getting worse alerts
  again); **snooze 1 h** takes it off the bar and brings it back if it's still open. **Snooze 1 h** on a rule (Settings
  → Alerts) keeps it checking and recording, but quiet: off the bar and no notifications until then, or **wake**.
- **Export** in History saves the last 24 hours as Markdown, CSV or PDF.
- **Browser notifications** are optional per rule, and so is one more when it's back to normal; the bar always
  shows every alert. If the browser blocks them,
  Settings → Alerts says so.
- **A check that fails** (signed out, a PIM role ended, Azure not answering) raises one alert, and another when
  checks work again, so a watcher that stopped never looks like "all fine". A rule whose saved scope was deleted,
  or a Watchlist that's empty, shows why it can't run instead.
- Alerts stay on your machine: nothing is sent to Teams, email or ServiceNow. History and starting points are in
  memory, so restarting the console clears them and records a new starting point. Rules are saved in
  `config.yaml` under the tenant (`alert_rules`).

### Is this alert right?

An alert says *something crossed a line*; it doesn't say whether that's new, or whether the line is in the right
place. **Is this alert right?**, under a fired Azure metric alert (a resource's Needs attention) and under every open
issue of a monitoring tool (its tab, and a resource's Needs attention), reads the alert's **rule** and the **last
days of what the rule watches**, and answers with numbers. Nothing in it is specific to a resource type or a tool:
every source answers the same two questions (its rule, and the history), and the same arithmetic runs on the answer.

| Source | Rule read from | History read from |
|---|---|---|
| Azure Monitor metric alerts | the alert rule (metric, aggregation, operator, threshold, window, dimensions) | Azure Monitor metrics, at the rule's own window |
| New Relic NRQL conditions | the condition named by the issue, found by name in its account (query, thresholds, window) | the condition's own NRQL with `SINCE n days ago TIMESERIES` |
| Anything else | not yet: the panel says which part the source can't give (Azure log alert rules, New Relic APM or infrastructure conditions, other tools until they implement it) | |

**The numbers** (the latest bucket is left out while it fills in):

| Number | How it's worked out |
|---|---|
| Rule true | buckets where the value crossed the threshold ÷ all buckets in `alert_check.days` |
| Now ÷ last week | the latest value ÷ the value at the same time 7 days earlier |
| Usual | the median of all the buckets |
| Threshold ÷ usual | the threshold ÷ the usual value |

**The verdicts**, each shown with its numbers and the rule (and setting) behind it:

| Verdict | When | Setting |
|---|---|---|
| **Not new** | the rule was true in at least this share of the buckets: either the threshold sits inside the values the resource normally has, or the condition has been there all along and nobody has fixed it. The numbers can't tell those two apart; the chart and the threshold ÷ usual help you decide | `alert_check.always_on_percent` (50) |
| **Unusual** | true now, and in at most this share of the buckets | `alert_check.rare_percent` (5) |
| **Rare, cleared** | true in at most that share, and not now: it fired briefly (the alert can stay open until its source closes it) | `alert_check.rare_percent` |
| **In between** | anything else | both of the above |
| **Higher / lower than last week** | now ÷ last week at least 1 + this % (or at most its inverse); *new since last week* when last week was 0 | `alert_check.change_percent` (50 = 1.5×) |
| **Not enough history** | fewer values than this | `alert_check.min_buckets` (24) |

A rule with no fixed threshold (an Azure dynamic threshold, a New Relic anomaly condition) gets only the comparisons.

**How the history is read**, and said under *How it was read* in every check:

- At the rule's own window over `alert_check.days` (plus one day, so the oldest value has one a week before it),
  when the source's reads allow it. Azure answers 7 days of 1-minute values in one read. New Relic returns at most
  366 buckets a query, so the days are split into up to 8 queries run 4 at a time: a 5-minute condition is read
  in 7. Smaller windows (1 minute) use the smallest bucket 8 queries hold (5 minutes).
- When the bucket is bigger than the rule's window, a **count or sum** is divided down to the window (an hour of a
  per-minute count ÷ 60); a **rate of a level** (NRQL's `rate(latest(…), 1 minute)`, which New Relic divides by the
  bucket's length) is multiplied back up; an **average, maximum or minimum** is compared as it is, and a spike
  shorter than a bucket can be smoothed out.
- Last week's value is the bucket exactly 7 days before now, or the nearest within one bucket when that one is
  empty.
- A rule split by dimensions (Azure) or facets (New Relic) fires when any of them crosses, so each bucket takes the one
  nearest to firing (the highest for a "greater than" rule, the lowest for a "less than" one).
- New Relic: an issue names its condition but not its ID, so the condition is found by name in the issue's account.
  When several conditions share the name, the check says so instead of guessing. The query, account and condition
  are shown so you can open them in New Relic.

**Cost:** only when you click, kept 15 minutes. Azure: one rule read and one metrics read, 1 to 1.5 s measured. New
Relic: one condition search and one NRQL query over the period (data scanned counts against your New Relic usage).

**From a terminal:** `azure-health-console alert-check --azure <alert or rule ID>`, or `--connection <name>
--account <id> --condition "<name>"` for a monitoring tool's condition, prints the same check (`--json` for all of it).

### Access (PIM)

**On a resource, a resource group or a subscription** (click the subscription's name above a group in Browse), a
**Your access here** card answers first what your roles add up to there: *You can read everything · change some
things · can't manage access* (hover a part for the roles giving it; control plane only, from each role's
permissions, so custom roles count too). Then PIM activations with the time left, and **Activate <role> to change**
for an eligible role that would give you more. Your standing roles, where each comes from and whether directly or
through a group, fold away underneath. One read at that scope, so you needn't open this tab to check.


What you can see, and what you could activate. Two tabs: **My access** (yours) and **Check someone's access** (another
user's), so one is never mistaken for the other. The tab's label shows how many PIM roles are active.

- **Active now:** each activated role, the group it came through, and the time left (yellow under an hour).
- **Refused in this session:** what a read refused since the page opened, and the eligible role that would open it.
- **Azure roles you have now:** your standing assignments and the PIM roles you've activated (with the time left),
  given to you directly or through a group, by subscription, with the same filter and scope tick as below.
- **Azure roles you can activate:** across the whole tenant, so no scope is needed (with one applied, ticked
  *only roles that cover the scope in use* narrows them), readers first, with the
  group each comes through and what it covers. One you already hold through a standing role, yours or a group's,
  at that scope or above, reads **already yours** and sorts last, under one heading per subscription (management groups and the tenant
  root last; the first three open, ten headings at a time with **show more**). A filter searches roles, scopes and
  groups and opens every heading.
- **Check someone's access** (its own tab): type a name, sign-in name, mail or object ID (2 characters or more) and pick the user.
  The page is laid out like **My access**: counts first (roles now, roles they can activate, groups), one filter, then
  their roles now and the roles they can activate in the subscriptions in scope, grouped by subscription with the same
  Role, Scope, Type and Via group columns (**Order**: by subscription name, or most roles first; remembered in this
  browser), including roles held at management groups, and their groups, the ones
  that give them a role in scope first. Groups and active roles show first; eligible roles take
  one read per subscription (8 at a time) and follow, and above 100 subscriptions the page asks before reading them.
  It shows only what your own access can read, and says what it couldn't. Roles from a PIM group the user is only
  *eligible* to join aren't shown: Azure lists those only to the user themselves.
- **Groups you're in now,** from Microsoft Graph. PIM groups you've activated appear here (they look the same as
  permanent ones). The PIM groups you *could* activate can't be read with the Azure CLI's sign-in (Microsoft only
  lets its own apps read them), so there's a link to the portal's PIM groups page instead.

The console never activates anything: activate in the portal, then click **Refresh access** (account menu, top right).
**Refresh access** drops cached tokens and results, keeps the scope you're using (Settings → *Open with* only applies
when the page opens) and reloads the page you're on. **What the console needs** (a sub-tab here) lists what each
feature needs and checks it against your roles in the scope: what you have, what a PIM-eligible role would give
(with *activate*), and what neither does; see [Permissions](#permissions). Azure
roles take effect without signing in again; a **PIM group** changes your sign-in, so run `az login` again.

### Settings

A menu on the left: **General** (saved scopes, Open with, Triage), **Thresholds** (health, expiry,
subnets), **Dependency graph**, **AKS**, **Files and Watchlist**, **Integrations**, **Alerts** and **Logs**. Each setting has a plain
name, what it does, its default, and its config key in small print; **reset** puts back a changed one.

A bar at the bottom appears as soon as something changes ("2 unsaved changes: …") with **Discard** and **Save**.
Save validates first (nothing is written if a value is wrong, and the message says which), keeps your comments
and the previous file as `config.yaml.bak`, and applies at once. **Reload from file** picks up hand edits.

### Logs

**Settings → Logs** shows what happened in the console, on this machine. Nothing in it is sent anywhere, and it never
holds tokens, keys or secret values (they're hidden before anything is written). Pick **Today**, **Last 7 days** or
**Last 30 days**; the filter box finds any text, such as an error ID.

- **Errors:** a bug in the console, a check that failed, or an error in the page itself, each with an ID. When the page
  says *error ID 7f3a2c1d*, that's the row here; **Details** has the stack trace and **Copy for a bug report** copies it
  with subscription and object IDs hidden.
- **What was done:** who did what and when: settings saved (which ones), connections and keychain secrets changed,
  Watchlist and alert rules, sign-in and sign-out, every read started, and the sensitive reads (Check someone's access
  names who was looked up). A request without the console's key is listed as *refused*.
- **Requests sent:** first the answer, "✓ Only reads left this machine today" (or what didn't), then every request the
  console sent, one block per service with its address and total, by method and path (names and IDs as `{…}`) and how
  each was answered. Each POST says which read it is: services take a few reads only as POST (Resource Graph, Log
  Analytics and Cost Management queries, the AKS user credential, New Relic's GraphQL, the ServiceNow sign-in token).
  Anything else is stopped before it's sent and counted as *refused*. *No answer* means a timeout or a dropped connection.

The log is kept while the console runs with `serve`: one file per kind and day in `logs.dir`, readable only by you,
deleted after `logs.keep_days` (30).

### Links

Every view has a URL: `http://127.0.0.1:8765/?id=<resource-id>` (add `&depth=2` for its dependencies),
`?rg=<subscription-id>/<resource-group>`, `?view=triage`; `&node=<resource-id>` and `&tab=nodes` open a resource
in a group and one of its tabs. Anyone running the console with access to the resource can open the same link; use
**Export ▾ → Copy link** to share a view (their browser uses its own console's key). The address bar itself keeps only the tenant, so a reload opens Triage;
Back and Forward move between what you opened.

---

## Connecting ServiceNow and monitoring tools

**Settings → Integrations.** Every connection is read-only and off until you turn it on, and each card says
what's still missing (a field, a password or key, or a sign-in).

**They belong to a tenant:** everything on the page (connections and their keys) is saved for the tenant you're
signed in to, under `tenants.<domain or ID>.integrations`. Switching tenant switches the tabs, the Scope bar's
connection pickers and the Triage cards. If two tenants use the same instance, add it in each.

**ServiceNow:** one card per connection (e.g. *SN Prod* and *SN NonProd*), each with:
- the instance URL and its sign-in, one of:
  - **Username and password** — e.g. a service account with read access to the tables below. Simple, but many
    SSO instances block basic auth over the REST API (the browser login works, yet the API returns 401), so use
    OAuth there.
  - **OAuth: sign in with your own ServiceNow login** — you authenticate in the browser (single sign-on and MFA
    included) and the console keeps a refresh token; reads run as you. An admin registers one OAuth app on the
    instance once, then every user self-serves — see [the setup below](#servicenow-oauth-setup).

  The account sees only what ServiceNow's read rules allow it: when it can't read a table's records, the tab says so;
- the tables to read (`incident`, `change_request`, `task_ci` (a change's affected CIs), `change_task`, `em_alert`,
  `em_event`; one the instance doesn't have, or you can't read, is reported and skipped), the **incident filter**
  (default `active=true^state!=6`), the fields where Azure names are looked for, how many **days back and ahead**
  changes are listed (30 and 14), **how many at most** (incidents 2,000, highest priority first; changes 5,000 each for
  open and closed; read a page at a time, and the tab says when a cap is reached or ServiceNow hid some), and a change's extra fields to look in (`justification`, `implementation_plan`,
  `backout_plan`, `test_plan`, `business_service`).

<a id="servicenow-oauth-setup"></a>
**Setting up ServiceNow OAuth (one-time, by an admin).** Use this when basic auth is blocked, which is common on
SSO instances. One app registration covers every user; nobody needs a per-user key.

1. In ServiceNow: **System OAuth → Application Registry → New → Create an OAuth API endpoint for external
   clients**. On newer releases it's listed under **[Deprecated UI]** — that one is correct; the "New Inbound
   Integration Experience" wizard doesn't produce the right record.
2. **Name** it, and set the **Redirect URL** to the exact value the console shows on the ServiceNow card (use its
   Copy button). It includes the console's host and port, so start the console on a fixed port
   (`--port 8765`) and open it the same way each time — a mismatch gives `Invalid redirect_uri` at sign-in.
3. Set **Scope Restriction** to **Broadly scoped**. The default **Securely scoped** issues a token that can't read
   the tables, so every read returns **403 even as admin** — this is the easiest step to miss.
4. Turn on **PKCE** if the option is there (the console uses it).
5. **Submit**, then reopen the app and copy the **Client ID** and **Client Secret**.

Then in the console's ServiceNow card: choose **OAuth: sign in with your own ServiceNow login**, enter the
instance, Client ID and Client Secret, **Save** (stores the secret), then **Sign in with ServiceNow** and approve
in the browser. **Test connection** should now read. What's readable still depends on the signed-in user's roles
(e.g. `itil` for incidents and changes; the Event Management plugin and `evt_mgmt_operator` for `em_alert`/
`em_event`) — a table the account can't read, or the instance doesn't have, is reported and skipped.

**Monitoring tools:** one card per connection (e.g. *NR Prod* and *NR NonProd*). **New Relic** connects today:
region, account IDs and a **User API key** (`NRAK-…`; it sees what its user sees). Datadog and Grafana aren't offered
until they connect; one already in a config file still loads and shows its card.

**Test connection** checks exactly what's typed and saves nothing; it says who you're signed in as and what's
readable. **Save** keeps the fields in `config.yaml` and the secret in your keychain.

**Where secrets go:** passwords, client secrets, tokens and API keys go to your **OS keychain** (macOS Keychain,
Windows Credential Manager, Linux Secret Service), named for what they hold (e.g. *Azure Health Console - NR Prod
(API key)*). They're never written to `config.yaml`, shown again, or logged. Where the keychain is blocked, set the
environment variable each card names (e.g. `AHC_MONITOR_NR_PROD_API_KEY`) before starting the console. A ServiceNow
secret from the environment (`AHC_SERVICENOW_PASSWORD`, `AHC_SERVICENOW_CLIENT_SECRET`) is sent only to the instance
`AHC_SERVICENOW_INSTANCE` names (e.g. `AHC_SERVICENOW_INSTANCE=yourcompany.service-now.com`), so it never reaches
another connection or an instance changed in Settings; the card says when it's held back. The console never connects
ServiceNow to an address on your own machine or its link (`localhost`, `127.x`, `169.254.x`); an internal instance
over the VPN is fine.

**Connection names** are 1 to 40 characters (letters, digits, spaces and `. _ - ( )`) and unique, ignoring case
and punctuation ("NR Prod" and "nr-prod" clash, since they'd share an environment variable).

---

## How far back you can look

Each history read goes as far back as the console offers or Azure keeps, whichever is shorter. Measured on a large
tenant (2026-11-20):

| Source | Azure keeps | The console offers |
|---|---|---|
| Activity Log (Timeline: operations) | 90 days | 30 days |
| Alerts (Timeline, Triage) | 30 days | 30 days |
| Resource changes (Timeline, Investigate, Triage) | 14 days | up to 14 days, said when a longer window is picked |
| Platform metrics (Metrics tab) | 93 days | 90 days |
| Application Insights (Investigate, a resource's Application Insights tab) | 30 days by default, per component (90 or more on some) | 30 days |
| Container Insights (Workloads → Look back) | the Log Analytics workspace's retention, 30 days by default | 30 days |
| Kubernetes events (Workloads, the API) | about 1 hour (Kubernetes' own default) | what the cluster has |
| Monitoring tools' logs (New Relic) | the tool's plan | 7 days |
| OOM kills and warning events from a monitoring tool (Workloads, New Relic) | the tool's plan | 30 days (follows Look back) |
| The console's own alert history (Alerts bar) | in memory, `alerts.history_hours` (1 to 168), at most 500 per sign-in | 24 hours |

Long windows cost more: 30 days of a busy Application Insights component took about 14 s (590,000 requests) and 30
days of Container Insights about 10 s, against under a second for the last hour.

## Performance and scale

Measured on a large tenant (well over a hundred subscriptions, tens of thousands of resources), from an empty cache.
Anything read from Resource Graph costs about the same for 10 subscriptions as for a hundred, so the scope's size rarely
matters; what fans out (one call per subscription or resource) is read only where you open it, never behind Triage or a
timer.

| What | First read | How it scales |
|---|---|---|
| Triage (8 cards) | 3–4 s | one query per card, whatever the scope |
| Security, Network, Search, Billed while idle | 1–7 s | one or a few queries, whatever the scope |
| A resource's health | 4–8 s for most types, then Azure's detectors fill in (up to ~25 s more; 30 s for a slow Logic App detector) | per resource |
| A resource's tabs | 0.1–3 s each; Diagnostics shows each detector as it answers (Function Apps ~50 s for all of them) | per tab |
| A Key Vault's page | grows with the vault: Azure lists 25 secrets a page, so the page reads at most the first 1,000 secrets and 1,000 keys and says when there are more; **Secrets & certificates** reads them all | per vault |
| Activity Log (busy resource) | ~3 s | four time slices read at once |
| Access, first open | ~3 s, then cached | reads you, your groups and roles |
| Cost card (subscription or group) | milliseconds once stored; a subscription not stored yet 4–12 s (two queries side by side) | the cost store |
| Cost view | milliseconds once stored; the first read ~20 subscriptions a minute (Azure's throttle) | the cost store, filled in the background |
| Secrets | one read per vault, when you open it | per vault |
| Is this alert right? | 1–1.5 s for an Azure alert (7 days at the rule's own window in one read); about 2 s for a 5-minute New Relic condition (7 queries, 4 at a time); only when clicked, kept 15 minutes | per alert |

A refused read is answered from memory for 5 minutes (an unreachable private cluster for 2), and a read on screen
waits at most 20 s on Azure's throttling before it says so; background jobs wait it out.

## Permissions

The console only reads, and only what your account can already read. Nothing here is required up front: where
something is missing, the page says what and how to get it. **Access → What the console needs** checks each row below
against your own roles in the scope you picked (✓ what you have, *activate* what a PIM-eligible role would give, ✗ what
neither does), worked out from what your roles allow, so custom roles count.

**Recommended access** for everything: **Reader** on the subscriptions (Cost included), **Azure Kubernetes Service
Cluster User Role** and **Azure Kubernetes Service RBAC Reader** on the clusters, **AcrPull** on the registries.
Or one custom role that holds exactly that: **Access → What the console needs → Download the custom role**
(`azure-health-console-reader.json`: `*/read`, the AKS user credential, Kubernetes objects other than secrets, registry
images). Set its `AssignableScopes`, then `az role definition create --role-definition azure-health-console-reader.json`.
Plus the settings in the table that aren't roles (Defender plans, Container Insights).

| Feature | Needs | Exact actions (for a custom role) | Without it |
|---|---|---|---|
| Triage, Browse, health, metrics, timeline, Readiness, Search, Secrets (names and dates), Network | **Reader** on the subscriptions | `*/read` | Subscriptions you can't read aren't in the scope picker; resources you can't read show as not readable |
| Cost | **Cost Management Reader** or **Reader** on the subscriptions (a resource group's costs: on the group). Some offer types (sponsorships, some credit-based subscriptions) have no Cost Management at all | `Microsoft.CostManagement/*/read`, `Microsoft.Consumption/*/read` | The Cost card names the subscriptions it couldn't read and the role to ask for |
| Access audit, Check someone's access | **Reader** on the subscriptions (role assignments and PIM records) | `Microsoft.Authorization/*/read` | Those subscriptions are read resource group by resource group from Resource Graph; groups still refused can't be told from PIM activations |
| AKS Workloads, Nodes, pod logs, Running image | **AKS Cluster User Role** and a Kubernetes read role (**AKS RBAC Reader**, or a `view` binding), plus a network path to the API server (VPN for private clusters) | `Microsoft.ContainerService/managedClusters/listClusterUserCredential/action`; data `Microsoft.ContainerService/managedClusters/*/read` | Workloads and Nodes name the role, binding or network path that's missing; platform metrics and Container Insights still show |
| Vulnerabilities: find tags (registry images) | **AcrPull** on the registries (or *Container Registry Repository Reader* where it uses repository permissions), and a network its firewall allows (often VPN) | `Microsoft.ContainerRegistry/registries/pull/read`, or data `…/repositories/content/read` | The image's digest still shows; the page says what refused |
| Access audit (group members), Check someone's access | Your account can read groups' members and other users in **Microsoft Graph** (the default for members; some tenants restrict it) | none (directory setting) | The audit names the groups whose members couldn't be listed; the user search shows Graph's refusal |
| Vulnerabilities: findings, Recommendations from Defender | **Defender for Servers** and **Defender for Containers** turned on (paid plans, a setting, not a role); the free tier gives some recommendations | none (setting) | Only the platform checks show (AKS versions and node images, runtimes, TLS); Advisor's recommendations still show |
| AKS dependencies of a workload | **Reader** on the cluster's subscription (managed identities and their federated credentials, role assignments, Cosmos DB role assignments); for its configuration, the AKS roles and network path above | `*/read` | Without the Kubernetes API the list comes from the workload identity's roles alone, and says so |
| AKS workload details | As AKS Workloads (above), plus every namespace's pods for *Would a change fit*; its CPU and memory history from **Container Insights** or a monitoring tool; the app's heap from the monitoring tool (New Relic's APM agents) | `*/read` | Each part says what it couldn't read |
| Workload path | **Reader** on the App Gateways and API Management services (routes, APIs, diagnostic settings) and on the cluster's node resource group and virtual network (its load balancer and subnets, for suggestions); their logs in a workspace (**Reader** on the resource, or **Log Analytics Reader**); a monitoring tool for requests or pods where Azure has none | `*/read` | Each hop says what it couldn't read; suggestions note what wasn't listed |
| AKS logs and pod history (Container Insights) | **Container Insights** turned on for the cluster (a setting), and **Reader** on the cluster or **Log Analytics Reader** on its workspace | `*/read` | Workloads says Container Insights isn't available and shows the other layers |
| Key Vault expiry (Secrets, a vault's page) | **Reader** on the vault: names and dates only, never values. Data roles like *Key Vault Secrets User* aren't enough. A vault firewall that doesn't allow trusted Azure services blocks it | `*/read` | The page says the firewall or role refused it |
| vCPU quota (Readiness) | **Reader** on the subscription | `*/read` | That check is skipped and named |
| Investigate | **Reader** on the Application Insights components; on API Management for its loggers and gateway logs; on the AKS clusters for the pod's Container Insights rows | `*/read` | Each step names what it couldn't read |
| Resource Health | the `Microsoft.ResourceHealth` provider registered in the subscription | none (registration is a write, so the console never does it) | That signal is reported as not registered |
| Diagnostics | a resource type with detectors in Azure's read API: App Service, AKS, Redis, Logic Apps, Batch | `*/read` | The tab says the type has none (or that Azure refused it), links to the portal and shows Microsoft's troubleshooting guides |
| Application Insights tab | an Application Insights resource (for an app, linked to it) | `*/read` | The tab says no component is linked to the app, and how to find one |
| ServiceNow, monitoring tools (tabs, cards, alert signals) | a connection in Settings → Integrations (New Relic); the account rules below | none (their own accounts) | Their tabs and cards don't appear |
| PIM hints (Access tab, "activate" on refusals) | roles assigned through PIM | none | The tab shows your standing access only |
| Saving integration secrets | an OS keychain (macOS Keychain, Windows Credential Manager, or a Secret Service on Linux) | none | Set them as `AHC_…` environment variables instead |
| Browser notifications (Alerts) | notifications allowed for `127.0.0.1` in your browser | none | Alerts still show in the bar on every tab |

**ServiceNow:** the connection's account needs **read access to the records** of each table it reads: `incident`,
`change_request`, and when ticked `task_ci`, `change_task`, `em_alert`, `em_event`. Being allowed to call the API
isn't enough: ServiceNow counts the records a query matches, then removes every row its read rules (ACLs) keep from
that account, so an account without read access gets an empty answer, not an error. The console says so on the
ServiceNow tab, on Triage and in Test connection ("ServiceNow has 6,363 matching change_request records but shows none
to <account>"), and once for the whole connection when no table is readable. Commonly the **itil** role gives that
access, with **snc_read_only** to keep the account read-only; your ServiceNow admins know which role fits. A table
whose records are all hidden is skipped for 15 minutes (Refresh asks again).

**Which AKS credential is used:** first a local kubeconfig context for the cluster (`~/.kube/config`,
`$KUBECONFIG`, AKS desktop's folder, or `aks.kubeconfig_paths`), with the console's own Entra token; its sign-in
command is never run. Only when nothing matches does it fetch the user credential from Azure, kept in memory
(`aks.fetch_credentials: false` turns that off). PIM fixes the Azure roles; a Kubernetes binding or a network
path it doesn't.

---

## Configuration reference

All settings are optional; most are also editable in **Settings**. The annotated example is
[`config.example.yaml`](config.example.yaml). Unknown keys are rejected, so typos fail loudly.

| Setting | Default | Meaning |
|---|---|---|
| `default_tenant` | the `az` default | tenant ID to open with |
| `tenants.<key>.saved_scopes` | none | named Scope bar setups (Saved scopes ▾ writes these): `name`, `subscriptions`, `resource_groups`, `tag_filters`, `connections` |
| `tenants.<key>.open_with` | `none` | what the page opens with: `none`, `all` or a saved scope's name |
| `tenants.<key>.favourite_subscriptions`, `favourite_resource_groups`, `tag_filters` | none | before 1.0.0; read as the *Favourites* saved scope until moved |
| `tenants.<key>.alert_rules` | none | alert rules (Settings → Alerts writes these): `name`, `scope` (a saved scope) or `watchlist: true`, `signals`, `servicenow_priority`, `interval_minutes`, `notify`, `notify_recovered`, `enabled` |
| `tenants.<key>.integrations` | none | ServiceNow and monitoring connections (Settings → Integrations writes these) |
| `triage.scope` | `none` | before 1.0.0; read as Open with until a tenant has its own `open_with` |
| `triage.changes_hours` | `1` | how far back Triage looks for changes on the resources its cards list (shown under each one) |
| `triage.include_system_changes` | `false` | include changes made by Azure itself (noisy) |
| `triage.secret_scan_max_vaults` | `200` | Secrets scans automatically up to this many vaults; Readiness reads at most this many and names the rest |
| `triage.capacity_near_max_percent` | `80` | Triage lists autoscale at this share of its maximum or more (at the maximum is red) |
| `triage.resolved_hours` | `24` | resolved Service Health issues stay listed this long |
| `triage.azure_status_feed` | `true` | the service status card reads the public Azure status feed (`azure.status.microsoft`, no token, nothing about you sent) |
| `updates.check` | `true` | an installed copy (pip, pipx, brew) asks PyPI every 6 hours whether a newer version is out: a dot on the account button, a menu line, and About gives the upgrade command for how it was installed. Never from a checkout; nothing about you sent |
| `graph.auto` | `false` | draw dependencies on every click |
| `graph.depth` / `graph.max_nodes` | `2` / `60` | hops followed; stop drawing at this many resources |
| `graph.inbound` / `inbound_limit` / `inbound_scope` | `true` / `25` / `subscription` | also find what points at a resource, how many, and where to look |
| `health.window_minutes` | `15` | metric window for "now" (and for Against last week) |
| `health.compare_last_week` | `true` | read the same window a week earlier |
| `health.critical_max_severity` / `warning_max_severity` | `1` / `2` | alert severities counted as critical / warning |
| `health.alert_hours` | `24` | stateless alerts older than this are ignored |
| `health.changes_hours` | `24` | changes shown per resource |
| `health.expiry_warning_days` / `expiry_critical_days` | `30` / `7` | expiry turns yellow / red this many days before |
| `health.heuristic_limit` | `8` | metrics picked for types without a profile |
| `health.detectors` | `true` | run Azure's detectors in each check |
| `health.baseline_change_percent` | `200` | a group's metric "moved" against last week at this share or more (200 = doubled), or its inverse |
| `appinsights.failed_min` / `failed_rate_points` | `5` / `1` | failing more: at least this many failed requests, and the rate this many points above the same hour last week (Triage's spike card, Investigate, Path) |
| `appinsights.exceptions_min` / `exceptions_percent` | `10` / `200` | throwing more: at least this many exceptions, at this share of last week or more |
| `appinsights.critical_rate_percent` / `critical_failed_min` | `20` / `20` | failing turns red at this rate with at least this many failed |
| `path.drop_percent` / `drop_min` | `50` / `100` | Path: traffic under this share of last week (when it had this many) is a drop |
| `path.restarts_min` / `restarts_critical` | `3` / `10` | Path: pod restarts rising from (and twice last week), red from |
| `cost.spike_percent` | `200` | a day at this share of the usual day (median of the 7 before) is a spike |
| `security.risky_ports` | `22, 3389, 1433, 3306, 5432, 6379, 27017, 5985, 5986` | ports that are a warning when open to the internet |
| `vulnerabilities.eol_warning_days` | `90` | an App Service runtime turns yellow this many days before its end of support |
| `network.subnet_warning_percent` / `subnet_critical_percent` | `80` / `90` | subnet usage that turns yellow / red |
| `audit.keep_days` | `7` | Access audit: days its last result per scope is kept on this machine; 0 keeps nothing |
| `alerts.history_hours` | `24` | how long the Alerts bar keeps the console's own alerts (1 to 168), in memory: a restart clears them |
| `alert_check.days` | `7` | Is this alert right?: days of history read (1 to 30) |
| `alert_check.always_on_percent` / `rare_percent` | `50` / `5` | true in at least this share: *not new*; true now and in at most this share: *unusual* |
| `alert_check.change_percent` | `50` | now this much above (or below) last week counts as a change (50 = 1.5×) |
| `alert_check.min_buckets` | `24` | the fewest values before any verdict |
| `aks.kubeconfig_paths` | none | extra kubeconfig files or folders |
| `aks.fetch_credentials` | `true` | fetch the AKS user credential when no local context matches |
| `aks.memory_near_limit_percent` / `cpu_near_limit_percent` | `85` / `80` | a container near its memory limit (OOM) / CPU limit (throttled) |
| `aks.cpu_above_request_percent` / `cpu_idle_percent` | `90` / `30` | CPU above / below this share of its request |
| `aks.hpa_target_high_percent` / `hpa_near_max_percent` / `hpa_overshoot_percent` | `85` / `80` / `150` | an HPA target that's high; an HPA near its max; a metric this far past its target |
| `aks.pool_requests_percent` | `80` | a node pool this full is a finding (and amber) when it can't grow |
| `aks.restarts_warning` | `5` | restarts from this many on are a warning |
| `aks.leftover_hours` / `rollout_hours` | `24` / `24` | a Failed pod older than this is left over; a rollout this recent shows |
| `aks.node_image_warning_days` / `node_image_critical_days` | `60` / `180` | node image age that turns yellow / red (Nodes and Security) |
| `aks.restarts_look_back_hours` / `oom_look_back_hours` / `events_look_back_hours` | `1` / `24` / `1` | Workloads' Look back on *default* |
| `aks.oom_near_limit_percent` / `oom_node_full_percent` | `90` / `95` | an OOM kill's probable cause without a kernel message: its own limit / the node |
| `watchlist_file` | `~/.config/azure-health-console/watchlist.yaml` | where the Watchlist is saved |
| `logs.dir` | `~/.local/state/azure-health-console/logs` | where this machine's log is kept (Settings → Logs) |
| `logs.keep_days` | `30` | days the log keeps, 1 to 365 |
| `profiles_dir` | `~/.config/azure-health-console/profiles` | your own health profiles |
| `exports_dir` | `exports` | where the commands write files |
| `cache_dir` | `~/.cache/azure-health-console` | where the cost store is kept (`costs-<tenant>-<account>.json.gz`, mode 600) |
| `pdf_font` | built-in | a `.ttf` for PDFs with non-Latin names |

---

## How health is decided

Nothing is hard-coded per resource type. Each resource gets:

1. **Generic signals:** provisioning state, Resource Health, fired alerts, recent changes, Service Health for its
   region, and scaling headroom.
2. **Your metric alert rules:** the metric, threshold and severity your team already set, checked against now.
3. **Profiles** (`healthconsole/profiles/*.yaml`): curated metrics and thresholds for common types. Your team's own
   rule on the same metric wins.
4. **Fallback:** for other types, metrics whose names look like problems, compared with last week.
5. **Type extras** where Azure offers them: AKS pods and detectors, Key Vault expiry, App Service and Redis
   detectors.

A resource's colour is its worst signal; a group's or graph's is its worst resource.

**Your own profiles:** put a file in `~/.config/azure-health-console/profiles/`; it replaces the built-in profile
for the same `type`. Start from `azure-health-console profile-draft <resource-id>`:

```yaml
type: microsoft.cache/redis
metrics:
  - {name: serverLoad, aggregation: Maximum, operator: GreaterThan, warn: 80, crit: 90}
  - {name: cacheLatency, aggregation: Average}   # no thresholds: shown, not judged
```

---

## Commands

`azure-health-console <command>` (from the source: `python -m healthconsole <command>`). Options that apply to
every command go **before** it.

| Command | What it does |
|---|---|
| `serve [--port 8765] [--open]` | the web page, on 127.0.0.1 only. No command at all does `serve --open` |
| `config` | what your config resolves to: file, tenant entry, saved scopes, Open with |
| `export <resource-id> [--format md\|pdf\|both] [--depth N]` | a resource's snapshot to `exports/` |
| `readiness --peak-end YYYY-MM-DD [--subscription <name or pattern>]… [--scope <saved scope>] [--no-deep] [--include-secrets] [--problems-only] [--format md\|pdf\|both]` | the pre-peak readiness report (default: what you open with) |
| `bench [--resource <id>] [--only a,b] [--secrets]` | times what the page does, read-only, and writes `exports/bench-*.md` |
| `profile-draft <resource-id>` | a starting health profile for that resource's type |
| `alert-check --azure <id>` or `--connection <name> --account <id> --condition "<name>" [--priority critical] [--json]` | [Is this alert right?](#is-this-alert-right) for one alert, as the page's button |
| `whoami` | the identity and tenant in use |

| Option | Effect |
|---|---|
| `--tenant <id>` | another tenant you're signed in to |
| `--config <path>` | another config file |
| `--log-calls` | print every request as it's sent (method and path, never tokens) |
| `-v`, `--verbose` | more detail in the terminal, including a line for every request the page makes; without it only the start-up lines, warnings and errors show |

**`bench`** runs Triage, search, a resource, its dependencies, its group, the AKS fleet and Network one after
another, each with an empty cache, and reports the wall time, the number of Azure calls, where the time goes by
kind of call, and the 15 slowest. `--secrets` adds the vault scan (opt-in: it lists secret names). Run it before
and after a change, on the same scope, to compare.

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Something went wrong inside the console … Error ID …" | A bug in the console, not Azure. **Settings → Logs → Errors**: type the ID in the filter, open **Details**, and **Copy for a bug report** |
| "Sign in to Azure to continue" | Run the command it shows (`az login`, or `az login --tenant <id>`); the page carries on by itself. If the Azure CLI isn't installed, it links to the installer |
| "az login finished, but no account was saved: … needs a sign-in of its own" | `az login` without a tenant skipped that tenant (usually for multi-factor sign-in) and saved nothing. Press **Sign in to …** next to it, or run `az login --tenant <id>` |
| Triage is empty | No scope is applied yet: pick subscriptions in the Scope bar and press **Apply** |
| A saved scope picks the wrong subscriptions | `azure-health-console config` shows how many each scope's patterns match |
| A resource is grey with "No access to …" | The message names the role, and lists the PIM roles you could activate. Activate one, then **Refresh access**. A refused read is remembered for 5 minutes (an unreachable private cluster for 2), so reopening it answers at once; **Refresh access** or the view's **Refresh** asks Azure again |
| "This resource can't be read: it may no longer exist" | Resource Graph can list a deleted resource for a while (minutes to hours) after it's gone, so search and Browse can still show it. If **Your access here** says Azure answered 404, it was deleted; otherwise it's your access (see the row above) |
| A resource's **Cost** tab says Azure is throttling | The cost store is still reading other subscriptions in the background and uses the same allowance; try again in the time it gives, or once the first read is done |
| "Azure couldn't be reached … (the connection failed 3 times)" | The console already retried; your network or VPN dropped. Try again |
| "Azure didn't answer within …s" | One slow Azure API; only that check is affected. Try again later |
| AKS Workloads: API server not reachable | Private cluster: connect to VPN. The rest of the cluster's health still works |
| `ModuleNotFoundError: healthconsole` | From the source, use `python -m healthconsole` from the repo folder. On macOS with Python 3.14, an install under `~/Documents` can be flagged hidden and skipped |
| PDF shows `?` in names | Set `pdf_font` to a `.ttf` that covers the script |

---

## For maintainers

### Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest -q                                  # every test uses fakes: no Azure, keychain or ServiceNow
.venv/bin/python -m flake8 . --select=E              # the pipeline's lint (errors only), with the repo's .flake8
.venv/bin/python -m healthconsole --log-calls serve  # see every request as it goes out
```

```
healthconsole/
  core/            framework-free: the read-only Azure client (arm.py), discovery, health, triage, search,
                   readiness, baseline, PIM, the Graph groups read, checks, watchlist, alert rules, reports,
                   vulnerabilities; and what they share: guarded.py (every outbound client extends
                   GuardedClient), cache.py (TtlCache), cards.py (cards and items), scopes.py (naming scopes)
  core/explore/    a resource's tabs: metrics, details, scaling, diagnostics, timeline, App Insights,
                   AKS workloads (read-only Kubernetes client), Key Vault expiry
  core/integrations/  ServiceNow and monitoring tools, behind one provider interface, each with its own guard
  web/             Flask blueprint (mountable: `authorize=` is your sign-in check) and the local app with its key; static/js/ is the page
  profiles/        built-in per-type health profiles
  cli.py           the commands
packaging/         the Homebrew formula template
```

Rules worth knowing before changing anything:
- **Read-only by construction:** every Azure, Kubernetes, ServiceNow, New Relic and Graph request passes a guard
  that refuses anything but a read before it's sent. A new request is added to [`AZURE-CALLS.md`](AZURE-CALLS.md).
- **Generic:** no tenant, subscription, tag or instance names in code, defaults or tests.
- **Nothing silently missing:** a setting that isn't configured, or something that couldn't be read, is named on
  the page with its fix.

### Built with

| Layer | What it uses |
|---|---|
| Backend | Python 3.11+ and [Flask](https://flask.palletsprojects.com/) (the page template is Flask's Jinja). [requests](https://requests.readthedocs.io/) for every outbound call, behind the read-only guards; [azure-identity](https://pypi.org/project/azure-identity/) to use your `az login`; [ruamel.yaml](https://pypi.org/project/ruamel.yaml/) for the config file; [fpdf2](https://pypi.org/project/fpdf2/) for PDF exports; [keyring](https://pypi.org/project/keyring/) for secrets in the OS keychain; [truststore](https://pypi.org/project/truststore/) for the OS's certificate store (corporate proxies). |
| Frontend | Plain JavaScript, no framework and no build step: `web/static/js/` (the page: one file per area, loaded in order by `SCRIPTS` in `web/__init__.py`) and `diagram.js` (the dependency diagrams). [Chart.js](https://www.chartjs.org/) 4.4.4, bundled under `web/static/vendor/`, draws the charts. Diagrams are SVG drawn on the server (`core/diagram.py`). |
| Style | Hand-written CSS (`web/static/style.css`), no CSS framework; light and dark themes from CSS variables. |
| Tests and lint | [pytest](https://pytest.org/), with fakes for Azure, Kubernetes, ServiceNow and the keychain; [flake8](https://flake8.pycqa.org/) for errors. |
| Packaging and release | PyPI, pipx and Homebrew (the formula template is in `packaging/`), released by an Azure DevOps pipeline (see [Releasing](#releasing)). |

The versions each needs are in [`pyproject.toml`](pyproject.toml).

### Releasing

Releases come from a private pipeline built on a shared release template:

1. Bump [`VERSION`](VERSION) (semver) and date the CHANGELOG's section for it.
2. The code is exported without private paths (`export-ignore` in `.gitattributes`) and checked: a list of what must
   never be published stops the release on any match.
3. The sdist and the wheel are built from that export.
4. After an approval they go to an Azure Artifacts feed and **PyPI**; this repository's `main` becomes the export as
   one commit, tagged `v<version>`, with a GitHub release holding the files and `SHA256SUMS.txt`; and the
   [Homebrew tap](https://github.com/RafaelSonego/homebrew-tools)'s formula is updated from `packaging/homebrew/`.
   The private commit is tagged last.
