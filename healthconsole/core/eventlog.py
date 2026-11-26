"""What happened in the console, on this machine: errors (each with an ID the page shows), an audit of what people did,
and every request sent out counted by service, method and path, so it's plain that only reads left. JSON lines, one
file per kind and day, kept `logs.keep_days`. Off until `configure()`: tests and one-off CLI commands write nothing."""
import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .localfiles import private_dir

KINDS = ("errors", "audit", "requests")
FLUSH_SECONDS = 30
MAX_TEXT = 8000
_FILE = re.compile(r"^(errors|audit|requests)-(\d{4}-\d{2}-\d{2})\.jsonl?$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Path segments whose next segment is a name or ID: grouped as {…}, so a pattern counts every resource of a kind.
_NAMED = {"subscriptions", "resourcegroups", "managementgroups", "namespaces", "users", "groups", "serviceprincipals",
          "applications", "nodes", "pods", "deployments", "statefulsets", "daemonsets", "replicasets", "jobs", "cronjobs",
          "services", "configmaps", "horizontalpodautoscalers", "poddisruptionbudgets", "repositories", "manifests",
          "tags", "packages"}
# Never written: credentials in a URL, a header or an error message.
_SECRETS = [(re.compile(r"(?i)\b(bearer)\s+[\w.~+/=-]+"), r"\1 [hidden]"),
            (re.compile(r"(?i)([?&](?:sig|signature|code|token|access_token|key|password|secret)=)[^&\s\"']+"), r"\1[hidden]"),
            (re.compile(r"(?i)\b(password|secret|api[_-]?key|client_secret|token)(\s*[=:]\s*)[^\s,;\"']+"), r"\1\2[hidden]")]

_lock = threading.RLock()
_state = {"dir": None, "keep_days": 30, "day": None}
_counts = Counter()  # (day, service, method, host, path, outcome) -> requests
_dirty = threading.Event()
logger = logging.getLogger(__name__)


def configure(directory, keep_days=30):
    """Turns the log on (or off with `directory=None`); today's request counts carry on across restarts."""
    if enabled():
        flush()
    with _lock:
        folder = Path(directory).expanduser() if directory else None
        if folder:
            private_dir(folder)  # refuses a folder another account can change (UnsafePath)
            os.chmod(folder, 0o700)
        _state.update(dir=folder, keep_days=max(1, int(keep_days)), day=None)
        _counts.clear()
        if folder:
            for row in _read_json(folder / f"requests-{_today()}.json"):
                _counts[tuple(row[k] for k in ("day", "service", "method", "host", "path", "outcome"))] += row["count"]
    prune()
    if folder and not any(t.name == "eventlog-flush" for t in threading.enumerate()):
        threading.Thread(target=_flusher, name="eventlog-flush", daemon=True).start()


def enabled():
    return _state["dir"] is not None


def folder():
    return _state["dir"]


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def redact(text):
    text = str(text if text is not None else "")
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text[:MAX_TEXT]


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def _append(kind, event):
    with _lock:
        if not enabled():
            return
        day = _today()
        if _state["day"] != day:  # a new day: drop what's past keep_days
            _state["day"] = day
            prune()
        path = _state["dir"] / f"{kind}-{day}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(_clean(event), ensure_ascii=False, default=str) + "\n")


def new_id():
    return uuid.uuid4().hex[:8]


def record_error(message, *, source, error=None, user=None, user_id=None, context=None, error_id=None, stack=""):
    """Writes one error and returns its ID (None when the log is off): the page shows the ID, Logs finds it."""
    if not enabled():
        return None
    error_id = error_id or new_id()
    if error is not None:
        stack = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    _append("errors", {"at": _now(), "id": error_id, "source": source, "message": message, "type":
                       type(error).__name__ if error is not None else "", "user": user or "", "user_id": user_id or "",
                       "context": context or {},
                       "stack": stack})
    return error_id


def audit(action, *, kind, outcome, user=None, user_id=None, detail=None):
    """One thing someone did: `kind` is change, sensitive (a read about people or secrets) or read (started a read)."""
    _append("audit", {"at": _now(), "action": action, "kind": kind, "outcome": outcome, "user": user or "",
                      "user_id": user_id or "", "detail": detail or {}})


def path_pattern(url):
    """The request's path with names and IDs as {…}: ARM's provider types and actions stay, values go."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    out, i = [], 0
    while i < len(parts):
        seg = parts[i]
        if seg.lower() == "providers" and i + 1 < len(parts):
            out += [seg, parts[i + 1]]
            i += 2
            while i < len(parts) and parts[i].lower() != "providers":  # type, name, type, name…
                out.append(parts[i])
                if i + 1 < len(parts) and parts[i + 1].lower() != "providers":
                    out.append("{…}")
                i += 2
            continue
        if seg.lower() in _NAMED and i + 1 < len(parts):
            out += [seg, "{…}"]
            i += 2
            continue
        out.append("{…}" if _GUID.match(seg) or seg.isdigit() else seg)
        i += 1
    return "/" + "/".join(out)


def outcome_of(status):
    if isinstance(status, int):
        return f"{status // 100}xx"
    return "refused" if status == "refused" else "no answer"


def count_request(service, method, url, status):
    """One request sent (or refused before sending, `status="refused"`), counted, never its query or body."""
    if not enabled():
        return
    key = (_today(), service, method.upper(), urlsplit(url).netloc.lower(), path_pattern(url), outcome_of(status))
    with _lock:
        _counts[key] += 1
    _dirty.set()


def flush():
    """Writes today's request counts (one file per day, rewritten whole, so a crash loses at most FLUSH_SECONDS)."""
    with _lock:
        if not enabled():
            return
        by_day = {}
        for (day, *rest), n in _counts.items():
            by_day.setdefault(day, []).append(dict(zip(("day", "service", "method", "host", "path", "outcome"), [day, *rest]),
                                                   count=n))
        for day, rows in by_day.items():
            path = _state["dir"] / f"requests-{day}.json"
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rows, f)
            os.replace(tmp, path)
        for key in [k for k in _counts if k[0] != _today()]:  # a past day is on disk for good
            del _counts[key]
    _dirty.clear()


