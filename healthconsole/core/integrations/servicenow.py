"""A read-only ServiceNow client: GETs on the Table API for the configured tables (plus sys_user, to say who
you are), and POSTs only to /oauth_token.do to get a token. Sign-in: basic, OAuth browser or OAuth client."""
import base64
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from secrets import token_urlsafe
from urllib.parse import urlencode, urlsplit

import requests

from . import secrets
from .. import guarded
from ..cache import TtlCache
from ..guarded import GuardedClient, ServiceError


TARGET = "servicenow"
TIMEOUT_SECONDS = 20
TOKEN_MARGIN_SECONDS = 60
TABLE_NAME = re.compile(r"^[a-z0-9_]{1,80}$")
SECRET_FIELDS = {"basic": ("password",), "oauth_client": ("client_secret",),
                 "oauth_browser": ("client_secret", "refresh_token")}


PAGE_PARALLEL = 4  # pages of one table read at once
HIDDEN_SECONDS = 900  # a table whose rows are all hidden from an account is skipped this long (Refresh asks again)
_hidden = TtlCache("ServiceNow tables hidden from an account", HIDDEN_SECONDS)
READ_ACCESS = ("read access to their records (ServiceNow's read rules, or ACLs): commonly the itil role, with "
               "snc_read_only to keep the account read-only")
ASLEEP_HINT = ("The instance may be down or asleep: a developer instance sleeps when unused and is woken from "
               "developer.servicenow.com.")


def forget_hidden():
    """Refresh: ask again whether tables are readable (a role may have been granted)."""
    _hidden.clear()


def no_access(who, tables):
    """What an account that can read none of the tables needs, in one sentence the page and Test connection share."""
    return (f"{who} can't read any of the ServiceNow records the console uses: ServiceNow hides or refuses "
            f"{', '.join(tables)}. Ask your ServiceNow admins for this account's {READ_ACCESS}.")


class ServiceNowError(ServiceError):
    """`kind`: auth, access, missing, unreachable (the instance isn't answering as ServiceNow: down, asleep or gone; the
    whole connection is unreadable) or config; `status` is ServiceNow's HTTP status, when it answered."""

    def __init__(self, message, kind="config", status=None):
        super().__init__(message, kind)
        self.status = status


class WriteBlocked(guarded.WriteBlocked):
    """Something tried a ServiceNow call that isn't a read."""


def normalise_instance(text):
    """"dev123", "dev123.service-now.com/nav_to.do" or "http://..." all become "https://dev123.service-now.com"."""
    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    try:
        host, port = (parts.hostname or "").lower(), parts.port  # never a user:password@ part
    except ValueError:  # a port that isn't a number
        return ""
    if host and "." not in host:
        host = f"{host}.service-now.com"
    return f"https://{host}{f':{port}' if port and port != 443 else ''}" if host else ""


def host_of(instance):
    """The instance's host (and port when not 443), from any form `normalise_instance` takes."""
    return urlsplit(normalise_instance(instance)).netloc


def check_read_only(method, url, host, tables):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() != host:
        raise WriteBlocked(f"Only https://{host} is allowed, not '{parts.netloc or url}'.")
    path = parts.path.rstrip("/").lower()
    if method == "POST" and path == "/oauth_token.do":
        return
    match = re.fullmatch(r"/api/now/table/([a-z0-9_]+)", path)
    if method == "GET" and match and match.group(1) in set(tables) | {"sys_user"}:
        return
    raise WriteBlocked(f"{method} {parts.path} is not an allowed ServiceNow read.")


