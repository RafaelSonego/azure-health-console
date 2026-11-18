"""Microsoft's troubleshooting catalogue for types without detectors: support services, their problem types, the
solutions each problem type offers and the self-help guides' text. All tenant-level GETs; a Diagnostics or Solutions
entry opens only by a PUT on Microsoft.Help (selfHelp refuses them with 403), so those are listed for the portal."""
from html.parser import HTMLParser

from .. import ids
from ..cache import TtlCache

SUPPORT_API = "2024-04-01"
HELP_API = "2024-03-01-preview"
CATALOGUE_TTL_SECONDS = 24 * 3600
SOLUTION_KINDS = {"SelfHelp": "guide", "Solutions": "portal", "Diagnostics": "portal"}
# Reference data, the same for every viewer: Refresh access doesn't clear it.
_catalogue = TtlCache("troubleshooting catalogue", CATALOGUE_TTL_SECONDS, access_bound=False)


def services(client, resource_id):
    """The support services that list this resource's type, e.g. Blob Storage and Files Storage for a storage account."""
    resource_type = ids.resource_type_of(resource_id).lower()
    listed = _catalogue.get_or_load("services", lambda: client.get_paged(
        "/providers/Microsoft.Support/services", SUPPORT_API))
    found = [{"id": item.get("name"), "name": (item.get("properties") or {}).get("displayName") or item.get("name")}
             for item in listed
             if resource_type in {t.lower() for t in (item.get("properties") or {}).get("resourceTypes") or []}]
    return sorted(found, key=lambda s: s["name"].lower())


def problems(client, service_id):
    """A service's problem types, grouped as the portal's two dropdowns ("Group / Problem")."""
    listed = _catalogue.get_or_load(("problems", service_id), lambda: client.get_paged(
        f"/providers/Microsoft.Support/services/{service_id}/problemClassifications", SUPPORT_API))
    out = []
    for item in listed:
        group, _, name = ((item.get("properties") or {}).get("displayName") or item.get("name") or "").partition(" / ")
        out.append({"id": item.get("name"), "group": group.strip(), "name": (name or group).strip()})
    return sorted(out, key=lambda p: (p["group"].lower(), p["name"].lower()))


def solutions(client, problem_id):
    """What Microsoft offers for one problem type: guides the console can show, and what only the portal opens."""
    def load():
        payload = client.get("/providers/Microsoft.Help/discoverySolutions", HELP_API,
                             params={"$filter": f"ProblemClassificationId eq '{problem_id}'"},
                             tolerate=(400, 404)) or {}  # 400: "ProblemClassificationId(s) not supported"
        found = []
        for item in payload.get("value") or []:
            for solution in (item.get("properties") or {}).get("solutions") or []:
                found.append({"id": solution.get("solutionId"), "description": solution.get("description") or "",
                              "kind": SOLUTION_KINDS.get(solution.get("solutionType"), "other"),
                              "inputs": solution.get("requiredInputs") or []})
        return found
    return _catalogue.get_or_load(("solutions", problem_id), load)


def guide(client, solution_id):
    """A self-help guide's title and text, as Markdown the page renders safely."""
    def load():
        props = (client.get(f"/providers/Microsoft.Help/selfHelp/{solution_id}", HELP_API) or {}).get("properties") or {}
        return {"id": solution_id, "title": props.get("title") or "", "text": html_to_markdown(props.get("content") or "")}
    return _catalogue.get_or_load(("guide", solution_id), load)


class _ToMarkdown(HTMLParser):
    _BLOCKS = {"p", "div", "ul", "ol", "blockquote", "table", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self._href = [], None

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n#### ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "code":
            self.out.append("`")
        elif tag == "br" or tag in self._BLOCKS:
            if not (self.out and self.out[-1] == "\n- "):  # <li><p>…: the text stays on the bullet
                self.out.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith(("https://", "http://")) else None
            if self._href:
                self.out.append("[")

    def handle_endtag(self, tag):
        if tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "code":
            self.out.append("`")
        elif tag == "a" and self._href:
            self.out.append(f"]({self._href})")
            self._href = None
        elif tag in self._BLOCKS or tag.startswith("h"):
            self.out.append("\n\n")

    def handle_data(self, data):
        self.out.append(data.replace("\n", " "))


def html_to_markdown(html):
    """Headings, lists, links, bold and code kept; everything else (comments marking embedded widgets) dropped."""
    parser = _ToMarkdown()
    parser.feed(html)
    lines = [line.strip() for line in "".join(parser.out).splitlines()]
    text, blank = [], False
    for line in lines:
        if line or not blank:
            text.append(line)
        blank = not line
    return "\n".join(text).strip()
