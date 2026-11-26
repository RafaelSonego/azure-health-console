"""ServiceNow and monitoring-tool connections. `status` says, in plain words, what's missing for each one
(a field, a secret, a sign-in), so the page can always tell the user why something isn't shown."""
from . import newrelic, secrets, servicenow

MONITOR_SECRETS = {"newrelic": ("api_key",), "datadog": ("api_key", "app_key"), "grafana": ("token",)}
SECRET_LABELS = {"password": "password", "client_secret": "client secret", "refresh_token": "ServiceNow sign-in",
                 "api_key": "API key", "app_key": "application key", "token": "service account token"}
SUPPORTED_MONITORS = {"newrelic"}  # Datadog and Grafana are configurable now and connect in a later version


# What the page shows instead of a connection's data: {"available": False, reason, problems, fix}. ServiceNow and every
# monitoring tool answer the same way, so the page has one way to show it.
def unavailable(name, problems, enabled=True):
    """None when the connection can be read; else why not, from its status' problems."""
    if not enabled:
        return {"available": False, "reason": f"{name} is turned off.", "problems": [],
                "fix": "Turn it on in Settings → Integrations."}
    if problems:
        return {"available": False, "reason": f"{name} isn't fully configured.", "problems": problems,
                "fix": "Complete it in Settings → Integrations, then Test connection."}
    return None


def failed(error):
    """A ServiceError (guarded.py) as that answer."""
    fix = ("Check it with Test connection in Settings → Integrations."
           if error.kind in ("auth", "config", "unreachable", "access") else "")
    return {"available": False, "reason": str(error), "problems": [], "fix": fix}


def bind(integrations, tenant_id):
    """One tenant's integrations, each monitor carrying its tenant (where its key is kept). Never saved."""
    return {"servicenow": integrations["servicenow"],
            "monitors": [{**m, "tenant": tenant_id} for m in integrations["monitors"]]}


def monitor_scope(monitor):
    """Keychain scope of a monitor's key: its tenant, so two tenants' "New Relic" connections keep their own."""
    return monitor.get("tenant") or monitor["type"]


def move_monitor_secrets(monitors, tenant_id):
    """Moves keys stored before integrations were per tenant (scoped by tool) to this tenant."""
    for monitor in monitors:
        target = f"monitor:{monitor['name']}"
        for field in MONITOR_SECRETS[monitor["type"]]:
            value = secrets._from_keychain(target, monitor["type"], field)
            if value:
                secrets.put(target, tenant_id, field, value)
                secrets.delete(target, monitor["type"], field)


def _secret_status(target, scope, fields, typed=None):
    typed = typed or {}
    return {field: "typed" if typed.get(field) else secrets.source(target, scope, field) for field in fields}


def servicenow_status(cfg, typed=None):
    host = servicenow.host_of(cfg["instance"])
    fields = servicenow.SECRET_FIELDS[cfg["auth"]]
    stored = _secret_status(servicenow.TARGET, host, fields, typed) if host else {f: None for f in fields}
    problems = []
    if not cfg["instance"]:
        problems.append("The instance URL isn't set (e.g. https://yourcompany.service-now.com).")
    if cfg["auth"] == "basic" and not cfg["username"]:
        problems.append("The username isn't set.")
    if cfg["auth"] != "basic" and not cfg["client_id"]:
        problems.append("The OAuth client ID isn't set (from the OAuth app in ServiceNow's Application Registry).")
    for field in fields:
        if stored[field]:
            continue
        variable = secrets.env_name(servicenow.TARGET, field)
        if field == "refresh_token":
            problems.append("Not signed in yet: click Sign in with ServiceNow.")
        elif host and secrets.env_unbound(servicenow.TARGET, host, field):
            problems.append(f"{variable} is set, but it's sent only to the instance {secrets.SERVICENOW_INSTANCE_ENV} "
                            f"names ({secrets.env_instance() or 'not set'}). Set {secrets.SERVICENOW_INSTANCE_ENV}={host} "
                            "and restart the console, or type the secret here and Save.")
        else:
            problems.append(f"No {SECRET_LABELS[field]} yet: type it and Save, or set the {variable} environment "
                            f"variable with {secrets.SERVICENOW_INSTANCE_ENV}={host or 'your instance'}.")
    if not cfg["tables"]:
        problems.append("No tables are selected.")
    return {"kind": "servicenow", "name": cfg.get("name") or "ServiceNow", "enabled": cfg["enabled"],
            "ready": cfg["enabled"] and not problems,
            "problems": problems, "secrets": stored, "secret_fields": list(fields), "scope": host,
            "env": {f: secrets.env_name(servicenow.TARGET, f) for f in fields}}


def monitor_status(monitor, typed=None):
    kind = monitor["type"]
    target = f"monitor:{monitor['name']}"
    fields = MONITOR_SECRETS[kind]
    if kind == "grafana" and monitor.get("auth") == "azure":
        fields = ()  # Azure Managed Grafana signs in with your az login
    stored = _secret_status(target, monitor_scope(monitor), fields, typed)
    problems = []
    if kind not in SUPPORTED_MONITORS:
        problems.append(f"{kind.capitalize()} is saved but not connected yet: it arrives in a later version.")
    if kind == "newrelic" and not monitor.get("account_ids"):
        problems.append("No account IDs are set (New Relic → Administration → Access management → Accounts).")
    if kind == "datadog" and not monitor.get("site"):
        problems.append("The Datadog site isn't set (e.g. datadoghq.com or datadoghq.eu).")
    if kind == "grafana" and not monitor.get("url"):
        problems.append("The Grafana URL isn't set.")
    for field in fields:
        if not stored[field]:
            problems.append(f"No {SECRET_LABELS[field]} yet: type it and Save, or set the "
                            f"{secrets.env_name(target, field)} environment variable.")
    return {"kind": kind, "name": monitor["name"], "enabled": monitor.get("enabled", False),
            "ready": monitor.get("enabled", False) and not problems, "problems": problems, "secrets": stored,
            "secret_fields": list(fields), "scope": monitor_scope(monitor),
            "env": {f: secrets.env_name(target, f) for f in fields}}


