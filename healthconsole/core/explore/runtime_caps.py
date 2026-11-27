"""A container's runtime memory cap against its memory limit: the flags its env and arguments set (Java, Node.js, Go,
.NET), the cap they give, and the room left under the limit for everything outside it. Facts only: no flag set is a
fact too ("can grow to the limit"). Only the matching flags are kept, never the rest of a value."""
import re

# Env vars and arguments that carry a runtime's memory settings; only the matching flags are kept, never the value.
MEMORY_ENV = ("JAVA_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CATALINA_OPTS", "NODE_OPTIONS",
              "GOMEMLIMIT", "DOTNET_GCHeapHardLimit", "DOTNET_GCHeapHardLimitPercent")
JAVA_ENV = ("JAVA_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CATALINA_OPTS")
MEMORY_FLAG = re.compile(r"(-Xm[xs][0-9]+[kKmMgG]?|-XX:(?:Max|Initial|Min)RAMPercentage=[0-9.]+|-XX:MaxMetaspaceSize=[0-9]+[kKmMgG]?"
                         r"|-XX:MaxDirectMemorySize=[0-9]+[kKmMgG]?|-XX:[+-]UseContainerSupport|--max-old-space-size=[0-9]+)")
PLAIN_VALUE = re.compile(r"^[0-9]+(\.[0-9]+)?[A-Za-z]{0,3}$|^0x[0-9a-fA-F]+$")  # GOMEMLIMIT=6GiB, DOTNET_…=0x…
_UNITS = {"k": 1 / 1024, "m": 1, "g": 1024}
JVM_DEFAULT_PERCENT = 25  # the JVM's MaxRAMPercentage when none is set (JDK 10 and later, container-aware)
_GO_UNITS = {"": 1 / 2**20, "B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "TiB": 1024**2}


def memory_flags(container):
    """[{source, flag}] for the container's runtime memory settings in its env, command and args. Only flags that
    match MEMORY_FLAG (or a plain number for GOMEMLIMIT and .NET) are returned; the rest of a value is never kept."""
    out = []
    for env in container.get("env") or []:
        name = env.get("name") or ""
        if name not in MEMORY_ENV:
            continue
        if "valueFrom" in env:
            out.append({"source": f"env {name}", "flag": "", "note": "set from a ConfigMap or Secret, not read"})
            continue
        value = str(env.get("value") or "")
        if name.startswith(("GOMEMLIMIT", "DOTNET_")):
            if PLAIN_VALUE.match(value.strip()):
                out.append({"source": f"env {name}", "flag": f"{name}={value.strip()}"})
            continue
        out += [{"source": f"env {name}", "flag": flag} for flag in MEMORY_FLAG.findall(value)]
    for field in ("command", "args"):
        for part in container.get(field) or []:
            out += [{"source": field, "flag": flag} for flag in MEMORY_FLAG.findall(str(part))]
    return out


def heap_max_mib(flags, memory_limit_mib=None):
    """The heap ceiling the flags set, in MiB: -Xmx, or MaxRAMPercentage of the memory limit; None when unset."""
    for f in flags:
        m = re.match(r"-Xmx([0-9]+)([kKmMgG]?)$", f["flag"])
        if m:
            return int(m.group(1)) * _UNITS.get(m.group(2).lower(), 1 / 2**20)
    for f in flags:
        m = re.match(r"-XX:MaxRAMPercentage=([0-9.]+)$", f["flag"])
        if m and memory_limit_mib:
            return float(m.group(1)) / 100 * memory_limit_mib
    return None


def _value(flags, prefix):
    return next((f["flag"][len(prefix):] for f in flags if f["flag"].startswith(prefix)), None)


def runtime_cap(container, limit_mib=None, flags=None):
    """{runtime, cap_mib, how, outside_mib, of_limit}: the runtime the flags name and the memory cap they set. A Java
    container (a JAVA_* env var or a JVM flag) with no cap set is at the JVM's default share of the limit. Only the
    spec is read (env, command, args): a cap set in the image or through envFrom isn't seen, so none found says only that."""
    flags = memory_flags(container) if flags is None else flags
    names = {(e.get("name") or "") for e in container.get("env") or []}
    unread = any(f.get("note") for f in flags)
    runtime, cap, how = "", None, ""
    heap = heap_max_mib(flags, limit_mib)
    node = _value(flags, "--max-old-space-size=")
    go = _value(flags, "GOMEMLIMIT=")
    dotnet, dotnet_pct = _value(flags, "DOTNET_GCHeapHardLimit="), _value(flags, "DOTNET_GCHeapHardLimitPercent=")
    if heap is not None:
        runtime, cap, how = "Java", heap, "heap max (-Xmx)" if _value(flags, "-Xmx") else "heap max (MaxRAMPercentage)"
    elif node:
        runtime, cap, how = "Node.js", float(node), "old space (--max-old-space-size)"
    elif go:
        m = re.fullmatch(r"([0-9.]+)\s*([KMGT]iB|B)?", go.strip())
        if m:
            runtime, cap, how = "Go", float(m.group(1)) * _GO_UNITS[m.group(2) or ""], "soft limit (GOMEMLIMIT)"
    elif dotnet and dotnet.lower().startswith("0x"):
        runtime, cap, how = ".NET", int(dotnet, 16) / 2**20, "GC heap limit (GCHeapHardLimit)"
    elif dotnet_pct and dotnet_pct.lower().startswith("0x") and limit_mib:
        runtime, cap, how = ".NET", int(dotnet_pct, 16) / 100 * limit_mib, "GC heap limit (GCHeapHardLimitPercent)"
    elif names & set(JAVA_ENV) or any(f["flag"].startswith(("-X", "-XX:")) for f in flags):
        runtime = "Java"
        if limit_mib:
            cap, how = JVM_DEFAULT_PERCENT / 100 * limit_mib, f"heap max not set: the JVM's default, {JVM_DEFAULT_PERCENT}% of the limit"
        else:
            how = "heap max not set, and no memory limit: the JVM sizes it from the node's memory"
    cap = round(cap) if cap is not None else None
    return {"runtime": runtime, "cap_mib": cap, "how": how or ("set from a ConfigMap or Secret, not read" if unread else ""),
            "limit_mib": round(limit_mib) if limit_mib else None,
            "outside_mib": round(limit_mib - cap) if cap is not None and limit_mib else None,
            "of_limit": round(100 * cap / limit_mib) if cap is not None and limit_mib else None}


def workload_caps(containers):
    """[{container, runtime, cap_mib, limit_mib, of_limit, how}] for a workload's pod template containers."""
    from .aks_checks import parse_mem  # here: aks_checks imports this module

    out = []
    for c in containers:
        limit = parse_mem(((c.get("resources") or {}).get("limits") or {}).get("memory"))
        out.append({"container": c.get("name"), **runtime_cap(c, limit)})
    return out
