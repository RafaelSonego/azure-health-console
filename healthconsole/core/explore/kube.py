"""A read-only Kubernetes API client for AKS: GETs on an allow-list of paths only.
Credentials stay in memory; the cluster CA is loaded from memory, never written to disk."""
import base64
import json
import os
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml
from requests.adapters import HTTPAdapter

from .. import access
from ..cache import TtlCache
from ..guarded import GuardedClient
from ..arm import WriteBlocked

AKS_AAD_SERVER_APP = "6dae42f8-4368-4678-94ff-3960e28e3630/.default"
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
CLIENT_TTL_SECONDS = 600

# Read paths only. Secrets, configmaps, exec, attach and port-forward are deliberately absent.
_ALLOWED = [re.compile(p) for p in (
    r"^/version$",
    r"^/api/v1/(nodes|pods|events)$",
    r"^/api/v1/namespaces/[^/]+/pods$",
    r"^/api/v1/namespaces/[^/]+/pods/[^/]+/log$",
    r"^/apis/apps/v1/(deployments|statefulsets|daemonsets|replicasets)$",
    r"^/apis/batch/v1/(jobs|cronjobs)$",
    r"^/apis/autoscaling/v2/horizontalpodautoscalers$",
    r"^/apis/policy/v1/poddisruptionbudgets$",
    r"^/apis/metrics\.k8s\.io/v1beta1/(pods|nodes)$",
)]


def _kube_message(response):
    try:
        return (response.json().get("message") or "")[:300]
    except ValueError:
        return response.text[:300]


class KubeUnavailable(RuntimeError):
    """The API can't be used from here; the message says why and what to try."""


class KubeUnreachable(KubeUnavailable):
    """No network path to the API server (a timeout or refused connection), as opposed to a refused request."""


def check_kube_read(method, path):
    if method != "GET" or not any(p.match(path) for p in _ALLOWED):
        raise WriteBlocked(f"{method} {path} is not an allowed Kubernetes read.")


class _CaAdapter(HTTPAdapter):
    def __init__(self, context):
        self._context = context
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context
        return super().init_poolmanager(*args, **kwargs)


class KubeClient(GuardedClient):
    """One cluster's API server: GETs on `_ALLOWED` only, with the console's own Entra token or a kubeconfig's."""
    source = ""
    label = "K8S"

    def check(self, method, url, **request):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != urlsplit(self.server).netloc:  # the token goes only to its server
            raise WriteBlocked(f"Kubernetes requests go only to the cluster's own server over https, not {parts.scheme}://{parts.netloc}.")
        check_kube_read(method, parts.path)

    def unreachable(self, method, url, error):
        return KubeUnreachable(
            "The cluster's API server isn't reachable from this machine. Private clusters "
            "need VPN or a network path to the API server's private address.")

    def __init__(self, server, ca_pem, token_source):
        self.server = server.rstrip("/")
        self._token_source = token_source
        self._session = requests.Session()
        context = ssl.create_default_context(cadata=ca_pem)
        self._session.mount("https://", _CaAdapter(context))

    def get(self, path, params=None, raw=False):
        check_kube_read("GET", path)  # before the token is fetched, so a refused path costs nothing
        response = self.send(self._session, "GET", f"{self.server}{path}", (CONNECT_TIMEOUT, READ_TIMEOUT),
                             params=params, headers={"Authorization": f"Bearer {self._token_source()}"})
        if response.status_code in (401, 403):
            raise KubeUnavailable(access.message(
                f"the Kubernetes API ({path}, HTTP {response.status_code})", access.KUBERNETES_ROLE,
                _kube_message(response)))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text if raw else response.json()

    def items(self, path, params=None):
        # resourceVersion=0 lets the API server answer from its watch cache instead of reading etcd.
        if "metrics.k8s.io" not in path:
            params = {"resourceVersion": "0", **(params or {})}
        payload = self.get(path, params) or {}
        return payload.get("items", [])


_clients = TtlCache("Kubernetes clients", CLIENT_TTL_SECONDS)  # access-bound: a new PIM role means a new credential

# Where AKS desktop (built on Headlamp) keeps the clusters you registered in it.
# Best effort: the folder isn't documented; set aks.kubeconfig_paths if yours differs.
_DESKTOP_APP_DIRS = ("AKS desktop", "aks-desktop", "Headlamp")


def _desktop_dirs():
    roots = [Path.home() / "Library" / "Application Support", Path.home() / ".config"]
    if os.environ.get("APPDATA"):
        roots.append(Path(os.environ["APPDATA"]))
    return [root / app / "kubeconfigs" for root in roots for app in _DESKTOP_APP_DIRS]


def kubeconfig_files(extra_paths=()):
    """$KUBECONFIG (or ~/.kube/config), AKS desktop's folder, plus any configured files or folders."""
    candidates = []
    env = os.environ.get("KUBECONFIG")
    candidates += [Path(p) for p in env.split(os.pathsep) if p] if env else [Path.home() / ".kube" / "config"]
    candidates += _desktop_dirs()
    candidates += [Path(p).expanduser() for p in extra_paths]
    files = []
    for path in candidates:
        if path.is_dir():
            files += sorted(f for f in path.iterdir() if f.is_file() and not f.name.startswith("."))
        elif path.is_file():
            files.append(path)
    return list(dict.fromkeys(files))


