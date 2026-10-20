"""Usage limits for herdr-ai agents, and handing a goal to another provider.

With the "Hand off at 90%" setting on (off by default), every lead or solo agent
herdr-ai starts is told how to check its provider's usage and, when a limit is
near and the goal is unfinished, to hand the goal to the next provider with quota: Codex, then Claude, then whichever of Cursor and Kiro
resets sooner. Antigravity's Gemini Flash is never handed a goal or a team to
lead; teams spend it on trivial crew work instead.
"""

import json
import shutil
import threading
import time

import agent_picker as ap
import preferences
import provider_priority

NEAR = 90          # percent of any window used: hand off before the hard stop
FRESH = 5 * 60     # older meters are re-read before deciding
REFRESH_WAIT = 3.0
ORDER = ("Codex", "Claude", "Cursor|Kiro")   # handoff and orchestrator priority
GEMINI = "Antigravity"
AUTO = preferences.Setting(
    "herdr_ai.handoff", "Herdr AI", f"Hand off at {NEAR}%", [False, True], False,
    hint="Tell lead and solo agents to pass an unfinished goal to the next provider near a usage limit.",
    names={False: "off", True: "on"})


def enabled():
    preferences.register(AUTO)
    return preferences.get(AUTO.key) is True


def command():
    return shutil.which("herdr-ai") or "herdr-ai"


def read_usage(names, refresh=True):
    """Cached meters for names, re-reading stale ones for up to REFRESH_WAIT seconds."""
    names = list(dict.fromkeys((agent(name) or {}).get("usage_provider", name) for name in names))
    try:
        usage = json.loads(ap.USAGE_CACHE.read_text(encoding="utf-8"))
        usage = usage if isinstance(usage, dict) else {}
    except (OSError, ValueError):
        usage = {}
    if not refresh:
        return usage
    now, readers = time.time(), []
    for name in names:
        entry = usage.get(name) or {}
        if name in ap.FETCHERS and now - float(entry.get("fetched", 0) or 0) > FRESH:
            reader = threading.Thread(target=ap._fetch, args=(name,), daemon=True)
            reader.start()
            readers.append(reader)
    deadline = time.monotonic() + REFRESH_WAIT
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    with ap.USAGE_LOCK:
        usage.update({k: v for k, v in ap.USAGE.items() if k in names})
    return usage


def agent(name):
    return next((a for a in ap.AGENTS if a["name"] == name), None)


def windows(entry, now):
    """The provider's live windows: [(label, percent used, resets)]."""
    found = []
    for w in (entry or {}).get("windows") or []:
        try:
            pct, resets = float(w["pct"]), float(w["resets"])
        except (KeyError, TypeError, ValueError):
            continue
        found.append((str(w.get("label", "?")), pct if resets > now else 0.0, resets))
    return found


def entry_for(name, usage):
    profile = agent(name) or {}
    entry = usage.get(profile.get("usage_provider", name))
    labels = profile.get("usage_labels")
    if entry and labels:
        return dict(entry, windows=[w for w in entry.get("windows", []) if w.get("label") in labels])
    return entry


def state(name, usage, now=None):
    """(used, resets) for the window that limits name first, or None if unknown.
    Cursor and Kiro use the pool their configured model draws from."""
    now = time.time() if now is None else now
    entry = entry_for(name, usage)
    if name in provider_priority.PROVIDERS:
        allowance = provider_priority.allowance(agent(name) or {"name": name}, entry, now)
        return None if allowance is None else (100 - allowance[0], allowance[1])
    found = windows(entry, now)
    if not found:
        return None
    _, pct, resets = max(found, key=lambda w: w[1])
    return pct, resets


def usable(name, usage, now=None):
    """True below NEAR, False at or over it, None when the meter is unknown."""
    found = state(name, usage, now)
    return None if found is None else found[0] < NEAR


def candidates(exclude=(), installed=None):
    """Providers in ORDER that are installed and not excluded; the Cursor|Kiro tier stays grouped."""
    installed = installed if installed is not None else {
        a["name"] for a in ap.AGENTS if ap.installed(a)}
    tiers = []
    for tier in ORDER:
        names = [n for n in tier.split("|") if n in installed and n not in exclude]
        if names:
            tiers.append(names)
    return tiers


