"""The shape every list of findings shares (Triage, Readiness, Vulnerabilities' platform checks): a card with a title,
a description and items, each item a resource with a status and a detail. The page draws them all with one renderer
(`renderCard`), so building them in one place keeps the two sides in step."""
from . import taxonomy
from .model import red_or_yellow


def item(row, status, detail, id_key="id", name=None, display_type=None, **extra):
    """One resource line from a Resource Graph row (id, name, type, kind, resourceGroup, subscriptionId)."""
    resource_id = row.get(id_key) or ""
    return {"id": resource_id, "name": name or row.get("name") or resource_id.rsplit("/", 1)[-1],
            "display_type": display_type or taxonomy.display_name((row.get("type") or "").lower(), row.get("kind")),
            "resource_group": row.get("resourceGroup") or "", "subscription_id": (row.get("subscriptionId") or "").lower(),
            "status": status, "detail": detail, **extra}


def card(key, title, description, items, error=None, **extra):
    """A card coloured by its worst item; `error` says why it (or part of it) couldn't be read."""
    return {"key": key, "title": title, "description": description, "items": items, "error": error or None,
            "status": red_or_yellow(i["status"] for i in items), **extra}
