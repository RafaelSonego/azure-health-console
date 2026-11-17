"""Parsing Azure resource IDs. Everything keys off the lowercased ID, since ARM
is inconsistent about casing between listings and a resource's own properties."""
import re

# Matches a resource ID anywhere inside a property value or key.
RESOURCE_ID_PATTERN = re.compile(
    r"/subscriptions/[0-9a-fA-F-]{36}"
    r"/resourceGroups/[^/\"'\s]+"
    r"/providers/[^\"'\s]+",
    re.IGNORECASE,
)

_PROVIDERS_SPLIT = re.compile(r"/providers/", re.IGNORECASE)

# "/subscriptions/s/resourceGroups/r/providers/ns/type/name" split on "/".
TOP_LEVEL_ID_SEGMENTS = 9


def normalise(resource_id):
    return (resource_id or "").rstrip("/").lower()


def _segments(resource_id):
    return [part for part in (resource_id or "").split("/") if part]


def name_of(resource_id):
    segments = _segments(resource_id)
    return segments[-1] if segments else ""


def subscription_of(resource_id):
    segments = _segments(resource_id)
    for index, segment in enumerate(segments):
        if segment.lower() == "subscriptions" and index + 1 < len(segments):
            return segments[index + 1].lower()
    return ""


def resource_group_of(resource_id):
    segments = _segments(resource_id)
    for index, segment in enumerate(segments):
        if segment.lower() == "resourcegroups" and index + 1 < len(segments):
            return segments[index + 1]
    return ""


def resource_type_of(resource_id):
    """`microsoft.web/sites`, or `microsoft.sql/servers/databases` for a nested type."""
    parts = _PROVIDERS_SPLIT.split(resource_id or "")
    if len(parts) < 2:
        return ""
    segments = [segment for segment in parts[-1].split("/") if segment]
    if len(segments) < 2:
        return ""
    namespace, tail = segments[0], segments[1:]
    return "/".join([namespace] + tail[0::2]).lower()


def parent_of(resource_id):
    """The ID one type/name pair up, or None for a top-level resource."""
    segments = normalise(resource_id).split("/")
    if len(segments) <= TOP_LEVEL_ID_SEGMENTS:
        return None
    return "/".join(segments[:-2])


def top_level(resource_id):
    segments = normalise(resource_id).split("/")
    return "/".join(segments[:TOP_LEVEL_ID_SEGMENTS])
