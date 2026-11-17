"""Secret, key and certificate expiry from Azure Resource Manager's metadata lists: names and dates only,
never values; needs Reader. A vault firewall that doesn't allow trusted Azure services blocks it."""
from datetime import datetime, timezone

from ..arm import AccessDenied, ArmError, ArmTimeout
from ..model import CRITICAL, HEALTHY, INFO, WARNING, Signal

KEYVAULT_API_VERSION = "2023-07-01"
LIST_TIMEOUT_SECONDS = 15
FIREWALL = ("The vault's firewall blocks this listing: it doesn't allow trusted Azure services "
            "(network bypass is None), so even Azure Resource Manager can't list its secrets from here.")
LISTS = ("secrets", "keys")
PAGE_MAX_PAGES = 40  # a vault's own page reads at most 1,000 of each (ARM lists 25 a page); the Secrets view reads all
KINDS = ("certificates", "secrets", "keys")
# ARM has no certificate list; a Key Vault certificate is stored as a secret with one of these
# content types, plus a key of the same name, so both are reported once, as the certificate.
CERTIFICATE_CONTENT_TYPES = ("application/x-pkcs12", "application/x-pem-file")


def _item(entry, warn_days, now, critical_days=0):
    attributes = (entry.get("properties") or {}).get("attributes") or {}
    expires = attributes.get("exp")
    enabled = attributes.get("enabled", True)
    item = {"name": entry.get("name"), "enabled": enabled, "expires": None, "days_left": None}
    if expires:
        moment = datetime.fromtimestamp(expires, timezone.utc)
        item["expires"] = moment.date().isoformat()
        item["days_left"] = (moment - now).days
    if not enabled:
        item["status"], item["note"] = INFO, "disabled"
    elif expires is None:
        item["status"], item["note"] = INFO, "no expiry date set"
    elif item["days_left"] < 0:
        item["status"], item["note"] = CRITICAL, f"expired {-item['days_left']} day(s) ago"
    elif item["days_left"] <= critical_days:
        item["status"], item["note"] = CRITICAL, f"expires in {item['days_left']} day(s)"
    elif item["days_left"] <= warn_days:
        item["status"], item["note"] = WARNING, f"expires in {item['days_left']} day(s)"
    else:
        item["status"], item["note"] = HEALTHY, f"expires in {item['days_left']} days"
    return item


_ORDER = {CRITICAL: 0, WARNING: 1, HEALTHY: 2, INFO: 3}


def _sorted(items):
    return sorted(items, key=lambda i: (_ORDER[i["status"]], i["days_left"] if i["days_left"] is not None else 10**6))


def expiry(client, vault_id, warn_days=30, critical_days=7, max_pages=None):
    """{"certificates": [...], "secrets": [...], "keys": [...], "errors": [...]}, most urgent first. `max_pages` caps
    each list (a vault with thousands of secrets took minutes to page through); a capped list says so in `errors`."""
    now = datetime.now(timezone.utc)
    out = {"warn_days": warn_days, "critical_days": critical_days, "errors": [],
           "certificates": [], "secrets": [], "keys": []}
    listed = {}

    def read(kind):
        if max_pages is None:
            return client.get_paged(f"{vault_id}/{kind}", KEYVAULT_API_VERSION, timeout=LIST_TIMEOUT_SECONDS)
        items, more = client.get_pages(f"{vault_id}/{kind}", KEYVAULT_API_VERSION, max_pages=max_pages,
                                       timeout=LIST_TIMEOUT_SECONDS)
        if more:
            out["errors"].append(f"Only the first {len(items):,} {kind} were checked here (the vault has more); "
                                 "Secrets & certificates checks them all.")
        return items

    for kind in LISTS:
        try:
            try:
                listed[kind] = read(kind)
            except ArmTimeout:  # a vault's listing is sometimes slow once and fine the next time
                listed[kind] = read(kind)
        except AccessDenied as error:
            out["errors"].append(str(error))
        except ArmError as error:
            text = FIREWALL if "ForbiddenByFirewall" in str(error) else str(error).splitlines()[0]
            if text not in out["errors"]:
                out["errors"].append(text)
    certificates = set()
    for entry in listed.get("secrets") or []:
        content_type = str((entry.get("properties") or {}).get("contentType") or "").lower()
        kind = "certificates" if content_type in CERTIFICATE_CONTENT_TYPES else "secrets"
        if kind == "certificates":
            certificates.add(str(entry.get("name")).lower())
        out[kind].append(_item(entry, warn_days, now, critical_days))
    out["keys"] = [_item(entry, warn_days, now, critical_days) for entry in listed.get("keys") or []
                   if str(entry.get("name")).lower() not in certificates]
    for kind in KINDS:
        out[kind] = _sorted(out[kind])
    return out


def _names(items):
    return f"{', '.join(i['name'] for i in items[:3])}{'…' if len(items) > 3 else ''}"


def signals(result):
    """Snapshot signals per kind: critical if anything expired or is within the critical days,
    warning if anything is within the warning days."""
    found = []
    critical_days = result.get("critical_days", 0)
    for kind in KINDS:
        items = result.get(kind) or []
        if not items:
            continue
        expired = [i for i in items if i["status"] == CRITICAL and i["days_left"] < 0]
        urgent = [i for i in items if i["status"] == CRITICAL and i["days_left"] >= 0]
        soon = [i for i in items if i["status"] == WARNING]
        if expired:
            found.append(Signal("key vault", f"{kind} expired", CRITICAL,
                                f"{len(expired)} expired: {_names(expired)}", value=len(expired)))
        if urgent:
            found.append(Signal("key vault", f"{kind} expiring", CRITICAL,
                                f"{len(urgent)} within {critical_days} days: {_names(urgent)}", value=len(urgent)))
        if soon:
            found.append(Signal("key vault", f"{kind} expiring soon", WARNING,
                                f"{len(soon)} within {result['warn_days']} days: {_names(soon)}", value=len(soon)))
        if not expired and not urgent and not soon:
            dated = sum(1 for i in items if i["expires"])
            found.append(Signal("key vault", f"{kind} expiry", HEALTHY,
                                f"{len(items)} {kind}, {dated} with an expiry date, none expiring within "
                                f"{result['warn_days']} days" if dated else
                                f"{len(items)} {kind}, none with an expiry date", value=len(items)))
    return found
