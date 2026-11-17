"""A read-only New Relic client. NerdGraph is GraphQL over POST, so the guard parses each request and lets
queries through, never a mutation or subscription. The User API key sees what its user can see."""
import re
import time
from urllib.parse import urlsplit

import requests

from . import secrets
from .. import guarded
from ..guarded import GuardedClient
from .provider import ProviderError


ENDPOINTS = {"us": "https://api.newrelic.com/graphql", "eu": "https://api.eu.newrelic.com/graphql"}
TIMEOUT_SECONDS = 20
SECRET_FIELDS = ("api_key",)
_COMMENT = re.compile(r"#[^\n]*")
_STRING = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"')
_OPERATION = re.compile(r"(?:^|[}\s])(mutation|subscription)\b")


class NewRelicError(ProviderError):
    """NerdGraph's answer when it isn't data: auth, access, network or config."""


class WriteBlocked(guarded.WriteBlocked):
    """Something tried a NerdGraph operation that isn't a query."""


def check_read_only(method, url, body):
    parts = urlsplit(url)
    if method != "POST" or f"{parts.scheme}://{parts.netloc}{parts.path}" not in ENDPOINTS.values():
        raise WriteBlocked(f"{method} {url} is not the NerdGraph endpoint.")
    query = (body or {}).get("query")
    if not isinstance(query, str) or not query.strip():
        raise WriteBlocked("A NerdGraph request needs a query.")
    code = _STRING.sub('""', _COMMENT.sub("", query))  # names inside strings don't count
    if _OPERATION.search(code):
        raise WriteBlocked("Only NerdGraph queries are allowed; mutations and subscriptions are refused.")


