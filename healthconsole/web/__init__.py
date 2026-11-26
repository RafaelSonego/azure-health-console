"""The Flask front end. `create_blueprint` is what a hosting app mounts;
`create_local_app` wraps it with the `az login` credential and a loopback guard."""
import hmac
import io
import json
import logging
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Flask, abort, g, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException

from ..core import (acr, alerts, cache, checks, config, cost, graph, personas, inventory, jobs, network, pim, profiles, readiness, report,
                    search, security, settings_file, snapshot, tables, triage, user_access, vulnerabilities)
from ..core import (access_audit, audit_store, requirements, alert_check, azure_alerts, cost_store, diagram, integrations, investigate,
                    pypi_stats, summary_history)
from ..core import eventlog
from ..core.guarded import ServiceError
from ..core.integrations.provider import ALERT_HISTORY, OOM_KILLS, REQUESTS, WARNING_EVENTS, WORKLOADS
from ..core.investigate import gateway as inv_gateway, pod as inv_pod
from ..core.integrations import secrets as integration_secrets
from ..core.integrations import servicenow as sn
from ..core.integrations import matching, monitors, sn_data
from ..core import watchlist
from ..core.watchlist import Watchlist
from ..core import access as access_roles
from ..core.cache import TtlCache
from ..core.arm import AccessDenied, ArmClient, ArmError, Throttled, WriteBlocked, rg_clause
from ..core.credentials import AzureCliTokenProvider, CredentialError
from ..core.discovery import DiscoveryOptions
from ..core.explore import (activity, advice, aks, appinsights, dependencies, details, detectors, help_guides, kube, metrics,
                            kube_yaml, pod_detail, scaling, workload_detail, workload_fit, workload_path, workload_problems)
from ..core.health import HealthOptions
from ..core.localfiles import private_dir, read_private_text, write_private_text
from ..signin import AzLogin, SignInError, accounts as signin_accounts_read, logout as signin_logout
from .. import __version__

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
# The page's script, in load order: one global scope, so a file may only use at load what an earlier one declares.
SCRIPTS = [f"js/{name}.js" for name in (
    "core", "signin", "scope", "browse", "explore", "lists", "resource-tabs", "access", "timeline-appinsights",
    "investigate", "metrics", "aks-workloads", "aks-nodes", "sections", "wiring", "views", "triage", "vulnerabilities",
    "secrets", "readiness", "summary", "cost", "audit", "watchlist", "settings", "logs", "servicenow", "monitoring", "scopes",
    "integrations", "nav", "user-access", "alerts", "boot")]
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CROSS_SITE = {"cross-site", "same-site"}  # same-site includes another port on 127.0.0.1
LOCAL_OPEN_PATHS = ("/static/", "/integrations/servicenow/callback")
MAX_DEPTH = 4
SECRET_CACHE_SECONDS = 600
ALERT_CHECK_SECONDS = 900  # Is this alert right?: one check per alert, kept 15 minutes
LONG_EXPIRED_DAYS = 90
# The audit's kind for each action: change (settings, connections, keychain, lists), sensitive (a read about people or
# secrets); every other POST starts a read. Client errors are logged as errors, not as actions.
AUDIT_CHANGES = {"settings_save", "settings_reload", "integrations_servicenow", "integrations_monitors",
                 "integrations_move_legacy", "integrations_secret", "servicenow_sign_in", "watchlist_add",
                 "watchlist_remove", "alerts_save", "alerts_acknowledge", "alerts_snooze", "alerts_dismiss",
                 "saved_scopes_save", "open_with_save", "saved_scopes_move_legacy", "cache_clear", "path_link_save",
                 "signin_start", "signin_cancel", "signout"}
AUDIT_SENSITIVE = {"access_user", "secrets_scan", "access_audit_start"}
AUDIT_SKIP = {"client_errors"}
CLIENT_ERRORS_PER_MINUTE = 30  # same fold as the Secrets tab: long past expiry is almost always unused


def refuse_other_sites(api_prefix):
    """Only the page itself calls the API: another site's request (a form, a link, a page on another port) is refused,
    whatever cookie the browser would send with it; actions take JSON only, which a plain form can't send."""
    if request.path.startswith(api_prefix) and request.headers.get("Sec-Fetch-Site") in CROSS_SITE:
        abort(403, "The console's API answers only its own page.")
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            abort(403, "The console's API answers only its own page.")
        if not request.is_json:
            abort(415, "Send JSON.")


