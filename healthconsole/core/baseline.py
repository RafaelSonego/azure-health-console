"""Last week as the baseline for a resource group: which metrics moved most against the same window a week
earlier. Built from values every snapshot already reads, so it costs no extra calls."""
import math

from .model import CRITICAL, INFO, WARNING

FACTOR = 2.0
MAX_MOVERS = 25


def _move(signal):
    """(direction, ratio) when a metric at least doubled or halved; None for small or steady ones."""
    value, base = signal.value, signal.baseline
    if value is None or base is None or (value == 0 and base == 0):
        return None
    if base == 0:
        return "new", None
    if value == 0:
        return "gone", None
    ratio = value / base
    if ratio >= FACTOR:
        return "up", ratio
    if ratio <= 1 / FACTOR:
        return "down", ratio
    return None


def compare(nodes, window_minutes):
    compared, without, movers = 0, 0, []
    for node in nodes:
        pairs = [s for s in node.signals if s.value is not None and s.baseline is not None]
        if not pairs:
            without += 1 if any(s.value is not None for s in node.signals) else 0
            continue
        compared += 1
        for signal in pairs:
            moved = _move(signal)
            if not moved:
                continue
            direction, ratio = moved
            movers.append({"node_id": node.id, "name": node.name, "display_type": node.display_type,
                           "resource_group": node.resource_group, "signal": signal.name, "unit": signal.unit,
                           "value": signal.value, "baseline": signal.baseline, "direction": direction,
                           "ratio": round(ratio, 2) if ratio else None,
                           "status": signal.status if signal.status in (CRITICAL, WARNING) else INFO})
    # Status first, then the biggest move either way; appearing or vanishing counts as biggest.
    movers.sort(key=lambda m: (m["status"] == INFO, -(abs(math.log(m["ratio"])) if m["ratio"] else 99)))
    return {"window_minutes": window_minutes, "compared": compared, "without_baseline": without,
            "movers": movers[:MAX_MOVERS], "more": max(len(movers) - MAX_MOVERS, 0)}


def describe(mover):
    if mover["direction"] == "new":
        return "none last week"
    if mover["direction"] == "gone":
        return "none now"
    return f"{mover['ratio']:g}x last week" if mover["direction"] == "up" else f"{1 / mover['ratio']:.1f}x lower than last week"
