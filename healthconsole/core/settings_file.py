"""Saving settings from the page: edit config.yaml in place, keeping comments and quoting,
validate the result before writing, and keep the previous file as config.yaml.bak."""
import io
import json
import threading
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import config
from .localfiles import UnsafePath, read_trusted, write_private_text

RESET = "__default__"
_lock = threading.Lock()


def _yaml():
    handler = YAML()
    handler.preserve_quotes = True
    handler.indent(mapping=2, sequence=4, offset=2)
    return handler


def _load(file):
    if not file.is_file():
        return CommentedMap()
    try:
        text = read_trusted(file, "Settings").decode("utf-8")
    except UnsafePath as error:
        raise config.ConfigError(str(error)) from error
    data = _yaml().load(text) or CommentedMap()
    if not isinstance(data, dict):
        raise config.ConfigError(f"{file}: expected a mapping of settings.")
    return data


def _plain(data):
    return json.loads(json.dumps(data))


def _write(file, data):
    settings = config.from_data(_plain(data), str(file))
    if file.is_file():
        backup = file.with_name(file.name + ".bak")
        write_private_text(backup, read_trusted(file, "Settings").decode("utf-8"))
    out = io.StringIO()
    _yaml().dump(data, out)
    write_private_text(file, out.getvalue())
    return settings


def _tenant_entry(data, tenant_key):
    if not tenant_key or tenant_key == "*":
        raise config.ConfigError("This belongs to one tenant: its ID or domain is needed, not \"*\".")
    tenants = data.setdefault("tenants", CommentedMap())
    key = next((k for k in tenants if str(k).lower() == tenant_key.lower()), tenant_key)
    entry = tenants.get(key)
    if entry is None:
        entry = tenants[key] = CommentedMap()
    return entry


def apply_integrations(file, tenant_key, servicenow=None, monitors=None):
    """Changes one tenant's integrations: `servicenow` and `monitors` are each the whole list of connections.
    Never a secret: those go to the OS keychain. Validated before anything is written, like `apply`."""
    file = Path(file).expanduser()
    for items, what in ((servicenow, "ServiceNow"), (monitors, "monitoring")):
        if items is not None:
            config.check_connection_names(items, what)  # a plain message for the card, before the file is touched
    with _lock:
        data = _load(file)
        section = _tenant_entry(data, tenant_key).setdefault("integrations", CommentedMap())
        if servicenow is not None:
            section["servicenow"] = CommentedSeq(CommentedMap(c.items()) for c in servicenow)
        if monitors is not None:
            section["monitors"] = CommentedSeq(CommentedMap(m.items()) for m in monitors)
        return _write(file, data)


def _store_scopes(entry, scopes):
    entry["saved_scopes"] = CommentedSeq(CommentedMap((k, v) for k, v in scope.items() if v not in (None, [], {}) or k == "name")
                                         for scope in scopes)
    if not scopes:
        del entry["saved_scopes"]


def apply_saved_scopes(file, tenant_key, scopes):
    """Replaces one tenant's saved scopes with `scopes` (validated before anything is written)."""
    file = Path(file).expanduser()
    with _lock:
        data = _load(file)
        _store_scopes(_tenant_entry(data, tenant_key), scopes)
        return _write(file, data)


def apply_open_with(file, tenant_key, value):
    """What this tenant opens with: "none", "all" or a saved scope's name."""
    value = str(value or "").strip()
    if not value:
        raise config.ConfigError("Open with needs a value: none, all or a saved scope's name.")
    file = Path(file).expanduser()
    with _lock:
        data = _load(file)
        _tenant_entry(data, tenant_key)["open_with"] = value
        return _write(file, data)


