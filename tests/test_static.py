import re
from collections import Counter
from pathlib import Path

from healthconsole.web import SCRIPTS

STATIC = Path(__file__).resolve().parents[1] / "healthconsole" / "web" / "static"
TEMPLATES = STATIC.parent / "templates"
TOP_LEVEL = re.compile(r"^(?:async\s+)?function\s+([\w$]+)|^(?:const|let|class)\s+([\w$]+)", re.M)


def app_js():
    """The page's script: every file in load order, as one text."""
    return "".join((STATIC / name).read_text() for name in SCRIPTS)


def test_every_script_file_is_loaded_and_each_runs_strict():
    on_disk = {f"js/{f.name}" for f in (STATIC / "js").glob("*.js")}
    assert on_disk == set(SCRIPTS), f"not loaded: {on_disk - set(SCRIPTS)}; missing: {set(SCRIPTS) - on_disk}"
    assert all((STATIC / name).read_text().startswith('"use strict";') for name in SCRIPTS)


def test_load_time_calls_only_use_what_an_earlier_file_declares():
    # A bare top-level call, or a handler passed by name, runs while the files load: across files nothing is hoisted.
    declared_in = {}
    for i, name in enumerate(SCRIPTS):
        for found in top_level_names((STATIC / name).read_text()):
            declared_in.setdefault(found, i)
    late = []
    for i, name in enumerate(SCRIPTS):
        for line in (STATIC / name).read_text().splitlines():
            used = re.findall(r"^([A-Za-z_$][\w$]*)\(", line) + re.findall(r'^\S.*addEventListener\("[\w-]+", ([A-Za-z_$][\w$]*)\)', line)
            late += [f"{name}: {u}" for u in used if declared_in.get(u, -1) > i]
    assert not late, f"used at load before its file has loaded: {late}"


def top_level_names(source):
    return [a or b for a, b in TOP_LEVEL.findall(source)]


def test_no_top_level_name_declared_twice():
    twice = sorted(name for name, count in Counter(top_level_names(app_js())).items() if count > 1)
    assert not twice, f"declared more than once in app.js (the last one silently wins): {twice}"


def test_finds_a_repeated_function():
    assert top_level_names("function ago(a) {}\nconst x = 1;\nasync function ago(b) {}\n") == ["ago", "x", "ago"]


def test_page_asks_for_versioned_files_its_script_still_finds():
    from flask import Flask

    from healthconsole.core import config
    from healthconsole.web import create_blueprint
    from tests.conftest import FakeClient
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant=None: FakeClient(), config.load("/nonexistent"), authorize=lambda: None))
    page = app.test_client().get("/").get_data(as_text=True)
    sources = re.findall(r'(?:src|href)="([^"]*static/(?:js/[\w-]+\.js|style\.css)[^"]*)"', page)
    assert len(sources) == len(SCRIPTS) + 1 and all(re.search(r"\?v=\d+$", s) for s in sources)
    scripts = [s.split("static/")[1].split("?")[0] for s in sources if "/js/" in s]
    assert scripts == SCRIPTS  # in load order: a file may use at load only what an earlier one declares
    # BASE is cut from the first script's own address: the page's root, wherever the blueprint is mounted
    assert 'const BASE = document.currentScript.src.replace(/static\\/js\\/[^/]*$/, "");' in (STATIC / SCRIPTS[0]).read_text()


def test_portal_links_in_the_page_get_the_tenant():
    html = (TEMPLATES / "index.html").read_text()
    links = re.findall(r'<a [^>]*href="https://portal\.azure\.com/#[^"]*"[^>]*>', html)
    assert links and all("data-portal=" in a for a in links)  # applyContext fills in #@tenant from data-portal
    assert "tenantPortal(data.activate_url)" in app_js()


def test_functions_that_pass_kind_to_tool_params_define_it():
    bodies = re.findall(r"^(?:async )?function (\w+)\(([^)]*)\)(.*?)^\}", app_js(), re.S | re.M)
    defines = re.compile(r"\b(?:const|let)\s+kind\b|\bfor \((?:const|let) kind of\b")
    missing = [name for name, params, body in bodies
               if "toolParams(kind" in body and not re.search(r"\bkind\b", params) and not defines.search(body)]
    assert bodies and not missing, f"kind is never defined in: {missing}"


def test_every_setting_section_is_drawn_on_the_settings_page():
    import re
    from healthconsole.core import config
    js = app_js()
    pane_of = dict(re.findall(r'(\w+): "(\w+)"', re.search(r"const PANE_OF = \{([^}]*)\}", js).group(1)))
    drawn = set(re.findall(r'\["(\w+)", "[^"]*"\]', re.search(r"const THRESHOLD_SECTIONS = \[(.*?)\];", js, re.S).group(1)))
    sections = {section for section, _key, _kind, _help in config.SCHEMA if section}
    assert sections <= set(pane_of), f"no pane: {sections - set(pane_of)}"
    missing = {s for s in sections if pane_of[s] == "thresholds"} - drawn
    assert not missing, f"in the Thresholds pane but never drawn: {missing}"