def pick(tiers, usage, now=None):
    """The first provider with quota, by tier; within a tier the one resetting sooner.
    Known quota wins over an unknown meter. Returns (name, reason) or (None, reason)."""
    now = time.time() if now is None else now
    for wanted in (True, None):
        for names in tiers:
            fits = [n for n in names if usable(n, usage, now) is wanted]
            if not fits:
                continue
            name = min(fits, key=lambda n: (state(n, usage, now) or (0, float("inf")))[1])
            if wanted is None:
                return name, "usage unknown"
            used, resets = state(name, usage, now)
            return name, f"{used:.0f}% used, resets in {ap.until(resets)}"
    return None, f"every provider is at {NEAR}% or more of a limit"


def target(caller, usage=None, installed=None, now=None):
    """Who takes over caller's unfinished goal."""
    tiers = candidates({caller, GEMINI}, installed)
    usage = read_usage([n for t in tiers for n in t]) if usage is None else usage
    return pick(tiers, usage, now)


def orchestrator(usage=None, installed=None, now=None):
    """The lead for a team whose crew cannot lead: Codex (Sol), Claude (Opus), then Cursor or Kiro (Opus)."""
    tiers = candidates({GEMINI}, installed)
    usage = read_usage([n for t in tiers for n in t]) if usage is None else usage
    name, _ = pick(tiers, usage, now)
    return name or (tiers[0][0] if tiers else None)


def report(me=None, usage=None, now=None):
    """The text `herdr-ai --usage` prints."""
    now = time.time() if now is None else now
    names = [a["name"] for a in ap.AGENTS if ap.installed(a) and a.get("usage_provider", a["name"]) in ap.FETCHERS]
    usage = read_usage(names) if usage is None else usage
    width = max([len(n) for n in names] + [8])
    on = enabled()
    lines = ["Usage by provider (percent used, time to reset). " +
             (f"Hand off at {NEAR}%." if on else "Handoff is off (herdr-ai --settings).")]
    for name in names:
        found = windows(entry_for(name, usage), now)
        meters = "   ".join(f"{label} {pct:3.0f}% {ap.until(resets)}" for label, pct, resets in found) or "unknown"
        fit = usable(name, usage, now)
        flag = {True: "ok", False: "AT LIMIT", None: "?"}[fit]
        if name == GEMINI:
            flag += " (Gemini Flash: trivial crew work only)"
        mark = "*" if name == me else " "
        lines.append(f"{mark} {name:<{width}}  {meters:<34}  {flag}")
    if me == "Star":
        lines.append("You (*) are Star: each turn is routed to a provider below the limit. This pane stays.")
    elif me:
        mine = usable(me, usage, now)
        nxt, why = target(me, usage, now=now)
        verdict = {True: "below the limit", False: "AT THE LIMIT" + (": hand off now" if on else ""),
                   None: "unknown"}[mine]
        lines.append(f"You (*) are {me}: {verdict}." +
                     (f" Next provider: {nxt or 'none'} ({why})." if on else ""))
    return "\n".join(lines)


def rules(name, role, usage_pane=None):
    """The usage paragraph for an agent's instructions. role: lead, crew, or solo.
    Empty for a lead or solo agent while handoff is off; crew still stop near a limit."""
    if role != "crew" and not enabled():
        return ""
    cmd = command()
    pane = (f" The usage pane {usage_pane}, at the bottom right of this tab, shows the same meters as bars;"
            f" read it with `herdr pane read {usage_pane} --source visible`.") if usage_pane else ""
    check = (f"Usage limits: you run on {name}'s subscription, which has rolling usage limits. "
             f"Run `{cmd} --usage` to see how much of each provider is used and when it resets.{pane} "
             f"Check it before starting a long task and after each major step.")
    if role == "crew":
        return (check + f" If {name} is at {NEAR}% or more of any limit, stop at a safe point and say so at the top "
                f"of your reply; the lead decides who continues. Never run {cmd} --handoff yourself.")
    return (check + f" When {name} reaches {NEAR}% of any limit and the user's goal is not finished, hand off "
            f"instead of running into the limit: stop starting new work, write handoff notes to a file (the goal, "
            f"what is done, what is left, files touched, how to verify, open questions), run "
            f"`{cmd} --handoff \"<the goal in one line>\" <notes file>`, then end your turn with one line naming "
            f"who takes over. herdr-ai picks the next provider with quota (Codex, then Claude, then whichever of "
            f"Cursor and Kiro resets sooner), restarts it in your pane once your turn ends, and starts it with "
            f"/goal. Do not hand off a finished goal. If it reports that no provider has quota, tell the user "
            f"and stop.")
