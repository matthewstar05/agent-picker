"""Star's meter: one estimated limit for StarCLI, built from the providers it balances.

StarCLI has no quota of its own; it spends the providers'. Its limit is their
remaining quota weighted by how much work each can do: capacity (how much a
full quota buys, star.json) times average quality across task kinds. A provider
at the router's hard stop counts as empty, since StarCLI will not route there.

Two figures come out of it. "now" is what StarCLI can spend right away, from
each provider's tightest window; "long" leaves out the 5-hour windows, so it
shows what the weekly and monthly plans still hold.

The bar is one gold-to-sky gradient lit for the combined figure. The row under
it lists when each provider's binding limit resets, soonest first, each after a
dot in that provider's color.
"""

import math
import time

import star_router

STAR = "Star"
GRADIENT = [(255, 206, 96), (255, 234, 168), (196, 230, 255), (255, 220, 128)]   # gold to pale sky
COLOR = (255, 212, 110)
TRACK = (72, 72, 80)
_config = None


def config():
    global _config
    if _config is None:
        try:
            _config = star_router.load_config()
        except (OSError, ValueError, KeyError):
            _config = {}
    return _config


def weight(spec):
    quality = list(spec.get("quality", {}).values()) or [0.8]
    return float(spec.get("capacity", 1.0)) * sum(quality) / len(quality)


def _left(found, pools, now):
    """(percent left, binding window) of a set of windows: the tightest one, or for
    separate pools (Cursor's Auto and API) their average. A window past the
    router's limit counts as empty."""
    lefts = [((0.0 if pct >= star_router.limit_for(resets, now) else 100 - pct), label, resets)
             for label, pct, resets, _ in found]
    tightest = min(lefts, key=lambda w: w[0])
    value = sum(w[0] for w in lefts) / len(lefts) if pools else tightest[0]
    return value, tightest


def summary(usage, installed, agents, now=None):
    """Star's estimated limit and its parts, or None when no provider has a reading.

    usage: the agent-picker usage cache. installed: provider names on this
    computer. agents: profiles by name (for colors)."""
    now = time.time() if now is None else now
    parts = []
    for name, spec in config().items():
        if name not in installed or name not in agents:
            continue
        found = star_router.windows(usage.get(name), now)
        routed_pools = {tier.get("pool") for tier in spec.get("tiers", {}).values() if tier.get("pool")}
        if routed_pools and found:
            found = [window for window in found if window[0] in routed_pools]
        if not found:
            continue
        pools = bool(spec.get("pools"))
        left_now, binding = _left(found, pools, now)
        longer = [w for w in found if w[0] != "5h"] or found
        left_long, _ = _left(longer, pools, now)
        parts.append({"name": name, "agent": agents[name], "weight": weight(spec),
                      "now": left_now, "long": left_long, "label": binding[1], "resets": binding[2]})
    if not parts:
        return None
    total = sum(p["weight"] for p in parts)
    parts.sort(key=lambda p: (p["resets"] or float("inf"), -p["weight"]))
    soonest = next((p for p in parts if p["resets"]), None)
    return {"now": sum(p["weight"] * p["now"] for p in parts) / total,
            "long": sum(p["weight"] * p["long"] for p in parts) / total,
            "parts": parts, "soonest": soonest}


def _mix(a, b, f):
    return tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3))


def _grad(agent, u):
    stops = agent.get("bar_gradient") or agent.get("gradient") or [agent.get("color", COLOR)]
    if stops == "rainbow" or not isinstance(stops, list):
        stops = [agent.get("color", COLOR)]
    u %= 1.0
    pos = u * len(stops)
    i = int(pos)
    return _mix(tuple(stops[i % len(stops)]), tuple(stops[(i + 1) % len(stops)]), pos - i)


def bar(info, cells, depleting=True, t=0.0, driving=(), live=False, glyphs=("▰", "▱"), style=None):
    """[(glyph, rgb)]: Star's estimated limit as one bar in its gold-to-sky
    gradient (or a usage-panel theme's), lit for what is left, or for what is
    used when bars fill. A glint runs along it while StarCLI is driving a turn."""
    style = style or {"gradient": GRADIENT, "color": COLOR}
    frac = info["now"] / 100 if depleting else 1 - info["now"] / 100
    lit = round(frac * cells)
    if frac > 0.005 and lit == 0 and cells:
        lit = 1
    out = []
    for x in range(cells):
        if x < lit:
            c = _grad(style, x / max(1, cells) * 0.75 - t * 0.08)
            if live and driving:
                c = _mix(c, (255, 255, 255), 0.5 * (0.5 + 0.5 * math.sin(x * 2 * math.pi / 12 - t * 7)) ** 6)
            out.append((glyphs[0], c))
        else:
            out.append((glyphs[1], TRACK))
    return out, None


def countdown(epoch, now=None, short=False):
    """1h 01m, 5d 22h, 20d; short keeps the first unit: 1h, 5d."""
    now = time.time() if now is None else now
    if not epoch:
        return "?"
    s = epoch - now
    if s <= 0:
        return "now"
    m = int(s // 60)
    if m < 60:
        return f"{max(1, m)}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h" if short else f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d" if short or d >= 10 else f"{d}d {h}h"


def resets_row(info, width, sizes=None, now=None):
    """[(text, rgb)] cells: when each provider's binding limit resets, soonest
    first, each after a dot in its color. Names and full countdowns when they
    fit ('● Codex 52m  ● Claude 1d 14h'), else '●52m ●1d 14h', else '●52m ●1d'."""
    parts = info["parts"]
    for named, short in ((True, False), (True, True), (False, False), (False, True)):
        pieces = []
        for part in parts:
            label = (SHORT.get(part["name"], part["name"]) + " " if named else "") + countdown(part["resets"], now, short)
            pieces.append([("●", tuple(part["agent"].get("color", COLOR)))] + ([(" ", None)] if named else [])
                          + [(ch, _pastel(part["agent"])) for ch in label])
        gap = [(" ", None)] * (2 if named else 1)
        total = sum(len(p) for p in pieces) + len(gap) * (len(pieces) - 1)
        if total <= width:
            break
    out = []
    for piece in pieces:
        if len(out) + len(piece) + (len(gap) if out else 0) > width:
            break
        out += (gap if out else []) + piece
    return out


SHORT = {"Antigravity": "Gemini"}


def _pastel(agent):
    pastel = agent.get("pastel")
    color = tuple(pastel) if isinstance(pastel, list) else tuple(agent.get("color", COLOR))
    return _mix(color, (235, 235, 240), 0.25)


def pct_color(u=0.0):
    return _grad({"gradient": GRADIENT}, u)
