"""Every Azure call goes through `ArmClient`, which is read-only by construction:
`check_read_only` rejects anything but GETs and Resource Graph queries before sending."""
import copy
import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from . import access, ids
from .cache import TtlCache
from .credentials import ARM_SCOPE

from .guarded import GuardedClient, WriteBlocked, calls  # noqa: F401 (re-exported for the modules that import them from here)

logger = logging.getLogger(__name__)

ARM_HOST = "management.azure.com"
ARM_ENDPOINT = f"https://{ARM_HOST}"
RESOURCE_GRAPH_PATH = "/providers/microsoft.resourcegraph/resources"
RESOURCE_GRAPH_API_VERSION = "2022-10-01"
PROVIDERS_API_VERSION = "2021-04-01"

REQUEST_TIMEOUT_SECONDS = 30
MAX_THROTTLE_RETRIES = 3
ON_SCREEN_THROTTLE_SECONDS = 20  # a page read waits this long at most on 429s; a background job waits them out
# A dropped connection (often a pooled one Azure closed) is retried; every request is a read, so it's safe.
CONNECTION_RETRIES = 2
CACHE_TTL_SECONDS = 60
REFUSAL_SECONDS = 300  # a refused read is answered from memory until then, or until Refresh access / Refresh
GRAPH_PAGE_SIZE = 1000
GRAPH_SUBSCRIPTION_BATCH = 500
MAX_CONNECTIONS = 32


class ArmError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AccessDenied(ArmError):
    """403. Carries what was being read and the role that grants it (see access.py)."""

    def __init__(self, path, azure_message=""):
        self.what, self.role = access.describe(path)
        self.azure_message = azure_message
        super().__init__(access.message(self.what, self.role, azure_message), 403)

    @property
    def short(self):
        return access.short(self.what, self.role)

    def to_dict(self):
        return {"what": self.what, "required": self.role, "azure_message": self.azure_message,
                "pim_hint": access.PIM_HINT}


class Throttled(ArmError):
    """Azure kept answering 429 (too many requests); `retry_after` is how long it asked to wait."""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Azure is throttling these reads (too many requests). Try again in {retry_after} s.", 429)


class ArmTimeout(ArmError):
    """Azure didn't answer in time. Reported for that one request, never fatal."""


class Cancelled(BaseException):
    """The job this call belongs to was cancelled. A BaseException so the per-signal
    `except Exception` guards don't swallow it and the job stops at once."""


# The only POSTs allowed: each is a read that Azure exposes as an action.
_READ_POSTS = (
    re.compile(r"^/providers/microsoft\.resourcegraph/resources$"),
    re.compile(r"^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/.+/providers/microsoft\.insights/logs$"),
    re.compile(r"^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.containerservice"
               r"/managedclusters/[^/]+/listclusterusercredential$"),
    re.compile(r"^(/subscriptions/[^/]+(/resourcegroups/[^/]+)?|/providers/microsoft\.management/managementgroups/[^/]+)"
               r"/providers/microsoft\.costmanagement/(query|forecast)$"),
)


def check_read_only(method, url):
    parts = urlsplit(url)
    host = parts.netloc.lower().removesuffix(":443")  # some nextLinks (API Management's) name the port
    if parts.scheme != "https" or host != ARM_HOST:
        raise WriteBlocked(f"Only {ARM_HOST} is allowed, not '{parts.netloc or url}'.")
    if method == "GET":
        return
    path = parts.path.rstrip("/").lower()
    if method == "POST" and any(pattern.match(path) for pattern in _READ_POSTS):
        return
    raise WriteBlocked(
        f"{method} {parts.path} is not a read. Only GETs, Resource Graph, Log Analytics and "
        f"Cost Management queries, and AKS user credentials are allowed."
    )


def kql_string(text):
    """A KQL string literal; JSON string escaping is valid KQL."""
    return json.dumps(str(text))


def kql_list(values):
    """A KQL dynamic array literal; JSON string escaping is valid KQL."""
    return f"dynamic({json.dumps(sorted(set(values)))})"


def rg_clause(group_keys):
    """Limit a query to resource groups ("sub/rg") picked by tag filters; empty means no limit."""
    if group_keys is None:
        return ""
    return (" | where strcat(tolower(subscriptionId), '/', tolower(resourceGroup)) in "
            f"({kql_list([k.lower() for k in group_keys])})")


def _retry_after(response):
    """Retry-After, or the longest of Cost Management's own `x-ms-ratelimit-…-retry-after` headers."""
    waits = [v for k, v in response.headers.items() if k.lower() == "retry-after"
             or (k.lower().startswith("x-ms-ratelimit-") and k.lower().endswith("-retry-after"))]
    try:
        return max(int(float(v)) for v in waits) if waits else 5
    except ValueError:
        return 5