class ServiceNowClient(GuardedClient):
    """One instance: GETs on the tables chosen in Settings (and sys_user for Test), plus the OAuth token POST."""
    _tokens = {}
    _lock = threading.Lock()
    label = "SN"

    def check(self, method, url, **request):
        check_read_only(method, url, self.host, self.tables)
        guarded.refuse_local_host("ServiceNow", self.host)

    def unreachable(self, method, url, error):
        return ServiceNowError(f"ServiceNow ({self.host}) didn't answer: {error}. {ASLEEP_HINT}", "unreachable")

    def __init__(self, cfg, session=None, typed=None):
        """`typed`: secrets typed on the page but not saved yet, so Test can check them first."""
        self.cfg = cfg
        self.typed = {k: v for k, v in (typed or {}).items() if v}
        self.instance = normalise_instance(cfg["instance"])
        self.host = host_of(self.instance)
        self.tables = [t for t in cfg["tables"] if TABLE_NAME.match(t)]
        self._session = session or requests.Session()
        if isinstance(self._session, requests.Session):
            self._session.mount("https://", guarded.NotLocalAdapter())

    # -------------------------------------------------------------- auth

    def secret(self, field):
        return self.typed.get(field) or secrets.get(TARGET, self.host, field)

    def _token(self):
        auth = self.cfg["auth"]
        key = (self.host, auth, self.cfg.get("client_id"), bool(self.typed))
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] - TOKEN_MARGIN_SECONDS > time.time():
                return cached[0]
        body = {"client_id": self.cfg["client_id"], "client_secret": self.secret("client_secret") or ""}
        if auth == "oauth_client":
            body["grant_type"] = "client_credentials"
        else:
            refresh = self.secret("refresh_token")
            if not refresh:
                raise ServiceNowError("Not signed in to ServiceNow yet: click Sign in with ServiceNow in Settings.",
                                      "auth")
            body.update(grant_type="refresh_token", refresh_token=refresh)
        payload = self._token_request(body)
        if payload.get("refresh_token") and auth == "oauth_browser":
            secrets.put(TARGET, self.host, "refresh_token", payload["refresh_token"])
        with self._lock:
            self._tokens[key] = (payload["access_token"], time.time() + int(payload.get("expires_in") or 1800))
        return payload["access_token"]

    def _token_request(self, body):
        url = f"{self.instance}/oauth_token.do"
        response = self.send(self._session, "POST", url, TIMEOUT_SECONDS, data=body)
        if not response.ok:
            detail = _error_text(response)
            raise ServiceNowError(f"ServiceNow refused the OAuth sign-in ({response.status_code}): {detail}. "
                                  "Check the client ID and secret, and that the OAuth app allows this grant.", "auth",
                                  response.status_code)
        return response.json()

    def forget_token(self):
        with self._lock:
            for key in [k for k in self._tokens if k[0] == self.host]:
                self._tokens.pop(key)

    def _auth(self):
        if self.cfg["auth"] == "basic":
            password = self.secret("password")
            if not self.cfg["username"] or not password:
                raise ServiceNowError("ServiceNow needs a username and a stored password (Settings → Integrations).",
                                      "config")
            return {"auth": (self.cfg["username"], password)}
        return {"headers": {"Authorization": f"Bearer {self._token()}"}}

    # -------------------------------------------------------------- reads

    @property
    def who(self):
        """The account reads run as, for messages."""
        return self.cfg["username"] if self.cfg["auth"] == "basic" and self.cfg["username"] else "this sign-in"

    def table(self, name, query="", fields=(), limit=100):
        """Rows of one table; display values, so references read as names."""
        rows, total = self._read(name, query, fields, limit)
        self._hidden_check(name, rows, total, f"the first {limit:,}" if total and total > limit else "")
        return rows

    def table_pages(self, name, query="", fields=(), page=500, cap=5000):
        """(rows, total): every row up to `cap`, `page` at a time. The first page says how many match
        (X-Total-Count) and the rest are read PAGE_PARALLEL at once; `query` needs an ORDERBY so pages don't overlap."""
        if _hidden.get((self.host, self.who, name)):
            raise ServiceNowError(f"ServiceNow hides the {name} records from {self.who}: the account needs {READ_ACCESS}.",
                                  "access", 200)
        rows, total = self._read(name, query, fields, page)
        if total is None:  # no count header: page on until a short page
            while len(rows) < cap:
                more = self._read(name, query, fields, page, len(rows))[0]
                rows += more
                if len(more) < page:
                    break
            return rows[:cap], len(rows)
        wanted = min(total, cap)
        offsets = list(range(page, wanted, page))  # read rules can thin any page, so always by offset
        with ThreadPoolExecutor(max_workers=PAGE_PARALLEL) as pool:
            def read(batch):
                return [row for chunk in pool.map(lambda o: self._read(name, query, fields, min(page, wanted - o), o)[0], batch)
                        for row in chunk]
            probe = offsets[:PAGE_PARALLEL - 1]  # with the first page, PAGE_PARALLEL pages: enough to tell "none at all"
            rows += read(probe)
            if not rows:
                self._hidden_check(name, rows, total, f"the first {page * (len(probe) + 1):,}" if offsets[len(probe):] else "")
            rows += read(offsets[len(probe):])
        return rows[:cap], total

    def _hidden_check(self, name, rows, total, within=""):
        """ServiceNow counts matches before its read rules, then removes the rows this account can't read: none at
        all left means no access, not an empty table."""
        if total and not rows:
            seen = f" ({within})" if within else ""
            error = ServiceNowError(f"ServiceNow has {total:,} matching {name} records but shows none{seen} to {self.who}: "
                                    f"the account needs {READ_ACCESS}.", "access", 200)
            _hidden.put((self.host, self.who, name), True)
            raise error

    def _read(self, name, query="", fields=(), limit=100, offset=0):
        """(rows, total) for one page; total is ServiceNow's X-Total-Count, None when it doesn't send one."""
        url = f"{self.instance}/api/now/table/{name}"
        check_read_only("GET", url, self.host, self.tables)
        params = {"sysparm_limit": str(limit), "sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"}
        if offset:
            params["sysparm_offset"] = str(offset)
        if query:
            params["sysparm_query"] = query
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        kwargs = self._auth()
        response = self.send(self._session, "GET", url, TIMEOUT_SECONDS, params=params, auth=kwargs.get("auth"),
                             headers={"Accept": "application/json", **kwargs.get("headers", {})})
        if response.status_code == 401:
            self.forget_token()
            raise ServiceNowError("ServiceNow rejected the sign-in (401). Check the username and password, "
                                  "or sign in again.", "auth", 401)
        if response.status_code == 403:
            raise ServiceNowError(f"No access to the {name} table (403): the account needs a role that can read it.",
                                  "access", 403)
        if response.status_code in (400, 404):
            raise ServiceNowError(f"The {name} table isn't available on this instance ({_error_text(response)}).",
                                  "missing", response.status_code)
        if response.status_code >= 500:
            raise ServiceNowError(f"ServiceNow ({self.host}) isn't answering ({response.status_code} "
                                  f"{_error_text(response)}). {ASLEEP_HINT}", "unreachable", response.status_code)
        if not response.ok:
            raise ServiceNowError(f"ServiceNow returned {response.status_code}: {_error_text(response)}", "network",
                                  response.status_code)
        count = (getattr(response, "headers", None) or {}).get("X-Total-Count")
        total = int(count) if str(count or "").isdigit() else None
        try:
            rows = response.json().get("result") or []
        except ValueError:  # a sign-in or "instance unavailable" page instead of the API
            raise ServiceNowError(f"ServiceNow ({self.host}) answered with a web page, not its API "
                                  f"({_error_text(response)}). {ASLEEP_HINT}", "unreachable", response.status_code) from None
        return rows, total

    def test(self):
        """Who you are and which configured tables are readable, each with a reason when not."""
        out = {"ok": False, "who": "", "tables": []}
        if self.cfg["auth"] == "basic" and self.cfg["username"]:
            rows = self.table("sys_user", f"user_name={self.cfg['username']}", ("name", "user_name"), 1)
            out["who"] = _value(rows[0].get("name")) if rows else self.cfg["username"]
        for name in self.tables:
            try:
                self.table(name, fields=("sys_id",), limit=1)
                out["tables"].append({"name": name, "ok": True, "detail": "readable"})
            except ServiceNowError as error:
                if error.kind in ("auth", "unreachable"):
                    raise
                out["tables"].append({"name": name, "ok": False, "detail": str(error)})
        out["ok"] = any(t["ok"] for t in out["tables"])
        return out