def test_every_built_link_goes_through_safe_href():
    js = app_js()
    unchecked = [m.group(0) for m in re.finditer(r'href="\$\{(?!safeHref\()[^}]*\}', js)]
    assert not unchecked or unchecked == ['href="${url}'], unchecked  # inlineMarkdown's own link only takes https?://
    assert "safeHref(url)) window.open(url" in js  # the ServiceNow sign-in address too



def test_nothing_reaches_the_api_without_the_launch_key_header():
    js, html = app_js(), (TEMPLATES / "index.html").read_text()
    assert "sendBeacon" not in js  # a beacon can't carry a header
    assert not re.search(r'href="(?:\$\{[^}]*BASE[^}]*\})?api/', js + html)  # a plain link can't either
    assert not re.search(r"window\.open\((?!\"\")", js.replace('window.open(url, "_blank", "noopener");', ""))
    assert "headers.set(LAUNCH_HEADER, key)" in js and 'LAUNCH_HEADER = "X-AHC-Key"' in js


def test_the_page_takes_the_key_from_the_fragment_only_and_cleans_the_address():
    js = (STATIC / "js" / "core.js").read_text()
    assert "location.hash.slice(1)" in js and 'searchParams.get("key")' not in js  # a query reaches the server
    assert "/^[A-Za-z0-9_-]{32,128}$/.test(key)" in js
    assert 'history.replaceState(null, "", location.pathname + location.search)' in js


def test_list_settings_are_compared_by_their_contents():
    js = app_js()
    body = re.search(r"^function settingsChanges\(\) \{(.*?)^\}", js, re.S | re.M).group(1)
    assert "!sameSetting(now, field.value)" in body and "now !== field.value" not in body  # else a list stays unsaved
    assert "JSON.stringify(a || []) === JSON.stringify(b || [])" in js


def test_the_folded_browse_strip_says_what_it_hides():
    js, html = app_js(), (TEMPLATES / "index.html").read_text()
    assert 'id="pane-unfold-count"' in html and 'id="pane-unfold-filter"' in html
    describe = re.search(r"const describe = \(\) => \{(.*?)\n  \};", js, re.S).group(1)
    assert '$("rg-count").textContent' in describe and '$("rg-filter").value' in describe
    assert 'observe($("rg-count")' in js and '$("rg-filter").addEventListener("input", describe)' in js


def test_every_partial_result_note_can_be_closed():
    js = app_js()
    assert ".observe(document.body, { childList: true, subtree: true })" in js  # notes drawn later get the × too
    assert "closedNotices.has(key)" in js and "closedNotices.add(note.dataset.noticeKey)" in js
    assert js.count('class="notice partial"') >= 20  # the notes it covers keep the class it looks for


def test_investigate_counts_say_their_window_next_to_most_recent():
    js = app_js()
    assert '"Failed with it"' not in js and '"Last failure"' not in js
    assert js.count("in the ${invSpan()}") >= 4 and "of all failures" in js and "most recent <strong>${esc(ago(r.last_seen))}" in js


def test_investigate_keeps_a_trail_and_folds_step_two_when_going_deeper():
    js, html = app_js(), (TEMPLATES / "index.html").read_text()
    assert 'id="inv-trail"' in html and "data-inv-unfold" in html
    step = re.search(r"^function invStep\(id, html\) \{(.*?)^\}", js, re.S | re.M).group(1)
    assert "renderInvTrail()" in step  # every step that opens updates the trail
    assert 'invCameFrom(b); loadInvSamples(' in js and re.search(r"invCameFrom\(b\);\s*invFollowedSample\(b\);\s*showInvSub", js)
    loader = re.search(r"^async function loadInvOperations\(\) \{(.*?)^\}", js, re.S | re.M).group(1)
    assert "inv.picked = null" in loader and '"collapsed"' in loader  # a new app starts unfolded


def test_a_component_with_many_apps_lists_a_few_and_stays_filterable():
    js = app_js()
    body = re.search(r"^function invAppsHtml\(c\) \{(.*?)^\}", js, re.S | re.M).group(1)
    assert "names.slice(0, INV_REPORTERS_SHOWN)" in body and "more</span>" in body
    assert "<span hidden>" in body  # the rest stay in the row's text, which the list filter searches


def test_a_followed_sample_folds_its_list_and_the_way_back_is_on_both_steps():
    js, html = app_js(), (TEMPLATES / "index.html").read_text()
    assert html.count("data-inv-back-samples") == 2  # step 3's folded heading and step 4's heading
    followed = re.search(r"^function invFollowedSample\(button\) \{(.*?)^\}", js, re.S | re.M).group(1)
    assert '"collapsed"' in followed and "inv.followedTraces" in followed and "✓ followed" in followed
    assert "inv.followedTraces || new Set()).has(r.trace_id)" in js  # marks survive the list being drawn again


