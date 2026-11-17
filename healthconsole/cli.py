"""`azure-health-console serve | export | readiness | bench | config | profile-draft | alert-check | whoami`."""
import argparse
import json
import logging
import socket
import sys
import threading
import webbrowser
from . import __version__
from pathlib import Path

from .core import config, health, profiles, readiness, report, snapshot, tables, yamlio
from .core.arm import ArmClient, ArmError
from .core.credentials import AzureCliTokenProvider, CredentialError
from .core.guarded import ServiceError
from .core.discovery import DiscoveryOptions
from .core.health import HealthOptions


def _client(args, settings):
    tenant = args.tenant or settings.get("default_tenant")
    return ArmClient(AzureCliTokenProvider(tenant_id=tenant))


def _free_port(preferred):
    """The preferred port if it's free on 127.0.0.1, otherwise any free one."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("No free port on 127.0.0.1.")


def cmd_serve(args, settings):
    from .web import create_local_app, launch_url

    port = _free_port(args.port)
    if port != args.port:
        print(f"Port {args.port} is busy, using {port}.")
    app = create_local_app(settings, port, args.config, start_alerts=True)
    url = launch_url(app, port)
    print(f"Azure Health Console on {url}  (read-only; open this address once per browser, then http://127.0.0.1:{port} works)")
    print("Keep this window open while you use it; close it (or press Ctrl+C) to stop.")
    if args.open:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


def cmd_export(args, settings):
    client = _client(args, settings)
    graph = dict(settings["graph"])
    if args.depth is not None:
        graph["depth"] = args.depth
    if args.no_inbound:
        graph["inbound"] = False
    result = snapshot.build(
        client, args.resource_id, DiscoveryOptions(**graph), HealthOptions(**settings["health"]),
        profiles.load(settings.get("profiles_dir")),
        progress=lambda message: print(f"  {message}…", file=sys.stderr),
    )
    out_dir = Path(args.out or settings["exports_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = report.FORMATS if args.format == "both" else (args.format,)
    for fmt in formats:
        content = report.render(result, fmt, settings.get("pdf_font"))
        target = out_dir / report.filename(result, fmt)
        target.write_bytes(content)
        print(target)
    counts = result.counts()
    print(f"{result.status}: {len(result.nodes)} resources, {counts['critical']} critical, "
          f"{counts['warning']} warning, {counts['unknown']} unknown", file=sys.stderr)


def cmd_readiness(args, settings):
    """The pre-peak readiness report for whole subscriptions: the ones named, a saved scope, else what you open with."""
    from .core import inventory

    try:
        peak_end = readiness.parse_peak_end(args.peak_end)
    except ValueError as error:
        sys.exit(f"error: {error}")
    client = _client(args, settings)
    identity = client.identity
    tenant = next((t for t in inventory.list_tenants(client) if t["id"] == identity.tenant_id), {"id": identity.tenant_id})
    subscriptions = inventory.list_subscriptions(client)
    if args.subscription:
        chosen = [s["id"] for s in subscriptions if config.matches([p.lower() for p in args.subscription], s["name"], s["id"])]
    else:
        try:
            _label, chosen = config.chosen_subscriptions(settings, tenant, subscriptions, args.scope)
        except config.ConfigError as error:
            sys.exit(f"error: {error}")
    if not chosen:
        sys.exit("error: no subscriptions: pass --subscription (name, ID or pattern) or --scope (a saved scope), "
                 "or set Open with to a saved scope (Settings → General).")
    print(f"Readiness for {len(chosen)} subscription(s) as {identity.name} (read-only)…", file=sys.stderr)
    options = {**settings["health"], **settings["network"]}
    result = readiness.run(client, chosen, options, peak_end, deep=not args.no_deep,
                           include_secrets=args.include_secrets, kube_settings=settings["aks"],
                           progress=lambda message: print(f"  {message}…", file=sys.stderr))
    doc = readiness.to_doc(result, {s["id"]: s["name"] for s in subscriptions}, include_healthy=not args.problems_only)
    out_dir = Path(args.out or settings["exports_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    for fmt in (("md", "pdf") if args.format == "both" else (args.format,)):
        target = out_dir / tables.filename(doc, fmt)
        target.write_bytes(tables.render(doc, fmt, settings.get("pdf_font")))
        print(target)
    c = result["counts"]
    print(f"{c['critical']} critical, {c['warning']} warning, {c['healthy']} healthy", file=sys.stderr)


def cmd_profile_draft(args, settings):
    """A starting profile from the resource's own metric definitions; thresholds left for you."""
    client = _client(args, settings)
    rows = client.query(f"Resources | where tolower(id) == {json.dumps(args.resource_id.lower())}"
                        " | project type=tolower(type)")
    if not rows:
        sys.exit("That resource can't be read with this session.")
    definitions = health._metric_definitions(client, args.resource_id)
    picked = health._heuristic_checks(definitions, limit=12)
    draft = {"type": rows[0]["type"], "metrics": [
        {"name": check.spec.name, "aggregation": check.spec.aggregation} for check in picked
    ]}
    print(f"# Draft profile. Add operator/warn/crit, then save under {settings['profiles_dir']}/")
    print(f"# All {len(definitions)} metrics: " + ", ".join(sorted(d["name"] for d in definitions.values())))
    print(yamlio.dump(draft), end="")


