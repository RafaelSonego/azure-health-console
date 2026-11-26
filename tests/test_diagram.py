import re

from flask import Flask

from healthconsole.core import config, diagram
from healthconsole.web import create_blueprint
from tests.conftest import FakeClient

RG = "/subscriptions/s/resourcegroups/rg/providers"
KV, PE, NIC = f"{RG}/microsoft.keyvault/vaults/kv", f"{RG}/microsoft.network/privateendpoints/kv-pe", f"{RG}/microsoft.network/networkinterfaces/kv-pe.nic"
VNET = f"{RG}/microsoft.network/virtualnetworks/vnet"
VM, VM_NIC, PIP = f"{RG}/microsoft.compute/virtualmachines/vm", f"{RG}/microsoft.network/networkinterfaces/vm-nic", f"{RG}/microsoft.network/publicipaddresses/pip"
AKS, POOL = f"{RG}/microsoft.containerservice/managedclusters/aks", f"{RG}/microsoft.compute/virtualmachinescalesets/pool"
ID, ACR = f"{RG}/microsoft.managedidentity/userassignedidentities/aks-id", f"{RG}/microsoft.containerregistry/registries/acr"


def node(node_id, display, status="healthy"):
    return {"id": node_id, "name": node_id.rsplit("/", 1)[-1], "type": "/".join(node_id.split("/")[-3:-1]),
            "display_type": display, "status": status}


def edge(source, target, label="uses", kind=""):
    return {"source": source, "target": target, "label": label, "kind": kind}


def estate(nic_status="healthy"):
    nodes = [node(KV, "Key Vault"), node(PE, "Private Endpoint"), node(NIC, "Network Interface", nic_status),
             node(VNET, "Virtual Network"), node(VM, "Virtual Machine"), node(VM_NIC, "Network Interface"),
             node(PIP, "Public IP"), node(AKS, "AKS Cluster"), node(POOL, "VM Scale Set"), node(ID, "Managed Identity"),
             node(ACR, "Container Registry")]
    edges = [edge(PE, KV, "private link to"), edge(PE, NIC), edge(NIC, PE), edge(PE, VNET, "in network"),
             edge(VM, VM_NIC), edge(VM_NIC, PIP), edge(AKS, POOL, "node pool", "part"), edge(AKS, ID, "authenticates as"),
             edge(ID, ACR, "AcrPull", "role")]
    return nodes, edges


def test_plumbing_folds_into_what_it_serves_and_parts_into_their_owner():
    nodes, edges = estate()
    folded = diagram.fold(nodes, edges, root_id="")
    # the endpoint's link to the VNet doesn't count; a VM's NIC between the VM and its public IP stays
    assert set(folded["hidden"]) == {PE, NIC, POOL, ID}
    assert folded["locked"] == [KV] and folded["parts"] == {AKS: 1}
    # the identity only AKS signs in as is drawn as AKS: 🔑 on it, its roles from it
    assert folded["via"] == {ID: AKS} and folded["keys"] == {AKS: ["aks-id"]}
    assert folded["left"] == {"Managed Identity": 1, "Network Interface": 1, "Private Endpoint": 1, "VM Scale Set": 1}


def test_a_problem_the_root_and_show_everything_are_always_drawn():
    nodes, edges = estate(nic_status="critical")
    assert NIC not in diagram.fold(nodes, edges)["hidden"]
    assert PE not in diagram.fold(nodes, edges, root_id=PE)["hidden"]
    assert diagram.fold(nodes, edges, show_all=True)["hidden"] == []


def test_names_are_cut_in_the_middle_where_shared_prefixes_would_hide_the_difference():
    assert diagram.short("shop-prod-build-agents-03-vmss", 18) == "shop-pro…s-03-vmss"
    assert diagram.short("short", 22) == "short"


def snapshot(**overrides):
    nodes, edges = estate(**overrides)
    nodes[0]["name"] = "<kv>"
    return {"root_id": "/subscriptions/s/resourcegroups/rg", "title": "Resource group rg", "nodes": nodes, "edges": edges}


def test_the_svg_draws_what_fold_keeps_with_status_role_arrows_and_icons():
    svg = diagram.render_svg(snapshot(), title="rg")
    assert f'data-id="{KV}"' in svg and f'data-id="{PE}"' not in svg and f'data-id="{POOL}"' not in svg
    assert "&lt;kv&gt;" in svg and "<kv>" not in svg
    assert 'class="dg-edge dg-edge-role"' in svg and 'id="dg-icon-key-vault"' in svg
    assert "Key Vault · 🔒" in svg and "AKS Cluster · +1" in svg and "st-healthy" in svg
    everything = diagram.render_svg(snapshot(), show_all=True)
    assert f'data-id="{PE}"' in everything