def test_step_one_lists_what_needs_a_look_and_finds_any_component_by_name():
    js, html = app_js(), (TEMPLATES / "index.html").read_text()
    assert 'id="inv-app-find"' in html and "Pick the app" in html and "Which app is failing?" not in html
    assert "Boolean(c.status) || (c.server_errors || 0) > 0" in js  # 5xx counts even when the rate rule stays quiet
    shown = re.search(r"^function invAppsShown\(all\) \{(.*?)^\}", js, re.S | re.M).group(1)
    assert "invReporters(c)" in shown and "inv.appsAll ? all" in shown
    assert 'list.dataset.nofilter = ""' in js  # one search box on that list, not two



def test_a_components_status_says_why_with_the_setting_behind_it():
    js = app_js()
    assert js.count("${aiWhyHtml(c)}") == 2  # Investigate's list and the resource page's App Insights panel
    assert re.search(r"AI_CAUSE_SETTINGS = \{ failed: \"appinsights\.failed_rate_points\", exceptions: \"appinsights\.exceptions_percent\",\s*steady: \"appinsights\.critical_rate_percent\" \}", js)


def test_sign_in_offers_a_tenant_and_each_skipped_tenant_its_own_button():
    js = (STATIC / "js" / "signin.js").read_text()
    assert 'id="signin-screen-tenant"' in js and "renderSignInControls(box, field.value.trim())" in js
    assert 'data-signin-tenant="${esc(t.id)}"' in js and "[data-signin-tenant], [data-signin-continue]" in js
    assert "skippedHtml(data, output)" in js


def test_no_error_report_is_sent_once_the_launch_key_is_refused():
    js = (STATIC / "js" / "core.js").read_text()
    assert "launchKeyRefused = true;" in js and "if (launchKeyRefused || pageErrorsSent.has(key)" in js


def test_sign_out_is_its_own_menu_item_and_the_switch_panel_only_signs_in():
    html, js = (TEMPLATES / "index.html").read_text(), (STATIC / "js" / "signin.js").read_text()
    assert 'id="signout-menu"' in html and 'id="signout-account"' in html and "Switch account or tenant" in html
    assert '$("signout-menu").addEventListener("click"' in js and "signout-run" not in js
    assert "#signout-menu" in (STATIC / "js" / "boot.js").read_text()  # the menu closes when it's picked


def test_panes_turns_off_every_horizontal_scroll_box_around_tables():
    """A box with overflow-x: auto is a scroll container: Panes' sticky table headers (top: --head-h) would stick
    inside it, under the first rows, instead of under the view's header."""
    css = (STATIC / "style.css").read_text()
    boxes = re.search(r"\n([^\n{]*#inv-apps-out[^\n{]*)\{ overflow-x: auto; \}", css).group(1)
    panes = re.search(r"body\.layout-panes :is\(([^)]*)\) \{\s*overflow-x: visible;", css).group(1)
    turned_off = {p.strip() for p in panes.split(",")}
    assert {b.strip() for b in boxes.split(",")} <= turned_off
    # Azure's detector text (.md) and every section's body hold tables too, each in a rule of its own.
    for box in (".md", ".section-body"):
        assert re.search(rf"\n{re.escape(box)} \{{[^}}]*overflow-x: auto", css), box
        assert box in turned_off, box


def test_a_section_s_header_counts_what_later_reads_add():
    """OOM kills and warning events from a monitoring tool arrive after the page is drawn: the header must count them
    (it read "none" above New Relic's kills), and What's wrong now must count every kill, not the rows listed."""
    sections, workloads = (STATIC / "js" / "sections.js").read_text(), (STATIC / "js" / "aks-workloads.js").read_text()
    assert 'setPartCount(body, "ev-oom", ciRows("oomkilled").length + extra.extra)' in sections
    assert 'setPartCount(body, "ev-events", eventRows.length + extra.extra)' in sections
    assert "mergeMonitorOom(problems, extra.rows, extra.label, extra.byWorkload)" in sections
    assert "for (const r of byWorkload || rows)" in workloads


def test_every_investigate_section_folds_and_is_read_through_one_gate():
    js, html = app_js(), (TEMPLATES / "index.html").read_text()
    parts = dict(re.findall(r'<details class="inv-part" id="([^"]+)" data-part="([^"]+)" open>', html))
    gate = dict((pid, key) for key, pid in re.findall(r'(\w+): "(inv-\w+-part)"', re.search(r"const INV_PARTS = \{[^}]*\}", js).group(0)))
    assert parts == gate and len(parts) == 5 and 'data-inv-parts="open"' in html and 'data-inv-parts="close"' in html
    body = re.search(r"async function loadInvOperations\(\) \{(.*?)\n\}", js, re.S).group(1)
    assert all(f'invPart("{key}"' in body for key in parts.values()) and not re.search(r"\n  loadInv\w+\(", body)


def test_fmt_escapes_text_so_no_caller_has_to():
    # fmt's output goes straight into HTML in most of its ~250 calls: a string from Azure must come back escaped.
    body = re.search(r"^function fmt\(value\) \{\n(.*?)^\}", app_js(), re.M | re.S).group(1)
    assert 'typeof value !== "number") return esc(String(value))' in body
    assert "esc(fmt(" not in app_js()  # escaping twice would show &amp; on the page
