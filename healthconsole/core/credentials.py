"""Where Azure tokens come from. Core only ever calls `get_token(scope)` and
reads `identity`, so a hosted front end can pass a delegated-token provider."""
import base64
import json
import threading
import time
from dataclasses import dataclass

ARM_SCOPE = "https://management.azure.com/.default"
TOKEN_REFRESH_MARGIN_SECONDS = 120


class CredentialError(Exception):
    """No usable token. The message says what to run; `reason` is cli_missing, tenant or signed_out."""

    def __init__(self, message, reason="signed_out", command="az login"):
        super().__init__(message)
        self.reason = reason
        self.command = command


def _reason(text, tenant_id):
    lowered = text.lower()
    if "not installed" in lowered or ("not found" in lowered and "azure cli" in lowered):
        return "cli_missing"
    if tenant_id and ("tenant" in lowered or "aadsts90002" in lowered or "aadsts50020" in lowered):
        return "tenant"
    return "signed_out"


@dataclass(frozen=True)
class Identity:
    object_id: str
    name: str
    tenant_id: str


def identity_from_token(token):
    """Read, not verified: only used to label reports and key caches."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return Identity("", "unknown", "")
    # A personal or external account has no upn, and its unique_name is "<provider>#<address>" (live.com#me@gmail.com).
    unique = (claims.get("unique_name") or "").rsplit("#", 1)[-1]
    name = (claims.get("upn") or claims.get("email") or unique
            or claims.get("preferred_username") or claims.get("appid") or "")
    return Identity(claims.get("oid", ""), name, claims.get("tid", ""))


class AzureCliTokenProvider:
    """The terminal's `az login` session, optionally for a tenant other than the default."""

    def __init__(self, tenant_id=None, credential=None):
        self.tenant_id = tenant_id
        self._credential = credential
        self._tokens = {}
        self._lock = threading.Lock()

    def _get_credential(self):
        if self._credential is None:
            from azure.identity import AzureCliCredential

            self._credential = AzureCliCredential(
                tenant_id=self.tenant_id, process_timeout=30
            )
        return self._credential

    def get_token(self, scope=ARM_SCOPE):
        with self._lock:
            cached = self._tokens.get(scope)
            if cached and cached.expires_on - TOKEN_REFRESH_MARGIN_SECONDS > time.time():
                return cached.token
            try:
                token = self._get_credential().get_token(scope)
            # Broad on purpose: a missing CLI, a stale login and an unknown
            # tenant all raise differently, and each needs the same hint.
            except Exception as error:
                hint = (f"az login --tenant {self.tenant_id}"
                        if self.tenant_id else "az login")
                detail = str(error).strip()[:300]
                raise CredentialError(
                    f"The Azure CLI couldn't provide a token. Run `{hint}` "
                    f"and try again. ({detail})", _reason(detail, self.tenant_id), hint
                ) from error
            self._tokens[scope] = token
            return token.token

    def refresh(self):
        """Drop cached tokens so a new `az login` (or PIM group change) is picked up."""
        with self._lock:
            self._tokens.clear()

    @property
    def identity(self):
        return identity_from_token(self.get_token())
