"""Pinned resources for a peak-traffic event, kept in their own YAML file so the page can
add and remove them without rewriting (and losing comments in) config.yaml."""
import threading
from pathlib import Path

import yaml

from . import baseline, snapshot
from .arm import ArmError
from .localfiles import write_private_text

_lock = threading.Lock()


class Watchlist:
    def __init__(self, path):
        self.path = Path(path).expanduser()

    def items(self, tenant=None):
        """Items for one tenant (plus any saved before tenants were recorded); all when tenant is None."""
        with _lock:
            return self._for(self._read(), tenant)

    @staticmethod
    def _for(items, tenant):
        if tenant is None:
            return items
        return [i for i in items if not i.get("tenant") or i["tenant"].lower() == tenant.lower()]

    def _read(self):
        if not self.path.is_file():
            return []
        data = yaml.safe_load(self.path.read_text()) or {}
        items = []
        for i in data.get("items") or []:
            if isinstance(i, dict) and i.get("id"):
                item = {"id": str(i["id"]), "name": str(i.get("name") or i["id"].rsplit("/", 1)[-1]),
                        "kind": i.get("kind", "resource")}
                if i.get("tenant"):
                    item["tenant"] = str(i["tenant"])
                items.append(item)
        return items

    def _write(self, items):
        header = "# Managed by Azure Health Console's Watchlist; editing by hand is fine.\n"
        write_private_text(self.path, header + yaml.safe_dump({"items": items}, sort_keys=False))

    def add(self, resource_id, name, kind="resource", tenant=None):
        with _lock:
            items = self._read()
            if not any(i["id"].lower() == resource_id.lower() for i in items):
                item = {"id": resource_id, "name": name, "kind": kind}
                if tenant:
                    item["tenant"] = tenant
                items.append(item)
                self._write(items)
            return self._for(items, tenant)

    def remove(self, resource_id, tenant=None):
        with _lock:
            items = [i for i in self._read() if i["id"].lower() != resource_id.lower()]
            self._write(items)
            return self._for(items, tenant)


def check(client, items, discovery_options, health_options, profiles, progress=None):
    """One quick check per item (the resource, or the group's members, without dependencies): Check now and the
    Watchlist's alert rule. An item that can't be read is `unknown` with the reason, so the others still report."""
    rows = []
    for index, item in enumerate(items, 1):
        if progress:
            progress(f"Checking {item['name']} ({index} of {len(items)})")
        try:
            if item["kind"] == "resource group":
                sub, group = item["id"].split("/")[2], item["id"].rstrip("/").split("/")[-1]
                snap = snapshot.build_group(client, sub, group, discovery_options, health_options, profiles)
            else:
                snap = snapshot.build(client, item["id"], discovery_options, health_options, profiles)
            problems = [f"{n.name}: {s.name}: {s.detail}" for n in snap.nodes for s in n.problems()]
            movers = baseline.compare(snap.nodes, health_options.window_minutes)["movers"]
            rows.append({**item, "status": snap.status, "counts": snap.counts(), "problems": problems[:5],
                         "checked_at": snap.taken_at, "movers": [{**m, "move": baseline.describe(m)} for m in movers]})
        except ArmError as error:
            rows.append({**item, "status": "unknown", "counts": {}, "problems": [str(error).splitlines()[0]],
                         "checked_at": "", "movers": []})
    return rows
