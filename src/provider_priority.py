"""Pick the workbench's fourth agent using quota that will expire soonest."""

import json
import math
import threading
import time

import provider_defaults
from cursor_pools import model_pool

DAY = 86400
SOON = 5 * DAY
FRESH = 15 * 60
MAX_AGE = DAY
REFRESH_WAIT = 2.0
PROVIDERS = ("Cursor", "Kiro")


def allowance(agent, entry, now):
    """Return remaining percent and reset time for the model's current pool."""
    if not isinstance(entry, dict):
        return None
    try:
        age = now - float(entry["fetched"])
        if not math.isfinite(age) or not 0 <= age <= MAX_AGE:
            return None
        defaults = provider_defaults.current(agent) or {}
        model = defaults.get("model", "").lower()
        label = "mo" if agent["name"] == "Kiro" else model_pool(model)
        if not isinstance(entry.get("windows"), list):
            return None
        window = next(w for w in entry["windows"] if isinstance(w, dict) and w.get("label") == label)
        pct, resets = float(window["pct"]), float(window["resets"])
        if not math.isfinite(pct) or not math.isfinite(resets) or pct < 0 or resets <= now:
            return None
        return max(0.0, 100 - pct), resets
    except (KeyError, TypeError, ValueError, StopIteration):
        return None


def choose(agents, usage, now=None):
    """Soon-expiring usable quota wins; otherwise compare percent left per day."""
    now = time.time() if now is None else now
    available = [a for name in PROVIDERS for a in agents if a["name"] == name]
    if not available:
        raise RuntimeError("Neither Cursor nor Kiro is installed. Run --picker-doctor.")
    quotas = {a["name"]: allowance(a, usage.get(a["name"]), now) for a in available}
    usable = [a for a in available if quotas[a["name"]] and quotas[a["name"]][0] > 0]
    if usable:
        soon = [a for a in usable if quotas[a["name"]][1] - now <= SOON]
        if soon:
            winner = min(soon, key=lambda a: (quotas[a["name"]][1], -quotas[a["name"]][0]))
            why = "quota resets within five days"
        else:
            winner = max(usable, key=lambda a: quotas[a["name"]][0] /
                         max((quotas[a["name"]][1] - now) / DAY, 1 / 24))
            why = "more remaining quota per day until reset"
        left, resets = quotas[winner["name"]]
        reason = f"{why} ({left:.1f}% left, {(resets - now) / DAY:.1f} days)"
    else:
        unknown = [a for a in available if quotas[a["name"]] is None]
        winner = (unknown or available)[0]
        reason = "usage unavailable; installed-provider fallback" if unknown else "both quotas exhausted; installed-provider fallback"
    if len(available) == 1:
        reason = "only installed candidate; " + reason
    return winner["name"], reason


def select(picker):
    """Use recent shared meters, giving stale readers a bounded refresh window."""
    agents = [a for a in picker.AGENTS if a["name"] in PROVIDERS and picker.installed(a)]
    try:
        usage = json.loads(picker.USAGE_CACHE.read_text(encoding="utf-8"))
        if not isinstance(usage, dict):
            usage = {}
    except (OSError, ValueError):
        usage = {}
    now = time.time()
    results, lock, readers = {}, threading.Lock(), []

    def refresh(name):
        try:
            windows = picker.FETCHERS[name]()
            if windows:
                with lock:
                    results[name] = {"windows": windows, "fetched": time.time()}
        except Exception:
            pass  # A failed usage read must never prevent an installed CLI launch.

    for agent in agents:
        name, entry = agent["name"], usage.get(agent["name"])
        if allowance(agent, entry, now) is None or now - float(entry["fetched"]) > FRESH:
            reader = threading.Thread(target=refresh, args=(name,), daemon=True)
            reader.start()
            readers.append(reader)
    deadline = time.monotonic() + REFRESH_WAIT
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    with lock:
        usage.update(results)
    return choose(agents, usage)
