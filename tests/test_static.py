import re
from collections import Counter
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "healthconsole" / "web" / "static" / "app.js"
TOP_LEVEL = re.compile(r"^(?:async\s+)?function\s+([\w$]+)|^(?:const|let|class)\s+([\w$]+)", re.M)


def top_level_names(source):
    return [a or b for a, b in TOP_LEVEL.findall(source)]


def test_no_top_level_name_declared_twice():
    twice = sorted(name for name, count in Counter(top_level_names(APP_JS.read_text())).items() if count > 1)
    assert not twice, f"declared more than once in app.js (the last one silently wins): {twice}"


def test_finds_a_repeated_function():
    assert top_level_names("function ago(a) {}\nconst x = 1;\nasync function ago(b) {}\n") == ["ago", "x", "ago"]


def test_page_asks_for_versioned_files_its_script_still_finds():
    from flask import Flask

    from healthconsole.core import config
    from healthconsole.web import create_blueprint
    from tests.conftest import FakeClient
    app = Flask("test")
    app.register_blueprint(create_blueprint(lambda tenant=None: FakeClient(), config.load("/nonexistent")))
    page = app.test_client().get("/").get_data(as_text=True)
    sources = re.findall(r'(?:src|href)="([^"]*static/(?:app\.js|style\.css)[^"]*)"', page)
    assert len(sources) == 2 and all(re.search(r"\?v=\d+$", s) for s in sources)
    finder = re.search(r"""querySelector\("script\[src\*='([^']+)'\]"\)""", APP_JS.read_text())
    assert finder and any(finder.group(1) in s for s in sources if "app.js" in s)  # else BASE is null and nothing loads


def test_portal_links_in_the_page_get_the_tenant():
    html = (APP_JS.parent.parent / "templates" / "index.html").read_text()
    links = re.findall(r'<a [^>]*href="https://portal\.azure\.com/#[^"]*"[^>]*>', html)
    assert links and all("data-portal=" in a for a in links)  # applyContext fills in #@tenant from data-portal
    assert "tenantPortal(data.activate_url)" in APP_JS.read_text()


def test_functions_that_pass_kind_to_tool_params_define_it():
    bodies = re.findall(r"^(?:async )?function (\w+)\(([^)]*)\)(.*?)^\}", APP_JS.read_text(), re.S | re.M)
    defines = re.compile(r"\b(?:const|let)\s+kind\b|\bfor \((?:const|let) kind of\b")
    missing = [name for name, params, body in bodies
               if "toolParams(kind" in body and not re.search(r"\bkind\b", params) and not defines.search(body)]
    assert bodies and not missing, f"kind is never defined in: {missing}"
