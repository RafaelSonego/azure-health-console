"""The one way the console sends a request to anything outside it. Each client (Azure Resource Manager, Microsoft
Graph, a container registry, a Kubernetes API server, ServiceNow, New Relic) extends `GuardedClient` and says only
what it allows (`check`) and how to explain "couldn't connect" (`unreachable`); sending, the refusal before anything
leaves the machine, and the `--log-calls` line are shared. Like a Java abstract class: `check` must be implemented."""
import ipaddress
import logging
import time
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError

from . import eventlog

MAX_REDIRECTS = 3  # followed one by one, each through the client's own check
calls = logging.getLogger("healthconsole.calls")  # what --log-calls prints: method, path, status, never tokens


class WriteBlocked(RuntimeError):
    """A request a client's `check` refused: nothing was sent. Every client's refusal is one of these (or extends it),
    so the web layer handles them all the same way."""


class ServiceError(Exception):
    """A service outside the console answered in a way the user can act on. `kind` groups it (auth, access, missing,
    network, config, other) and the message says what to do; each client's own error extends this one."""

    def __init__(self, message, kind="config"):
        super().__init__(message)
        self.kind = kind


def local_address(address):
    """True for an address on this machine or its link (loopback, link-local such as the cloud metadata service
    169.254.169.254, unspecified, multicast, reserved). Private ranges are not local: an internal service is fine."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0].strip("[]"))
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved


def refuse_local_host(label, host):
    """Before sending: a host that is a local address, or localhost, is refused (WriteBlocked). Names are checked
    again where it counts, against the address actually connected to (NotLocalAdapter)."""
    name = (host or "").rsplit(":", 1)[0] if not (host or "").startswith("[") else host.split("]", 1)[0] + "]"
    if name.rstrip(".").lower() in ("localhost", "localhost.localdomain") or name.lower().endswith(".localhost") \
            or local_address(name):
        raise WriteBlocked(f"{label} requests never go to a local address ({host}).")


class _NotLocalConnection(HTTPSConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        if not getattr(self, "_tunnel_host", None):  # through a proxy the peer is the proxy, which resolves the name
            peer = sock.getpeername()[0]
            if local_address(peer):
                sock.close()
                raise NewConnectionError(self, f"{self.host} is at {peer}, a local address; nothing was sent")
        return sock


class _NotLocalPool(HTTPSConnectionPool):
    ConnectionCls = _NotLocalConnection


class NotLocalAdapter(HTTPAdapter):
    """https only to addresses that aren't local, checked on the connected socket, so a name that resolves (or is
    rebound) to 127.0.0.1 or 169.254.169.254 gets nothing, credentials included."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {**self.poolmanager.pool_classes_by_scheme, "https": _NotLocalPool}


class GuardedClient(ABC):
    label = "HTTP"  # the log line's first word: ARM, GRAPH, ACR, K8S, SERVICENOW, NEWRELIC

    @abstractmethod
    def check(self, method, url, **request):
        """Raise `WriteBlocked` for anything this client mustn't send. Runs before every request, so a refusal never
        reaches the network."""

    def unreachable(self, method, url, error):
        """The exception to raise when the host can't be reached (a timeout or a dropped connection)."""
        return error

    def describe(self, method, url, request):
        """What the log line shows for this request (default: its path)."""
        return urlsplit(url).path

    def send(self, session, method, url, timeout, log_failures=True, started=None, **request):
        """Check, send, log one line; `request` is what `requests` takes (params, json, data, headers). A client that
        retries its own failures (ARM) passes `log_failures=False`, logs each retry itself, and passes `started` so the
        line's duration covers the retries."""
        self._checked(method, url, request)
        started = started or time.monotonic()
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = session.request(method, url, timeout=timeout, allow_redirects=False, **request)
            except requests.RequestException as error:  # a timeout, a dropped connection, a bad TLS handshake…
                if log_failures:
                    self.log(method, url, request, started, "unreachable")
                raise self.unreachable(method, url, error) from error
            self.log(method, url, request, started, response.status_code)
            redirect = 300 <= response.status_code < 400
            location = (getattr(response, "headers", None) or {}).get("Location") if redirect else None
            if not location:
                return response
            # A redirect is followed only for a read, and only where this client's own check allows the new URL.
            if method.upper() != "GET":
                raise WriteBlocked(f"{self.label} answered {method} with a redirect to {location}; not followed.")
            url = urljoin(url, location)
            self._checked(method, url, request)
        raise WriteBlocked(f"{self.label} redirected more than {MAX_REDIRECTS} times; stopped at {url}.")

    def _checked(self, method, url, request):
        try:
            self.check(method, url, **request)
        except WriteBlocked:
            eventlog.count_request(self.label, method, url, "refused")
            raise

    def log(self, method, url, request, started, status):
        eventlog.count_request(self.label, method, url, status)
        calls.info("%s %s %s status=%s ms=%d", self.label, method, self.describe(method, url, request), status,
                   (time.monotonic() - started) * 1000)
