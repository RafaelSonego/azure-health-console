"""A workload's names: checking the cluster, namespace, kind and name a request gives, the pattern its pods carry in
Container Insights, and which of a monitoring tool's app names is the workload's (by shared words, never a list)."""
import re
from collections import Counter

CLUSTER_ID = re.compile(r"^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.containerservice"
                        r"/managedclusters/[^/]+$", re.I)
K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
KINDS = ("Deployment", "StatefulSet", "DaemonSet")
TOP_CANDIDATES = 15
# Words that name nothing in any naming scheme (protocols, plumbing). A team's own words (an environment, a company
# prefix, a region code) are found in the data instead: see common_words.
STOP_WORDS = {"api", "apis", "app", "apps", "svc", "service", "services", "http", "https", "www", "com", "net", "org",
              "default", "web", "backend", "azure"}
COMMON_SHARE, COMMON_MIN = 0.5, 4  # a word in at least half of 4+ names is the convention, not the workload


def check_workload(cluster_id, namespace, kind, name):
    if not CLUSTER_ID.match(cluster_id or ""):
        raise ValueError("cluster must be an AKS cluster ID")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    if not K8S_NAME.match(namespace or "") or not K8S_NAME.match(name or ""):
        raise ValueError("namespace and workload must be Kubernetes names")


def owner_regex(kind, name):
    """KubePodInventory's ControllerName for the workload's pods: a Deployment's ReplicaSets add a hash."""
    escaped = re.escape(name)
    return f"^{escaped}-[a-z0-9]{{5,10}}$" if kind == "Deployment" else f"^{escaped}$"


def tokens(text):
    """The words in a name that could name a workload (lowercase, 3+ characters, not STOP_WORDS)."""
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS and not w.isdigit() and not re.fullmatch(r"v\d+", w)}


def common_words(texts):
    """Words in at least half of `texts` (when there are COMMON_MIN or more): what every name carries (an environment,
    a company prefix, a region code) can't tell one workload from another, whatever the naming scheme."""
    texts = [t for t in texts if t]
    if len(texts) < COMMON_MIN:
        return set()
    counts = Counter(w for t in texts for w in tokens(t))
    return {w for w, n in counts.items() if n >= COMMON_SHARE * len(texts)}


def app_candidates(names, name):
    """Monitoring-tool app names that could be the workload's: same name first, then shared words."""
    wanted, out = tokens(name) - common_words(names), []
    for app in names:
        exact = app.lower() == name.lower()
        shared = wanted & tokens(app)
        if exact or shared:
            out.append({"app": app, "score": 10 if exact else 2 * len(shared),
                        "why": ["same name as the workload"] if exact else [f"shares {', '.join(sorted(shared))}"]})
    out.sort(key=lambda r: (-r["score"], r["app"]))
    return out[:TOP_CANDIDATES]
