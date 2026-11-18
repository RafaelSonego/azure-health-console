"""A resource group's health as one SVG diagram: bands by role, right-angle connectors, a colour per health status.
Works on a snapshot's dicts (Snapshot.to_dict), so the page and a new tab draw the same picture. fold() decides what's
drawn; the page's single-resource graph reads its result too."""
from functools import lru_cache
from html import escape
from pathlib import Path
from urllib.parse import quote

from . import taxonomy

PE_TYPE = "microsoft.network/privateendpoints"
PROBLEM = {"critical", "warning"}

NODE_W, NODE_H, ICON = 208, 72, 26
H_GAP, V_GAP, BAND_GAP = 30, 46, 34
PAD, BAND_LABEL_H, TITLE_H = 28, 36, 64  # band titles clear of the role labels above a band's first row
SMALL_H = 34  # the compact row of identities nothing here uses
IDENTITY = "microsoft.managedidentity/userassignedidentities"
COLUMNS = 5
WIDTH = PAD * 2 + COLUMNS * NODE_W + (COLUMNS - 1) * H_GAP
MAX_LABELLED_EDGES = 24  # past this, plain labels cover the picture; role labels stay
REASON_CHARS = 23
LABEL_CHARS = 34  # a connector's label fits above the box it enters
NAME_CHARS, TYPE_CHARS = 18, 26  # what fits beside the icon and the status dot at these font sizes

ICON_DIR = Path(__file__).resolve().parent.parent / "diagram_icons"
# Keyed on taxonomy.display_name() in lower case; a type without an entry is drawn without an icon.
ICON_FILES = {
    "action group": "alerts.svg",
    "ai foundry project": "foundry-project.svg",
    "ai search": "ai-search.svg",
    "ai services": "ai-services.svg",
    "aks cluster": "kubernetes.svg",
    "api connection": "api-connection.svg",
    "api management": "api-management.svg",
    "app configuration": "app-configuration.svg",
    "app service plan": "app-service-plan.svg",
    "application gateway": "application-gateway.svg",
    "application insights": "application-insights.svg",
    "automation account": "automation-account.svg",
    "azure firewall": "firewall.svg",
    "azure ml / foundry hub": "machine-learning.svg",
    "azure monitor workspace": "monitor.svg",
    "backup vault": "backup-vault.svg",
    "bastion": "bastion.svg",
    "batch account": "batch-accounts.svg",
    "bot service": "bot-service.svg",
    "budget": "budget.svg",
    "communication services": "communication-services.svg",
    "container app": "container-app.svg",
    "container app job": "container-app.svg",
    "container apps environment": "container-apps-environment.svg",
    "container instance": "container-instances.svg",
    "container registry": "container-registry.svg",
    "cosmos db": "cosmos-db.svg",
    "dashboard": "dashboard.svg",
    "data explorer cluster": "data-explorer.svg",
    "data factory": "data-factories.svg",
    "databricks workspace": "databricks.svg",
    "disk": "disk.svg",
    "disk access": "disk.svg",
    "dns zone": "dns-zone.svg",
    "event grid system topic": "event-grid.svg",
    "event grid topic": "event-grid.svg",
    "event hub": "event-hubs.svg",
    "external service": "external-service.svg",
    "firewall policy": "firewall-policy.svg",
    "front door": "front-door.svg",
    "front door / cdn": "front-door.svg",
    "function app": "function-app.svg",
    "key vault": "key-vault.svg",
    "load balancer": "load-balancer.svg",
    "local network gateway": "local-network-gateway.svg",
    "log alert": "alerts.svg",
    "log analytics": "log-analytics.svg",
    "logic app": "logic-app.svg",
    "logic app (standard)": "logic-app.svg",
    "managed grafana": "grafana.svg",
    "managed identity": "managed-identity.svg",
    "management group": "management-group.svg",
    "metric alert": "alerts.svg",
    "mysql flexible server": "mysql.svg",
    "nat gateway": "nat-gateway.svg",
    "network interface": "network-interface.svg",
    "nsg": "network-security-groups.svg",
    "policy assignment": "policy.svg",
    "postgresql flexible server": "postgresql.svg",
    "private dns zone": "dns-zone.svg",
    "private endpoint": "private-endpoint.svg",
    "public ip": "public-ip.svg",
    "recovery services vault": "recovery-services-vault.svg",
    "redis cache": "redis.svg",
    "resource group": "resource-group.svg",
    "route table": "route-table.svg",
    "service bus": "service-bus.svg",
    "signalr service": "signalr.svg",
    "sql database": "sql-database.svg",
    "sql server": "sql-server.svg",
    "static web app": "static-web-app.svg",
    "storage account": "storage-account.svg",
    "stream analytics job": "stream-analytics.svg",
    "subnet": "subnet.svg",
    "subscription": "subscription.svg",
    "synapse workspace": "synapse.svg",
    "traffic manager": "traffic-manager.svg",
    "virtual machine": "virtual-machine.svg",
    "virtual network": "virtual-networks.svg",
    "virtual wan": "virtual-wan.svg",
    "virtual wan hub": "virtual-wan-hub.svg",
    "vm extension": "vm-extension.svg",
    "vm scale set": "vm-scale-set.svg",
    "vpn / expressroute gateway": "virtual-network-gateway.svg",
    "waf policy": "waf-policy.svg",
    "web app": "app-services.svg",
    "web certificate": "app-service-certificates.svg",
    "web pubsub": "web-pubsub.svg",
    "workbook": "dashboard.svg",
    "network security group": "network-security-groups.svg",
    "app gateway waf policy": "waf-policy.svg",
    "autoscale setting": "auto-scale.svg",
}