def _value(cell):
    if isinstance(cell, dict):
        return cell.get("display_value") or cell.get("value") or ""
    return cell or ""


def _error_text(response):
    try:
        error = response.json().get("error") or {}
        return str(error.get("message") or error.get("detail") or error)[:200] or response.reason
    except ValueError:  # an HTML page: its title says more than its markup
        title = re.search(r"<title>(.*?)</title>", response.text or "", re.I | re.S)
        text = title.group(1) if title else re.sub(r"<[^>]+>", " ", response.text or "")
        return " ".join(text.split())[:200] or response.reason or ""


# ------------------------------------------------------------------ OAuth with your own login

def start_browser_sign_in(cfg, redirect_uri):
    """(URL to open, state, verifier). ServiceNow shows its own login page; PKCE protects the code."""
    state = token_urlsafe(24)
    verifier = token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect_uri,
                       "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{cfg['instance'].rstrip('/')}/oauth_auth.do?{query}", state, verifier


def finish_browser_sign_in(cfg, code, redirect_uri, verifier, session=None):
    """Swaps the code for tokens and keeps the refresh token in the keychain."""
    client = ServiceNowClient(cfg, session)
    payload = client._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                     "client_id": cfg["client_id"], "client_secret": client.secret("client_secret") or "",
                                     "code_verifier": verifier})
    if not payload.get("refresh_token"):
        raise ServiceNowError("ServiceNow didn't return a refresh token; enable refresh tokens on the OAuth app.",
                              "auth")
    secrets.put(TARGET, client.host, "refresh_token", payload["refresh_token"])
    client.forget_token()
    return client