def status(integrations):
    """`integrations`: one tenant's, from `bind`."""
    return {"keychain": secrets.keychain(), "servicenow": [servicenow_status(c) for c in integrations["servicenow"]],
            "monitors": [monitor_status(m) for m in integrations["monitors"]]}


def find_servicenow(integrations, name):
    return next((c for c in integrations["servicenow"] if c["name"].lower() == (name or "").lower()), None)


def find_monitor(integrations, name):
    return next((m for m in integrations["monitors"] if m["name"].lower() == (name or "").lower()), None)


def secret_target(integrations, target):
    """(target, scope, allowed fields) for "servicenow:<name>" or "monitor:<name>"; ValueError when unknown."""
    if target.startswith("servicenow:"):
        cfg = find_servicenow(integrations, target.split(":", 1)[1])
        if not cfg:
            raise ValueError("Save the connection first, then store its secret.")
        host = servicenow.host_of(cfg["instance"])
        if not host:
            raise ValueError("Set and save the instance URL first: secrets are stored per instance.")
        fields = {f for fields in servicenow.SECRET_FIELDS.values() for f in fields} - {"refresh_token"}
        return servicenow.TARGET, host, fields
    if target.startswith("monitor:"):
        monitor = find_monitor(integrations, target.split(":", 1)[1])
        if not monitor:
            raise ValueError("Save the connection first, then store its key.")
        return f"monitor:{monitor['name']}", monitor_scope(monitor), set(MONITOR_SECRETS[monitor["type"]])
    raise ValueError(f"Unknown integration '{target}'.")


def test(integrations, target, values=None, typed=None):
    """Tests what's on the card (`values` and `typed` secrets override what's saved), saving nothing.
    The result always carries a message a person can act on."""
    typed = {k: v for k, v in (typed or {}).items() if isinstance(v, str) and v.strip()}
    if target.startswith("servicenow:"):
        saved = find_servicenow(integrations, target.split(":", 1)[1])
        if not saved:
            return {"ok": False, "message": "Save the connection first."}
        cfg = {**saved, **(values or {}), "name": saved["name"]}
        cfg["instance"] = servicenow.normalise_instance(cfg["instance"])
        state = servicenow_status(cfg, typed)
        # Saved and environment secrets go only to the instance they were saved for; another one needs them typed.
        moved = servicenow.host_of(cfg["instance"]) != servicenow.host_of(servicenow.normalise_instance(saved.get("instance") or ""))
        untyped = [f for f in state["secret_fields"] if not typed.get(f)] if moved and cfg["auth"] != "oauth_browser" else []
        if untyped:
            return {"ok": False, "message": f"Can't test yet: to test {state['scope'] or 'a different instance'}, type its "
                    f"{' and '.join(f.replace('_', ' ') for f in untyped)} here. Saved and environment secrets are only "
                    "sent to the instance they were saved for (save first to use them)."}
        problems = [p for p in state["problems"] if "Not signed in" not in p or cfg["auth"] != "oauth_browser"]
        if problems:
            return {"ok": False, "message": "Can't test yet: " + " ".join(problems)}
        try:
            result = servicenow.ServiceNowClient(cfg, typed=typed).test()
        except servicenow.ServiceNowError as error:
            return {"ok": False, "message": str(error)}
        readable = [t["name"] for t in result["tables"] if t["ok"]]
        who = f" as {result['who']}" if result["who"] else ""
        message = (f"Connected to {state['scope']}{who}. Readable: {', '.join(readable)}." if readable
                   else f"Signed in to {state['scope']}{who}, but none of the tables are readable. "
                        + servicenow.no_access(servicenow.ServiceNowClient(cfg).who, [t["name"] for t in result["tables"]]))
        return {"ok": result["ok"], "message": message, "details": result["tables"]}
    saved = find_monitor(integrations, target.split(":", 1)[1] if target.startswith("monitor:") else "")
    if not saved:
        return {"ok": False, "message": "Save the connection first."}
    monitor = {**saved, **(values or {}), "name": saved["name"], "type": saved["type"]}
    state = monitor_status(monitor, typed)
    if state["problems"]:
        return {"ok": False, "message": "Can't test yet: " + " ".join(state["problems"])}
    try:
        result = newrelic.NewRelicClient(monitor, typed=typed).test()
    except newrelic.NewRelicError as error:
        return {"ok": False, "message": str(error)}
    missing = [str(a["id"]) for a in result["accounts"] if not a["ok"]]
    who = f" as {result['who']}" if result["who"] else ""
    message = f"Connected to New Relic{who}. " + (
        f"These account IDs aren't visible to this key: {', '.join(missing)}." if missing
        else f"{len(result['accounts'])} account(s) readable.")
    return {"ok": result["ok"], "message": message,
            "details": [{"name": f"{a['id']}", "ok": a["ok"], "detail": a["detail"]} for a in result["accounts"]]}