# Colours are the console's theme variables (static/style.css); theme_css() brings them to a page of its own.
STYLE = """<style>
.dg { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.dg-title { font-size: 15px; font-weight: 700; fill: var(--text); }
.dg-band { font-size: 12px; font-weight: 600; letter-spacing: .08em; fill: var(--muted); }
.dg-box { fill: var(--panel); stroke: var(--border); stroke-width: 1; }
.dg-node { cursor: pointer; }
.dg-node:hover .dg-box, .dg-node:focus .dg-box { stroke: var(--accent); stroke-width: 2; }
.dg-node.selected .dg-box { stroke: var(--accent); stroke-width: 3; }
.dg-node.hit .dg-box { stroke: var(--accent); stroke-width: 2.5; }
.st-critical .dg-box { fill: var(--critical-bg); stroke: var(--critical); stroke-width: 1.5; }
.st-warning .dg-box { fill: var(--warning-bg); stroke: var(--warning); stroke-width: 1.5; }
.dg-name { font-size: 13px; font-weight: 600; fill: var(--text); }
.dg-type { font-size: 11px; fill: var(--muted); }
.dg-dot { stroke: var(--panel); stroke-width: 1.5; }
.dg-dot.st-critical { fill: var(--critical); } .dg-dot.st-warning { fill: var(--warning); }
.dg-dot.st-healthy { fill: var(--healthy); } .dg-dot.st-unknown, .dg-dot.st-info { fill: var(--unknown); }
.dg-edges path { fill: none; stroke: var(--muted); stroke-width: 1.3; opacity: .75; stroke-linejoin: round; }
.dg-node.root .dg-box { stroke: var(--accent); stroke-width: 2.5; }
.dg-why { font-size: 10.5px; font-weight: 600; }
.dg-why.st-critical { fill: var(--critical); } .dg-why.st-warning { fill: var(--warning); }
.dg-edge:hover path { stroke: var(--accent); stroke-width: 2.2; opacity: 1; }
.dg-edge-role path { stroke: var(--warning); stroke-dasharray: 5 4; opacity: .9; }
.dg-arrow { fill: var(--muted); }
.dg-label { font-size: 10px; fill: var(--muted); text-anchor: middle; }
.dg-edge-role-label { fill: var(--warning); }
.dg-halo { fill: var(--bg); opacity: .9; }
.tier-ingress .dg-accent { fill: var(--tier-ingress); } .tier-compute .dg-accent { fill: var(--tier-compute); }
.tier-platform .dg-accent { fill: var(--tier-platform); } .tier-data .dg-accent { fill: var(--tier-data); }
.tier-security .dg-accent { fill: var(--tier-security); } .tier-observability .dg-accent { fill: var(--tier-observability); }
.tier-network .dg-accent { fill: var(--tier-network); } .tier-other .dg-accent { fill: var(--tier-other); }
.dg-legend { font-size: 11px; fill: var(--muted); }
.dg-legend-role { stroke: var(--warning); stroke-dasharray: 5 4; stroke-width: 1.5; }
.dg.focusing .dg-node:not(.lit), .dg.focusing .dg-edge:not(.lit), .dg.focusing .dg-lbl:not(.lit) { opacity: .12; }
.dg.focusing .dg-edge.lit path { stroke: var(--accent); stroke-width: 2.6; opacity: 1; }
.dg.focusing .dg-edge-role.lit path { stroke: var(--warning); }
.dg-node, .dg-edge, .dg-lbl { transition: opacity .12s; }
</style>"""


