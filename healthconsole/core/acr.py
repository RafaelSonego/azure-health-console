"""A container registry's data plane, read-only and narrow: which tags point at one image digest, for the
Vulnerabilities tab. Your Azure token is exchanged for a registry token (two sign-in POSTs that read and change
nothing), then one manifest GET. Registry credentials (`listCredentials`) are an ARM POST the ARM guard refuses."""
import re
from urllib.parse import quote, urlsplit

import requests

from .arm import WriteBlocked
from .guarded import GuardedClient, ServiceError
from .cache import TtlCache
from .credentials import ARM_SCOPE

HOST = re.compile(r"^[a-z0-9]{5,50}\.azurecr\.(io|cn|us)$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")
_POSTS = ("/oauth2/exchange", "/oauth2/token")
_MANIFEST = re.compile(r"^/acr/v1/[a-z0-9._/-]+/_manifests/sha256:[0-9a-f]{64}$")
TIMEOUT = (5, 20)
CACHE_SECONDS = 300
_tags = TtlCache("registry tags", CACHE_SECONDS)
ROLE = ("AcrPull on the registry (or Container Registry Repository Reader, on registries using repository "
        "permissions). Azure's Reader role doesn't read the registry's contents")


class RegistryError(ServiceError):
    """Why tags couldn't be read (`kind`: access, network, missing or other); `required` names the role it needs."""

    def __init__(self, message, kind, required=""):
        super().__init__(message, kind)
        self.required = required


def check_registry_request(method, url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not HOST.match(parts.netloc.lower()):
        raise WriteBlocked(f"Only Azure container registries are reachable here, not '{parts.netloc}'.")
    if (method == "POST" and parts.path in _POSTS) or (method == "GET" and _MANIFEST.match(parts.path)):
        return
    raise WriteBlocked(f"{method} {parts.path} isn't an allowed registry read.")


class _Registry(GuardedClient):
    label = "ACR"

    def check(self, method, url, **request):
        check_registry_request(method, url)

    def unreachable(self, method, url, error):
        return RegistryError(f"{urlsplit(url).netloc} can't be reached from this machine: its firewall or private "
                             "endpoint doesn't let this network in. A VPN or an allowed network is needed.", "network")

    def describe(self, method, url, request):
        return urlsplit(url).path.split("/_manifests/")[0]  # the repository, not the digest


_REGISTRY = _Registry()


def _send(session, method, url, **kwargs):
    return _REGISTRY.send(session, method, url, TIMEOUT, **kwargs)


def _refused(response, login_server, refused):
    """A 403 is either the registry's firewall (the caller's IP isn't allowed, checked live 2026-11-09) or a missing
    role, and they need different fixes."""
    text = getattr(response, "text", "") or ""
    if "is not allowed access" in text or "aka.ms/acr/firewall" in text:
        return RegistryError(f"{login_server} doesn't accept connections from this machine's IP address (its firewall). "
                             "A VPN or a network the registry allows is needed; a role won't change this.", "network")
    return RegistryError(refused, "access", ROLE)


def tags(token_provider, login_server, repository, digest, tenant_id, session=None):
    """{"tags": [...], "created", "updated"} for one image in one registry; cached per viewer for five minutes."""
    login_server = login_server.lower()
    if not HOST.match(login_server) or not REPO.match(repository or "") or not DIGEST.match(digest or ""):
        raise RegistryError("That isn't a registry image this console can look up.", "other")
    identity = token_provider.identity
    key = (identity.tenant_id, identity.object_id, login_server, repository, digest)
    hit = _tags.get(key)
    if hit is not None:
        return hit
    session = session or requests.Session()
    base = f"https://{login_server}"
    refused = (f"{login_server} refused to let you read its images. Reading tags needs {ROLE}. If you're eligible "
               "for it through PIM, activate it, then click Refresh access.")
    exchange = _send(session, "POST", f"{base}/oauth2/exchange", data={
        "grant_type": "access_token", "service": login_server, "tenant": tenant_id,
        "access_token": token_provider.get_token(ARM_SCOPE)})
    if exchange.status_code in (401, 403):
        raise _refused(exchange, login_server, refused)
    if exchange.status_code >= 400:
        raise RegistryError(f"{login_server} answered {exchange.status_code} to the sign-in.", "other")
    token = _send(session, "POST", f"{base}/oauth2/token", data={
        "grant_type": "refresh_token", "service": login_server, "refresh_token": exchange.json().get("refresh_token", ""),
        "scope": f"repository:{repository}:metadata_read"})
    if token.status_code in (401, 403):
        raise _refused(token, login_server, refused)
    if token.status_code >= 400:
        raise RegistryError(f"{login_server} answered {token.status_code} for a read token.", "other")
    manifest = _send(session, "GET", f"{base}/acr/v1/{quote(repository, safe='/')}/_manifests/{digest}",
                     headers={"Authorization": f"Bearer {token.json().get('access_token', '')}"})
    if manifest.status_code in (401, 403):
        raise _refused(manifest, login_server, refused)
    if manifest.status_code == 404:
        raise RegistryError("This image isn't in the registry any more (deleted since Defender scanned it).", "missing")
    if manifest.status_code >= 400:
        raise RegistryError(f"{login_server} answered {manifest.status_code} for the image.", "other")
    info = manifest.json().get("manifest") or {}
    result = {"tags": sorted(info.get("tags") or []), "created": info.get("createdTime") or "",
              "updated": info.get("lastUpdateTime") or ""}
    return _tags.put(key, result)  # refusals aren't kept: the next click asks again (after PIM, say)


def forget():
    _tags.clear()
