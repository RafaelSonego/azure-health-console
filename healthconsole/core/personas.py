"""Personas: how a kind of user looks at the console, never what they may see. Azure RBAC decides what anyone can read;
a persona picks the home page, which views lead the menu (the rest stay under More), which Triage cards and resource
tabs come first, and how much detail pages show. Built in below, in the app's own view, card and tab names; a user or
team overrides or adds personas in the config file (`personas:`)."""

VIEWS = ("summary", "triage", "browse", "investigate", "readiness", "security", "cost", "secrets", "network", "tools",
         "watchlist", "access")
CARDS = ("alerts", "appinsights", "service_health", "failed", "aks", "autoscale", "private_endpoints", "public_access",
         "subnets")
TABS = ("overview", "timeline", "metrics", "workloads", "nodes", "appinsights", "details", "scaling", "diagnostics",
        "vulnerabilities", "recommendations", "cost", "security")
SUMMARY = ("problems", "changes", "cost", "security", "readiness", "access")  # Summary's cards; empty means all
DETAIL = ("full", "summary")
FIELDS = {"label": str, "about": str, "home": str, "views": list, "cards": list, "tabs": list, "detail": str,
          "summary": list}

BUILT_IN = {
    "manager": {
        "label": "Manager / lead",
        "about": "Are we OK, what does it cost, are we ready? Summaries in plain words, ready to share.",
        "home": "summary", "views": ["summary", "triage", "cost", "readiness", "security"],
        "cards": ["alerts", "service_health", "failed", "public_access"], "tabs": ["overview", "cost"], "detail": "summary",
        "summary": ["problems", "changes", "cost", "security", "readiness"]},
    "sre": {
        "label": "SRE / on-call",
        "about": "What's broken, why, and what changed: from an alert to the request and the pod behind it.",
        "home": "triage", "views": ["triage", "investigate", "browse", "watchlist", "tools"],
        "cards": ["alerts", "appinsights", "service_health", "failed", "aks", "autoscale"],
        "tabs": ["overview", "timeline", "appinsights", "workloads", "metrics"], "detail": "full"},
    "devops": {
        "label": "DevOps / platform",
        "about": "Is it healthy, configured right and ready to ship? Findings with what to do about them.",
        "home": "triage", "views": ["triage", "browse", "investigate", "readiness", "network", "secrets", "cost"],
        "cards": ["failed", "autoscale", "subnets", "aks", "private_endpoints", "alerts"],
        "tabs": ["overview", "details", "scaling", "diagnostics", "metrics"], "detail": "full"},
    "security": {
        "label": "Security",
        "about": "What's exposed, vulnerable or weakly configured, and who changed what.",
        "home": "security", "views": ["security", "access", "browse", "secrets", "triage"],
        "cards": ["public_access", "private_endpoints", "failed", "alerts"],
        "tabs": ["security", "vulnerabilities", "details", "timeline"], "detail": "full"},
    "everything": {
        "label": "Everything",
        "about": "Every view and card, full detail.",
        "home": "triage", "views": list(VIEWS), "cards": [], "tabs": [], "detail": "full"},
}
DEFAULT = "everything"


def problems(entries, where="personas"):
    """What's wrong with a `personas:` block, one line each; empty when it's fine. A built-in name overrides only the
    fields given; a new name needs a label and views."""
    if not isinstance(entries, dict):
        return [f"{where}: expected a mapping of persona name to its settings."]
    out = []
    for name, entry in entries.items():
        at = f"{where}.{name}"
        if not isinstance(entry, dict):
            out.append(f"{at}: expected a mapping.")
            continue
        unknown = set(entry) - set(FIELDS)
        if unknown:
            out.append(f"{at}: unknown key(s) {sorted(unknown)}; allowed {sorted(FIELDS)}.")
        for field, kind in FIELDS.items():
            if field in entry and not isinstance(entry[field], kind):
                out.append(f"{at}.{field}: expected a {'list' if kind is list else 'string'}.")
        for field, allowed in (("views", VIEWS), ("cards", CARDS), ("tabs", TABS), ("summary", SUMMARY)):
            bad = [v for v in entry.get(field) or [] if isinstance(entry.get(field), list) and v not in allowed]
            if bad:
                out.append(f"{at}.{field}: unknown {bad}; choose from {list(allowed)}.")
        if "home" in entry and entry["home"] not in VIEWS:
            out.append(f"{at}.home: unknown '{entry['home']}'; choose from {list(VIEWS)}.")
        if "detail" in entry and entry["detail"] not in DETAIL:
            out.append(f"{at}.detail: choose from {list(DETAIL)}.")
        if name not in BUILT_IN and not ({"label", "views"} <= set(entry)):
            out.append(f"{at}: a new persona needs at least a label and views.")
    return out


def merged(overrides=None):
    """[{name, label, about, home, views, cards, tabs, detail, built_in}], built-ins with the config's changes on top."""
    out = {name: {"summary": [], **entry, "built_in": True} for name, entry in BUILT_IN.items()}
    for name, entry in (overrides or {}).items():
        base = out.get(name) or {"label": name, "about": "", "home": (entry.get("views") or ["triage"])[0], "cards": [],
                                 "tabs": [], "summary": [], "detail": "full", "built_in": False}  # a new one opens on its first view
        out[name] = {**base, **entry}
    for entry in out.values():
        if entry["home"] not in entry["views"]:
            entry["views"] = [entry["home"], *entry["views"]]
    return [{"name": name, **entry} for name, entry in out.items()]