def cmd_config(args, settings):
    """What the config resolves to for the signed-in tenant: file, matched entry, saved scopes, Open with."""
    from .core import inventory

    file = Path(args.config).expanduser() if args.config else config.path()
    print(f"Config file: {file} ({'found' if file.is_file() else 'not found: built-in defaults in use'})")
    client = _client(args, settings)
    identity = client.identity
    tenants = inventory.list_tenants(client)
    tenant = next((t for t in tenants if t["id"] == identity.tenant_id), {"id": identity.tenant_id})
    print(f"Signed in as {identity.name}, tenant {tenant.get('name', '')} ({tenant['id']}, {tenant.get('domain', '')})")
    keys = config.entry_keys(settings, tenant, wildcard=True)
    print(f"Tenant entry used: {keys[0] if keys else 'none'}")
    subscriptions = inventory.list_subscriptions(client)
    scopes = config.saved_scopes(settings, tenant)
    print(f"Saved scopes: {len(scopes) or 'none'}")
    for scope in scopes:
        matched = config.scope_subscription_ids(scope, subscriptions)
        print(f"  {scope['name']}{' (from your old favourites: Settings → General → Move)' if scope.get('legacy') else ''}:"
              f" {len(matched)} of {len(subscriptions)} subscriptions"
              + (f", groups {scope['resource_groups']}" if scope["resource_groups"] else "")
              + (f", tags {scope['tag_filters']}" if scope["tag_filters"] else ""))
        if not matched:
            print("    Nothing matched. Patterns compare against the subscription name or ID, case-insensitive;"
                  " use * for any text, e.g. 'app-*-002'.")
    opened = config.open_with(settings, tenant)
    exists = opened in config.OPEN_WITH or config.find_scope(settings, tenant, opened)
    print(f"Open with: {opened}" + ("" if exists else " (no saved scope has that name: it opens with nothing)"))
    legacy = config.legacy_scope(settings, tenant)
    if legacy:
        print(f"Old settings still read: {', '.join(legacy['keys'])}. Settings → General → Move turns them into saved scopes.")
    print(f"Watchlist file: {Path(settings['watchlist_file']).expanduser()}")