def _flusher():
    while True:
        _dirty.wait()
        time.sleep(FLUSH_SECONDS)
        try:
            flush()
        except OSError as error:
            logger.warning("couldn't write the request counts: %s", error)


def prune():
    with _lock:
        if not enabled():
            return
        oldest = (datetime.now(timezone.utc) - timedelta(days=_state["keep_days"])).strftime("%Y-%m-%d")
        for f in _state["dir"].iterdir():
            m = _FILE.match(f.name)
            if m and m.group(2) < oldest:
                f.unlink(missing_ok=True)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def read(kind, days=1, user_id=None, limit=500):
    """Newest first: errors and audit events, or request counts per day; `user_id` keeps only that user's events."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    if not enabled():
        return []
    flush()
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    files = sorted((f for f in _state["dir"].iterdir() if (m := _FILE.match(f.name)) and m.group(1) == kind
                    and m.group(2) >= since), reverse=True)
    if kind == "requests":
        return [row for f in files for row in _read_json(f)]
    out = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if user_id is None or event.get("user_id") == user_id:
                out.append(event)
                if len(out) >= limit:
                    return out
    return out


class ErrorHandler(logging.Handler):
    """ERROR records from the console's own loggers land in the errors log too (`extra={"recorded": True}` skips one
    already written with its ID)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, record):
        if getattr(record, "recorded", False) or not enabled():
            return
        try:
            error = record.exc_info[1] if record.exc_info else None
            record_error(record.getMessage(), source=record.name, error=error)
        except Exception:  # the log must never take the console down
            self.handleError(record)