def fold(nodes, edges, root_id="", show_all=False):
    """What the diagram leaves out. Plumbing (taxonomy.GRAPH_PLUMBING) is drawn only where it joins two other
    resources (links to the Network band don't count); a private endpoint becomes 🔒 on what it serves, and what's
    part of a resource (an AKS cluster's node pools) +N on it. An identity just one resource signs in as becomes 🔑 on
    it, its roles drawn from that resource ("via"); identities nothing links go in a compact row. The root and anything
    critical or warning are drawn. {"hidden", "locked", "parts", "via", "keys", "compact", "left"}"""
    by_id = {n["id"]: n for n in nodes}
    peers = {n["id"]: set() for n in nodes}
    for e in edges:
        if e["source"] in peers and e["target"] in peers:
            peers[e["source"]].add(e["target"])
            peers[e["target"]].add(e["source"])

    def others(node_id):
        return [p for p in peers[node_id]
                if not taxonomy.graph_role(by_id[p]["type"]) and taxonomy.tier(by_id[p]["type"]) != "network"]

    owner = {e["target"]: e["source"] for e in edges if e.get("kind") == "part" and e["source"] in by_id}
    users = {}
    for e in edges:
        if e.get("kind") != "role" and e["source"] in by_id and e["source"] != e["target"] \
                and (by_id.get(e["target"]) or {}).get("type") == IDENTITY:
            users.setdefault(e["target"], set()).add(e["source"])
    hidden, locked, parts, via, keys, compact = set(), set(), {}, {}, {}, []
    for n in nodes if not show_all else []:
        if n["id"] == root_id or n.get("status") in PROBLEM:
            continue
        if n["id"] in owner:
            hidden.add(n["id"])
            parts[owner[n["id"]]] = parts.get(owner[n["id"]], 0) + 1
            continue
        if n["type"] == IDENTITY and len(users.get(n["id"], ())) == 1:
            user = next(iter(users[n["id"]]))
            hidden.add(n["id"])
            via[n["id"]] = user
            keys.setdefault(user, []).append(n["name"])
            continue
        if n["type"] == IDENTITY and not peers[n["id"]]:
            compact.append(n["id"])
            continue
        if not taxonomy.graph_role(n["type"]) or len(others(n["id"])) > 1:
            continue
        hidden.add(n["id"])
        if n["type"] == PE_TYPE:
            locked.update(others(n["id"]))
    left = {}
    for node_id in hidden:
        left[by_id[node_id]["display_type"]] = left.get(by_id[node_id]["display_type"], 0) + 1
    return {"hidden": sorted(hidden), "locked": sorted(locked - hidden), "parts": parts, "via": via, "keys": keys,
            "compact": sorted(compact), "left": dict(sorted(left.items(), key=lambda kv: (-kv[1], kv[0])))}