def test_a_snapshot_dict_carries_the_fold():
    from healthconsole.core.model import Edge, Node, Snapshot
    snap = Snapshot(root_id="r", tenant_id="t", taken_by="me", taken_at="now", depth=1,
                    nodes=[Node(id=PE, name="pe", type="microsoft.network/privateendpoints", display_type="Private Endpoint", hop=0),
                           Node(id=KV, name="kv", type="microsoft.keyvault/vaults", display_type="Key Vault", hop=0)],
                    edges=[Edge(PE, KV, "private link to")])
    assert snap.to_dict()["graph"]["locked"] == [KV]


def http():
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    return app.test_client()


def test_the_route_returns_the_svg_or_a_page_and_refuses_a_bad_body():
    client = http()
    reply = client.post("/api/diagram", json={"snapshot": snapshot()})
    assert reply.status_code == 200 and reply.mimetype == "image/svg+xml" and b"dg-node" in reply.data
    page = client.post("/api/diagram", json={"snapshot": snapshot(), "page": True, "title": "rg"})
    assert page.mimetype == "text/html" and b"<svg" in page.data and b"Fit" in page.data
    assert client.post("/api/diagram", json={"snapshot": {"nodes": "x"}}).status_code == 400
    assert client.post("/api/diagram", json={"snapshot": {"nodes": [{"id": 1}], "edges": []}}).status_code == 400


LONE = f"{RG}/microsoft.managedidentity/userassignedidentities/spare-id"


def test_roles_start_at_the_resource_that_signs_in_and_unused_identities_go_in_a_small_row():
    snap = snapshot()
    snap["nodes"].append(node(LONE, "Managed Identity"))
    folded = diagram.fold(snap["nodes"], snap["edges"])
    assert folded["compact"] == [LONE] and LONE not in folded["hidden"]
    svg = diagram.render_svg(snap)
    assert f'data-source="{AKS}" data-target="{ACR}"' in svg and "AcrPull (via aks-id)" in svg  # in the hover text
    assert '">AcrPull</text>' in svg  # on the canvas, just the role
    assert f'data-id="{ID}"' not in svg and "AKS Cluster · +1 · 🔑" in svg
    assert "IDENTITIES NOTHING HERE USES" in svg and f'data-id="{LONE}"' in svg
    assert "a role: who can reach what" in svg and "🔒 private endpoint" in svg


def test_a_shared_identity_keeps_its_own_box():
    nodes, edges = estate()
    nodes.append(node(f"{RG}/microsoft.web/sites/app", "Web App"))
    edges.append(edge(f"{RG}/microsoft.web/sites/app", ID, "authenticates as"))
    folded = diagram.fold(nodes, edges)
    assert ID not in folded["hidden"] and folded["via"] == {}


def test_the_registry_sits_with_hosting_and_the_new_tab_can_focus():
    from healthconsole.core import taxonomy
    assert taxonomy.tier("Microsoft.ContainerRegistry/registries") == "platform"
    assert "function diagramFocus" in diagram.page("<svg></svg>", "t")


def test_browse_rows_say_their_band_and_whether_they_are_plumbing():
    from healthconsole.core import inventory
    aks = inventory._resource({"id": AKS, "name": "aks", "type": "microsoft.containerservice/managedclusters"})
    nic = inventory._resource({"id": NIC, "name": "nic", "type": "microsoft.network/networkinterfaces"})
    acr = inventory._resource({"id": ACR, "name": "acr", "type": "microsoft.containerregistry/registries"})
    assert (aks["tier_rank"], aks["plumbing"]) == (1, False) and acr["tier_rank"] == 2 and nic["plumbing"] is True


def _segments(path):
    """The straight segments of an M/V/H path."""
    tokens, out = path.split(), []
    x = y = None
    i = 0
    while i < len(tokens):
        op = tokens[i]
        if op == "M":
            x, y = float(tokens[i + 1]), float(tokens[i + 2])
            i += 3
        elif op in ("V", "H"):
            nx, ny = (x, float(tokens[i + 1])) if op == "V" else (float(tokens[i + 1]), y)
            out.append((x, y, nx, ny))
            x, y = nx, ny
            i += 2
        else:
            raise AssertionError(f"unexpected path op {op}")
    return out