def create_blueprint(client_for, settings, url_prefix="", config_file=None, start_alerts=False, alert_session_ttl=None, *,
                     authorize):
    """`authorize()` is the host's own check of the caller, run before every request: None lets it through, a
    response refuses it (required: the blueprint has no sign-in of its own). `client_for(tenant_id)` returns an
    ArmClient for the current viewer and tenant. `settings` is
    updated in place when the Settings tab saves, so every request sees the current values. `start_alerts` runs
    alert rules in the background from now on (local: the default sign-in's too); `alert_session_ttl` (seconds,
    hosted) stops a viewer's rules once their page stops asking, since their delegated token is gone with it."""
    if not callable(authorize):
        raise TypeError("create_blueprint needs authorize=: the host's check of the caller (None lets a request through).")
    blueprint = Blueprint("healthconsole", __name__, url_prefix=url_prefix,
                          template_folder=str(HERE / "templates"),
                          static_folder=str(HERE / "static"))

    @blueprint.before_request
    def caller_checked():
        refuse_other_sites(f"{url_prefix}/api/")
        return authorize()
    runner = jobs.JobRunner()
    config_file = Path(config_file).expanduser() if config_file else config.path()
    live = {}

    def reload_live():
        live["profiles"] = profiles.load(settings.get("profiles_dir"))
        live["watchlist"] = Watchlist(settings["watchlist_file"])

    reload_live()
    alert_runner = alerts.AlertRunner(session_ttl=alert_session_ttl,
                                      history_hours=lambda: settings["alerts"]["history_hours"])
    blueprint.alert_runner = alert_runner

    def client():
        """The viewer's client; with `fresh=1` (a Refresh button) one that skips the request cache."""
        arm_client = client_for(request.args.get("tenant") or (request.get_json(silent=True) or {}).get("tenant"))
        return arm_client.fresh() if request.args.get("fresh") == "1" and hasattr(arm_client, "fresh") else arm_client

    def owner(arm_client):
        identity = arm_client.identity
        return f"{identity.tenant_id}/{identity.object_id}"

    def start_job(arm_client, work, kind=None, replace=False):
        """`work(client, progress, publish)` gets a client bound to the job, so cancelling the job
        stops its Azure calls; `replace` cancels the owner's running job of the same kind."""
        cancel = threading.Event()
        bound = arm_client.bound(cancel) if hasattr(arm_client, "bound") else arm_client
        return runner.start(owner(arm_client), lambda progress, publish: work(bound, progress, publish),
                            kind=kind, replace=replace, cancel=cancel)

    @blueprint.errorhandler(CredentialError)
    def credential_error(error):
        return jsonify(error=str(error), kind="credential", reason=getattr(error, "reason", "signed_out"),
                       command=getattr(error, "command", "az login")), 401

    @blueprint.errorhandler(AccessDenied)
    def access_denied(error):
        return jsonify(error=str(error), kind="access", **error.to_dict()), 403

    @blueprint.errorhandler(Throttled)
    def throttled(error):
        return jsonify(error=str(error), kind="throttled", retry_after=error.retry_after), 429

    @blueprint.errorhandler(jobs.TooManyJobs)
    def too_many_jobs(error):
        return jsonify(error=str(error), kind="busy"), 429

    @blueprint.errorhandler(WriteBlocked)
    def write_blocked(error):
        return jsonify(error=str(error), kind="blocked"), 500

    @blueprint.errorhandler(ArmError)
    def arm_error(error):
        return jsonify(error=str(error), kind="azure"), 502

    @blueprint.errorhandler(HTTPException)
    def request_error(error):
        """`abort(400, "why")` reaches the page as that reason, not Flask's HTML page."""
        return jsonify(error=error.description, kind="request"), error.code

    def who():
        """(name, id) of the person behind this request, for the log; ("", "") when signed out."""
        try:
            arm_client = client()
            return arm_client.identity.name, owner(arm_client)
        except Exception:  # signed out, or no tenant yet: the event is still written
            return "", ""

    @blueprint.errorhandler(Exception)
    def unexpected(error):
        """A bug in the console, not an answer from Azure: logged with an ID the page shows."""
        name, user_id = who()
        error_id = eventlog.record_error(str(error), source="server", error=error, user=name, user_id=user_id,
                                         context={"method": request.method, "path": request.path})
        logger.error("unexpected error on %s %s", request.method, request.path, exc_info=error, extra={"recorded": True})
        where = f" Error ID {error_id}: Settings → Logs has the details." if error_id else ""
        return jsonify(error=f"Something went wrong inside the console (not in Azure).{where}", kind="internal",
                       error_id=error_id), 500

    @blueprint.after_request
    def audit_action(response):
        action = (request.endpoint or "").rsplit(".", 1)[-1]
        if request.method not in SAFE_METHODS and request.path.startswith(f"{url_prefix}/api/") and action not in AUDIT_SKIP:
            name, user_id = who()
            eventlog.audit(action, kind="change" if action in AUDIT_CHANGES else "sensitive" if action in AUDIT_SENSITIVE
                           else "read", outcome=response.status_code, user=name, user_id=user_id,
                           detail=g.get("audit_detail"))
        return response

    client_error_times, client_error_lock = [], threading.Lock()

    @blueprint.post("/api/client-errors")
    def client_errors():
        """An error in the page itself (its JavaScript), which only the browser sees; a few a minute at most."""
        now = time.monotonic()
        with client_error_lock:
            client_error_times[:] = [t for t in client_error_times if now - t < 60]
            if len(client_error_times) >= CLIENT_ERRORS_PER_MINUTE:
                return jsonify(error_id=None, dropped=True), 202
            client_error_times.append(now)
        body = request.get_json(silent=True) or {}
        name, user_id = who()
        error_id = eventlog.record_error(str(body.get("message") or "")[:2000], source="page", user=name, user_id=user_id,
                                         context={k: str(body.get(k) or "")[:500] for k in ("where", "view", "line")},
                                         stack=str(body.get("stack") or "")[:6000])
        return jsonify(error_id=error_id), 202

    @blueprint.get("/api/logs")
    def logs_view():
        """This machine's log: errors, actions, or requests sent grouped. Locally one person sees it all."""
        kind = request.args.get("kind", "errors")
        if kind not in eventlog.KINDS:
            abort(400, f"kind must be one of {list(eventlog.KINDS)}")
        days = min(max(int(request.args.get("days") or 1), 1), settings["logs"]["keep_days"])
        return jsonify(enabled=eventlog.enabled(), folder=str(eventlog.folder() or settings["logs"]["dir"]),
                       keep_days=settings["logs"]["keep_days"], kind=kind, days=days,
                       rows=eventlog.read(kind, days=days))

    def asset(name):
        # The file's change time in its URL, so a browser never keeps an old copy after an update.
        return url_for("healthconsole.static", filename=name, v=int((HERE / "static" / name).stat().st_mtime))

    @blueprint.get("/")
    def index():
        return render_template("index.html", asset=asset, scripts=SCRIPTS)

    def tenant_of(arm_client):
        tenant_id = arm_client.identity.tenant_id
        tenants = inventory.list_tenants(arm_client)
        return next((t for t in tenants if t["id"] == tenant_id), {"id": tenant_id}), tenants

    @blueprint.get("/api/session")
    def session():
        """Who the Azure CLI is signed in as right now (its default tenant), read fresh from the CLI each time, so
        the page notices `az logout` / `az login` made in another terminal."""
        default = client_for(None)
        refresh_tokens = getattr(getattr(default, "token_provider", None), "refresh", None)
        if refresh_tokens:
            refresh_tokens()
        identity = default.identity
        tenant, _ = tenant_of(default)
        return jsonify(tenant_id=identity.tenant_id, tenant_name=tenant.get("name") or identity.tenant_id,
                       object_id=identity.object_id, name=identity.name)

    @blueprint.get("/api/context")
    def context():
        arm_client = client()
        if request.args.get("verify") == "1":
            # The page just loaded: ask the CLI again, so `az logout` or a new `az login` takes effect now.
            refresh_tokens = getattr(getattr(arm_client, "token_provider", None), "refresh", None)
            if refresh_tokens:
                refresh_tokens()
        identity = arm_client.identity
        tenant, tenants = tenant_of(arm_client)
        return jsonify(
            identity={"name": identity.name, "tenant_id": identity.tenant_id},
            tenants=tenants,
            **scope_payload(tenant),
            graph=settings["graph"],
            triage_refresh_minutes=settings["triage"]["auto_refresh_minutes"],
            summary=settings["summary"],
            config_path=str(config_file),
            watchlist_path=str(live["watchlist"].path),
            version=__version__,
        )

    @blueprint.get("/api/about/pypi")
    def about_pypi():  # public download counts, read when About opens; nothing about the viewer is sent
        return jsonify(pypi_stats.read(refresh=request.args.get("refresh") == "1"))

    @blueprint.get("/api/update")
    def update_check():  # a newer release on PyPI? Nothing about the viewer is sent
        return jsonify(pypi_stats.update(__version__, settings["updates"]["check"], refresh=request.args.get("refresh") == "1"))

    def scope_args(source):
        """Subscriptions and optional tag-filtered resource groups ("sub/rg") from a request."""
        subscriptions = [s.lower() for s in source.getlist("subscription")] if hasattr(source, "getlist") \
            else [s.lower() for s in source.get("subscriptions") or []]
        groups = source.getlist("group") if hasattr(source, "getlist") else source.get("groups")
        filtered = (source.get("filtered") in ("1", True)) if hasattr(source, "get") else False
        return subscriptions, (groups if filtered else None)

    def scope_payload(tenant):
        """Saved scopes (old favourites last, as `legacy` ones until moved), what the page opens with, and what's left
        of the old settings for the Move notice."""
        legacy = config.legacy_scope(settings, tenant)
        return {"saved_scopes": config.saved_scopes(settings, tenant), "open_with": config.open_with(settings, tenant),
                "reserved_names": list(config.OPEN_WITH),
                "legacy_scope": {"keys": legacy["keys"], "open_with": legacy["open_with"],
                                 "scopes": [s["name"] for s in legacy["scopes"]]} if legacy else None}

    @blueprint.get("/api/search")
    def find():
        arm_client = client()
        subscriptions, groups = scope_args(request.args)
        return jsonify(search.search(arm_client, request.args.get("q", ""), subscriptions=subscriptions or None,
                                     groups=groups, only_scope=request.args.get("only_scope") == "1"))

    @blueprint.get("/api/triage")
    def triage_view():
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        options = {**settings["health"], **settings["triage"], **settings["network"], **settings["security"]}
        return jsonify(triage.triage(client(), subscriptions, options, groups))

    # A running or finished job per viewer and scope, reused for a while instead of starting the same work again.
    reused_jobs = TtlCache("reused jobs", SECRET_CACHE_SECONDS)
    saved_jobs = TtlCache("saved audit jobs", SECRET_CACHE_SECONDS)  # job ID -> when its saved result was read

    def audit_path(arm_client):
        identity = arm_client.identity
        return audit_store.path_for(settings["cache_dir"], identity.tenant_id, identity.object_id)

    def reusable_job(key, arm_client):
        job_id = reused_jobs.get(key)
        job = job_id and runner.get(job_id, owner(arm_client))
        return job_id if job and job["status"] in (jobs.RUNNING, jobs.DONE) else None

    @blueprint.post("/api/secrets")
    def secrets_scan():
        """The Secrets tab: one vault scan per identity and scope, reused for 10 minutes."""
        body = request.get_json(silent=True) or {}
        subscriptions, groups = scope_args(body)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        arm_client = client()
        key = (owner(arm_client), tuple(sorted(subscriptions)), tuple(sorted(groups)) if groups is not None else None)
        reused = None if body.get("rescan") else reusable_job(("secrets",) + key, arm_client)
        if reused:
            return jsonify(job_id=reused, cached=True)
        vaults = checks.count_vaults(arm_client, subscriptions, groups)
        limit = settings["triage"]["secret_scan_max_vaults"]
        if vaults > limit and not body.get("force"):
            return jsonify(too_many=vaults, limit=limit)
        health = settings["health"]

        def work(bound, progress, publish):
            result = checks.keyvault_scan(bound, subscriptions, health["expiry_warning_days"], groups, progress,
                                          health["expiry_critical_days"])
            result["errors"] = checks.summarise_errors(result["errors"])
            return result

        job_id = start_job(arm_client, work, kind="secrets", replace=True)  # a new scope replaces the old scan
        reused_jobs.put(("secrets",) + key, job_id)
        return jsonify(job_id=job_id, vaults=vaults)

    def sub_names(arm_client):
        return {sub["id"]: sub["name"] for sub in inventory.list_subscriptions(arm_client)}

    @blueprint.get("/api/secrets/<job_id>/export")
    def secrets_export(job_id):
        job = _job(job_id)
        if job["status"] != jobs.DONE:
            abort(409, "The scan isn't finished yet.")
        result, names = job["result"], sub_names(client())
        wanted = set(request.args.getlist("status")) or {"critical", "warning"}
        include_old = request.args.get("include_old") == "1"
        rows = [r for r in result["rows"] if r["status"] in wanted
                and (include_old or r["days_left"] is None or r["days_left"] >= -LONG_EXPIRED_DAYS)]
        rows.sort(key=checks.urgency)
        sections = [{
            "heading": f"{len(rows)} item(s): {', '.join(sorted(wanted))}",
            "columns": ["Status", "Subscription", "Resource group", "Vault", "Type", "Name", "Expires",
                        "Days left", "Note"],
            "widths": [7, 14, 14, 13, 8, 16, 8, 6, 14], "status_column": 0,
            # Same words and colours as the tab: expired red, critical (not yet expired) orange.
            "tones": {"expired": "critical", "critical": "soon"},
            "icons": {"expired": "🔴", "critical": "🟠", "warning": "🟡", "no expiry": "⚪"},
            "rows": [[secret_label(r), names.get(r["subscription_id"], r["subscription_id"]), r["resource_group"],
                      r["vault"], r["kind"], r["name"], r["expires"], r["days_left"], r["note"]] for r in rows]}]
        if result["errors"]:
            sections.append({"heading": "Not readable", "columns": ["Reason"], "rows": [[e] for e in result["errors"]]})
        return export_table({
            "title": "Secrets and certificates expiry",
            "subtitle": f"{result['vaults']} Key Vault(s) and App Service certificates in scope. Red: expired; orange: "
                        f"within {result['critical_days']} days; yellow: within {result['warn_days']} days. "
                        "Names and dates only; values are never read.",
            "sections": sections,
        })

    def secret_label(row):
        if row["status"] == "critical":
            return "expired" if row["days_left"] is not None and row["days_left"] < 0 else "critical"
        return "no expiry" if row["status"] == "info" else row["status"]

    def export_table(doc):
        fmt = request.args.get("format", "md")
        if fmt not in tables.FORMATS:
            abort(400, f"format must be one of {tables.FORMATS}")
        mimetypes = {"md": "text/markdown", "csv": "text/csv", "pdf": "application/pdf"}
        return send_file(io.BytesIO(tables.render(doc, fmt, settings.get("pdf_font"))), as_attachment=True,
                         download_name=tables.filename(doc, fmt), mimetype=mimetypes[fmt])

    @blueprint.get("/api/network")
    def network_overview():
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        options = settings["network"]
        return jsonify(network.overview(client(), subscriptions, groups, options["subnet_warning_percent"],
                                        options["subnet_critical_percent"]))

    def vnet_detail():
        resource_id = _resource_id()
        if "/microsoft.network/virtualnetworks/" not in resource_id.lower():
            abort(400, "id must be a virtual network")
        options = settings["network"]
        return network.vnet_detail(client(), resource_id, options["subnet_warning_percent"],
                                   options["subnet_critical_percent"])

    @blueprint.get("/api/security")
    def security_posture():
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        return jsonify(security.posture(client(), subscriptions, rg_clause(groups), risky_ports=settings["security"]["risky_ports"]))

    @blueprint.get("/api/personas")
    def persona_list():
        """The built-in personas with the config file's changes, and the one Settings opens with (`persona`; empty
        leaves it to the viewer's browser)."""
        return jsonify(personas=personas.merged(settings.get("personas")), default=personas.DEFAULT,
                       configured=settings.get("persona") or "", summary_cards=list(personas.SUMMARY))

    @blueprint.get("/api/cost/summary")
    def cost_summary():
        """A subscription's or resource group's month, last 30 days, services, top parts and budgets."""
        scope = request.args.get("id", "").rstrip("/")
        parts = scope.strip("/").split("/")
        if not (len(parts) in (2, 4) and parts[0].lower() == "subscriptions" and (len(parts) == 2 or parts[2].lower() == "resourcegroups")):
            abort(400, "id must be a subscription or resource group ID")
        arm_client = client()
        return jsonify(cost.summary(arm_client, scope, cost_store_of(arm_client, cost_type_arg()),
                                    refresh=bool(getattr(arm_client, "is_fresh", False))))

    @blueprint.post("/api/cost")
    def cost_overview():
        """Every subscription in scope by resource group, as a job that publishes as each subscription answers."""
        body = request.get_json(silent=True) or {}
        subscriptions, _groups = scope_args(body)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        arm_client = client()
        store, refresh = cost_store_of(arm_client, cost_type_arg(body)), bool(getattr(arm_client, "is_fresh", False))
        everything = [s["id"] for s in inventory.list_subscriptions(arm_client)
                      if (s.get("state") or "").lower() in cost_store.ACTIVE_STATES]
        spike = settings["cost"]["spike_percent"] / 100
        work = lambda bound, progress, publish: cost.overview(bound, subscriptions, store, everything,  # noqa: E731
                                                              progress, publish, refresh=refresh, spike_factor=spike)
        return jsonify(job_id=start_job(arm_client, work, kind="cost", replace=True)), 202

    budgets_cache = TtlCache("budgets", cost.SCOPE_SECONDS)

    @blueprint.get("/api/cost/budgets")
    def cost_budgets():
        """Every budget on the subscriptions in scope, one read each; kept an hour, Refresh reads again."""
        subscriptions, _groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        arm_client = client()
        key = (owner(arm_client), tuple(sorted(subscriptions)))
        return jsonify(budgets_cache.get_or_load(key, lambda: cost.budgets_for(arm_client, subscriptions),
                                                 refresh=bool(getattr(arm_client, "is_fresh", False))))

    def summary_path(arm_client):
        identity = arm_client.identity
        return summary_history.path_for(settings["cache_dir"], identity.tenant_id, identity.object_id)

    @blueprint.post("/api/summary/changes")
    def summary_changes():
        """Keeps what this Summary listed (on this machine, for you) and answers what changed since the baseline."""
        body = request.get_json(silent=True) or {}
        subscriptions, groups = scope_args(body)
        if not subscriptions or not isinstance(body.get("items"), dict):
            abort(400, "Send the scope's subscriptions and the summary's items.")
        cfg = settings["summary"]
        return jsonify(summary_history.record(summary_path(client()), summary_history.scope_key(subscriptions, groups),
                                              body["items"], cfg["compare_hours"], cfg["keep_days"]))

    @blueprint.get("/api/summary/baseline")
    def summary_baseline():
        """What What changed will compare against for this scope, read before any card answers; keeps nothing."""
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        cfg = settings["summary"]
        return jsonify(summary_history.baseline(summary_path(client()), summary_history.scope_key(subscriptions, groups),
                                                cfg["compare_hours"], cfg["keep_days"]))

    @blueprint.get("/api/cost/totals")
    def cost_totals():
        """The scope's costs in one management-group query, when readable; {"available": false} otherwise."""
        subscriptions, _groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        arm_client = client()
        kind = cost_type_arg()
        found = cost.totals(arm_client, subscriptions, cost_store_of(arm_client, kind), cost_type=kind,
                            spike_factor=settings["cost"]["spike_percent"] / 100)
        return jsonify(found or {"available": False})

    @blueprint.get("/api/cost/<job_id>/export")
    def cost_export(job_id):
        job = _job(job_id)
        if job["status"] != jobs.DONE:
            abort(409, "The costs aren't finished reading yet.")
        return export_table(cost.to_doc(job["result"], sub_names(client())))

    @blueprint.get("/api/cost/waste")
    def cost_waste():
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        return jsonify(cost.waste(client(), subscriptions, rg_clause(groups)))

    @blueprint.get("/api/network/vnet")
    def network_vnet():
        return jsonify(vnet_detail())

    @blueprint.get("/api/network/vnet/export")
    def network_vnet_export():
        v = vnet_detail()
        return export_table({
            "title": f"VNet {v['name']}",
            "subtitle": f"{v['resource_group']} - {v['location']} - address space {', '.join(s['cidr'] for s in v['spaces'])}. "
                        f"IP usage from {v['usage_source']}; Azure reserves 5 addresses in every IPv4 subnet.",
            "sections": [
                {"heading": "Subnets", "status_column": 0, "widths": [7, 14, 14, 7, 7, 7, 12, 12, 20],
                 "columns": ["Status", "Subnet", "CIDR", "Usable", "In use", "% used", "NSG", "Route table",
                             "Delegations"],
                 "rows": [[s["status"], s["name"], ", ".join(s["cidrs"]), s["usable"], s["used"], s["percent"], s["nsg"],
                           s["route_table"], s["delegations"]] for s in v["subnets"]]},
                {"heading": "Address spaces", "columns": ["Address space", "Total IPs", "Allocated", "Free", "% allocated"],
                 "rows": [[s["cidr"], s["total"], s["allocated"], s["free"], s["percent"]] for s in v["spaces"]]},
                {"heading": "Free ranges", "columns": ["Address space", "Free CIDR"],
                 "rows": [[s["cidr"], f] for s in v["spaces"] for f in s["free_ranges"]]},
                {"heading": "Sizes that still fit", "columns": ["Address space", "Size", "First free", "How many",
                                                                "Usable IPs each"],
                 "rows": [[r["address_space"], r["prefix"], r["next_cidr"], r["count"], r["usable_ips"]]
                          for r in v["sizes"]]},
            ] + ([{"heading": "Outside every address space", "columns": ["Subnet prefix"],
                   "rows": [[o] for o in v["outside"]]}] if v["outside"] else []),
        })

    @blueprint.post("/api/checks")
    def start_check():
        body = request.get_json(silent=True) or {}
        only_id = str(body.get("id") or "")
        if only_id:  # one cluster, from its own page: its resource group is the scope
            parts = only_id.lower().split("/")
            if len(parts) < 9 or parts[1] != "subscriptions":
                abort(400, "id must be an AKS cluster's resource ID")
            body = {**body, "subscriptions": [parts[2]], "groups": [f"{parts[2]}/{parts[4]}"], "filtered": True}
        subscriptions, groups = scope_args(body)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        arm_client = client()
        if body.get("check") != "aks":
            abort(400, "check must be 'aks' (Key Vault expiry is the Secrets tab: /api/secrets)")
        work = lambda bound, progress, publish: checks.aks_fleet(  # noqa: E731
            bound, subscriptions, groups, bool(body.get("detectors")), progress, only_id or None,
            settings["aks"]["leftover_hours"])
        return jsonify(job_id=start_job(arm_client, work, kind="aks-one" if only_id else "aks", replace=True)), 202

    @blueprint.get("/api/checks/<job_id>")
    def check_status(job_id):
        job = _job(job_id)
        return jsonify(status=job["status"], phase=job["phase"], error=job["error"], credential=job.get("credential"),
                       result=job["result"] if job["status"] == "done" else None, partial=job["partial"])

    @blueprint.post("/api/readiness")
    def start_readiness():
        """The pre-peak readiness report, as a job; poll it at /api/checks/<job_id>."""
        body = request.get_json(silent=True) or {}
        subscriptions, groups = scope_args(body)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        try:
            peak_end = readiness.parse_peak_end(body.get("peak_end"))
        except ValueError as error:
            abort(400, str(error))
        options = {**settings["health"], **settings["network"],
                   "secret_scan_max_vaults": settings["triage"]["secret_scan_max_vaults"]}
        deep, include_secrets = bool(body.get("deep", True)), bool(body.get("include_secrets"))
        work = lambda bound, progress, publish: readiness.run(  # noqa: E731
            bound, subscriptions, options, peak_end, groups, deep, include_secrets, settings["aks"], progress)
        purpose = body.get("purpose") or "view"  # Summary's quick run must not cancel the Readiness page's
        if purpose not in ("view", "summary"):
            abort(400, "purpose must be view or summary.")
        return jsonify(job_id=start_job(client(), work, kind=f"readiness:{purpose}", replace=True)), 202

    @blueprint.get("/api/readiness/<job_id>/export")
    def readiness_export(job_id):
        job = _job(job_id)
        if job["status"] != jobs.DONE:
            abort(409, "The report isn't finished yet.")
        doc = readiness.to_doc(job["result"], sub_names(client()), request.args.get("healthy", "1") == "1")
        return export_table(doc)

    def my_roles():
        """Your PIM roles, with the roles you already hold so they aren't offered as eligible; `refresh=1` rereads."""
        session = client()
        return pim.roles(session, refresh=request.args.get("refresh") == "1",
                         standing=user_access.standing(session, getattr(session, "token_provider", None)))

    @blueprint.get("/api/pim/hints")
    def pim_hints():
        """Eligible PIM roles that would cover `id`, for a "no access" message. Read-only; never activates."""
        target = request.args.get("id", "")
        if not target.startswith("/"):
            abort(400, "id must be an Azure resource or scope ID")
        found = my_roles()
        return jsonify(pim.hints(found, target, request.args.get("required", "")))

    @blueprint.get("/api/pim")
    def pim_list():
        """Eligible and active PIM roles; `subscription`/`group` (the scope in use) mark what each one covers."""
        found = my_roles()
        subscriptions, groups = scope_args(request.args)
        group_ids = [f"/subscriptions/{g.split('/', 1)[0]}/resourcegroups/{g.split('/', 1)[1]}" for g in groups] if groups is not None else None
        return jsonify(pim.listing(found, request.args.get("q", ""), subscriptions, group_ids,
                                   request.args.get("scope_only") == "1"))

    @blueprint.get("/api/access/groups")
    def access_groups():
        """The groups you're in now (activated PIM groups included), from Microsoft Graph; read-only."""
        provider = getattr(client(), "token_provider", None)
        if provider is None:
            return jsonify(groups=[], error="This session has no Azure CLI sign-in to read Microsoft Graph with.")
        try:
            return jsonify(groups=graph.my_groups(provider, refresh=request.args.get("refresh") == "1"))
        except ArmError as error:
            return jsonify(groups=[], error=str(error).splitlines()[0])

    @blueprint.get("/api/access/users")
    def access_users():
        """Users whose name, sign-in name or mail starts with `q` (at least 2 characters), up to 10."""
        provider = getattr(client(), "token_provider", None)
        if provider is None:
            return jsonify(users=[], error="This session has no Azure CLI sign-in to read Microsoft Graph with.")
        try:
            return jsonify(users=graph.search_users(provider, request.args.get("q", "")))
        except ArmError as error:
            return jsonify(users=[], error=str(error).splitlines()[0])

    @blueprint.get("/api/access/here")
    def access_here():
        """Your roles that apply to one resource, resource group or subscription, and what you could activate there."""
        target = request.args.get("id", "")
        if not target.lower().startswith("/subscriptions/"):
            abort(400, "id must be a subscription, resource group or resource ID")
        arm_client = client()
        return jsonify(user_access.my_access(arm_client, getattr(arm_client, "token_provider", None), target))

    @blueprint.post("/api/access/user")
    def access_user():
        """Another user's groups, active and eligible roles in the scope's subscriptions, as a job (reused 5 min)."""
        body = request.get_json(silent=True) or {}
        user = body.get("user") or {}
        if not isinstance(user, dict) or not graph.GUID.match(str(user.get("id", ""))):
            abort(400, "Pick a user from the search first.")
        subscriptions, _groups = scope_args(body)
        if not subscriptions:
            abort(400, "Pick at least one subscription in the Scope bar: roles are read for the subscriptions in scope.")
        arm_client = client()
        provider = getattr(arm_client, "token_provider", None)
        if provider is None:
            abort(400, "This session has no Azure CLI sign-in to read Microsoft Graph with.")
        if len(subscriptions) > user_access.MAX_SUBSCRIPTIONS and not body.get("force"):
            return jsonify(too_many=len(subscriptions), limit=user_access.MAX_SUBSCRIPTIONS)
        person = {k: str(user.get(k) or "") for k in ("id", "name", "upn", "mail")}
        g.audit_detail = {"looked_up": person["upn"] or person["name"], "looked_up_id": person["id"],
                          "subscriptions": len(subscriptions)}
        key = (owner(arm_client), person["id"].lower(), tuple(sorted(subscriptions)))
        reused = None if body.get("again") else reusable_job(("user access",) + key, arm_client)
        if reused:
            return jsonify(job_id=reused, cached=True)

        def work(bound, progress, publish):
            return user_access.check(bound, provider, person, subscriptions, progress, publish)

        job_id = start_job(arm_client, work, kind="user-access", replace=True)
        reused_jobs.put(("user access",) + key, job_id)
        return jsonify(job_id=job_id)

    @blueprint.get("/api/access/needs")
    def access_needs():
        """What each feature needs, and whether your roles give it in the scope (standing, or eligible through PIM)."""
        subscriptions, _groups = scope_args(request.args)
        arm_client = client()
        return jsonify(requirements.check(arm_client, getattr(arm_client, "token_provider", None), subscriptions))

    @blueprint.get("/api/access/custom-role")
    def access_custom_role():
        """A custom role definition with everything the console reads, to edit (AssignableScopes) and create yourself."""
        body = json.dumps(requirements.CUSTOM_ROLE, indent=2) + "\n"
        return send_file(io.BytesIO(body.encode()), as_attachment=True, download_name="azure-health-console-reader.json",
                         mimetype="application/json")

    @blueprint.post("/api/security/access-audit")
    def access_audit_start():
        """People who can do more than read without PIM in the scope, as a job (reused 10 min). The scope's saved
        result (`audit.keep_days`) answers first, as a finished job, without asking about many subscriptions."""
        body = request.get_json(silent=True) or {}
        subscriptions, groups = scope_args(body)
        if not subscriptions:
            abort(400, "Pick at least one subscription in the Scope bar: role assignments are read per subscription.")
        arm_client = client()
        provider = getattr(arm_client, "token_provider", None)
        if provider is None:
            abort(400, "This session has no Azure CLI sign-in to read Microsoft Graph with.")
        ignore = list(settings["audit"]["ignore_actions"])
        keep_days = settings["audit"]["keep_days"]
        key = ("access audit", owner(arm_client), tuple(sorted(subscriptions)), tuple(sorted(groups or ())), tuple(ignore))
        reused = None if body.get("again") else reusable_job(key, arm_client)
        if reused:
            return jsonify(job_id=reused, cached=True, saved_at=saved_jobs.get(reused))
        path, saved_key = audit_path(arm_client), audit_store.scope_key(subscriptions, groups, ignore)
        saved = None if body.get("again") else audit_store.get(path, saved_key, keep_days)
        if saved:
            job_id = start_job(arm_client, lambda bound, progress, publish: saved["result"], kind="access-audit", replace=True)
            reused_jobs.put(key, job_id)
            saved_jobs.put(job_id, saved["at"])
            return jsonify(job_id=job_id, saved_at=saved["at"])
        if len(subscriptions) > access_audit.MAX_SUBSCRIPTIONS and not body.get("force"):
            return jsonify(too_many=len(subscriptions), limit=access_audit.MAX_SUBSCRIPTIONS)

        def work(bound, progress, publish):
            result = access_audit.audit(bound, provider, subscriptions, groups, ignore, progress)
            try:
                audit_store.put(path, saved_key, result, keep_days)
            except OSError as error:
                logger.warning("Access audit not saved to %s: %s", path, error)
            return result

        job_id = start_job(arm_client, work, kind="access-audit", replace=True)
        reused_jobs.put(key, job_id)
        return jsonify(job_id=job_id)

    @blueprint.get("/api/security/access-audit/<job_id>/export")
    def access_audit_export(job_id):
        job = _job(job_id)
        if job["status"] != jobs.DONE:
            abort(409, "The audit isn't finished yet.")
        result, labels = job["result"], access_audit.LEVEL_LABELS
        rows = [[p["name"], p.get("upn") or "", "guest" if p.get("guest") else "", labels[i["level"]], i["role"], i["scope_label"],
                 i["type_label"], i["via"] or "direct", i["until"] or "standing", "yes" if i["eligible_too"] else "",
                 i.get("condition") or "", i.get("note") or ("PIM status unknown: resource group not readable" if i.get("pim_unknown") else "")]
                for p in result["people"] for i in p["items"]]
        s = result["summary"]
        return export_table({
            "title": "Access audit",
            "subtitle": f"{s['people']} people can do more than read without PIM ({s['assignments']} role assignments, "
                        f"{s['subscriptions']} subscription(s)). Read-only, with your own access.",
            "sections": [{"heading": "What each person can do without PIM",
                          "columns": ["Person", "Sign-in name", "Guest", "Can", "Role", "Where", "Type", "How",
                                      "Until", "Could use PIM instead", "Condition", "Note"], "rows": rows},
                         {"heading": "Assignments to deleted users or groups", "columns": ["Object ID", "Type", "Role", "Can", "Where"],
                          "rows": [[o["principal_id"], o["principal_type"], o["role"], labels[o["level"]], o["scope_label"]]
                                   for o in result.get("orphaned") or []]},
                         {"heading": "Notes", "columns": ["Note"], "rows": [[n] for n in result["notes"]]}]})

    @blueprint.get("/api/watchlist")
    def watchlist_items():
        return jsonify(live["watchlist"].items(client().identity.tenant_id))

    @blueprint.post("/api/watchlist")
    def watchlist_add():
        body = request.get_json(silent=True) or {}
        if not str(body.get("id", "")).lower().startswith("/subscriptions/"):
            abort(400, "id must be an Azure resource or resource group ID")
        return jsonify(live["watchlist"].add(body["id"], body.get("name") or body["id"].rsplit("/", 1)[-1],
                                             body.get("kind", "resource"), client().identity.tenant_id))

    @blueprint.post("/api/watchlist/remove")
    def watchlist_remove():
        body = request.get_json(silent=True) or {}
        return jsonify(live["watchlist"].remove(str(body.get("id", "")), client().identity.tenant_id))

    def watchlist_check(arm_client, items, health_options=None, options=None, progress=None):
        health_options = health_options or HealthOptions(**{**settings["health"], "detectors": False, "aks_leftover_hours": settings["aks"]["leftover_hours"]})
        options = options or DiscoveryOptions(**{**settings["graph"], "depth": 0, "inbound": False})
        return watchlist.check(arm_client, items, options, health_options, live["profiles"], progress)

    @blueprint.post("/api/watchlist/check")
    def watchlist_check_now():
        arm_client = client()
        items = live["watchlist"].items(arm_client.identity.tenant_id)
        health_options = HealthOptions(**{**settings["health"], "detectors": False, "aks_leftover_hours": settings["aks"]["leftover_hours"]})
        options = DiscoveryOptions(**{**settings["graph"], "depth": 0, "inbound": False})

        def work(bound, progress, publish):
            return {"rows": watchlist_check(bound, items, health_options, options, progress)}
        return jsonify(job_id=start_job(arm_client, work, kind="watchlist", replace=True)), 202

    # ---------------------------------------------------------------- alerts (phase 13)

    def alert_source(arm_client):
        tenant, _ = tenant_of(arm_client)
        tenant_id = arm_client.identity.tenant_id

        def saved_scope(name):
            return config.find_scope(settings, tenant, name)

        return alerts.Source(
            client=arm_client, rules=lambda: config.alert_rules(settings, tenant), saved_scope=saved_scope,
            watchlist_items=lambda: live["watchlist"].items(tenant_id),
            triage_options=lambda: {**settings["health"], **settings["triage"], **settings["network"], **settings["security"]},
            check_watchlist=lambda bound, items: watchlist_check(bound, items),
            servicenow_records=servicenow_records, monitor_records=monitor_records)

    def _strong(matches):
        """Exact or distinctive-name matches only: a short or plain name would alert on other teams' records."""
        return [m for m in matches if m["confidence"] in ("exact", "name")]

    def servicenow_records(arm_client, scope, subscriptions, groups, rule):
        """The saved scope's ServiceNow connection: open incidents at or above the rule's priority that name a resource
        in scope, as alert items (13b). Setup problems and read failures come back as text, never as an empty list."""
        name = (scope.get("connections") or {}).get("servicenow")
        prefix = f"sn|{(name or '').lower()}|"
        if not name:
            return [{"prefix": prefix, "items": [], "setup": f"ServiceNow: the saved scope '{scope['name']}' has no "
                     "ServiceNow connection. Pick one in the Scope bar, then Saved scopes → Update."}]
        _tenant, _key, integ = tenant_integrations(arm_client)
        cfg = integrations.find_servicenow(integ, name)
        blocked = servicenow_unavailable(cfg) if cfg else {
            "reason": f"There's no ServiceNow connection called {name} any more.",
            "fix": "Pick another in the Scope bar and update the saved scope."}
        if blocked:
            return [{"prefix": prefix, "items": [], "setup": f"ServiceNow: {blocked['reason']} {blocked['fix']}".strip()}]
        try:
            data = sn_data.collect(cfg, matching.inventory(arm_client, subscriptions, groups))
        except (sn.ServiceNowError, ArmError) as error:
            return [{"prefix": prefix, "items": [], "error": f"ServiceNow ({name}): {str(error).splitlines()[0]}"}]
        items = []
        for incident in data["incidents"]:
            hits = _strong(incident["matches"])
            if incident["priority"] > rule["servicenow_priority"] or not hits:
                continue
            items.append({"key": prefix + incident["number"], "signal": "servicenow",
                          "status": "critical" if incident["priority"] <= 1 else "warning",
                          "title": f"{incident['number']} ({incident['priority_label'] or 'P' + str(incident['priority'])}): "
                                   f"{incident['title']}",
                          "detail": f"{name} · {incident['group'] or 'no assignment group'} · names "
                                    f"{', '.join(h['name'] for h in hits[:3])}",
                          "resource_id": hits[0]["id"], "name": incident["number"]})
        return [{"prefix": prefix, "items": items, "truncated": data["truncated"]["incident"]}]

    def monitor_records(arm_client, scope, subscriptions, groups, rule):
        """Each monitoring connection the saved scope names: open alerts (issues) on something in scope."""
        named = {kind: name for kind, name in (scope.get("connections") or {}).items() if kind != "servicenow"}
        if not named:
            return [{"prefix": "mon|", "items": [], "setup": f"Monitoring: the saved scope '{scope['name']}' has no "
                     "monitoring connection. Pick one in the Scope bar, then Saved scopes → Update."}]
        _tenant, _key, integ = tenant_integrations(arm_client)
        out = []
        for kind, name in named.items():
            prefix = f"mon|{name.lower()}|"
            monitor = integrations.find_monitor(integ, name)
            if not monitor or not monitor.get("enabled") or kind not in monitors.PROVIDERS:
                why = ("isn't connected yet" if monitor and kind not in monitors.PROVIDERS
                       else "is turned off" if monitor else "doesn't exist any more")
                out.append({"prefix": prefix, "items": [], "setup": f"Monitoring: {name} {why}. Fix it in Settings → "
                            "Integrations, or pick another in the Scope bar and update the saved scope."})
                continue
            try:
                view = monitors.connection(monitor, matching.inventory(arm_client, subscriptions, groups))
            except ArmError as error:
                out.append({"prefix": prefix, "items": [], "error": f"{name}: {str(error).splitlines()[0]}"})
                continue
            if not view.get("available"):
                key = "setup" if view.get("problems") else "error"
                out.append({"prefix": prefix, "items": [], key: f"{name}: {view['reason']} {view.get('fix', '')}".strip()})
                continue
            items = []
            for issue in view["issues"]:
                hits = _strong(issue["matches"])
                if issue["priority"] not in ("critical", "warning") or not hits:
                    continue
                items.append({"key": prefix + issue["id"], "signal": "monitors", "status": issue["priority"],
                              "title": f"{view['label']} ({name}): {issue['title']}",
                              "detail": " · ".join(x for x in (issue["condition"], issue["policy"],
                                                               ", ".join(issue["entities"][:3])) if x),
                              "resource_id": hits[0]["id"], "name": issue["title"] or issue["id"]})
            out.append({"prefix": prefix, "items": items, "truncated": view.get("truncated", False)})
        return out

    def alert_owner(arm_client):
        """Registers the viewer's rules the first time they're asked about, then just reads their state."""
        key = owner(arm_client)
        if not alert_runner.has(key):
            alert_runner.register(key, alert_source(arm_client))
        return key

    def alerts_payload(arm_client):
        tenant, _ = tenant_of(arm_client)
        return {**alert_runner.state(alert_owner(arm_client)),
                "saved_scopes": [s["name"] for s in config.saved_scopes(settings, tenant)],
                "scope_connections": {s["name"]: s["connections"] for s in config.saved_scopes(settings, tenant)},
                "signals": config.ALERT_SIGNALS, "signals_by_kind": config.ALERT_SIGNALS_BY_KIND,
                "default_signals": config.ALERT_DEFAULT_SIGNALS,
                "minutes": config.ALERT_MINUTES, "background": start_alerts}

    if start_alerts:
        def adopt():
            default = client_for(None)
            return owner(default), alert_source(default)
        alert_runner.start(adopt)

    @blueprint.get("/api/alerts")
    def alerts_view():
        return jsonify(alerts_payload(client()))

    @blueprint.post("/api/alerts/rules")
    def alerts_save():
        """Replaces the current tenant's alert rules (the page sends the whole list)."""
        body = request.get_json(silent=True) or {}
        rules = body.get("rules")
        if not isinstance(rules, list):
            abort(400, "rules must be a list")
        arm_client = client()
        failed = save_for_tenant(settings_file.apply_alert_rules, rules, tenant=tenant_of(arm_client)[0])
        if failed:
            return failed
        alert_runner.wake()
        return jsonify(alerts_payload(arm_client))

    @blueprint.post("/api/alerts/run")
    def alerts_run():
        body = request.get_json(silent=True) or {}
        arm_client = client()
        if not alert_runner.run_now(alert_owner(arm_client), str(body.get("name", ""))):
            abort(404, "No enabled rule by that name has been set up yet.")
        if not start_alerts:
            alert_runner.tick()  # nothing runs in the background (mounted without it): check in this request
        return jsonify(alerts_payload(arm_client))

    @blueprint.post("/api/alerts/acknowledge")
    def alerts_acknowledge():
        body = request.get_json(silent=True) or {}
        arm_client = client()
        if not alert_runner.acknowledge(alert_owner(arm_client), _seq(body)):
            abort(404, "That alert isn't in the history any more.")
        return jsonify(alerts_payload(arm_client))

    @blueprint.post("/api/alerts/snooze")
    def alerts_snooze():
        """{"seq": n} snoozes one alert, {"rule": name} a whole rule; "minutes": 60 by default, 0 ends it."""
        body = request.get_json(silent=True) or {}
        arm_client = client()
        minutes = body.get("minutes", 60)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 24 * 60:
            abort(400, "minutes must be 0 to 1440")
        key = alert_owner(arm_client)
        done = (alert_runner.snooze_rule(key, str(body["rule"]), minutes * 60) if body.get("rule")
                else alert_runner.snooze(key, _seq(body), minutes * 60))
        if not done:
            abort(404, "Nothing to snooze by that name or number.")
        return jsonify(alerts_payload(arm_client))

    @blueprint.get("/api/alerts/export")
    def alerts_export():
        """The kept alerts (`alerts.history_hours`) as Markdown, CSV or PDF (13c)."""
        arm_client = client()
        events = alert_runner.history(alert_owner(arm_client))

        def state_of(e):
            parts = []
            if e.get("resolved_at"):
                parts.append(f"resolved {e['resolved_at'][11:16]} UTC ({e.get('resolved_note', '')})")
            if e.get("acknowledged_at"):
                parts.append(f"acknowledged {e['acknowledged_at'][11:16]} UTC")
            return "; ".join(parts) or ("" if e["signal"] in alerts.RECOVERY_SIGNALS else "open")

        rows = [[e["at"][:16].replace("T", " "), e["status"], e["title"], e["rule"], e["detail"], state_of(e),
                 e.get("resource_id", "")] for e in events]
        return export_table({
            "title": "Alert history",
            "subtitle": f"The last {settings['alerts']['history_hours']} hours of alerts for {arm_client.identity.name}, "
                        "newest first, times in UTC. Kept in "
                        "the console's memory: a restart clears it.",
            "sections": [{"heading": f"{len(rows)} alert(s)", "columns": ["When (UTC)", "Status", "Alert", "Rule",
                                                                          "Detail", "State", "Resource"],
                          "rows": rows, "status_column": 1}],
        })

    def _seq(body):
        try:
            return int(body.get("seq", 0))
        except (TypeError, ValueError):
            abort(400, "seq must be a number")

    @blueprint.post("/api/alerts/dismiss")
    def alerts_dismiss():
        body = request.get_json(silent=True) or {}
        arm_client = client()
        try:
            seq = int(body.get("seq", 0))
        except (TypeError, ValueError):
            abort(400, "seq must be a number")
        alert_runner.dismiss(alert_owner(arm_client), seq)
        return jsonify(alerts_payload(arm_client))

    @blueprint.get("/api/subscriptions")
    def subscriptions():
        return jsonify(inventory.list_subscriptions(client()))

    @blueprint.get("/api/resource-groups")
    def resource_groups():
        return jsonify(inventory.list_resource_groups(client(), request.args.getlist("subscription")))

    @blueprint.get("/api/resources")
    def resources():
        groups = []
        for value in request.args.getlist("group"):
            subscription_id, _, name = value.partition("/")
            if subscription_id and name:
                groups.append((subscription_id, name))
        return jsonify(inventory.list_resources(client(), groups))

    @blueprint.get("/api/resources/find")
    def resources_find():
        """Browse's filter: resources in scope by name or type, across every group in it."""
        subscriptions, groups = scope_args(request.args)
        return jsonify(inventory.find_resources(client(), request.args.get("q", ""), subscriptions, groups))

    @blueprint.post("/api/snapshots")
    def start_snapshot():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not (body.get("resource_id") or body.get("resource_group")):
            abort(400, "resource_id or resource_group (\"<subscription-id>/<name>\") is required")
        arm_client = client()
        graph = dict(settings["graph"])
        if body.get("depth") is not None:
            graph["depth"] = max(0, min(int(body["depth"]), MAX_DEPTH))
        if body.get("inbound") is not None:
            graph["inbound"] = bool(body["inbound"])
        options = DiscoveryOptions(**graph)
        health_options = HealthOptions(**settings["health"], aks_leftover_hours=settings["aks"]["leftover_hours"])
        if body.get("resource_group"):
            subscription_id, _, group = body["resource_group"].partition("/")
            if not subscription_id or not group:
                abort(400, "resource_group must be \"<subscription-id>/<name>\"")
            work = lambda bound, progress, publish: snapshot.build_group(  # noqa: E731
                bound, subscription_id, group, options, health_options, live["profiles"], progress, publish)
        else:
            resource_id = body["resource_id"]
            work = lambda bound, progress, publish: snapshot.build(  # noqa: E731
                bound, resource_id, options, health_options, live["profiles"], progress, publish)
        # One check at a time per person: starting another cancels the one still running.
        return jsonify(job_id=start_job(arm_client, work, kind="snapshot", replace=True)), 202

    @blueprint.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        job = runner.cancel(job_id, owner(client()))
        if not job:
            abort(404)
        return jsonify(status=job["status"])

    @blueprint.get("/api/snapshots/<job_id>")
    def snapshot_status(job_id):
        job = _job(job_id)
        payload = {"status": job["status"], "phase": job["phase"], "error": job["error"], "credential": job.get("credential")}
        if job["status"] == jobs.DONE:
            payload["snapshot"] = job["result"].to_dict()
        elif job["partial"] is not None:
            payload["partial"] = job["partial"].to_dict()
        return jsonify(payload)

    @blueprint.get("/api/snapshots/<job_id>/export")
    def export(job_id):
        job = _job(job_id)
        if job["status"] != "done":
            abort(409, "The snapshot isn't finished yet.")
        fmt = request.args.get("format", "md")
        if fmt not in report.FORMATS:
            abort(400, f"format must be one of {report.FORMATS}")
        content = report.render(job["result"], fmt, settings.get("pdf_font"))
        return send_file(io.BytesIO(content), as_attachment=True,
                         download_name=report.filename(job["result"], fmt),
                         mimetype="application/pdf" if fmt == "pdf" else "text/markdown")

    @blueprint.post("/api/diagram")
    def diagram_svg():
        """The diagram of the snapshot the page holds (a kept check can outlive its job); `page` makes it a page of its
        own for a new tab. Rendering only: no Azure call."""
        body = request.get_json(silent=True) or {}
        snap = body.get("snapshot")
        if not isinstance(snap, dict) or not isinstance(snap.get("nodes"), list) or not isinstance(snap.get("edges"), list):
            abort(400, "A snapshot with nodes and edges is required.")
        title = str(body.get("title") or snap.get("title") or "Diagram")[:200]
        try:
            svg = diagram.render_svg(snap, bool(body.get("show_all")), title)
        except (KeyError, TypeError, AttributeError):
            abort(400, "The snapshot isn't one this console made.")
        if body.get("page"):
            return diagram.page(svg, title, str(body.get("note") or "")[:600]), 200, {"Content-Type": "text/html; charset=utf-8"}
        return svg, 200, {"Content-Type": "image/svg+xml; charset=utf-8"}

    @blueprint.get("/api/snapshots/<job_id>/print")
    def printable(job_id):
        """The report as a page to print or "Save as PDF" from the browser; no PDF library involved."""
        job = _job(job_id)
        if job["status"] != "done":
            abort(409, "The snapshot isn't finished yet.")
        return report.to_html(job["result"], printable=True)

    def settings_payload(arm_client):
        tenant, _ = tenant_of(arm_client)
        keys = config.entry_keys(settings, tenant, wildcard=True)
        fields = []
        for section, key, kind, help_text in config.SCHEMA:
            default = config.DEFAULTS[section][key] if section else config.DEFAULTS[key]
            value = settings[section][key] if section else settings[key]
            fields.append({"name": f"{section}.{key}" if section else key, "section": section or "files",
                           "type": list(kind) if isinstance(kind, tuple) else kind,
                           "help": help_text, "default": default, "value": value})
        return {
            "path": str(config_file), "exists": config_file.is_file(), "fields": fields,
            "tenant": {"key_used": keys[0] if keys else None,
                       "suggested_key": tenant.get("domain") or tenant.get("id"),
                       "name": tenant.get("name") or tenant.get("id")},
            **scope_payload(tenant),
            "kubeconfig_paths": settings["aks"]["kubeconfig_paths"],
        }

    def apply_settings(new):
        logs_before = dict(settings.get("logs") or {})
        settings.clear()
        settings.update(new)
        reload_live()
        if eventlog.enabled() and settings["logs"] != logs_before:  # a new folder or keep_days applies at once
            eventlog.configure(settings["logs"]["dir"], settings["logs"]["keep_days"])

    @blueprint.get("/api/settings")
    def settings_view():
        return jsonify(settings_payload(client()))

    @blueprint.post("/api/settings")
    def settings_save():
        body = request.get_json(silent=True) or {}
        g.audit_detail = {"changed": sorted(body["values"]) if isinstance(body.get("values"), dict) else []}
        try:
            new = settings_file.apply(config_file, values=body.get("values"))
        except config.ConfigError as error:
            return jsonify(error=str(error), kind="config"), 400
        try:
            apply_settings(new)
        except (OSError, profiles.ProfileError) as error:  # e.g. a log or profile folder another account can change
            return jsonify(error=f"Saved, but not all of it is in use: {error}", kind="config"), 400
        return jsonify(saved=True, backup=str(config_file) + ".bak", settings=settings_payload(client()))

    @blueprint.post("/api/settings/reload")
    def settings_reload():
        try:
            new = config.load(config_file)
        except config.ConfigError as error:
            return jsonify(error=str(error), kind="config"), 400
        try:
            apply_settings(new)
        except (OSError, profiles.ProfileError) as error:
            return jsonify(error=f"Reloaded, but not all of it is in use: {error}", kind="config"), 400
        return jsonify(settings_payload(client()))

    # ---------------------------------------------------------------- integrations (one tenant's each)

    oauth_pending = TtlCache("ServiceNow sign-ins in progress", 600, access_bound=False)  # 10 minutes to finish

    def redirect_uri():
        return f"{request.host_url.rstrip('/')}{url_prefix}/integrations/servicenow/callback"

    def tenant_integrations(arm_client=None):
        """(tenant, key it's saved under or None, its integrations with monitors bound to it)."""
        tenant, _ = tenant_of(arm_client or client())
        key, found = config.tenant_integrations(settings, tenant)
        return tenant, key, integrations.bind(found, tenant["id"])

    def integrations_payload():
        tenant, key, integ = tenant_integrations()
        legacy = config.legacy_integrations(settings)
        return {**integrations.status(integ), "redirect_uri": redirect_uri(),
                "tenant": {"id": tenant["id"], "name": tenant.get("name") or tenant["id"], "key": key,
                           "save_key": config.tenant_key(settings, tenant)},
                "legacy": ({"servicenow": [c["instance"] or c["name"] for c in legacy["servicenow"]],
                            "monitors": [m["name"] for m in legacy["monitors"]]} if legacy and key is None else None),
                "servicenow_settings": integ["servicenow"], "servicenow_tables": config.SERVICENOW_TABLES,
                "monitor_settings": [{k: v for k, v in m.items() if k != "tenant"} for m in integ["monitors"]],
                "monitor_types": [t for t in config.MONITOR_TYPES if t in integrations.SUPPORTED_MONITORS], "monitor_capabilities": monitors.capabilities()}

    def config_error(error):
        return jsonify(error=str(error), kind="config"), 400

    def save_for_tenant(writer, *args, tenant=None, **kwargs):
        """Writes this tenant's part of config.yaml with a settings_file function (file, tenant key, …) and applies it.
        Returns the error response when the result wouldn't validate, else None: `return save_for_tenant(…) or …`."""
        tenant = tenant or tenant_of(client())[0]
        try:
            apply_settings(writer(config_file, config.tenant_key(settings, tenant), *args, **kwargs))
        except config.ConfigError as error:
            return config_error(error)
        return None

    @blueprint.get("/api/integrations")
    def integrations_view():
        return jsonify(integrations_payload())

    @blueprint.post("/api/integrations/servicenow")
    def integrations_servicenow():
        """Saves the whole list of this tenant's ServiceNow connections."""
        body = request.get_json(silent=True) or {}
        connections = body.get("connections")
        if not isinstance(connections, list) or not all(isinstance(c, dict) for c in connections):
            abort(400, "connections must be a list")
        connections = [{**c, "instance": sn.normalise_instance(c["instance"])} if c.get("instance") else c
                       for c in connections]
        return save_for_tenant(settings_file.apply_integrations, servicenow=connections) or jsonify(integrations_payload())

    @blueprint.post("/api/integrations/monitors")
    def integrations_monitors():
        body = request.get_json(silent=True) or {}
        monitors = body.get("monitors")
        if not isinstance(monitors, list):
            abort(400, "monitors must be a list")
        return save_for_tenant(settings_file.apply_integrations, monitors=monitors) or jsonify(integrations_payload())

    @blueprint.post("/api/integrations/move-legacy")
    def integrations_move_legacy():
        """Moves the old global integrations (and their monitor keys) into the current tenant."""
        tenant, key, _ = tenant_integrations()
        legacy = config.legacy_integrations(settings)
        if key is not None or not legacy:
            return config_error("Nothing to move: this tenant already has its own integrations, or there are no global ones.")
        try:
            integrations.move_monitor_secrets(legacy["monitors"], tenant["id"])
        except integration_secrets.SecretStoreError as error:
            return config_error(error)
        return save_for_tenant(settings_file.move_legacy_integrations, tenant=tenant) or jsonify(integrations_payload())

    @blueprint.post("/api/integrations/secret")
    def integrations_secret():
        """Stores or removes one secret in the OS keychain; it's never sent back to the page."""
        body = request.get_json(silent=True) or {}
        _, _, integ = tenant_integrations()
        try:
            target, scope, fields = integrations.secret_target(integ, str(body.get("target") or ""))
        except ValueError as error:
            return config_error(error)
        field = body.get("field")
        if field not in fields:
            abort(400, f"field must be one of {sorted(fields)}")
        g.audit_detail = {"target": str(body.get("target") or ""), "field": field, "removed": bool(body.get("remove"))}
        try:
            if body.get("remove"):
                integration_secrets.delete(target, scope, field)
            else:
                value = str(body.get("value") or "").strip()
                if not value:
                    return config_error("Type the value first.")
                integration_secrets.put(target, scope, field, value)
        except integration_secrets.SecretStoreError as error:
            return config_error(error)
        if target == sn.TARGET:
            sn.ServiceNowClient(integrations.find_servicenow(integ, str(body["target"]).split(":", 1)[1])).forget_token()
        return jsonify(integrations_payload())

    @blueprint.post("/api/integrations/test")
    def integrations_test():
        body = request.get_json(silent=True) or {}
        _, _, integ = tenant_integrations()
        return jsonify(integrations.test(integ, str(body.get("target") or ""), body.get("values") or None,
                                         body.get("secrets") or None))

    @blueprint.post("/api/integrations/servicenow/sign-in")
    def servicenow_sign_in():
        tenant, _, integ = tenant_integrations()
        cfg = integrations.find_servicenow(integ, str((request.get_json(silent=True) or {}).get("name") or ""))
        if not cfg:
            return config_error("Save the connection first.")
        if cfg["auth"] != "oauth_browser" or not cfg["instance"] or not cfg["client_id"]:
            return config_error("Choose OAuth (your own login), set the instance URL and client ID, and save first.")
        url, state, verifier = sn.start_browser_sign_in(cfg, redirect_uri())
        oauth_pending.clear()  # one sign-in at a time
        oauth_pending.put(state, {"verifier": verifier, "tenant": tenant["id"], "connection": cfg["name"]})
        return jsonify(url=url)

    @blueprint.get("/integrations/servicenow/callback")
    def servicenow_callback():
        state, code = request.args.get("state", ""), request.args.get("code", "")
        pending = oauth_pending.pop(state, None)
        if request.args.get("error"):
            message = f"ServiceNow said: {request.args.get('error_description') or request.args.get('error')}"
        elif not pending:
            message = "This sign-in link expired or wasn't started here. Start it again from Settings."
        else:
            try:
                _, _, integ = tenant_integrations(client_for(pending["tenant"]))  # the tenant the sign-in started in
                cfg = integrations.find_servicenow(integ, pending["connection"])
                if not cfg:
                    raise sn.ServiceNowError(f"The {pending['connection']} connection was removed or renamed; start again.")
                sn.finish_browser_sign_in(cfg, code, redirect_uri(), pending["verifier"])
                return render_template("signed_in.html", ok=True, message=f"Signed in to {cfg['name']}.")
            except (sn.ServiceNowError, integration_secrets.SecretStoreError) as error:
                message = str(error)
        return render_template("signed_in.html", ok=False, message=message), 400

    # ---------------------------------------------------------------- ServiceNow data

    def chosen_connection(found_in, kind_label):
        """(tenant, the connection named in ?connection=, or None with what to tell the user)."""
        tenant, _, integ = tenant_integrations()
        name = request.args.get("connection", "")
        cfg = found_in(integ, name)
        tenant_name = tenant.get("name") or tenant["id"]
        if not name:
            return None, {"available": False, "reason": f"No {kind_label} connection is selected.", "problems": [],
                          "fix": f"Pick one in the Scope bar, or add one in Settings → Integrations for {tenant_name}."}
        if not cfg:
            return None, {"available": False, "reason": f"{tenant_name} has no {kind_label} connection called {name}.",
                          "problems": [], "fix": "Pick another in the Scope bar; it may have been renamed or removed."}
        return cfg, None

    def servicenow_unavailable(cfg):
        return integrations.unavailable(cfg["name"], integrations.servicenow_status(cfg)["problems"], cfg["enabled"])

    @blueprint.get("/api/servicenow")
    def servicenow_view():
        cfg, blocked = chosen_connection(integrations.find_servicenow, "ServiceNow")
        blocked = blocked or servicenow_unavailable(cfg)
        if blocked:
            return jsonify(blocked)
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        refresh = request.args.get("refresh") == "1"
        try:
            resources = matching.inventory(client(), subscriptions, groups, refresh)
            data = sn_data.collect(cfg, resources, refresh)
        except sn.ServiceNowError as error:
            return jsonify(integrations.failed(error))
        return jsonify(available=True, name=cfg["name"], **data)

    @blueprint.get("/api/servicenow/resource")
    def servicenow_resource():
        cfg, blocked = chosen_connection(integrations.find_servicenow, "ServiceNow")
        blocked = blocked or servicenow_unavailable(cfg)
        if blocked:
            return jsonify(blocked)
        resource_id = _resource_id()
        subscription = resource_id.split("/")[2].lower()
        try:
            resources = matching.inventory(client(), [subscription])
            data = sn_data.collect(cfg, resources)
        except sn.ServiceNowError as error:
            return jsonify(integrations.failed(error))
        notes = {k: v for k, v in data["notes"].items() if "isn't available" not in v and "isn't selected" not in v}
        return jsonify(available=True, name=cfg["name"], instance=data["instance"], fetched_at=data["fetched_at"], notes=notes,
                       **sn_data.for_resource(data, resource_id))

    # ---------------------------------------------------------------- monitoring tools

    def chosen_monitor():
        monitor, blocked = chosen_connection(integrations.find_monitor, "monitoring")
        if not blocked and not monitor.get("enabled"):
            blocked = {"available": False, "reason": f"{monitor['name']} is turned off.", "problems": [],
                       "fix": "Turn it on in Settings → Integrations."}
        return monitor, blocked

    alert_checks = TtlCache("alert checks", ALERT_CHECK_SECONDS)

    @blueprint.get("/api/alert-check")
    def alert_check_view():
        """Is this alert right? One alert's rule, its history and the verdicts (`core/alert_check.py`), from Azure Monitor
        (`source=azure&id=` an alert or metric alert rule) or a monitoring tool (`source=monitor&connection=&account=
        &condition=&priority=`, from an open issue). Only when asked; kept 15 minutes, Refresh reads again."""
        args, arm_client = request.args, client()
        if args.get("source") == "azure":
            ref = args.get("id", "")
            if not ref.lower().startswith("/subscriptions/") or ".." in ref:
                abort(400, "id must be an Azure alert or alert rule ID")
            source, key = azure_alerts.AzureMetricAlerts(arm_client), ref.lower()
        elif args.get("source") == "monitor":
            monitor, blocked = chosen_monitor()
            if blocked:
                return jsonify(blocked)
            source = monitors.provider_for(monitor)
            if source is None or ALERT_HISTORY not in source.capabilities:
                return jsonify(available=False, reason=f"{monitor['name']} can't give an alert's rule and history, "
                                                       "so its alerts can't be checked here yet.")
            try:
                ref = {"account_id": int(args.get("account", "")), "condition": args.get("condition", ""),
                       "policy": args.get("policy", ""), "priority": args.get("priority", "")}
            except ValueError:
                abort(400, "account must be a New Relic account number")
            key = f"{monitor['name']}|{json.dumps(ref, sort_keys=True)}"
        else:
            abort(400, "source must be azure or monitor")
        identity = arm_client.identity
        cache_key = (identity.tenant_id, identity.object_id, args.get("source"), key, json.dumps(settings["alert_check"], sort_keys=True))

        def run():
            try:
                return {"available": True, **alert_check.check(source, ref, settings["alert_check"])}
            except (ArmError, ServiceError) as error:
                return {"available": False, "reason": str(error).splitlines()[0][:400]}
        return jsonify(alert_checks.get_or_load(cache_key, run, refresh=bool(getattr(arm_client, "is_fresh", False))))

    @blueprint.get("/api/monitors")
    def monitors_view():
        """The selected connection's alerts in scope, or why it can't be read."""
        monitor, blocked = chosen_monitor()
        if blocked:
            return jsonify(blocked)
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        refresh = request.args.get("refresh") == "1"
        resources = matching.inventory(client(), subscriptions, groups, refresh)
        return jsonify(monitors.connection(monitor, resources, refresh))

    @blueprint.get("/api/monitors/resource")
    def monitors_resource():
        resource_id = _resource_id()
        monitor, blocked = chosen_monitor()
        if blocked:
            return jsonify(blocked)
        resources = matching.inventory(client(), [resource_id.split("/")[2].lower()])
        name = resource_id.rstrip("/").split("/")[-1]
        return jsonify(monitors.for_resource(monitor, resource_id, name, resources))

    def monitor_question(answer):
        """One resource's question to the selected monitoring connection (its plan, logs or a report): the resource from
        `id`, `minutes` as a number; `answer(monitor, resource_id, name, resources, minutes)` returns the reply."""
        resource_id = _resource_id()
        monitor, blocked = chosen_monitor()
        if blocked:
            return jsonify(blocked)
        try:
            minutes = int(request.args.get("minutes", "30"))
            resources = matching.inventory(client(), [resource_id.split("/")[2].lower()])
            return jsonify(answer(monitor, resource_id, resource_id.rstrip("/").split("/")[-1], resources, minutes))
        except ValueError as error:
            abort(400, str(error))

    def log_args():
        """The log reads' shared options: partitions, the name search (`mentions=0` leaves it out), the Azure ID match
        (`by_id=0` leaves it out), Load older's window."""
        until, since = request.args.get("until"), request.args.get("since")
        return {"partitions": request.args.getlist("partition"), "mentions": request.args.get("mentions") != "0",
                "by_id": request.args.get("by_id") != "0",
                "until": int(until) if until else None, "since": int(since) if since else None}

    @blueprint.get("/api/monitors/plan")
    def monitors_plan():
        """What the Query tab will read for one resource, before Run; nothing is queried but entities and partitions."""
        return monitor_question(lambda m, rid, name, res, minutes: monitors.plan(
            m, rid, name, res, minutes, request.args.get("mentions") != "0", request.args.get("by_id") != "0"))

    @blueprint.get("/api/monitors/logs")
    def monitors_logs():
        """Log lines for one resource from one connection; only when asked, since log queries cost."""
        return monitor_question(lambda m, rid, name, res, minutes: monitors.logs(
            m, rid, name, res, minutes, request.args.get("text", ""), **log_args()))

    @blueprint.get("/api/monitors/find")
    def monitors_find():
        """Resources and resource groups in scope whose name contains `q`, for the tool tab's query field."""
        subscriptions, groups = scope_args(request.args)
        text = request.args.get("q", "").strip().lower()
        if not subscriptions or len(text) < 2:
            return jsonify([])
        rows = [r for r in matching.inventory(client(), subscriptions, groups)
                if text in r["name"].lower() and r["type"].lower() != "microsoft.resources/subscriptions"]
        rows.sort(key=lambda r: (not r["name"].lower().startswith(text), len(r["name"]), r["name"].lower()))
        index, _ = matching.name_index(rows[:20])
        return jsonify([{"id": e["id"], "name": e["name"], "display_type": e["display_type"],
                         "resource_group": e["resource_group"], "subscription_id": e["subscription_id"]}
                        for r in rows[:20] for e in index[r["name"].lower()] if e["id"] == r["id"]])

    @blueprint.get("/api/monitors/report")
    def monitors_report():
        """Errors or slow transactions for one resource from one connection; only when asked."""
        return monitor_question(lambda m, rid, name, res, minutes: monitors.report(
            m, rid, name, res, minutes, request.args.get("kind", ""), request.args.get("text", "")))

    def entity_question(answer):
        """A question about entities picked on the page (`entity` IDs in one `account`, their `name`), to the selected
        connection; `answer(monitor, ids, account, minutes, name)` returns the reply."""
        monitor, blocked = chosen_monitor()
        if blocked:
            return jsonify(blocked)
        try:
            minutes, account = int(request.args.get("minutes", "30")), int(request.args.get("account", ""))
            return jsonify(answer(monitor, request.args.getlist("entity"), account, minutes, request.args.get("name", "")))
        except ValueError as error:
            abort(400, str(error))

    @blueprint.get("/api/monitors/entity-logs")
    def monitors_entity_logs():
        """Log lines for entities picked on a tool's tab, from the selected connection; only when asked."""
        return entity_question(lambda m, ids, account, minutes, name: monitors.entity_logs(
            m, ids, account, minutes, request.args.get("text", ""), name=name, resource_ids=request.args.getlist("resource"),
            **log_args()))

    @blueprint.get("/api/monitors/entities")
    def monitors_entities():
        """The selected connection's entities whose name contains `q`, for the Query tab (no Azure resource needed)."""
        monitor, blocked = chosen_monitor()
        if blocked:
            return jsonify([])
        return jsonify(monitors.find_entities(monitor, request.args.get("q", "")))

    @blueprint.get("/api/monitors/entity-plan")
    def monitors_entity_plan():
        """The Query tab's plan for an entity picked by name; nothing is queried but its partitions."""
        return entity_question(lambda m, ids, account, minutes, name: monitors.entity_plan(
            m, ids, account, name, request.args.get("apm") == "1", minutes, request.args.get("mentions") != "0",
            request.args.getlist("resource"), request.args.get("by_id") != "0"))

    @blueprint.get("/api/monitors/entity-report")
    def monitors_entity_report():
        """Errors or slow transactions for an entity picked by name; only when asked."""
        return entity_question(lambda m, ids, account, minutes, name: monitors.entity_report(
            m, ids, account, minutes, request.args.get("kind", ""), request.args.get("text", ""), name))

    @blueprint.post("/api/saved-scopes")
    def saved_scopes_save():
        """Replaces the current tenant's saved scopes (the page sends the whole list)."""
        body = request.get_json(silent=True) or {}
        scopes = body.get("scopes")
        if not isinstance(scopes, list):
            abort(400, "scopes must be a list")
        tenant, _ = tenant_of(client())
        stored = [s for s in scopes if not (isinstance(s, dict) and s.get("legacy"))]  # those live in the old keys until Move
        return save_for_tenant(settings_file.apply_saved_scopes, stored, tenant=tenant) or jsonify(scope_payload(tenant))

    @blueprint.post("/api/open-with")
    def open_with_save():
        """What this tenant's page opens with: none, all or a saved scope's name."""
        value = str((request.get_json(silent=True) or {}).get("open_with") or "").strip()
        tenant, _ = tenant_of(client())
        if value not in config.OPEN_WITH and config.find_scope(settings, tenant, value) is None:
            return config_error(f"No saved scope is called '{value}'.")
        return save_for_tenant(settings_file.apply_open_with, value, tenant=tenant) or jsonify(scope_payload(tenant))

    @blueprint.post("/api/saved-scopes/move-legacy")
    def saved_scopes_move_legacy():
        """Turns this tenant's old favourites, default tag filters and triage.scope into saved scopes and Open with."""
        tenant, _ = tenant_of(client())
        legacy = config.legacy_scope(settings, tenant)
        if not legacy:
            return config_error("Nothing to move: this tenant has no favourites or default filters left.")
        return save_for_tenant(settings_file.move_legacy_scope, legacy, tenant=tenant) or jsonify(scope_payload(tenant))

    def cost_store_of(arm_client, cost_type="actual"):
        return cost.store_for(arm_client, settings["cache_dir"], cost_type)

    def cost_type_arg(body=None):
        """The cost type a page asks for: `cost_type` actual (the default) or amortized."""
        try:
            return cost.cost_type_of((body or {}).get("cost_type") if body is not None else request.args.get("cost_type"))
        except ValueError as error:
            abort(400, str(error))

    @blueprint.post("/api/cache/clear")
    def cache_clear():
        """The account menu's Clear cached data: your stored costs (memory and disk) and every answer kept in memory
        for your access. Nothing in Azure changes."""
        arm_client = client()
        for kind in cost.COST_TYPES:
            cost_store_of(arm_client, kind).clear()
        summary_history.clear(summary_path(arm_client))
        audit_store.clear(audit_path(arm_client))
        cleared = cache.forget_access_bound()
        return jsonify(ok=True, cleared=["stored costs", "Summary's history", "saved access audits", *cleared])

    @blueprint.post("/api/refresh")
    def refresh():
        client().refresh()
        cleared = cache.forget_access_bound()  # PIM, Graph, inventory, integrations, Kubernetes clients, reused jobs...
        return jsonify(ok=True, cleared=cleared)

    def _resource_id():
        resource_id = request.args.get("id", "")
        if not resource_id.lower().startswith("/subscriptions/") or ".." in resource_id:
            abort(400, "id must be an Azure resource ID")
        return resource_id

    def app_insights_view(arm_client, resource_id, args, now_only=False):
        """A component's own data; for a Web or Function App or an API Management service, its component's, with how
        it was found. `now_only`: just its last hour from metrics (Overview's line)."""
        lowered, link = resource_id.lower(), None
        picked = args.get("component", "")
        if picked and not appinsights.COMPONENT_ID.fullmatch(picked):
            abort(400, "component must be an Application Insights component ID")
        if f"/providers/{appinsights.SITE_TYPE}/" in lowered or f"/providers/{inv_gateway.APIM_TYPE}/" in lowered:
            gateway = f"/providers/{inv_gateway.APIM_TYPE}/" in lowered
            link = (inv_gateway.link if gateway else appinsights.link)(arm_client, resource_id, picked)
            if not link["component_id"]:
                return jsonify(available=False, link=link)
            resource_id = link["component_id"]
        elif f"/providers/{appinsights.COMPONENT_TYPE}/" not in lowered:
            abort(400, "id must be an Application Insights component, a Web or Function App or an API Management service")
        if now_only:
            now, error = appinsights.measure(arm_client, resource_id, settings["appinsights"])
            return jsonify(available=True, link=link, component_id=resource_id, now=now, errors=[error] if error else [])
        try:
            return jsonify(available=True, link=link, **appinsights.insights(arm_client, resource_id, args.get("window", "1h")))
        except ValueError as error:
            abort(400, str(error))

    # ---------------------------------------------------------------- investigate
    # One request, step by step; each read only when asked, and each answer carries the queries it sent.

    def investigate_source():
        try:
            return investigate.source_for(client(), request.args.get("component", ""), request.args.getlist("also"))
        except ValueError as error:
            abort(400, str(error))

    def investigate_at(required=True):
        text = request.args.get("at", "")
        if not text and not required:
            return None
        try:
            return investigate.parse_time(text)
        except ValueError as error:
            abort(400, str(error))

    def log_spread():
        """Minutes either side of `at` that a pod's logs cover (Investigate 2; an OOM kill 5)."""
        spread = request.args.get("spread", "2")
        if not spread.isdigit() or not 1 <= int(spread) <= 30:
            abort(400, "spread must be 1 to 30 minutes")
        return int(spread)

    def investigate_answer(read):
        try:
            return jsonify(read())
        except ValueError as error:
            abort(400, str(error))

    def investigate_scope():
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        return subscriptions, groups

    @blueprint.get("/api/investigate/components")
    def investigate_components():
        subscriptions, groups = investigate_scope()
        return jsonify(investigate.components(client(), subscriptions, rg_clause(groups),
                                              measure_now=request.args.get("measure") != "0"))

    @blueprint.get("/api/investigate/measure")
    def investigate_measure():
        """The last hour of up to MEASURE_BATCH components, for the start list to fill in as each batch answers."""
        ids = list(dict.fromkeys(request.args.getlist("id")))
        if not ids or len(ids) > investigate.MEASURE_BATCH or not all(investigate.COMPONENT_ID.fullmatch(i) for i in ids):
            abort(400, f"id must be 1 to {investigate.MEASURE_BATCH} Application Insights component IDs")
        return jsonify(components=investigate.measure(client(), [{"id": i} for i in ids], settings["appinsights"]))

    @blueprint.get("/api/investigate/gateways")
    def investigate_gateways():
        """API Management services in scope and where each logs (reads each service's loggers and diagnostics)."""
        subscriptions, _ = investigate_scope()
        return jsonify(inv_gateway.gateways(client(), subscriptions))

    @blueprint.get("/api/investigate/dependencies")
    def investigate_dependencies():
        source = investigate_source()
        return investigate_answer(lambda: source.dependencies(request.args.get("window", "1h")))

    @blueprint.get("/api/investigate/recent-changes")
    def investigate_recent_changes():
        return investigate_answer(lambda: investigate.recent_changes(client(), request.args.getlist("id"),
                                                                     request.args.get("window", "1h")))

    @blueprint.get("/api/investigate/exceptions")
    def investigate_exceptions():
        source = investigate_source()
        return investigate_answer(lambda: source.exceptions(request.args.get("window", "1h")))

    @blueprint.get("/api/investigate/operations")
    def investigate_operations():
        source = investigate_source()
        return investigate_answer(lambda: source.operations(request.args.get("window", "1h")))

    @blueprint.get("/api/investigate/samples")
    def investigate_samples():
        source = investigate_source()
        return investigate_answer(lambda: source.samples(request.args.get("operation", ""), request.args.get("role", ""),
                                                         request.args.get("window", "1h"),
                                                         failed=request.args.get("all") != "1",
                                                         code=request.args.get("code") or None))

    @blueprint.get("/api/investigate/codes")
    def investigate_codes():
        source = investigate_source()
        return investigate_answer(lambda: source.codes(request.args.get("window", "1h")))

    @blueprint.get("/api/investigate/trace")
    def investigate_trace():
        source, at = investigate_source(), investigate_at(required=False)
        return investigate_answer(lambda: source.trace(request.args.get("trace", "").strip(), at))

    @blueprint.get("/api/investigate/search")
    def investigate_search():
        source = investigate_source()
        severity = request.args.get("severity", "")
        if severity not in ("", "1", "2", "3", "4"):
            abort(400, "severity must be 1 to 4")
        return investigate_answer(lambda: source.search(request.args.get("text", "").strip()[:200], severity,
                                                        request.args.get("window", "1h")))

    @blueprint.get("/api/investigate/gateway-logs")
    def investigate_gateway_logs():
        """Gateway log rows around a request's time with its path: probable, since they share no ID with it."""
        service_id, at = _resource_id(), investigate_at()
        if f"/providers/{inv_gateway.APIM_TYPE}/" not in service_id.lower():
            abort(400, "id must be an API Management service")
        dedicated = {"1": True, "0": False}.get(request.args.get("dedicated", ""))
        return investigate_answer(lambda: inv_gateway.logs(client(), service_id, at, request.args.get("path", ""), dedicated))

    @blueprint.get("/api/investigate/pod")
    def investigate_pod():
        subscriptions, _ = investigate_scope()
        at = investigate_at()
        return investigate_answer(lambda: inv_pod.find(client(), subscriptions, request.args.get("instance", "").strip(), at))

    @blueprint.get("/api/investigate/pod-logs")
    def investigate_pod_logs():
        cluster_id, at = _resource_id(), investigate_at()
        return jsonify(aks.logs(client(), cluster_id, request.args.get("namespace", ""), request.args.get("pod") or None,
                                request.args.get("container") or None, request.args.get("lines", 200),
                                request.args.get("source", "auto"), settings["aks"], around=at, spread=log_spread(),
                                previous=request.args.get("previous") == "1"))

    def monitor_cluster_read(capability, what, default_hours, read):
        """A cluster read from the scope's monitoring connection (`connection=`) by capability, whichever tool it is:
        (monitor, source, hours, found) or (None, None, None, the answer to send)."""
        resource_id = _resource_id()
        hours = request.args.get("ci_hours") or str(default_hours)
        if not hours.isdigit() or int(hours) not in aks.CI_HOURS:
            abort(400, f"ci_hours must be one of {list(aks.CI_HOURS)}")
        monitor, blocked = chosen_monitor()
        if blocked:
            return None, None, None, jsonify(blocked)
        source = monitors.provider_for(monitor)
        if source is None or capability not in source.capabilities:
            return None, None, None, jsonify(available=False, reason=f"{monitor['name']} can't give a cluster's {what} here yet.")
        try:
            found = read(source, resource_id.rstrip("/").split("/")[-1], int(hours),
                         bool(getattr(client(), "is_fresh", False)))
        except ServiceError as error:
            return None, None, None, jsonify(available=False, reason=str(error).splitlines()[0][:400])
        return monitor, source, int(hours), found

    @blueprint.get("/api/explore/oom-kills-monitor")
    def explore_oom_kills_monitor():
        """A cluster's OOM kills from the scope's monitoring connection (`connection=`), for clusters whose Container
        Insights has none: the same rows and causes as Workloads' OOMKilled."""
        monitor, source, hours, found = monitor_cluster_read(
            OOM_KILLS, "OOM kills", settings["aks"]["oom_look_back_hours"],
            lambda source, cluster, hours, fresh: source.oom_kills(cluster, hours, refresh=fresh))
        if monitor is None:
            return found
        rows = aks.oom_rows(found["rows"], aks.oom_thresholds(settings["aks"]))
        by_workload = [{"namespace": o["Namespace"], "workload": workload_problems.workload_of(o["OwnerKind"], o["Owner"]),
                        "pods": o["Pods"], "kills": o["Kills"]} for o in found.get("by_owner") or []]
        return jsonify(available=True, connection=monitor["name"], label=source.label, hours=hours,
                       found=found["found"], rows=rows, total=found.get("total", len(rows)), by_workload=by_workload,
                       detailed=found.get("detailed", len(rows)), queries=found["queries"], notes=found["notes"])

    @blueprint.get("/api/explore/warning-events-monitor")
    def explore_warning_events_monitor():
        """A cluster's Kubernetes Warning events from the scope's monitoring connection, any tool with WARNING_EVENTS:
        rows shaped like Container Insights' warning events."""
        monitor, source, hours, found = monitor_cluster_read(
            WARNING_EVENTS, "warning events", settings["aks"].get("events_look_back_hours", 1),
            lambda source, cluster, hours, fresh: source.warning_events(cluster, hours, refresh=fresh))
        if monitor is None:
            return found
        return jsonify(available=True, connection=monitor["name"], label=source.label, hours=hours, found=found["found"],
                       rows=found["rows"], total=found.get("total", len(found["rows"])), queries=found["queries"],
                       notes=found["notes"])

    # ---------------------------------------------------------------- workload path (decision 150)

    apim_apis = TtlCache("API Management APIs", 3600)

    def path_workload(source):
        """The workload a path request names: an AKS one (cluster, namespace, kind, workload) or an App Service (site).
        {key, name, cluster_id, namespace, kind} or {key, name, site_id}; 400 when it isn't one."""
        site = (source.get("site") or "").rstrip("/")
        if site:
            if not workload_path.SITE_ID.match(site):
                abort(400, "site must be an App Service ID")
            return {"key": site.lower(), "name": site.rsplit("/", 1)[-1], "site_id": site}
        cluster, namespace = (source.get("cluster") or "").rstrip("/"), source.get("namespace") or ""
        kind, name = source.get("kind") or "", source.get("workload") or ""
        try:
            workload_path.check_workload(cluster, namespace, kind, name)
        except ValueError as error:
            abort(400, str(error))
        return {"key": workload_path.workload_key(cluster, namespace, kind, name), "name": name, "cluster_id": cluster,
                "namespace": namespace, "kind": kind}

    def path_tool():
        """(the Scope bar's monitoring tool as a provider, its capabilities, why not) for this request."""
        monitor, blocked = chosen_monitor()
        source = None if blocked else monitors.provider_for(monitor)
        return source, (getattr(source, "capabilities", ()) if source else ()), (blocked or {}).get("reason", "")

    def saved_link(workload):
        tenant, _ = tenant_of(client())
        return next((x for x in config.workload_links(settings, tenant) if x["key"].lower() == workload["key"]), None)

    @blueprint.get("/api/path/candidates")
    def path_candidates():
        """What could be this workload's front doors, app and component, best first with why; and its saved link."""
        workload, arm_client = path_workload(request.args), client()
        subscriptions, _ = scope_args(request.args)
        source, can, tool_reason = path_tool()
        paths, apps, notes = [], [], []
        if source is not None and REQUESTS in can:
            try:
                names = source.app_names()["names"]
                apps = workload_path.app_candidates(names, workload["name"])
                picked = (saved_link(workload) or {}).get("app") or (apps[0]["app"] if apps and apps[0]["score"] >= 10 else "")
                paths = source.request_paths(picked)["paths"] if picked else []
            except ServiceError as error:
                notes.append(f"{source.label}: {str(error).splitlines()[0][:200]}")
        elif tool_reason:
            notes.append(f"Monitoring tool: {tool_reason}")
        site_hosts, component = [], []
        if workload.get("site_id"):
            site = arm_client.get(workload["site_id"], "2023-12-01", tolerate=(403, 404)) or {}
            site_hosts = (site.get("properties") or {}).get("hostNames") or []
            found = appinsights.link(arm_client, workload["site_id"])
            if found.get("component_id"):
                component = [{"component": found["component_id"], "name": found["component_name"], "score": 10,
                              "why": ["the app reports to it"]}]
        apim, apim_notes = workload_path.apim_candidates(arm_client, subscriptions, workload["name"], paths, apim_apis)
        agw, agw_notes = workload_path.agw_candidates(arm_client, subscriptions, workload["name"],
                                                      workload.get("cluster_id", ""), site_hosts)
        components = component or workload_path.component_candidates(arm_client, subscriptions, workload["name"])
        return jsonify(workload=workload, link=saved_link(workload), apim=apim, agw=agw, apps=apps, components=components,
                       request_paths=paths[:10], tool=getattr(source, "label", ""), notes=notes + apim_notes + agw_notes)

    @blueprint.post("/api/path/link")
    def path_link_save():
        """Saves the front doors the viewer confirmed for one workload (an empty pick removes the link)."""
        body = request.get_json(silent=True) or {}
        workload = path_workload(body)
        link = {**{k: v for k, v in workload.items()}, "apim": [{"service_id": a.get("service_id", ""), "api": a.get("api", "")}
                                                                for a in body.get("apim") or [] if isinstance(a, dict)],
                "agw": [{"gateway_id": g.get("gateway_id", ""), "listener": g.get("listener", ""), "pool": g.get("pool", "")}
                        for g in body.get("agw") or [] if isinstance(g, dict)],
                "app": str(body.get("app") or ""), "component": str(body.get("component") or "")}
        return save_for_tenant(settings_file.apply_workload_link, link) or jsonify(link=saved_link(workload))

    @blueprint.get("/api/path/read")
    def path_read():
        """Each hop of the workload's saved link (or just its pods and requests when none is saved), and the verdict."""
        workload = path_workload(request.args)
        link = {**workload, **(saved_link(workload) or {})}
        source, can, _ = path_tool()
        return jsonify(**workload_path.read(client(), link, source, can, settings["path"], settings["appinsights"]),
                       link=link, tool=getattr(source, "label", ""))

    @blueprint.get("/api/workload/detail")
    def workload_detail_read():
        """One workload's details from the Kubernetes API, read when its line is opened on the Workloads tab."""
        workload = path_workload(request.args)
        if "cluster_id" not in workload:
            abort(400, "cluster, namespace, kind and workload are needed")
        return jsonify(workload_detail.read(client(), workload["cluster_id"], workload["namespace"], workload["kind"],
                                            workload["name"], kube_settings=settings["aks"]))

    @blueprint.get("/api/pod/detail")
    def pod_detail_read():
        """One pod as `kubectl describe pod` and `-o yaml` show it: the Kubernetes API, else Container Insights."""
        cluster = (request.args.get("cluster") or "").rstrip("/")
        if not workload_path.CLUSTER_ID.match(cluster):
            abort(400, "cluster must be an AKS cluster ID")
        try:
            return jsonify(pod_detail.read(client(), cluster, request.args.get("namespace", ""), request.args.get("pod", ""),
                                           kube_settings=settings["aks"]))
        except ValueError as error:
            abort(400, str(error))

    @blueprint.get("/api/kube/yaml")
    def kube_yaml_read():
        """One object's YAML read now; env values only with values=1 (Show values), never in the page's first read."""
        cluster = (request.args.get("cluster") or "").rstrip("/")
        if not workload_path.CLUSTER_ID.match(cluster):
            abort(400, "cluster must be an AKS cluster ID")
        try:
            return jsonify(kube_yaml.read(client(), cluster, request.args.get("namespace", ""), request.args.get("kind", ""),
                                          request.args.get("name", ""), values=request.args.get("values") == "1",
                                          kube_settings=settings["aks"]))
        except ValueError as error:
            abort(400, str(error))

    @blueprint.get("/api/workload/history")
    def workload_history_read():
        """Its CPU and memory over 30 days and its runtime's memory; `target_cpu` and `target_memory` are its HPA's %."""
        workload = path_workload(request.args)
        if "cluster_id" not in workload:
            abort(400, "cluster, namespace, kind and workload are needed")
        targets = {}
        for resource in ("cpu", "memory"):
            value = request.args.get(f"target_{resource}", "")
            if value:
                if not value.isdigit() or not 0 < int(value) <= 1000:
                    abort(400, f"target_{resource} must be a whole percent")
                targets[resource] = int(value)
        link = {**workload, **(saved_link(workload) or {})}
        source, can, _ = path_tool()
        names = (lambda: source.app_names()["names"]) if source is not None and REQUESTS in can else None
        return jsonify(workload_detail.history(client(), link, targets, source, can, names))

    @blueprint.get("/api/workload/fit")
    def workload_fit_read():
        """Would new requests fit: `container` is `name:cpu millicores:memory MiB`, once per container changed;
        `replicas` replaces the HPA's arithmetic. Reads only (the nodes and every live pod on them)."""
        workload = path_workload(request.args)
        if "cluster_id" not in workload:
            abort(400, "cluster, namespace, kind and workload are needed")
        new = {}
        for item in request.args.getlist("container"):
            match = re.fullmatch(r"([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?):([0-9]{1,7}):([0-9]{1,8})", item)
            if not match or not int(match.group(3)) and not int(match.group(4)):
                abort(400, "container must be name:cpu millicores:memory MiB")
            new[match.group(1)] = (int(match.group(3)), int(match.group(4)))
        if not new:
            abort(400, "at least one container with its new requests is needed")
        replicas = request.args.get("replicas", "")
        if replicas and (not replicas.isdigit() or not 0 < int(replicas) <= 10000):
            abort(400, "replicas must be a whole number from 1 to 10000")
        return jsonify(workload_fit.read(client(), workload["cluster_id"], workload["namespace"], workload["kind"],
                                         workload["name"], new, int(replicas) if replicas else None, kube_settings=settings["aks"]))

    @blueprint.get("/api/explore/workload-names")
    def explore_workload_names():
        """One namespace's Deployments, StatefulSets and DaemonSets for Dependencies' pickers, when Workloads' cluster-wide
        lists couldn't be read: the Kubernetes API in that namespace, Container Insights, then the scope's monitoring
        connection (`connection=`, optional)."""
        resource_id = _resource_id()
        namespace = request.args.get("namespace", "")
        if not aks.NAMESPACE.fullmatch(namespace):
            abort(400, "namespace isn't a Kubernetes namespace name")
        arm_client = client()
        result = aks.namespace_workloads(arm_client, resource_id, namespace, kube_settings=settings["aks"])
        if result["workloads"] or not request.args.get("connection"):
            return jsonify(result)
        monitor, blocked = chosen_monitor()
        source = None if blocked else monitors.provider_for(monitor)
        if source is None or WORKLOADS not in source.capabilities:
            return jsonify(result)
        try:
            found = source.workload_names(resource_id.rstrip("/").split("/")[-1], namespace,
                                          refresh=bool(getattr(arm_client, "is_fresh", False)))
        except ServiceError as error:
            return jsonify({**result, "notes": result["notes"] + [str(error).splitlines()[0][:200]]})
        if found["found"]:
            return jsonify({"source": monitor["name"], "workloads": found["workloads"], "notes": result["notes"]})
        return jsonify({**result, "notes": result["notes"] + [f"{monitor['name']} has nothing for {namespace} in the last hour."]})

    @blueprint.get("/api/explore/dependencies")
    def explore_dependencies():
        """What an AKS workload depends on in Azure: host names in its env and ConfigMaps (Kubernetes API) and its
        workload identity's roles (Azure). `pod=`, or `kind=` and `workload=`; values are never returned."""
        cluster_id = _resource_id()
        subscriptions, _ = investigate_scope()
        pod, name = request.args.get("pod") or None, request.args.get("workload") or None
        if not (pod or name):
            abort(400, "pod or workload is required")
        workload = (request.args.get("kind") or "Deployment", name) if name and not pod else None
        return investigate_answer(lambda: dependencies.dependencies(
            client(), subscriptions, cluster_id, request.args.get("namespace", ""), pod, workload, settings["aks"]))

    @blueprint.get("/api/investigate/pod-oom")
    def investigate_pod_oom():
        cluster_id, at = _resource_id(), investigate_at()
        return investigate_answer(lambda: aks.oom_kills(client(), cluster_id, request.args.get("pod", ""), at, settings["aks"]))

    @blueprint.get("/api/investigate/changes")
    def investigate_changes():
        resource_id, at = _resource_id(), investigate_at()
        return investigate_answer(lambda: inv_pod.changes(client(), resource_id, at, request.args.get("namespace", "")))

    # ---------------------------------------------------------------- vulnerabilities

    def vuln_filters(args):
        try:
            cvss = float(args.get("cvss_min") or 0)
        except ValueError:
            abort(400, "cvss_min must be a number")
        return {"severity": args.getlist("severity"), "fixable": args.get("fixable") == "1", "cvss_min": cvss,
                "type": args.get("type") or "", "running": args.get("running") == "1", "q": (args.get("q") or "")[:300]}

    def vuln_scope():
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        return subscriptions, groups

    @blueprint.get("/api/vulnerabilities")
    def vulnerabilities_view():
        """Defender's findings in scope (summary, resources by priority, software), and what isn't scanned."""
        subscriptions, groups = vuln_scope()
        arm_client = client()
        result = vulnerabilities.findings(arm_client, subscriptions, groups, vuln_filters(request.args))
        result["coverage"] = vulnerabilities.coverage(arm_client, subscriptions, groups)
        return jsonify(result)

    @blueprint.get("/api/vulnerabilities/headline")
    def vulnerabilities_headline():
        """Summary's line: the scope's totals and its top resources."""
        subscriptions, groups = vuln_scope()
        return jsonify(vulnerabilities.headline(client(), subscriptions, groups, settings["summary"]["top_items"]))

    @blueprint.get("/api/vulnerabilities/platform")
    def vulnerabilities_platform():
        subscriptions, groups = vuln_scope()
        return jsonify(cards=vulnerabilities.platform(client(), subscriptions, groups, settings=settings))

    @blueprint.get("/api/vulnerabilities/software")
    def vulnerabilities_software():
        subscriptions, groups = vuln_scope()
        name = request.args.get("name", "")
        if not name:
            abort(400, "name is required")
        return jsonify(vulnerabilities.for_software(client(), subscriptions, name, groups, vuln_filters(request.args)))

    @blueprint.get("/api/vulnerabilities/image-tags")
    def vulnerabilities_image_tags():
        """Which tags point at one vulnerable image, asked of the registry itself. A refusal is an answer, not an error:
        it names the role that reads tags, so it works once that role is granted or activated through PIM."""
        registry_id = request.args.get("registry", "")
        repository, digest = request.args.get("repository", ""), request.args.get("digest", "")
        arm_client = client()
        rows = arm_client.query("Resources | where tolower(id) == {target} | project loginServer=tostring(properties.loginServer),"
                                " pna=tostring(properties.publicNetworkAccess)".format(target=json.dumps(registry_id.lower())))
        if not rows or not rows[0].get("loginServer"):
            return jsonify(error="That registry isn't visible to you in Azure, so its address isn't known.", kind="access",
                           required="Reader on the registry", registry_id=registry_id)
        provider = getattr(arm_client, "token_provider", None)
        if provider is None:
            return jsonify(error="This session has no Azure CLI sign-in to sign in to the registry with.", kind="other")
        try:
            return jsonify(acr.tags(provider, rows[0]["loginServer"], repository, digest, arm_client.identity.tenant_id))
        except acr.RegistryError as error:
            note = (" The registry's public network access is off, so only its private endpoint answers."
                    if error.kind == "network" and (rows[0].get("pna") or "").lower() == "disabled" else "")
            return jsonify(error=str(error) + note, kind=error.kind, required=error.required, registry_id=registry_id)

    @blueprint.get("/api/vulnerabilities/running")
    def vulnerabilities_running():
        """The image (with its tag) each vulnerable container in one AKS cluster is running, from the pods' specs."""
        cluster_id = _resource_id()
        arm_client = client()
        refs = [f["image"] for f in vulnerabilities.for_resource(arm_client, cluster_id)["findings"] if f["image"].get("container")]
        if not refs:
            return jsonify(containers={}, note="No finding here names a container this console can find in the cluster.")
        try:
            kube_client = kube.client_for(arm_client, cluster_id, settings["aks"])
            return jsonify(containers=vulnerabilities.running_images(kube_client, refs), source=kube_client.source)
        except AccessDenied as error:
            return jsonify(containers={}, error=str(error).splitlines()[0], kind="access", required=error.role,
                           cluster_id=cluster_id)
        except kube.KubeUnavailable as error:
            unreachable = "isn't reachable" in str(error)
            return jsonify(containers={}, error=str(error), kind="network" if unreachable else "access",
                           required="" if unreachable else access_roles.KUBERNETES_ROLE, cluster_id=cluster_id)
        except ArmError as error:
            return jsonify(containers={}, error=str(error).splitlines()[0], kind="other", cluster_id=cluster_id)

    @blueprint.get("/api/vulnerabilities/export")
    def vulnerabilities_export():
        subscriptions, groups = vuln_scope()
        arm_client = client()
        filters = vuln_filters(request.args)
        found = vulnerabilities.findings(arm_client, subscriptions, groups, filters)
        cards = vulnerabilities.platform(arm_client, subscriptions, groups, settings=settings)
        gaps = vulnerabilities.coverage(arm_client, subscriptions, groups)
        names = sub_names(arm_client)
        s = found["summary"]
        sections = [
            {"heading": f"Resources by priority ({len(found['resources'])})",
             "columns": ["Resource", "Type", "Resource group", "Subscription", "Findings", "High", "Medium", "Low",
                         "Worst CVSS", "With a fix", "Running", "Reachable"],
             "rows": [[r["name"], r["rtype"], r["resourceGroup"], names.get(r["subscriptionId"].lower(), r["subscriptionId"]),
                       r["findings"], r["high"], r["medium"], r["low"], r["maxCvss"], r["fixable"],
                       "yes" if r["live"] else "", ", ".join(r["exposure"])] for r in found["resources"]]},
            {"heading": f"Software ({len(found['software'])})",
             "columns": ["Software", "Resources", "Findings", "High", "Worst CVSS", "Fixed in", "Types"],
             "rows": [[r["software"], r["resources"], r["findings"], r["high"], r["maxCvss"], ", ".join(r["fixedIn"]),
                       ", ".join(r["types"])] for r in found["software"]]},
        ]
        for card in cards:
            sections.append({"heading": card["title"], "note": card.get("note") or card.get("error") or "",
                             "columns": ["Status", "Resource", "Type", "Resource group", "Detail"], "status_column": 0,
                             "rows": [[i["status"], i["name"], i["display_type"], i["resource_group"], i["detail"]]
                                      for i in card["items"]]})
        sections.append({"heading": "Not scanned for vulnerabilities", "columns": ["Machine", "Resource group", "Subscription"],
                         "note": "; ".join(f"{p['label']} is off in {len(p['subscriptions'])} subscription(s)"
                                           for p in gaps["plans_off"]),
                         "rows": [[m["name"], m["resource_group"], names.get(m["subscription_id"], m["subscription_id"])]
                                  for m in gaps["machines"]]})
        return export_table({
            "title": "Vulnerabilities",
            "subtitle": f"{s['findings']} open findings on {s['resources']} resources (High {s['high']}, Medium "
                        f"{s['medium']}, Low {s['low']}; {s['fixable']} with a fix), from Microsoft Defender for Cloud, plus "
                        "platform checks. Read-only, with your own access.",
            "sections": sections})

    @blueprint.get("/api/triage/appinsights")
    def triage_app_insights():
        subscriptions, groups = scope_args(request.args)
        if not subscriptions:
            abort(400, "Pick at least one subscription.")
        return jsonify(appinsights.spikes(client(), subscriptions, rg_clause(groups), settings["appinsights"]))

    @blueprint.get("/api/explore/<view>")
    def explore(view):
        arm_client, resource_id, args = client(), _resource_id(), request.args
        if view == "metric-definitions":
            return jsonify(metrics.definitions(arm_client, resource_id))
        if view == "metric-series":
            if not args.get("metric") or not args.get("aggregation"):
                abort(400, "metric and aggregation are required")
            return jsonify(metrics.series(arm_client, resource_id, args["metric"], args["aggregation"],
                                          args.get("range", "6h"), args.get("split") or None))
        if view == "servicenow":
            return servicenow_resource()
        if view == "monitors":
            return monitors_resource()
        if view == "activity":
            hours = args.get("hours", "24")
            if not hours.isdigit() or int(hours) not in activity.HOURS:
                abort(400, f"hours must be one of {list(activity.HOURS)}")
            result = activity.activity(arm_client, resource_id, int(hours))
            health = settings["health"]
            for alert in result["alerts"]:
                sev = alert.get("severity")
                alert["status"] = ("info" if sev is None else "critical" if sev <= health["critical_max_severity"]
                                   else "warning" if sev <= health["warning_max_severity"] else "info")
            return jsonify(result)
        if view == "security":  # its settings, and its vulnerability totals so a clean config isn't read as all clear
            out = security.for_resource(arm_client, resource_id, settings["security"]["risky_ports"])
            try:
                out["vulnerabilities"] = vulnerabilities.counts_for(arm_client, resource_id)
            except ArmError as error:
                out["vulnerabilities"] = {"error": str(error).splitlines()[0]}
            return jsonify(out)
        if view == "cost":
            return jsonify(cost.for_resource(arm_client, resource_id, cost_type=cost_type_arg()))
        if view in ("appinsights", "appinsights-now"):
            return app_insights_view(arm_client, resource_id, args, now_only=view == "appinsights-now")
        if view == "recommendations":
            return jsonify(advice.recommendations(arm_client, resource_id))
        if view == "vulnerabilities":
            return jsonify(resource_vulnerabilities(arm_client, resource_id))
        if view == "scaling":
            return jsonify(scaling.scaling(arm_client, resource_id))
        if view == "diagnostics":
            if args.get("detector"):
                return jsonify(detectors.run_one(arm_client, resource_id, args["detector"]))
            if args.get("plan") == "1":
                return jsonify(detectors.plan(arm_client, resource_id, only_health=args.get("all") != "1"))
            return jsonify(detectors.run(arm_client, resource_id, only_health=args.get("all") != "1"))
        if view == "guides":  # Microsoft's troubleshooting catalogue, one step per call: services, problems, solutions, guide
            for key in ("service", "problem", "guide"):
                if args.get(key) and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args[key]):
                    abort(400, f"{key} is not an ID")
            if args.get("guide"):
                return jsonify(help_guides.guide(arm_client, args["guide"]))
            if args.get("problem"):
                return jsonify({"solutions": help_guides.solutions(arm_client, args["problem"])})
            if args.get("service"):
                return jsonify({"problems": help_guides.problems(arm_client, args["service"])})
            return jsonify({"services": help_guides.services(arm_client, resource_id)})
        if view == "details":
            return jsonify(details.details(arm_client, resource_id, settings["health"]["expiry_warning_days"],
                                           settings["health"]["expiry_critical_days"]))
        if view == "workloads":
            ci_hours = args.get("ci_hours")
            if ci_hours is not None and (not ci_hours.isdigit() or int(ci_hours) not in aks.CI_HOURS):
                abort(400, f"ci_hours must be one of {list(aks.CI_HOURS)}")
            return jsonify(aks.workloads(arm_client, resource_id, include_system=args.get("system") == "1",
                                         kube_settings=settings["aks"], ci_hours=int(ci_hours) if ci_hours else None))
        if view == "nodes":
            return jsonify(aks.nodes(arm_client, resource_id, kube_settings=settings["aks"]))
        if view == "logs":
            return jsonify(aks.logs(arm_client, resource_id, args.get("namespace", ""), args.get("pod") or None,
                                    args.get("container") or None, args.get("lines", 200),
                                    args.get("source", "auto"), settings["aks"], around=investigate_at(required=False),
                                    spread=log_spread(), previous=args.get("previous") == "1"))
        abort(404)

    def resource_vulnerabilities(arm_client, resource_id):
        """One resource's Defender findings, plus the platform checks that apply to its type."""
        out = {**vulnerabilities.for_resource(arm_client, resource_id), "checks": [], "errors": []}
        if out["truncated"]:  # the list is capped: the heading still counts every finding, as Overview and Summary do
            out["totals"] = vulnerabilities.counts_for(arm_client, resource_id)
        lowered = resource_id.lower()
        sub, group = lowered.split("/")[2], lowered.split("/")[4]
        try:
            if "/providers/microsoft.web/sites/" in lowered:
                out["checks"] = vulnerabilities.site_runtime(arm_client, resource_id, eol_days=settings["vulnerabilities"]["eol_warning_days"])
            else:
                cards = vulnerabilities.platform(arm_client, [sub], [f"{sub}/{group}"], settings=settings)
                out["checks"] = [{"name": c["title"], "value": "", "status": i["status"], "detail": i["detail"]}
                                 for c in cards for i in c["items"] if i["id"].lower() == lowered]
        except ArmError as error:
            out["errors"].append(str(error).splitlines()[0])
        return out

    def _job(job_id):
        job = runner.get(job_id, owner(client()))
        if not job:
            abort(404)
        return job

    return blueprint