def cmd_bench(args, settings):
    """Times each page action against the signed-in tenant and writes a Markdown report."""
    from .core import bench, inventory

    client = _client(args, settings)
    identity = client.identity
    tenant = next((t for t in inventory.list_tenants(client) if t["id"] == identity.tenant_id), {"id": identity.tenant_id})
    scenarios = [s.strip() for s in args.only.split(",")] if args.only else list(bench.DEFAULT_SCENARIOS)
    if args.secrets and "secrets" not in scenarios:
        scenarios.append("secrets")
    unknown = set(scenarios) - set(bench.SCENARIOS)
    if unknown:
        sys.exit(f"error: unknown scenario(s) {sorted(unknown)}; choose from {', '.join(bench.SCENARIOS)}")
    print(f"Benchmarking as {identity.name} (read-only)…", file=sys.stderr)
    result = bench.run(client, settings, tenant, scenarios, args.resource,
                       say=lambda message: print(message, file=sys.stderr))
    text = bench.to_markdown(result)
    out_dir = Path(args.out or settings["exports_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"bench-{result['taken_at'][:16].replace(' ', '-').replace(':', '')}.md"
    target.write_text(text)
    print(text)
    print(target, file=sys.stderr)


def cmd_alert_check(args, settings):
    """Is this alert right? for an Azure alert (--azure) or a monitoring tool's condition (--connection, --account,
    --condition): the same check as the page's button, with your own access and your stored keys."""
    from .core import alert_check, azure_alerts, integrations, inventory
    from .core.integrations import monitors

    client = _client(args, settings)
    if args.azure:
        source, ref = azure_alerts.AzureMetricAlerts(client), args.azure
    else:
        if not (args.connection and args.account and args.condition):
            sys.exit("error: give --azure <alert or rule ID>, or --connection, --account and --condition")
        identity = client.identity
        tenant = next((t for t in inventory.list_tenants(client) if t["id"] == identity.tenant_id), {"id": identity.tenant_id})
        _, found = config.tenant_integrations(settings, tenant)
        monitor = integrations.find_monitor(integrations.bind(found, tenant["id"]), args.connection)
        if not monitor:
            sys.exit(f"error: this tenant has no monitoring connection called '{args.connection}' (Settings → Integrations)")
        source = monitors.provider_for(monitor)
        ref = {"account_id": args.account, "condition": args.condition, "priority": args.priority or ""}
    result = alert_check.check(source, ref, settings["alert_check"])
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    rule, n = result["rule"], result["numbers"]
    print(f"{rule['name']} ({result['source']})")
    print(f"  rule: {rule['query']['text']}")
    if rule.get("threshold") is not None:
        print(f"  fires when {rule['operator']} {rule['threshold']:,}, per {rule['window_minutes']:g} min")
    print(f"  read: the last {result['days']} days, {result['scaled']}")
    for verdict in result["verdicts"]:
        print(f"\n  [{verdict['status']}] {verdict['text']}\n      rule: {verdict['rule']}"
              + (f" ({verdict['setting']})" if verdict["setting"] else ""))
    if n:
        print(f"\n  now {n['now']:,.2f} · last week {n['week_ago'] if n['week_ago'] is None else format(n['week_ago'], ',.2f')}"
              f" · usual {n['usual']:,.2f}" + (f" · true in {n['breaching']} of {n['values']} buckets ({n['breaching_percent']}%)"
                                               if "breaching" in n else ""))
    for note in result["notes"]:
        print(f"  note: {note}")
    for query in result["queries"]:
        print(f"\n  {query['label']}:\n    {query['query']}")


def cmd_whoami(args, settings):
    identity = _client(args, settings).identity
    print(f"{identity.name}  tenant {identity.tenant_id}")


def _setup_logging(verbose, log_calls):
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    # Flask's server logs every request at INFO unless its logger has a level: only -v shows them.
    logging.getLogger("werkzeug").setLevel(logging.INFO if verbose and not log_calls else logging.WARNING)
    if log_calls:
        logging.getLogger("healthconsole.calls").setLevel(logging.INFO)
        logging.getLogger().setLevel(min(logging.getLogger().level, logging.INFO))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="azure-health-console",
                                     description="Read-only Azure health view from your az login session.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--tenant", help="Tenant ID (default: config default_tenant, then the az CLI default)")
    parser.add_argument("--config", help=f"Config file (default: {config.path()})")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-calls", action="store_true",
                        help="Print every Azure and Kubernetes request (method and path, never tokens)")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the local web UI on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="Open the page in your browser once it's ready")
    serve.set_defaults(handler=cmd_serve)

    export = commands.add_parser("export", help="Snapshot one resource to Markdown and/or PDF")
    export.add_argument("resource_id")
    export.add_argument("--format", choices=("md", "pdf", "both"), default="md")
    export.add_argument("--depth", type=int)
    export.add_argument("--no-inbound", action="store_true")
    export.add_argument("--out", help="Output directory (default: config exports_dir)")
    export.set_defaults(handler=cmd_export)

    ready = commands.add_parser("readiness", help="Pre-peak readiness report (scaling headroom, vCPU quota, subnet "
                                                  "IPs, single points, HPAs, certificates) to Markdown and/or PDF")
    ready.add_argument("--peak-end", required=True, help="The day the peak ends (YYYY-MM-DD)")
    ready.add_argument("--subscription", action="append",
                       help="Subscription name, ID or pattern (repeatable; default: --scope, else what you open with)")
    ready.add_argument("--scope", help="A saved scope's name (its subscriptions)")
    ready.add_argument("--no-deep", action="store_true", help="Skip Kubernetes and Key Vault (one request per cluster and vault)")
    ready.add_argument("--include-secrets", action="store_true", help="Judge secrets and keys too, not only certificates")
    ready.add_argument("--problems-only", action="store_true", help="Leave out rows with room to spare")
    ready.add_argument("--format", choices=("md", "pdf", "both"), default="md")
    ready.add_argument("--out", help="Output directory (default: config exports_dir)")
    ready.set_defaults(handler=cmd_readiness)

    bench_cmd = commands.add_parser("bench", help="Time what the page does (triage, checks, search...) and "
                                                  "report the slowest Azure calls")
    bench_cmd.add_argument("--resource", help="Resource ID to check (default: one picked from what you open with)")
    bench_cmd.add_argument("--only", help="Comma-separated scenarios (triage,search,resource,resource-graph,group,"
                                          "aks-fleet,network,secrets)")
    bench_cmd.add_argument("--secrets", action="store_true",
                           help="Also time the Key Vault scan (lists secret names and dates, never values)")
    bench_cmd.add_argument("--out", help="Output directory (default: config exports_dir)")
    bench_cmd.set_defaults(handler=cmd_bench)

    draft = commands.add_parser("profile-draft", help="Print a draft health profile for a resource's type")
    draft.add_argument("resource_id")
    draft.set_defaults(handler=cmd_profile_draft)

    show = commands.add_parser("config", help="Show what your config resolves to: file, tenant entry, saved scopes, Open with")
    show.set_defaults(handler=cmd_config)

    check = commands.add_parser("alert-check", help="Is this alert right? Read an alert's rule and the last days of what "
                                                    "it watches (Azure metric alert, or a monitoring tool's condition)")
    check.add_argument("--azure", help="An Azure alert's ID, or a metric alert rule's ID")
    check.add_argument("--connection", help="A monitoring connection's name (Settings → Integrations)")
    check.add_argument("--account", type=int, help="The account the condition is in (New Relic: the account ID)")
    check.add_argument("--condition", help="The condition's name, as the alert names it")
    check.add_argument("--priority", help="Which threshold to use when the condition has several (e.g. critical)")
    check.add_argument("--json", action="store_true", help="Print the whole result as JSON")
    check.set_defaults(handler=cmd_alert_check)

    whoami = commands.add_parser("whoami", help="Show which identity and tenant the session uses")
    whoami.set_defaults(handler=cmd_whoami)

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # Double-clicked (or run with nothing): start the page and open it, like a desktop app.
        argv = ["serve", "--open"]
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.log_calls)
    try:
        settings = config.load(args.config)
        args.handler(args, settings)
    except (CredentialError, ArmError, ServiceError, config.ConfigError, profiles.ProfileError) as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