def test_no_connector_passes_behind_a_box():
    import re
    # a crowded group: 12 storage accounts (3 rows in one band), each used by one of 6 apps, all linked to one vault
    apps = [f"{RG}/microsoft.web/sites/app{i}" for i in range(6)]
    stores = [f"{RG}/microsoft.storage/storageaccounts/st{i:02}" for i in range(12)]
    nodes = [node(a, "Web App") for a in apps] + [node(s, "Storage Account") for s in stores] + [node(KV, "Key Vault")]
    edges = [edge(stores[i], apps[i % 6]) for i in range(12)] + [edge(a, KV, "uses vault") for a in apps]
    svg = diagram.render_svg({"root_id": "", "nodes": nodes, "edges": edges})
    boxes = [(float(x), float(y), float(x) + diagram.NODE_W, float(y) + diagram.NODE_H, nid) for nid, x, y in
             re.findall(r'data-id="([^"]+)"[^>]*><title>[^<]*</title><rect class="dg-box" x="([\d.]+)" y="([\d.]+)"', svg)]
    assert len(boxes) == 19
    edges_svg = re.findall(r'data-source="([^"]+)" data-target="([^"]+)"><title>[^<]*</title><path d="([^"]+)"', svg)
    assert len(edges_svg) == len(edges)
    for source, target, path in edges_svg:
        for x1, y1, x2, y2 in _segments(path):
            for bx1, by1, bx2, by2, nid in boxes:
                if nid in (source, target):
                    continue
                inside_x = max(min(x1, x2), bx1 + 1) < min(max(x1, x2), bx2 - 1)
                inside_y = max(min(y1, y2), by1 + 1) < min(max(y1, y2), by2 - 1)
                crosses = (x1 == x2 and bx1 < x1 < bx2 and inside_y) or (y1 == y2 and by1 < y1 < by2 and inside_x)
                assert not crosses, f"{source} -> {target} crosses {nid}"


def test_a_problem_box_says_why_and_the_root_is_outlined():
    snap = snapshot()
    vault = next(n for n in snap["nodes"] if n["id"] == KV)
    vault.update(status="critical", signals=[{"source": "secrets", "name": "Secret expiry", "status": "critical",
                                               "detail": "db-password expired 2 days ago"},
                                              {"source": "metrics", "name": "Availability", "status": "healthy", "detail": "100%"}])
    snap["root_id"] = ACR
    svg = diagram.render_svg(snap)
    assert 'class="dg-why st-critical"' in svg and ">Secret expiry: db-pass…<" in svg
    assert "Secret expiry: db-password expired 2 days ago" in svg  # the whole reason in the tooltip
    assert "Availability" not in svg.split("Secret expiry")[0][-200:]
    assert re.search(r'class="dg-node [^"]* root" data-id="' + re.escape(ACR), svg)


def test_roles_on_wider_scopes_show_in_the_tooltip_of_whoever_holds_them():
    snap = snapshot()
    ident = next(n for n in snap["nodes"] if n["id"] == ID)
    ident["roles"] = [{"role": "Reader", "scope": "/subscriptions/s", "scope_kind": "subscription", "scope_name": "prod"}]
    svg = diagram.render_svg(snap)
    # the identity folds into AKS, so AKS's tooltip carries its role
    assert "Signs in as aks-id" in svg and "Also holds: Reader on subscription prod" in svg


def test_the_printable_report_and_the_new_tab_use_the_diagram():
    from healthconsole.core import report
    from healthconsole.core.model import Edge, Node, Snapshot
    snap = Snapshot(root_id="/subscriptions/s/resourcegroups/rg", tenant_id="t", taken_by="me", taken_at="2026-11-17 10:00 UTC",
                    depth=1, kind="group", title="Resource group rg",
                    nodes=[Node(id=KV, name="kv", type="microsoft.keyvault/vaults", display_type="Key Vault", hop=0),
                           Node(id=ACR, name="acr", type="microsoft.containerregistry/registries", display_type="Container Registry", hop=0)],
                    edges=[Edge(ACR, KV, "uses vault")])
    html = report.to_html(snap, printable=True)
    assert 'class="dg"' in html and f'data-id="{KV}"' in html
    assert "data-p=" in diagram.page("<svg></svg>", "t")  # the Problems button


def test_colours_come_only_from_the_consoles_theme():
    css = (diagram.STATIC / "style.css").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", diagram.STYLE)  # the diagram has no colours of its own
    page = diagram.page("<svg></svg>", "t")
    assert "--tier-compute" in page and "prefers-color-scheme: dark" in page  # the page carries the theme, both modes
    light = diagram.theme_css(light_only=True)
    assert light.startswith(":root {") and "--tier-compute" in light and "prefers-color-scheme" not in light
    assert light in css  # read from style.css, not a copy


def test_each_box_says_what_a_filter_can_match_including_what_people_call_its_type():
    svg = diagram.render_svg(snapshot())
    aks = re.search(r'data-id="' + re.escape(AKS) + r'" data-search="([^"]+)"', svg).group(1)
    assert "aks" in aks and "kubernetes" in aks and "k8s" in aks  # name, label, and the alias from taxonomy
    page = diagram.page(svg, "t")
    assert "data-f" in page and "focus.filter" in page


def test_the_console_search_and_the_diagram_filter_share_the_type_names():
    from healthconsole.core import taxonomy
    assert "k8s" in taxonomy.names_for("Microsoft.ContainerService/managedClusters")
    assert "microsoft.containerservice/managedclusters" in taxonomy.types_called("k8s")