def add_signin(app, login):
    """Local only: the page's Sign in buttons run `az login` through `login`. A hosted app never calls this, so it has no
    /api/signin and the page hides the buttons."""

    @app.get("/api/signin")
    def signin_state():
        return jsonify(available=True, **login.state())

    @app.post("/api/signin")
    def signin_start():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(available=True, **login.start(str(body.get("tenant") or "").strip() or None,
                                                         body.get("device_code") is True))
        except SignInError as error:
            return jsonify(error=str(error)), 400

    @app.post("/api/signin/cancel")
    def signin_cancel():
        return jsonify(available=True, **login.cancel())

    @app.get("/api/signin/accounts")
    def signin_accounts():
        try:
            return jsonify(tenants=signin_accounts_read())
        except (SignInError, ValueError, OSError, subprocess.SubprocessError) as error:
            return jsonify(tenants=[], error=str(error)[:300])

    @app.post("/api/signout")
    def signout():
        body = request.get_json(silent=True) or {}
        try:
            signin_logout(str(body.get("username") or "").strip())
        except (SignInError, OSError, subprocess.SubprocessError) as error:
            return jsonify(error=str(error)[:300]), 400
        login.refresh_clients()
        return jsonify(ok=True)


LAUNCH_HEADER = "X-AHC-Key"
LAUNCH_COOKIE = "ahc_key"  # 1.1.x kept the key in a cookie; a browser still holding it is handed the key once
NO_KEY_TEXT = ("Open the console from the address azure-health-console printed when it started: it carries a key, so "
               "only your browser can use it. Once opened that way, this address keeps working in this browser.")