def move_legacy_scope(file, tenant_key, legacy):
    """Saves `legacy` (config.legacy_scope) as this tenant's saved scopes and open_with, and drops the old keys from its
    own entry. A "*" entry and the global triage.scope stay for other tenants; this one ignores them once open_with is set."""
    file = Path(file).expanduser()
    with _lock:
        data = _load(file)
        entry = _tenant_entry(data, tenant_key)
        current = [dict(s) for s in entry.get("saved_scopes") or []]
        _store_scopes(entry, current + [{k: v for k, v in s.items() if k in config.SAVED_SCOPE_KEYS} for s in legacy["scopes"]])
        entry["open_with"] = legacy["open_with"]
        for key in config.LEGACY_TENANT_KEYS:
            entry.pop(key, None)
        return _write(file, data)


def apply_workload_link(file, tenant_key, link):
    """Saves one workload's link, replacing the one with the same key; a link with no front door, app or component
    removes it. Validated before anything is written."""
    file = Path(file).expanduser()
    keep = {k: v for k, v in link.items() if v not in (None, "", [], {})}
    with _lock:
        data = _load(file)
        entry = _tenant_entry(data, tenant_key)
        current = [dict(x) for x in entry.get("workload_links") or [] if str(x.get("key", "")).lower() != link["key"].lower()]
        if any(keep.get(k) for k in ("apim", "agw", "app", "component")):
            current.append(keep)
        if current:
            entry["workload_links"] = CommentedSeq(CommentedMap(
                (k, CommentedSeq(CommentedMap(i.items()) for i in v) if isinstance(v, list) else v) for k, v in x.items())
                for x in current)
        else:
            entry.pop("workload_links", None)
        return _write(file, data)


def apply_alert_rules(file, tenant_key, rules):
    """Replaces one tenant's alert rules (each as the page sends it: kind, scope, signals, …), validated first."""
    file = Path(file).expanduser()
    stored = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise config.ConfigError("Each alert rule must be a mapping.")
        item = CommentedMap(name=str(rule.get("name") or "").strip())
        if rule.get("kind") == "watchlist" or rule.get("watchlist") is True:
            item["watchlist"] = True
        else:
            item["scope"] = rule.get("scope")
        for key in ("signals", "interval_minutes", "notify", "notify_recovered", "enabled", "servicenow_priority"):
            if key in rule:
                item[key] = CommentedSeq(rule[key]) if key == "signals" and isinstance(rule[key], list) else rule[key]
        stored.append(item)
    config.check_alert_rules(_plain(stored))  # a plain message for the page, before the file is touched
    with _lock:
        data = _load(file)
        entry = _tenant_entry(data, tenant_key)
        entry["alert_rules"] = CommentedSeq(stored)
        if not stored:
            del entry["alert_rules"]
        return _write(file, data)


def move_legacy_integrations(file, tenant_key):
    """Moves the old global `integrations` block into this tenant, which must not have its own yet."""
    file = Path(file).expanduser()
    with _lock:
        data = _load(file)
        legacy = data.get("integrations")
        if not legacy:
            raise config.ConfigError("There are no global integrations to move.")
        entry = _tenant_entry(data, tenant_key)
        if entry.get("integrations"):
            raise config.ConfigError("This tenant already has its own integrations; edit the file to merge them by hand.")
        entry["integrations"] = legacy
        del data["integrations"]
        return _write(file, data)


def apply(file, values=None):
    """`values`: {"section.key" or "key": value, or RESET to remove it}.
    Returns the new, validated settings; nothing is written if they don't validate."""
    file = Path(file).expanduser()
    with _lock:
        data = _load(file)
        known = {(section, key): kind for section, key, kind, _ in config.SCHEMA}
        for dotted, value in (values or {}).items():
            section, _, key = dotted.rpartition(".")
            section = section or None
            if (section, key) not in known:
                raise config.ConfigError(f"Unknown setting '{dotted}'.")
            target = data if section is None else data.setdefault(section, CommentedMap())
            if value == RESET:
                target.pop(key, None)
                if section and not target:
                    data.pop(section, None)
            else:
                target[key] = None if known[(section, key)] == "text" and value == "" else value
        return _write(file, data)