def _error_message(response):
    """Azure's message plus nested details: Resource Graph puts the useful part
    ("Query is invalid", "ParserFailure") there, and only a correlation ID on top."""
    try:
        body = response.json()
    except ValueError:
        text = response.text or ""
        if "<html" in text[:500].lower():  # a gateway's error page: its title, never the markup
            title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            return f"an error page instead of an answer ({' '.join(title.group(1).split())[:120]})" if title \
                else "an error page instead of an answer"
        return text[:200]
    if not isinstance(body, dict):  # some detectors answer with a bare JSON string or list
        return str(body)[:500]
    error = body.get("error") or {}
    if not isinstance(error, dict):
        return str(error)[:500]
    parts = [error.get("message") or error.get("code") or ""]
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        text = detail.get("message") or detail.get("code")
        if text and text not in parts:
            parts.append(text)
    return " | ".join(p for p in parts if p)[:500]


def _pooled_session():
    """Connections are reused up to MAX_CONNECTIONS; beyond that a request waits for one to free
    up, which also caps how many requests the console sends to Azure at once."""
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=MAX_CONNECTIONS, pool_block=True))
    return session


class ArmClient(GuardedClient):
    """Azure Resource Manager: GETs, plus the few read-only POSTs `check_read_only` lists; cached per identity, with
    retries for throttling and dropped connections."""
    label = "ARM"

    def check(self, method, url, **request):
        check_read_only(method, url)

    def describe(self, method, url, request):
        return f"{urlsplit(url).path} api-version={(request.get('params') or {}).get('api-version', '')}"

    def __init__(self, token_provider, session=None, cache_ttl=CACHE_TTL_SECONDS):
        self.token_provider = token_provider
        self._session = session or _pooled_session()
        self._cache = TtlCache("arm requests", cache_ttl, register=False)
        self._refused = TtlCache("arm refusals", REFUSAL_SECONDS, register=False)
        self._count_lock = threading.Lock()
        self._api_versions = {}
        self._cancel = None
        self._fresh = False
        self.call_count = 0

    @property
    def identity(self):
        return self.token_provider.identity

    def bound(self, cancel):
        """A view of this client for one job: same session, cache and token, its own call count,
        and every request raises `Cancelled` once `cancel` (a threading.Event) is set."""
        clone = copy.copy(self)
        clone._cancel = cancel
        clone.call_count = 0
        return clone

    @property
    def is_fresh(self):
        """A Refresh is asking: caches above this client should read again too."""
        return self._fresh

    def fresh(self):
        """A view of this client for a Refresh: it asks Azure again instead of answering from the cache, and caches
        the new answers for everyone else."""
        clone = copy.copy(self)
        clone._fresh = True
        return clone

    def _check_cancelled(self):
        if self._cancel is not None and self._cancel.is_set():
            raise Cancelled()

    def refresh(self):
        self._cache.clear()
        self._refused.clear()
        refresh = getattr(self.token_provider, "refresh", None)
        if refresh:
            refresh()

    def _request(self, method, url, params=None, body=None, tolerate=(), use_cache=True, timeout=None):
        if not url.startswith("http"):
            url = f"{ARM_ENDPOINT}{url}"
        check_read_only(method, url)
        self._check_cancelled()

        identity = self.identity
        # Keyed by identity so one person's results never answer another's call; ARM paths ignore case (queries don't).
        path, _, query = url.partition("?")
        key = (identity.tenant_id, identity.object_id, method, path.lower() + ("?" + query if query else ""),
               json.dumps(params, sort_keys=True), json.dumps(body, sort_keys=True))
        cached = self._cache.get(key) if use_cache and not self._fresh else None
        if cached is not None:
            return cached
        refused = None if self._fresh else self._refused.get(key)
        if refused is not None:  # asked before and refused: answer now, without waiting on Azure again
            raise AccessDenied(*refused)

        headers = {"Authorization": f"Bearer {self.token_provider.get_token(ARM_SCOPE)}"}
        started = time.monotonic()
        dropped = 0
        attempt = 0
        waited = 0
        while True:
            self._check_cancelled()
            with self._count_lock:
                self.call_count += 1
            request = {"headers": headers, "params": params, "json": body}
            try:
                response = self.send(self._session, method, url, timeout or REQUEST_TIMEOUT_SECONDS,
                                     log_failures=False, started=started, **request)
            except requests.Timeout as error:
                self.log(method, url, request, started, "timeout")
                raise ArmTimeout(f"Azure didn't answer within {timeout or REQUEST_TIMEOUT_SECONDS}s for "
                                 f"{method} {urlsplit(url).path}. Try again later.") from error
            except requests.ConnectionError as error:
                if dropped < CONNECTION_RETRIES:
                    dropped += 1
                    self.log(method, url, request, started, f"connection dropped, retry {dropped}")
                    time.sleep(0.5 * dropped)
                    continue
                self.log(method, url, request, started, "connection failed")
                raise ArmTimeout(f"Azure couldn't be reached for {method} {urlsplit(url).path} "
                                 f"(the connection failed {dropped + 1} times). Try again later.") from error
            if response.status_code == 429:
                wait = min(_retry_after(response), 30)
                on_screen = self._cancel is None  # a job's client has a cancel switch; a page's doesn't
                if attempt >= MAX_THROTTLE_RETRIES or (on_screen and waited + wait > ON_SCREEN_THROTTLE_SECONDS):
                    raise Throttled(max(wait, 1))
                attempt += 1
                waited += wait
                time.sleep(wait)
                continue
            break

        status = response.status_code
        if status in tolerate:
            return None
        if status == 403 or (status == 401 and "RBACAccessDenied" in (response.text or "")):  # 401: Cost Management's "no role"
            self._refused.put(key, (urlsplit(url).path, _error_message(response)))
            raise AccessDenied(urlsplit(url).path, _error_message(response))
        if status == 401:
            raise ArmError("Azure rejected the token (401). Run `az login` again.", status)
        if not response.ok:
            raise ArmError(f"Azure returned {status}: {_error_message(response)}", status)

        payload = response.json() if response.content else {}
        if use_cache:
            self._cache.put(key, payload)
        return payload

    def get(self, path, api_version, params=None, tolerate=(), timeout=None):
        merged = {"api-version": api_version, **(params or {})}
        return self._request("GET", path, params=merged, tolerate=tolerate, timeout=timeout)

    def get_paged(self, path, api_version, params=None, timeout=None):
        items = []
        payload = self.get(path, api_version, params, timeout=timeout)
        while payload:
            items.extend(payload.get("value", []))
            next_link = payload.get("nextLink")
            if not next_link:
                break
            payload = self._request("GET", next_link, timeout=timeout)
        return items

    def get_pages(self, path, api_version, params=None, max_pages=10, timeout=None):
        """(items, whether pages were left): get_paged with a cap, for lists that can run to thousands."""
        items, payload = [], self.get(path, api_version, params, timeout=timeout)
        for page in range(max_pages):
            if not payload:
                return items, False
            items.extend(payload.get("value", []))
            next_link = payload.get("nextLink")
            if not next_link:
                return items, False
            if page < max_pages - 1:
                payload = self._request("GET", next_link, timeout=timeout)
        return items, True

    def post(self, path, api_version, body, cache=True, params=None):
        """Only the read-only actions in `_READ_POSTS` get past the guard."""
        return self._request("POST", path, params={**(params or {}), "api-version": api_version}, body=body, use_cache=cache)

    def api_version(self, resource_id):
        """Newest stable API version for the resource's type, as its provider reports it."""
        subscription = ids.subscription_of(resource_id)
        resource_type = ids.resource_type_of(resource_id)
        namespace, _, short = resource_type.partition("/")
        key = (subscription, namespace)
        with self._count_lock:
            versions = self._api_versions.get(key)
        if versions is None:
            payload = self.get(f"/subscriptions/{subscription}/providers/{namespace}",
                               PROVIDERS_API_VERSION, tolerate=(403, 404)) or {}
            versions = {}
            for entry in payload.get("resourceTypes", []):
                listed = entry.get("apiVersions") or []
                stable = [v for v in listed if "preview" not in v.lower()]
                if stable or listed:
                    versions[(entry.get("resourceType") or "").lower()] = (stable or listed)[0]
            with self._count_lock:
                self._api_versions[key] = versions
        return versions.get(short)

    def query(self, kql, subscriptions=None, limit=None):
        """A Resource Graph query, every page up to `limit` rows. No `subscriptions` means every one the session can
        see. A `take` above GRAPH_PAGE_SIZE stops paging (no skip token), so pass `limit` instead."""
        subscriptions = sorted(set(subscriptions or []))
        batches = ([subscriptions[i:i + GRAPH_SUBSCRIPTION_BATCH]
                    for i in range(0, len(subscriptions), GRAPH_SUBSCRIPTION_BATCH)]
                   or [None])
        rows = []
        for batch in batches:
            skip_token = None
            while True:
                options = {"resultFormat": "objectArray", "$top": GRAPH_PAGE_SIZE}
                if skip_token:
                    options["$skipToken"] = skip_token
                body = {"query": kql, "options": options}
                if batch:
                    body["subscriptions"] = batch
                payload = self._request(
                    "POST", RESOURCE_GRAPH_PATH,
                    params={"api-version": RESOURCE_GRAPH_API_VERSION}, body=body,
                )
                page = payload.get("data") or []
                rows.extend(page)
                skip_token = payload.get("$skipToken")
                if limit is not None and len(rows) >= limit:
                    return rows[:limit]
                if not skip_token and len(page) >= GRAPH_PAGE_SIZE and str(payload.get("resultTruncated")).lower() == "true":
                    raise ArmError(f"Resource Graph stopped at {len(page)} rows: the query's `take` blocks paging.")
                if not skip_token:
                    break
        return rows