LAUNCH_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _launch_key(path):
    """This user's key: read from `path`, or made and saved there the first time. Only a private regular file of this
    user's in a folder only they can write is trusted; anything else is replaced. Unsaveable: a key for this start."""
    try:
        private_dir(path.parent)
        key = read_private_text(path).strip()
        if LAUNCH_KEY_SHAPE.match(key):
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            path.unlink()
        write_private_text(path, key)
    except OSError:
        pass
    return key


def launch_url(app, port):
    """The address to open: the key is in the fragment, which the browser never sends; the page keeps it and cleans
    the address."""
    return f"http://127.0.0.1:{port}/#key={app.config['AHC_LAUNCH_KEY']}"


def create_local_app(settings, port, config_file=None, start_alerts=False):
    app = Flask(__name__)
    clients = {}
    lock = threading.Lock()

    def client_for(tenant_id):
        key = (tenant_id or settings.get("default_tenant") or "").lower()
        with lock:
            if key not in clients:
                clients[key] = ArmClient(AzureCliTokenProvider(tenant_id=key or None))
            return clients[key]

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    # Only a page holding this key (from `launch_url`, once) can use the console, not another user or program on
    # 127.0.0.1. Kept in a private file so bookmarks survive restarts; the page sends it as `LAUNCH_HEADER`.
    launch_key = _launch_key(Path(settings["cache_dir"]).expanduser() / "launch-key")
    app.config["AHC_LAUNCH_KEY"] = launch_key

    @app.before_request
    def launch_key_holder():
        # Blocks DNS rebinding: a page elsewhere can't reach this server under another name.
        if request.host not in allowed_hosts:
            abort(403)
        if hmac.compare_digest(request.headers.get(LAUNCH_HEADER, ""), launch_key):
            refuse_other_sites("/api/")  # the sign-in routes are the app's own, not the blueprint's
            return None
        if request.path == "/":
            if hmac.compare_digest(request.cookies.get(LAUNCH_COOKIE, ""), launch_key):
                response = redirect(f"/#key={launch_key}")
                response.delete_cookie(LAUNCH_COOKIE, path="/")
                return response
            return None  # the page carries no data; without a key its first call gets the message below
        if request.method == "GET" and request.path.startswith(LOCAL_OPEN_PATHS):
            return None  # static files; ServiceNow's sign-in comes back here and is checked by its one-time state
        if request.path.startswith("/api/"):  # another program or browser without the key: worth knowing
            eventlog.audit("no_launch_key", kind="refused", outcome=401, detail={"path": request.path})
        return jsonify(error=NO_KEY_TEXT, kind="launch-key"), 401

    @app.after_request
    def audit_signin(response):
        if request.method == "POST" and request.endpoint in ("signin_start", "signin_cancel", "signout"):
            eventlog.audit(request.endpoint, kind="change", outcome=response.status_code)
        return response

    add_signin(app, AzLogin(on_success=lambda: [c.refresh() for c in list(clients.values())]))
    app.register_blueprint(create_blueprint(client_for, settings, config_file=config_file, start_alerts=start_alerts,
                                            authorize=lambda: None))  # every request already passed launch_key_holder
    return app