def short(text, limit):
    """Cut in the middle: names in one group share long prefixes, and the end is what tells them apart."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    return f"{text[:head]}…{text[len(text) - (limit - 1 - head):]}"


@lru_cache(maxsize=None)
def _icon_uri(name):
    path = ICON_DIR / name
    return "data:image/svg+xml," + quote(path.read_text(encoding="utf-8"), safe="") if path.is_file() else None


def _icon(display_type):
    name = ICON_FILES.get((display_type or "").lower())
    return (name[:-4], _icon_uri(name)) if name and _icon_uri(name) else (None, None)


def layout(nodes, links, compact=()):
    """Bands top to bottom in taxonomy.TIERS order on a grid of COLUMNS, so the gaps between columns line up and
    connectors can run along them. Each box sits under the average position of what it links to: a first pass looks
    at the bands above, a second at every link from the first. `compact` nodes go in one small row at the end."""
    first = _place(nodes, links, None)[0]
    placed, bands, height = _place(nodes, links, {k: v["x"] for k, v in first.items()})
    small = sorted((n for n in nodes if n["id"] in set(compact)), key=lambda n: n["name"].lower())
    if small:
        y = height - PAD
        bands.append({"label": "IDENTITIES NOTHING HERE USES", "y": y})
        y += BAND_LABEL_H
        row = max((p["row"] for p in placed.values()), default=-1) + 1
        for i, n in enumerate(small):
            if i and i % COLUMNS == 0:
                y += SMALL_H + V_GAP
                row += 1
            placed[n["id"]] = {"x": _col_x(i % COLUMNS), "y": y, "col": i % COLUMNS, "row": row, "h": SMALL_H,
                               "band": len(bands) - 1, "node": n, "small": True}
        height = y + SMALL_H + PAD
    return placed, bands, WIDTH, height


def _col_x(col):
    return PAD + col * (NODE_W + H_GAP)


def _gap_x(gap):
    """The middle of the gap left of column `gap` (gap COLUMNS is right of the last column)."""
    return PAD + gap * (NODE_W + H_GAP) - H_GAP / 2


def _place(nodes, links, hint):
    by_tier = {}
    for n in nodes:
        if not n.get("_compact"):
            by_tier.setdefault(taxonomy.tier(n["type"]), []).append(n)
    placed, bands, y, row_index = {}, [], PAD, 0
    for tier, label in taxonomy.TIERS:
        group = sorted(by_tier.get(tier, []), key=lambda n: n["name"].lower())
        if not group:
            continue

        def pull(n):
            xs = [hint[p] if hint is not None else placed[p]["x"] for p in links.get(n["id"], ())
                  if (hint is not None and p in hint and p != n["id"]) or (hint is None and p in placed)]
            return sum(xs) / len(xs) if xs else float("inf")
        group = [n for _, _, n in sorted((pull(n), i, n) for i, n in enumerate(group))]
        bands.append({"label": label.upper(), "y": y})
        y += BAND_LABEL_H
        for start in range(0, len(group), COLUMNS):
            row = group[start:start + COLUMNS]
            first_col = (COLUMNS - len(row)) // 2  # centred by whole columns, so the gaps still line up
            for i, n in enumerate(row):
                placed[n["id"]] = {"x": _col_x(first_col + i), "y": y, "col": first_col + i, "row": row_index,
                                   "h": NODE_H, "band": len(bands) - 1, "node": n}
            y += NODE_H + V_GAP
            row_index += 1
        y += BAND_GAP - V_GAP
    return placed, bands, y + PAD


class _Router:
    """Right-angle connectors that never pass behind a box: out of the source's top or bottom into the channel
    between rows, along it to a gap between columns, along the gap to the channel by the target, into the target.
    Connectors sharing a gap or channel are spread a few pixels apart; several leaving one box leave apart too."""

    def __init__(self):
        self.gaps, self.channels, self.ports = {}, {}, {}

    def _spread(self, table, key, step, slots):
        k = table.get(key, 0)
        table[key] = k + 1
        return ((k % slots) - (slots - 1) / 2) * step

    def path(self, s, t):
        sx = s["x"] + NODE_W / 2 + self._spread(self.ports, (id(s), "out"), 14, 5)
        tx = t["x"] + NODE_W / 2 + self._spread(self.ports, (id(t), "in"), 14, 5)
        if s["row"] == t["row"]:  # same row: an arc over it, through the channel above
            lift = 16 + self._spread(self.channels, (s["row"], "above"), 3, 3)
            y = s["y"] - lift
            return f"M {sx:.0f} {s['y']:.0f} V {y:.0f} H {tx:.0f} V {t['y']:.0f}", (tx, t["y"] - 3)
        down = t["y"] > s["y"]
        sy, ty = (s["y"] + s["h"], t["y"]) if down else (s["y"], t["y"] + t["h"])
        y1 = sy + (12 if down else -12) + self._spread(self.channels, (s["row"], "below" if down else "above"), 3, 3)
        y2 = ty + (-12 if down else 12) + self._spread(self.channels, (t["row"], "above" if down else "below"), 3, 3)
        if abs(t["row"] - s["row"]) == 1:  # neighbouring rows: only the channel between them
            mid = (sy + ty) / 2 + self._spread(self.channels, (min(s["row"], t["row"]), "between"), 3, 5)
            return f"M {sx:.0f} {sy:.0f} V {mid:.0f} H {tx:.0f} V {ty:.0f}", (tx, ty - 3 if down else ty + 11)
        gap = min(range(COLUMNS + 1), key=lambda g: (abs(_gap_x(g) - sx) + abs(_gap_x(g) - tx), abs(_gap_x(g) - tx)))
        gx = _gap_x(gap) + self._spread(self.gaps, gap, 4, 5)
        return (f"M {sx:.0f} {sy:.0f} V {y1:.0f} H {gx:.0f} V {y2:.0f} H {tx:.0f} V {ty:.0f}",
                (tx, ty - 3 if down else ty + 11))


def _reason(n):
    """The node's worst signals as text, worst first: why it's red or amber."""
    bad = [sig for sig in n.get("signals") or [] if sig.get("status") in PROBLEM]
    bad.sort(key=lambda sig: sig.get("status") != "critical")
    return [f'{sig.get("name") or sig.get("source")}: {sig.get("detail") or sig.get("status")}' for sig in bad]


def _wide_roles(n):
    return [f'{r.get("role")} on {r.get("scope_kind")} {r.get("scope_name")}' for r in n.get("roles") or []]


def render_svg(snapshot, show_all=False, title=""):
    """The snapshot (a Snapshot.to_dict()) as one inline SVG; nodes carry data-id for the page's clicks."""
    folded = fold(snapshot["nodes"], snapshot["edges"], snapshot.get("root_id", ""), show_all)
    hidden, via, compact = set(folded["hidden"]), folded["via"], set(folded["compact"])
    by_id = {n["id"]: n for n in snapshot["nodes"]}
    nodes = [{**n, "_compact": n["id"] in compact} for n in snapshot["nodes"] if n["id"] not in hidden]
    if not nodes:
        return '<svg class="dg" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Nothing to draw"></svg>'
    shown = {n["id"] for n in nodes}
    edges, seen = [], set()
    for e in snapshot["edges"]:  # a folded identity's roles start at the resource that signs in as it
        source, label = via.get(e["source"], e["source"]), e.get("label") or ""
        caption = label  # on the canvas; the hover text says which identity it goes through
        if e["source"] in via and e.get("kind") == "role":
            label = f"{label} (via {by_id[e['source']]['name']})"
        key = (source, e["target"], e.get("kind") or "")
        if source in shown and e["target"] in shown and source != e["target"] and key not in seen:
            seen.add(key)
            edges.append({**e, "source": source, "label": label, "caption": caption})
    links = {}
    for e in edges:
        links.setdefault(e["source"], []).append(e["target"])
        links.setdefault(e["target"], []).append(e["source"])
    placed, bands, width, height = layout(nodes, links, compact)
    labelled = len(edges) <= MAX_LABELLED_EDGES

    icons = {}
    for n in nodes:
        slug, uri = _icon(n["display_type"])
        if slug:
            icons[slug] = uri
    out = [f'<svg class="dg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height + TITLE_H:.0f}" '
           f'width="{width:.0f}" height="{height + TITLE_H:.0f}" role="img" aria-label="{escape(title or "Diagram")}">',
           STYLE, '<defs><marker id="dg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" class="dg-arrow"/></marker>',
           "".join(f'<g id="dg-icon-{s}"><image href="{u}" width="{ICON}" height="{ICON}"/></g>' for s, u in sorted(icons.items())),
           '</defs>', f'<text class="dg-title" x="{PAD}" y="26">{escape(title)}</text>', _legend(folded),
           f'<g transform="translate(0 {TITLE_H})">']
    out += [f'<text class="dg-band" x="{PAD}" y="{b["y"] + 14:.0f}">{escape(b["label"])}</text>' for b in bands]

    router, labels = _Router(), []
    out.append('<g class="dg-edges">')
    for e in sorted(edges, key=lambda e: (placed[e["source"]]["row"], placed[e["source"]]["col"], placed[e["target"]]["col"])):
        s, t = placed[e["source"]], placed[e["target"]]
        path, anchor = router.path(s, t)
        kind = e.get("kind") or "ref"
        ends = f'data-source="{escape(e["source"])}" data-target="{escape(e["target"])}"'
        out.append(f'<g class="dg-edge dg-edge-{escape(kind)}" {ends}><title>{escape(s["node"]["name"])} {escape(e.get("label") or "")} '
                   f'{escape(t["node"]["name"])}</title><path d="{path}" marker-end="url(#dg-arrow)"/></g>')
        if anchor and e.get("caption") and (labelled or kind == "role"):
            labels.append((anchor, e["caption"] if len(e["caption"]) <= LABEL_CHARS else e["caption"][:LABEL_CHARS - 1] + "…",
                           kind, ends, e["target"]))
    out.append('</g><g class="dg-nodes">')
    root = snapshot.get("root_id", "")
    for n in nodes:
        p, (slug, _) = placed[n["id"]], _icon(n["display_type"])
        x, y = p["x"], p["y"]
        status = escape(n.get("status") or "unknown")
        folded_in = [by_id[i] for i, user in via.items() if user == n["id"]]
        roles = _wide_roles(n) + [r for ident in folded_in for r in _wide_roles(ident)]
        reasons = _reason(n)
        tip = "\n".join([f'{n["name"]} — {n["display_type"]} — {status}', *reasons[:3],
                         *([f'Signs in as {", ".join(i["name"] for i in folded_in)}'] if folded_in else []),
                         *([f'Also holds: {"; ".join(roles[:6])}' + (f" (+{len(roles) - 6} more)" if len(roles) > 6 else "")] if roles else [])])
        cls = f'dg-node st-{status} tier-{taxonomy.tier(n["type"])}{" root" if n["id"] == root else ""}'
        search = escape(f'{n["name"]} {n["display_type"]} {taxonomy.names_for(n["type"])}'.lower())
        if p.get("small"):
            out.append(f'<g class="{cls}" data-id="{escape(n["id"])}" data-search="{search}" tabindex="0" role="button"><title>{escape(tip)}</title>'
                       f'<rect class="dg-box" x="{x:.0f}" y="{y:.0f}" width="{NODE_W}" height="{SMALL_H}" rx="8"/>'
                       f'<rect class="dg-accent" x="{x:.0f}" y="{y:.0f}" width="4" height="{SMALL_H}" rx="2"/>'
                       f'<circle class="dg-dot st-{status}" cx="{x + NODE_W - 12:.0f}" cy="{y + SMALL_H / 2:.0f}" r="4"/>'
                       f'<text class="dg-type" x="{x + 14:.0f}" y="{y + 21:.0f}">{escape(short(n["name"], NAME_CHARS + 6))}</text></g>')
            continue
        text_x = x + (54 if slug else 18)
        marks = (" · 🔒" if n["id"] in folded["locked"] else "") + \
            (f" · +{folded['parts'][n['id']]}" if n["id"] in folded["parts"] else "") + (" · 🔑" if folded_in else "")
        reason = (reasons or [""])[0]
        reason = reason if len(reason) <= REASON_CHARS else reason[:REASON_CHARS - 1] + "…"
        why = (f'<text class="dg-why st-{status}" x="{text_x:.0f}" y="{y + 62:.0f}">{escape(reason)}</text>'
               if reasons and status in PROBLEM else "")
        out.append(
            f'<g class="{cls}" data-id="{escape(n["id"])}" data-search="{search}" tabindex="0" role="button"><title>{escape(tip)}</title>'
            f'<rect class="dg-box" x="{x:.0f}" y="{y:.0f}" width="{NODE_W}" height="{NODE_H}" rx="10"/>'
            f'<rect class="dg-accent" x="{x:.0f}" y="{y:.0f}" width="5" height="{NODE_H}" rx="2.5"/>'
            + (f'<use href="#dg-icon-{slug}" x="{x + 16:.0f}" y="{y + 18:.0f}"/>' if slug else "")
            + f'<circle class="dg-dot st-{status}" cx="{x + NODE_W - 12:.0f}" cy="{y + 12:.0f}" r="5"/>'
            f'<text class="dg-name" x="{text_x:.0f}" y="{y + 25:.0f}">{escape(short(n["name"], NAME_CHARS if slug else NAME_CHARS + 4))}</text>'
            f'<text class="dg-type" x="{text_x:.0f}" y="{y + 43:.0f}">{escape(short(n["display_type"], TYPE_CHARS - len(marks)) + marks)}</text>'
            f'{why}</g>')
    out.append('</g><g class="dg-labels">')
    entering = {}
    for (ax, ay), caption, kind, ends, target in labels:
        w = len(caption) * 5.6 + 10
        side = entering.get(target, 0)
        entering[target] = side + 1
        # beside the arrow: the first label into a box on its right, the next on its left, then one row up
        left = ax + 6 if side % 2 == 0 else ax - 6 - w
        ay -= 13 * (side // 2)
        left = max(4, min(left, width - w - 4))
        out.append(f'<g class="dg-lbl" {ends}><rect class="dg-halo" x="{left:.0f}" y="{ay - 9:.0f}" width="{w:.0f}" height="12" rx="6"/>'
                   f'<text class="dg-label{" dg-edge-role-label" if kind == "role" else ""}" x="{left + w / 2:.0f}" y="{ay:.0f}">{escape(caption)}</text></g>')
    out.append('</g></g></svg>')
    return "".join(out)


def _legend(folded):
    """One line under the title: what the marks and colours mean, only the ones this picture uses."""
    items = [("role", "a role: who can reach what")]
    if folded["locked"]:
        items.append(("text", "🔒 private endpoint"))
    if folded["parts"]:
        items.append(("text", "+N node pools inside"))
    if folded["keys"]:
        items.append(("text", "🔑 the identity it signs in as"))
    out, x, y = ['<g class="dg-legend">'], PAD, 50
    for kind, text in items:
        if kind == "role":
            out.append(f'<line class="dg-legend-role" x1="{x}" y1="{y - 4}" x2="{x + 22}" y2="{y - 4}"/>')
            x += 28
        out.append(f'<text x="{x}" y="{y}">{escape(text)}</text>')
        x += len(text) * 6.2 + 22
    for status, text in (("healthy", "healthy"), ("warning", "warning"), ("critical", "critical"), ("unknown", "unknown")):
        out.append(f'<circle class="dg-dot st-{status}" cx="{x + 4}" cy="{y - 4}" r="4"/><text x="{x + 12}" y="{y}">{text}</text>')
        x += len(text) * 6.2 + 30
    out.append("</g>")
    return "".join(out)


STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
FOCUS_JS = STATIC / "diagram.js"
THEME_END = "/* end of theme"


def theme_css(light_only=False):
    """The console's theme variables, read from style.css (everything above its end-of-theme marker), for a page that
    doesn't load style.css: the diagram's own tab, or the printable report (light only, for paper)."""
    css = (STATIC / "style.css").read_text(encoding="utf-8").split(THEME_END, 1)[0]
    return css[:css.index("}") + 1] if light_only else css


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
{theme}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 13px -apple-system, sans-serif; }}
.bar {{ position: sticky; top: 0; display: flex; gap: 6px; align-items: center; padding: 8px 16px; background: var(--bg);
  border-bottom: 1px solid var(--border); }} .bar button {{ font: inherit; padding: 3px 10px; }}
