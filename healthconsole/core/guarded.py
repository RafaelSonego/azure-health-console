"""The one way the console sends a request to anything outside it. Each client (Azure Resource Manager, Microsoft
Graph, a container registry, a Kubernetes API server, ServiceNow, New Relic) extends `GuardedClient` and says only
what it allows (`check`) and how to explain "couldn't connect" (`unreachable`); sending, the refusal before anything
leaves the machine, and the `--log-calls` line are shared. Like a Java abstract class: `check` must be implemented."""
import logging
import time
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlsplit

import requests

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
        self.check(method, url, **request)
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
            self.check(method, url, **request)
        raise WriteBlocked(f"{self.label} redirected more than {MAX_REDIRECTS} times; stopped at {url}.")

    def log(self, method, url, request, started, status):
        calls.info("%s %s %s status=%s ms=%d", self.label, method, self.describe(method, url, request), status,
                   (time.monotonic() - started) * 1000)