class NewRelicClient(GuardedClient):
    """NerdGraph: POSTs of queries only; `check_read_only` reads the query and refuses mutations and subscriptions."""
    label = "NR"

    def check(self, method, url, **request):
        check_read_only(method, url, request.get("json"))

    def unreachable(self, method, url, error):
        return NewRelicError(f"New Relic didn't answer: {error}", "network")

    def __init__(self, monitor, session=None, typed=None):
        self.monitor = monitor
        self.typed = {k: v for k, v in (typed or {}).items() if v}
        self.target = f"monitor:{monitor['name']}"
        self.endpoint = ENDPOINTS[monitor.get("region") or "us"]
        self._session = session or requests.Session()
        self.last_errors = []

    def query(self, text, variables=None):
        key = self.typed.get("api_key") or secrets.get(self.target, self.monitor.get("tenant") or "newrelic", "api_key")
        if not key:
            raise NewRelicError(f"No New Relic API key stored for '{self.monitor['name']}' (Settings → Integrations).")
        body = {"query": text, "variables": variables or {}}
        response = self.send(self._session, "POST", self.endpoint, TIMEOUT_SECONDS, json=body,
                             headers={"API-Key": key, "Content-Type": "application/json"})
        if response.status_code in (401, 403):
            raise NewRelicError("New Relic rejected the API key. It must be a User key (starts with NRAK-) "
                                f"for the {self.monitor.get('region', 'us').upper()} region.", "auth")
        if not response.ok:
            raise NewRelicError(f"New Relic returned {response.status_code}.", "network")
        payload = response.json()
        errors = [e.get("message", "") for e in payload.get("errors") or []]
        if errors and not payload.get("data"):
            raise NewRelicError("New Relic said: " + "; ".join(errors)[:300], "access")
        self.last_errors = errors
        return payload.get("data") or {}

    def _partial(self, value):
        """A missing part of a partial answer is an error, not an empty list."""
        if value is None and self.last_errors:
            raise NewRelicError("New Relic said: " + "; ".join(self.last_errors)[:300], "access")
        return value or {}

    def test(self):
        """Who the key belongs to, and which configured account IDs it can read."""
        data = self.query("{ actor { user { name email } accounts { id name } } }")
        actor = data.get("actor") or {}
        visible = {a["id"]: a["name"] for a in actor.get("accounts") or []}
        wanted = self.monitor.get("account_ids") or []
        accounts = [{"id": i, "ok": i in visible, "detail": visible.get(i) or "not visible to this key"} for i in wanted]
        user = actor.get("user") or {}
        return {"ok": bool(visible) and all(a["ok"] for a in accounts), "who": user.get("name") or user.get("email") or "",
                "accounts": accounts, "visible": [{"id": i, "name": n} for i, n in sorted(visible.items())]}

    ENTITY_FIELDS = "guid name entityType domain type alertSeverity reporting permalink account { id name } tags { key values }"

    def entities(self, condition, limit):
        """(entities, truncated) from an entity search over the configured accounts, `condition` ANDed in."""
        accounts = ", ".join(f"'{int(a)}'" for a in self.monitor.get("account_ids") or [])
        search = f"accountId IN ({accounts}) AND {condition}"
        text = ("query($q: String!, $cursor: String) { actor { entitySearch(query: $q) { count "
                f"results(cursor: $cursor) {{ nextCursor entities {{ {self.ENTITY_FIELDS} }} }} }} }} }}")
        found, cursor = [], None
        while True:
            data = self.query(text, {"q": search, "cursor": cursor})
            page = self._partial(((data.get("actor") or {}).get("entitySearch") or {}).get("results"))
            found += page.get("entities") or []
            cursor = page.get("nextCursor")
            if not cursor or len(found) >= limit:
                return found[:limit], bool(cursor) or len(found) > limit

    def open_issues(self, account_id, days, limit):
        """Activated issues in one account (New Relic's grouping of alert incidents), newest first."""
        now = int(time.time() * 1000)
        text = ("query($id: Int!, $cursor: String, $start: EpochMilliseconds!, $end: EpochMilliseconds!) { actor {"
                " account(id: $id) { aiIssues { issues(filter: {states: [ACTIVATED]}, cursor: $cursor,"
                " timeWindow: {startTime: $start, endTime: $end}) { nextCursor issues { issueId title priority state"
                " entityGuids entityNames activatedAt createdAt conditionName policyName totalIncidents deepLinkUrl } } } } } }")
        found, cursor = [], None
        while True:
            variables = {"id": int(account_id), "cursor": cursor, "start": now - days * 86_400_000, "end": now}
            account = (self.query(text, variables).get("actor") or {}).get("account") or {}
            page = self._partial((account.get("aiIssues") or {}).get("issues"))
            found += page.get("issues") or []
            cursor = page.get("nextCursor")
            if not cursor or len(found) >= limit:
                return found[:limit], bool(cursor) or len(found) > limit

    CONDITION_FIELDS = ("id name enabled policyId type entityGuid runbookUrl nrql { query } terms { operator threshold thresholdDuration"
                        " thresholdOccurrences priority } signal { aggregationWindow aggregationMethod }")

    def nrql_conditions(self, account_id, name):
        """NRQL alert conditions named `name` in one account (New Relic matches the name without regard to case)."""
        text = ("query($id: Int!, $name: String!) { actor { account(id: $id) { alerts {"
                " nrqlConditionsSearch(searchCriteria: {name: $name}) { nrqlConditions { "
                f"{self.CONDITION_FIELDS} }} }} }} }} }} }}")
        account = (self.query(text, {"id": int(account_id), "name": name}).get("actor") or {}).get("account") or {}
        found = self._partial((account.get("alerts") or {}).get("nrqlConditionsSearch"))
        return found.get("nrqlConditions") or []

    def nrql(self, account_id, nrql):
        """Rows of one NRQL query in one account."""
        text = "query($id: Int!, $q: Nrql!) { actor { account(id: $id) { nrql(query: $q) { results } } } }"
        account = (self.query(text, {"id": int(account_id), "q": nrql}).get("actor") or {}).get("account") or {}
        return self._partial(account.get("nrql")).get("results") or []
