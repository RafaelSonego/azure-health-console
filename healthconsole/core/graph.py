"""Microsoft Graph, read-only and narrow, for the Access tab: your own groups, finding a user by name, and that
user's groups. GET only, on an allow-list of paths; anything else is refused before it is sent."""
import re
from urllib.parse import urlsplit

import requests

from .arm import ArmError, WriteBlocked
from .guarded import GuardedClient
from .cache import TtlCache

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_HOST = "graph.microsoft.com"
GROUPS_PATH = "/v1.0/me/transitiveMemberOf/microsoft.graph.group"
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
GUID = re.compile(f"^{_GUID}$")
_ALLOWED = [re.compile(r"^/v1\.0/me/transitiveMemberOf/microsoft\.graph\.group$"),
            re.compile(r"^/v1\.0/users$"),
            re.compile(f"^/v1\\.0/users/{_GUID}$"),
            re.compile(f"^/v1\\.0/users/{_GUID}/transitiveMemberOf/microsoft\\.graph\\.group$")]
USER_FIELDS = "id,displayName,userPrincipalName,mail,accountEnabled"
TIMEOUT = 20
MAX_PAGES = 20
CACHE_SECONDS = 300
SEARCH_CACHE_SECONDS = 60
SEARCH_LIMIT = 10
_cache = TtlCache("graph", CACHE_SECONDS)


def check_graph_read(method, url):
    parts = urlsplit(url)
    if method != "GET" or parts.netloc != GRAPH_HOST or not any(p.match(parts.path) for p in _ALLOWED):
        raise WriteBlocked(f"Only reading groups and finding users in Microsoft Graph is allowed, not {method} "
                           f"{parts.netloc}{parts.path}.")


def _message(response):
    try:
        return ((response.json().get("error") or {}).get("message") or "")[:300]
    except ValueError:
        return response.text[:300]


class _Graph(GuardedClient):
    label = "GRAPH"

    def check(self, method, url, **request):
        check_graph_read(method, url)

    def unreachable(self, method, url, error):
        return ArmError("Microsoft Graph couldn't be reached from this machine. Try again later.")


_GRAPH = _Graph()


def _get(token_provider, path, params, session=None, pages=MAX_PAGES):
    session = session or requests.Session()
    url, items = f"https://{GRAPH_HOST}{path}", []
    for _ in range(pages):
        check_graph_read("GET", url)  # before the token is fetched, so a refused path costs nothing
        response = _GRAPH.send(session, "GET", url, TIMEOUT, params=params,
                               headers={"Authorization": f"Bearer {token_provider.get_token(GRAPH_SCOPE)}"})
        if response.status_code >= 400:
            raise ArmError(f"Microsoft Graph answered {response.status_code}: {_message(response)}", response.status_code)
        data = response.json()
        if "value" not in data:
            return data
        items += data["value"]
        url, params = data.get("@odata.nextLink"), None
        if not url:
            break
    return items


def _viewer(token_provider):
    identity = token_provider.identity
    return f"{identity.tenant_id}/{identity.object_id}"


def _groups(token_provider, path, session):
    rows = _get(token_provider, path, {"$select": "id,displayName", "$top": "999"}, session)
    groups = [{"id": g.get("id"), "name": g.get("displayName") or g.get("id")} for g in rows]
    return sorted(groups, key=lambda g: (g["name"] or "").lower())


def my_groups(token_provider, session=None, refresh=False):
    """[{id, name}] of every group the signed-in user is in, directly or through other groups; cached per identity."""
    return _cache.get_or_load(_viewer(token_provider), lambda: _groups(token_provider, GROUPS_PATH, session), refresh)


def _user(row):
    return {"id": row.get("id"), "name": row.get("displayName") or "", "upn": row.get("userPrincipalName") or "",
            "mail": row.get("mail") or "", "enabled": row.get("accountEnabled") is not False}


def search_users(token_provider, text, session=None):
    """Up to 10 users whose name, sign-in name or mail starts with `text` (an object ID finds that user)."""
    text = (text or "").strip()
    if len(text) < 2:
        return []

    def load():
        if GUID.match(text):
            try:
                return [_user(_get(token_provider, f"/v1.0/users/{text.lower()}", {"$select": USER_FIELDS}, session))]
            except ArmError as error:
                if error.status == 404:
                    return []
                raise
        quoted = text.replace("'", "''")
        query = " or ".join(f"startswith({field},'{quoted}')" for field in ("displayName", "userPrincipalName", "mail"))
        rows = _get(token_provider, "/v1.0/users", {"$filter": query, "$select": USER_FIELDS, "$top": str(SEARCH_LIMIT)},
                    session, pages=1)
        return [_user(r) for r in rows]
    return _cache.get_or_load(f"{_viewer(token_provider)}|search|{text.lower()}", load, ttl=SEARCH_CACHE_SECONDS)


def user_groups(token_provider, user_id, session=None, refresh=False):
    """Every group `user_id` is in, directly or through other groups (not ones they're only eligible to join)."""
    if not GUID.match(user_id or ""):
        raise ValueError("A user's object ID is needed.")
    path = f"/v1.0/users/{user_id.lower()}/transitiveMemberOf/microsoft.graph.group"
    return _cache.get_or_load(f"{_viewer(token_provider)}|groups|{user_id.lower()}",
                              lambda: _groups(token_provider, path, session), refresh)


def forget():
    _cache.clear()
