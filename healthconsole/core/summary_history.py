"""Summary's What changed: each Summary keeps a snapshot of what it listed (problems, security findings, cost spikes,
readiness findings), one file per tenant and viewer in `cache_dir`, readable only by them. The newest snapshot at least
`compare_hours` old is the baseline; at most one snapshot per hour is kept per scope, for `keep_days`."""
import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .localfiles import private_dir

FORMAT = 1
AREAS = ("problems", "security", "cost", "readiness")
RANK = {"critical": 3, "warning": 2, "info": 1}
_lock = threading.Lock()


def scope_key(subscriptions, groups=None):
    """The same scope gives the same key whatever order it was picked in."""
    text = json.dumps([sorted(s.lower() for s in subscriptions), sorted(g.lower() for g in groups or [])])
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def path_for(directory, tenant_id, object_id):
    return Path(directory).expanduser() / f"summary-{tenant_id.lower()}-{object_id.lower()}.json"


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") == FORMAT:
            return data
    except (OSError, ValueError):
        pass
    return {"format": FORMAT, "scopes": {}}


def _save(path, data):
    private_dir(path.parent)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".summary-", suffix=".tmp")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, separators=(",", ":"))
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _clean(items):
    """{area: {key: {label, status}}}, only the known areas and fields, statuses as given."""
    out = {}
    for area in AREAS:
        rows = (items or {}).get(area)
        if isinstance(rows, dict):
            out[area] = {str(k)[:300]: {"label": str((v or {}).get("label") or k)[:300],
                                        "status": str((v or {}).get("status") or "")[:20]} for k, v in rows.items()}
    return out


def _diff(base, now):
    """Per area present in both: what's new or got worse, and what's gone."""
    out = {}
    for area in AREAS:
        if area not in base or area not in now:
            continue
        before, after = base[area], now[area]
        new = [{"key": k, **v, "was": before[k]["status"] if k in before else None} for k, v in after.items()
               if k not in before or RANK.get(v["status"], 0) > RANK.get(before[k]["status"], 0)]
        gone = [{"key": k, **v} for k, v in before.items() if k not in after]
        out[area] = {"new": new, "resolved": gone}
    return out


def record(path, key, items, compare_hours, keep_days, now=None):
    """Keeps this summary and answers what changed since the baseline: {since, areas} or {since: None, first}."""
    now = now or datetime.now(timezone.utc)
    items = _clean(items)
    with _lock:
        data = _load(path)
        shots = [s for s in data["scopes"].get(key, [])
                 if datetime.fromisoformat(s["at"]) >= now - timedelta(days=max(keep_days, 1))]
        cutoff = now - timedelta(hours=max(compare_hours, 1))
        older = [s for s in shots if datetime.fromisoformat(s["at"]) <= cutoff]
        base = older[-1] if older else None
        if shots and datetime.fromisoformat(shots[-1]["at"]) > now - timedelta(hours=1):
            shots[-1] = {"at": now.isoformat(), "items": {**shots[-1]["items"], **items}}  # within the hour: one snapshot
        else:
            shots.append({"at": now.isoformat(), "items": items})
        data["scopes"][key] = shots
        _save(path, data)
    if base is None:
        first = shots[0]["at"]
        return {"since": None, "first": first,
                "ready_at": (datetime.fromisoformat(first) + timedelta(hours=max(compare_hours, 1))).isoformat()}
    return {"since": base["at"], "areas": _diff(base["items"], items)}


def clear(path):
    with _lock:
        path.unlink(missing_ok=True)