.wrap {{ padding: 16px; overflow: auto; }} .note {{ color: var(--muted); padding: 0 16px; }}
</style></head><body><div class="bar"><strong>{title}</strong><span style="flex:1"></span>
<input data-f type="search" placeholder="Filter: name or type" aria-label="Filter the diagram by name or type">
<span id="fc"></span><button data-p="1" title="Light every critical or warning box and what it connects to">Problems</button>
<button data-z="-1">−</button><span id="z">100%</span><button data-z="1">+</button><button data-z="0">Fit</button></div>
<p class="note">{note}</p><div class="wrap" id="w">{svg}</div><script>
const svg = document.querySelector("#w svg"), w0 = svg.viewBox.baseVal.width, h0 = svg.viewBox.baseVal.height;
let z = 1; const set = (v) => {{ z = Math.max(0.2, Math.min(3, v)); svg.setAttribute("width", w0 * z);
  svg.setAttribute("height", h0 * z); document.getElementById("z").textContent = Math.round(z * 100) + "%"; }};
document.querySelector(".bar").addEventListener("click", (e) => {{ const d = e.target.dataset.z; if (d === undefined) return;
  set(d === "0" ? (document.getElementById("w").clientWidth - 32) / w0 : z + Number(d) * 0.1); }});
{focus}
const focus = diagramFocus(document.getElementById("w"));
document.querySelector("[data-p]").addEventListener("click", () => focus.problems());
document.querySelector("[data-f]").addEventListener("input", (e) => {{ const n = focus.filter(e.target.value);
  document.getElementById("fc").textContent = e.target.value.trim() ? n + " match" + (n === 1 ? "" : "es") : ""; }});
</script></body></html>"""


def page(svg, title, note=""):
    """The diagram as a page of its own (the page's "Open in new tab"), view-only, with zoom."""
    return PAGE.format(title=escape(title), note=escape(note), svg=svg, theme=theme_css(),
                       focus=FOCUS_JS.read_text(encoding="utf-8"))
