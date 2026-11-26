"""Access audit's last result per scope, kept on this machine so the audit opens at once: one gzipped file per tenant
and viewer in `cache_dir`, readable only by them. Access changes at any moment and Azure has no cheap "what changed
since", so this is a snapshot shown with its age, never patched; Refresh reads everything again and replaces it.
Kept for `audit.keep_days` (0 keeps nothing)."""
import gzip
import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .localfiles import check_file, private_dir

FORMAT = 1
_lock = threading.Lock()


def scope_key(subscriptions, groups, ignore):
    """Same scope and ignore list, same key, whatever order they were picked in; a changed ignore list is a new key."""
    text = json.dumps([sorted(s.lower() for s in subscriptions), sorted(g.lower() for g in groups or []),
                       sorted(ignore or [])])
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def path_for(directory, tenant_id, object_id):
    return Path(directory).expanduser() / f"audit-{tenant_id.lower()}-{object_id.lower()}.json.gz"


def _load(path):
    try:
        check_file(path, "Access audit cache")  # one another account could swap isn't read
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("format") == FORMAT:
            return data
    except (OSError, ValueError, EOFError):
        pass
    return {"format": FORMAT, "scopes": {}}


def _save(path, data):
    private_dir(path.parent)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".audit-", suffix=".tmp")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(handle, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as out:
            out.write(json.dumps(data, separators=(",", ":")).encode())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _fresh(scopes, keep_days, now):
    cutoff = now - timedelta(days=keep_days)
    return {k: v for k, v in scopes.items() if datetime.fromisoformat(v["at"]) >= cutoff}


def get(path, key, keep_days, now=None):
    """{at, result} for this scope if one was kept within `keep_days`, else None."""
    if keep_days <= 0:
        return None
    with _lock:
        return _fresh(_load(path)["scopes"], keep_days, now or datetime.now(timezone.utc)).get(key)


def put(path, key, result, keep_days, now=None):
    """Keeps this result as the scope's newest and drops the expired ones; returns when it was kept (or None)."""
    if keep_days <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    with _lock:
        data = _load(path)
        data["scopes"] = {**_fresh(data["scopes"], keep_days, now), key: {"at": now.isoformat(), "result": result}}
        _save(path, data)
    return now.isoformat()


def clear(path):
    with _lock:
        path.unlink(missing_ok=True)