def _host(url):
    return (urlsplit(url).hostname or "").lower()


def _cluster_hosts(arm, cluster_id):
    rows = arm.query(f"Resources | where tolower(id) == {json.dumps(cluster_id.lower())}"
                     " | project fqdn=tostring(properties.fqdn), private=tostring(properties.privateFQDN),"
                     " portal=tostring(properties.azurePortalFQDN)")
    return {h.lower() for h in (rows[0].values() if rows else []) if h}


def _token_source(arm, user, origin):
    """Entra sign-in (exec/auth-provider) uses the console's own token; the command is never run."""
    if "exec" in user or "auth-provider" in user:
        return lambda: arm.token_provider.get_token(AKS_AAD_SERVER_APP)
    if user.get("token"):
        static = user["token"]
        return lambda: static
    if user.get("tokenFile"):
        token = (origin.parent / user["tokenFile"]).read_text().strip()
        return lambda: token
    raise KubeUnavailable("This context uses client-certificate auth (a local account), which the console "
                          "doesn't support. Use an Entra-enabled cluster context.")


def _from_kubeconfig(arm, hosts, extra_paths):
    """(client, description) for the first local context whose API server is this cluster, or (None, why)."""
    skipped = []
    for path in kubeconfig_files(extra_paths):
        try:
            config = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(config, dict):
            continue
        clusters = {c.get("name"): c.get("cluster") or {} for c in config.get("clusters") or []}
        users = {u.get("name"): u.get("user") or {} for u in config.get("users") or []}
        contexts = config.get("contexts") or []
        current = config.get("current-context")
        contexts.sort(key=lambda c: c.get("name") != current)
        for context in contexts:
            detail = context.get("context") or {}
            cluster = clusters.get(detail.get("cluster"), {})
            if _host(cluster.get("server", "")) not in hosts:
                continue
            if urlsplit(cluster.get("server", "")).scheme != "https":
                skipped.append(f"{context.get('name')}: server isn't https")
                continue
            if cluster.get("insecure-skip-tls-verify"):
                skipped.append(f"{context.get('name')}: skips TLS verification")
                continue
            if cluster.get("certificate-authority-data"):
                ca_pem = base64.b64decode(cluster["certificate-authority-data"]).decode()
            elif cluster.get("certificate-authority"):
                ca_pem = (path.parent / cluster["certificate-authority"]).read_text()
            else:
                skipped.append(f"{context.get('name')}: no cluster CA")
                continue
            try:
                tokens = _token_source(arm, users.get(detail.get("user"), {}), path)
            except KubeUnavailable as error:
                skipped.append(f"{context.get('name')}: {error}")
                continue
            return KubeClient(cluster["server"], ca_pem, tokens), f"kubeconfig context '{context.get('name')}' ({path})"
    return None, "; ".join(skipped)


def _from_azure(arm, cluster_id):
    version = arm.api_version(cluster_id) or "2024-10-01"
    payload = arm.post(f"{cluster_id}/listClusterUserCredential", version, body=None, cache=False) or {}
    configs = payload.get("kubeconfigs") or []
    if not configs:
        raise KubeUnavailable("Azure returned no cluster credential for this identity.")
    config = yaml.safe_load(base64.b64decode(configs[0]["value"]))
    cluster = config["clusters"][0]["cluster"]
    ca_pem = base64.b64decode(cluster["certificate-authority-data"]).decode()
    tokens = _token_source(arm, config["users"][0]["user"], Path("."))
    return KubeClient(cluster["server"], ca_pem, tokens), "user credential fetched from Azure (in memory)"


def client_for(arm, cluster_id, settings=None):
    """A cached KubeClient: a matching local kubeconfig context first, else (if allowed) the
    cluster user credential from Azure. `client.source` says which was used."""
    settings = settings or {}
    identity = arm.identity
    key = (identity.tenant_id, identity.object_id, cluster_id.lower())
    cached = _clients.get(key)
    if cached is not None:
        return cached

    client, source = _from_kubeconfig(arm, _cluster_hosts(arm, cluster_id), settings.get("kubeconfig_paths") or [])
    if client is None:
        if not settings.get("fetch_credentials", True):
            name = cluster_id.rstrip("/").split("/")[-1]
            group = cluster_id.split("/")[4] if cluster_id.count("/") > 4 else "<resource-group>"
            raise KubeUnavailable(
                "No local kubeconfig context points at this cluster, and fetching credentials is off "
                f"(aks.fetch_credentials). Run `az aks get-credentials -g {group} -n {name}`, or register "
                "the cluster in AKS desktop." + (f" Skipped: {source}." if source else ""))
        client, source = _from_azure(arm, cluster_id)
    client.source = source
    return _clients.put(key, client)
