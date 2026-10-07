#!/usr/bin/env python3
"""usage: a live dashboard of every coding agent's plan limits, made to sit in a
Herdr pane all day.

Limits are read with agent-picker's fetchers, so both commands always agree and
share one cache. Refreshing follows what the agents are doing: Herdr is asked
every couple of seconds which panes are working, a provider with a working agent
is re-read often, and a provider is re-read a few seconds after one of its agents
finishes a turn (that is when the numbers move). Idle providers are read rarely,
so the usage endpoints are never hammered.

Each bar is lit up to what's been used. The track behind it is brighter up to
where an even pace through the window would be by now, and any part of the bar
past that point turns hot. "out 1h 20m" replaces the reset countdown when the
current pace would hit the limit before it resets.

Keys: t / → next theme, T / ← previous, r refresh now, a toggle animation, q quit.
Flags: --theme NAME (also saved), --themes (list them).
"""

import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time

import agent_picker as ap
from platform_runtime import Terminal

Line, mix, wave, clip, until = ap.Line, ap.mix, ap.wave, ap.clip, ap.until
ESC, RESET, BOLD, FAINT = ap.ESC, ap.RESET, ap.BOLD, ap.FAINT

STATE = ap.STATE / "usage-panel"
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"

# ---- themes -----------------------------------------------------------------
# "brand" draws each agent in its own colors, like agent-picker. Every other
# theme paints the whole panel in one gradient, spread across the cards.

THEMES = [
    {"name": "brand", "stops": None},
    {"name": "aurora", "stops": [(40, 230, 170), (60, 170, 255), (170, 110, 255)]},
    {"name": "sunset", "stops": [(255, 200, 90), (255, 120, 90), (230, 70, 150)]},
    {"name": "vapor", "stops": [(255, 110, 200), (160, 100, 255), (80, 220, 255)]},
    {"name": "ocean", "stops": [(30, 90, 200), (40, 180, 230), (120, 240, 230)]},
    {"name": "matrix", "stops": [(20, 120, 40), (60, 220, 90), (180, 255, 170)]},
    {"name": "ember", "stops": [(200, 40, 30), (255, 120, 30), (255, 210, 80)]},
    {"name": "sakura", "stops": [(255, 190, 215), (240, 130, 175), (255, 225, 238)]},
    {"name": "glacier", "stops": [(200, 235, 255), (120, 180, 255), (235, 245, 255)]},
    {"name": "mono", "stops": [(110, 110, 120), (200, 200, 210), (250, 250, 255)]},
    {"name": "rainbow", "stops": "rainbow"},
]
BRAND_FRAME = {"color": (190, 190, 198)}

WHITE, TEXT = (255, 255, 255), (232, 232, 238)
LABEL, SOFT, TRACK = ap.LABEL, ap.SOFT, ap.TRACK
AHEAD = (104, 104, 114)       # track up to where an even pace would be by now
WARN, HOT = ap.WARN, ap.HOT
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def load_pref(name, default):
    try:
        return (STATE / name).read_text().strip()
    except Exception:
        return default


def save_pref(name, value):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / name).write_text(str(value))
    except Exception:
        pass


def theme_index(name):
    return next((i for i, th in enumerate(THEMES) if th["name"] == name), 0)


# ---- live data --------------------------------------------------------------

LOCK = threading.Lock()
USAGE = {}           # provider -> {"windows": [...], "fetched": epoch}, shared with agent-picker
INFLIGHT = set()     # providers being read right now
WORKING = {}         # provider -> number of its Herdr panes that are working
HERDR_OK = False     # whether Herdr answered the last poll
FORCED = {}          # provider -> epoch it should be re-read at, regardless of its interval
LAST_TRY = {}        # provider -> epoch of the last read
FAILS = {}           # provider -> consecutive failed reads (backs the interval off)
HISTORY = {}         # provider -> [[epoch, pct of its first window], ...]
HISTORY_FILE = STATE / "history.json"

# Seconds between reads while one of its agents is working, when Herdr isn't
# available to say, and while all of its agents are idle. Codex is a local file
# read; Kiro starts a CLI; the rest are web endpoints that rate-limit polling.
INTERVALS = {
    "Claude": (60, 120, 300),
    "Codex": (5, 15, 60),
    "Cursor": (45, 120, 300),
    "Kiro": (120, 300, 900),
    "Antigravity": (60, 120, 300),
}
MIN_GAP = {"Claude": 20, "Codex": 2, "Cursor": 15, "Kiro": 60, "Antigravity": 20}
SETTLE = 4           # seconds after an agent finishes a turn before re-reading
WINDOW_LEN = {"5h": 5 * 3600, "wk": 7 * 86400}   # anything else is a month


def provider_of(agent):
    a = (agent or "").lower()
    for key, name in (("claude", "Claude"), ("codex", "Codex"), ("cursor", "Cursor"),
                      ("kiro", "Kiro"), ("agy", "Antigravity"), ("antigravity", "Antigravity"),
                      ("gemini", "Antigravity")):
        if key in a:
            return name
    return None


def watch_herdr(stop):
    """Poll Herdr for working panes; a pane leaving "working" queues a re-read."""
    global HERDR_OK
    seen = {}  # provider -> set of working pane ids
    while not stop.is_set():
        try:
            out = subprocess.run([HERDR, "pane", "list"], capture_output=True, text=True, timeout=5)
            panes = json.loads(out.stdout)["result"]["panes"]
            now_working = {}
            for pane in panes:
                name = provider_of(pane.get("agent"))
                if name and pane.get("agent_status") == "working":
                    now_working.setdefault(name, set()).add(pane["pane_id"])
            with LOCK:
                for name, before in seen.items():
                    if before - now_working.get(name, set()):  # a turn just ended
                        FORCED[name] = min(FORCED.get(name, math.inf), time.time() + SETTLE)
                WORKING.clear()
                WORKING.update({k: len(v) for k, v in now_working.items()})
                HERDR_OK = True
            seen = now_working
        except Exception:
            with LOCK:
                HERDR_OK = False
                WORKING.clear()
        stop.wait(2)


def merge_cache():
    """Adopt anything newer that agent-picker wrote, and write back what's newer here."""
    try:
        disk = json.loads(ap.USAGE_CACHE.read_text())
    except Exception:
        disk = {}
    changed = False
    with LOCK:
        for name, entry in disk.items():
            if entry.get("fetched", 0) > USAGE.get(name, {}).get("fetched", 0):
                USAGE[name] = entry
        for name, entry in USAGE.items():
            if entry.get("fetched", 0) > disk.get(name, {}).get("fetched", 0):
                disk[name], changed = entry, True
    if changed:
        try:
            ap.STATE.mkdir(parents=True, exist_ok=True)
            tmp = ap.USAGE_CACHE.with_name(f".usage.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(disk))
            os.replace(tmp, ap.USAGE_CACHE)
        except Exception:
            pass


def load_history():
    try:
        HISTORY.update(json.loads(HISTORY_FILE.read_text()))
    except Exception:
        pass


def record(name, windows):
    series = HISTORY.setdefault(name, [])
    series.append([time.time(), windows[0]["pct"]])
    cutoff = time.time() - 6 * 3600
    HISTORY[name] = [s for s in series if s[0] >= cutoff][-2000:]
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(HISTORY))
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


def fetch(name):
    try:
        windows = ap.FETCHERS[name]()
    except Exception:
        windows = None
    with LOCK:
        INFLIGHT.discard(name)
        if windows:
            USAGE[name] = {"windows": windows, "fetched": time.time()}
            FAILS[name] = 0
            record(name, windows)
        else:
            FAILS[name] = FAILS.get(name, 0) + 1
    if windows:
        merge_cache()


def interval(name):
    working, unknown, idle = INTERVALS[name]
    base = working if WORKING.get(name) else idle if HERDR_OK else unknown
    return min(900, base * 2 ** min(FAILS.get(name, 0), 4))


def schedule(providers, stop):
    """Start each provider's read once it's due; never two at once per provider."""
    tick = 0
    while not stop.is_set():
        now = time.time()
        with LOCK:
            for name in providers:
                if name in INFLIGHT:
                    continue
                last = LAST_TRY.get(name, 0)
                entry = USAGE.get(name)
                if entry and any(w.get("resets") and w["resets"] < now for w in entry["windows"]):
                    # a window rolled over: read it soon, once
                    FORCED[name] = min(FORCED.get(name, math.inf), max(now, entry["fetched"] + 30))
                due = last + interval(name)
                if name in FORCED:
                    due = min(due, max(FORCED[name], last + MIN_GAP[name]))
                if now >= due:
                    FORCED.pop(name, None)
                    LAST_TRY[name] = now
                    INFLIGHT.add(name)
                    threading.Thread(target=fetch, args=(name,), daemon=True).start()
        tick += 1
        if tick % 30 == 0:
            merge_cache()
        stop.wait(0.5)


def force_all(providers):
    with LOCK:
        for name in providers:
            FORCED[name] = 0
            LAST_TRY[name] = 0  # a manual refresh skips the minimum gap


# ---- drawing ----------------------------------------------------------------

class View:
    def __init__(self, agents):
        self.agents = agents
        self.theme = theme_index(load_pref("theme", "brand"))
        self.anim = load_pref("animate", "1") == "1"
        self.toast_until = 0

    def style(self, i):
        """The gradient a card is drawn in under the current theme."""
        stops = THEMES[self.theme]["stops"]
        agent = self.agents[i]
        if stops is None:
            # bars can use a lighter gradient than the picker's highlight
            return dict(agent, gradient=agent["bar_gradient"]) if agent.get("bar_gradient") else agent
        theme = {"gradient": stops}
        return {"gradient": stops, "glow": 1.0,
                "color": ap.grad(theme, i / max(1, len(self.agents)) * 0.85)}

    def frame_style(self):
        stops = THEMES[self.theme]["stops"]
        return BRAND_FRAME if stops is None else {"gradient": stops}


def window_len(label):
    return WINDOW_LEN.get(label, 30 * 86400)


def pace(w, now):
    """(fraction of the window elapsed, epoch the limit runs out at this pace or None)."""
    resets, pct = w.get("resets"), w["pct"]
    if not resets:
        return None, None
    length = window_len(w["label"])
    elapsed = now - (resets - length)
    frac = max(0.0, min(1.0, elapsed / length))
    out = None
    if pct > 0 and elapsed > length * 0.05:
        rate = pct / elapsed
        left = (100 - pct) / rate
        if now + left < resets:
            out = now + left
    return frac, out


def ago(epoch):
    s = int(time.time() - epoch)
    if s < 10:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


SPARK_FULL = 5.0   # percent of a limit used in one slice that draws a full block
SPARKS = {}  # (provider, slices) -> (computed at, rates); recomputed every few seconds


def spark(name, n, now):
    cached = SPARKS.get((name, n))
    if cached and now - cached[0] < 5:
        return cached[1]
    rates = _spark(name, n, now)
    SPARKS[(name, n)] = (now, rates)
    return rates


def _spark(name, n, now):
    """Burn rate over the last five hours: how much of the first limit each slice used."""
    series = list(HISTORY.get(name) or [])
    if len(series) < 2 or series[0][0] > now - 3600:
        return None  # under an hour of history is too little to show a trend
    start, step = now - 5 * 3600, 5 * 3600 / n

    def value_at(t):
        v = None
        for ts, pct in series:
            if ts > t:
                break
            v = pct
        return v

    deltas = []
    for k in range(n):
        a, b = value_at(start + k * step), value_at(start + (k + 1) * step)
        deltas.append(None if b is None else max(0.0, b - (a if a is not None else b)))
    # Fixed scale, so a 1% blip stays a small tick: a full block is 5% of the
    # limit used in one slice (about 25 minutes), or the busiest slice if more.
    scale = max([SPARK_FULL] + [d for d in deltas if d])
    return [None if d is None else d / scale for d in deltas]


def put_name(line, agent, st, brand, t, bold=True):
    name = agent["name"]
    if brand and agent.get("pastel") == "rainbow":
        for k, ch in enumerate(name):
            line.put(ch, ap.rainbow_rgb(k / 11 - t * 0.24, 0.2), bold=bold)
    elif brand:
        line.put(name, agent.get("pastel") or TEXT, bold=bold)
    else:
        line.put(name, mix(st["color"], WHITE, 0.55), bold=bold)


def draw_bar(line, w, bar, st, t, live, stale, now, offset):
    pct = min(100.0, w["pct"])
    frac, _ = pace(w, now)
    filled = round(pct / 100 * bar)
    if pct >= 0.5 and filled == 0:
        filled = 1
    ahead = round(frac * bar) if frac is not None else 0
    for x in range(bar):
        if x < filled:
            c = ap.grad(st, x / max(1, bar) * 0.5 + offset - t * 0.08)
            if x >= ahead and frac is not None:
                c = mix(c, HOT, 0.6)  # past an even pace
            if live:
                c = mix(c, WHITE, 0.5 * wave(x, t, 12.0, 7.0) ** 6)
            if stale:
                c = mix(c, SOFT, 0.55)
            line.put("▰", c)
        else:
            line.put("▱", AHEAD if x < ahead else TRACK)


CARDS = {}  # still cards, keyed by everything they show; moving ones are drawn every frame


def card(view, i, cw, t, now):
    """Three lines: the agent and its status, then a bar per limit.

    Only a card with a working agent moves. The rest only change once a second
    (their countdowns), so they are drawn once and reused, and the redraw skips
    their lines entirely."""
    name = view.agents[i]["name"]
    with LOCK:
        entry = USAGE.get(name)
        fetching = name in INFLIGHT
        working = WORKING.get(name, 0)
    # Working cards share the border's continuous 60 fps clock. Idle cards
    # still reuse their cached contents between countdown updates.
    step = t if working and view.anim else 0.0
    key = (i, cw, view.theme, view.anim, int(now), entry and entry["fetched"], fetching,
           int(t * 3) % 3 if fetching else 0, working, len(HISTORY.get(name) or ()), step)
    if CARDS.get(i, (None,))[0] != key:
        CARDS[i] = (key, _card(view, i, cw, step, now, entry, fetching, working, dots=key[7]))
    return CARDS[i][1]


def _card(view, i, cw, t, now, entry, fetching, working, dots=None):
    agent = view.agents[i]
    name, st = agent["name"], view.style(i)
    brand = THEMES[view.theme]["stops"] is None
    live = working > 0
    dots = int(t * 3) % 3 if dots is None else dots
    stale = bool(entry) and now - entry["fetched"] > 20 * 60 and not fetching

    head = Line().put(" ")
    if live:
        head.put(SPIN[int(t * 6) % len(SPIN)], agent["color"] if brand else st["color"], bold=True)
    else:
        head.put("●", agent["color"] if brand else st["color"])
    head.put(" ")
    put_name(head, agent, st, brand, t)
    if live:
        status, scol = (f"{working} working" if cw >= 30 else f"{working}×"), TEXT
    elif fetching:
        status, scol = "syncing" + "·" * (1 + dots), SOFT
    elif entry:
        status, scol = ago(entry["fetched"]), (WARN if stale else SOFT)
    else:
        status, scol = "", SOFT
    status = status.ljust(10) if fetching else status
    right = cw - 1 - len(status)
    name_end = len(head.cells)
    # Burn-rate sparkline, switched off: uncomment to draw it before the status.
    # end = right - 2
    # if cw >= 50 and entry:
    #     rates = spark(name, 12, now)
    #     if rates and end - name_end >= 12 + 2:
    #         end -= 12
    #         for k, r in enumerate(rates):
    #             glyph = "·" if r is None else ap.BLOCKS[int(r * 7.99)]
    #             color = TRACK if r is None or r == 0 else ap.grad(st, k / 12 * 0.5 + i * 0.13 - t * 0.05)
    #             head.at(end + k, glyph, color)
    head.at(right, status, scol)
    lines = [head.pad(cw)]

    if not entry:
        msg = ("reading limits" + "·" * (1 + dots)) if fetching else f"no data · is {name} signed in?"
        lines.append(Line().put("   ").put(clip(msg, cw - 4), SOFT).pad(cw))
    else:
        for k, w in enumerate(entry["windows"][:2]):
            w = dict(w)
            if w.get("resets") and w["resets"] < now:
                w["pct"] = 0  # rolled over since this was read
            pct = w["pct"]
            _, out = pace(w, now)
            line = Line().put("   ").put(w["label"].ljust(2), LABEL).put(" ")
            reset = f"↻ {until(w.get('resets'))}" if w.get("resets") else ""
            tail = f"out {until(out)}" if out else reset
            # Every row of a card gets the same bar, so the percentages line up;
            # a warning only changes the text after it.
            tail_w = 12 if cw - 6 - 5 - 12 >= 6 else 0
            bar = min(40, cw - 6 - 5 - tail_w - 1)
            if bar >= 3:
                draw_bar(line, w, bar, st, t if view.anim else 0, live and view.anim, stale, now, i * 0.13)
            pcol = HOT if pct >= 90 else WARN if pct >= 70 else mix(st["color"], WHITE, 0.6)
            line.put(f" {pct:3.0f}%", mix(pcol, SOFT, 0.5) if stale else pcol, bold=True)
            if tail_w and tail:
                line.put("  " + tail, HOT if out else SOFT)
                if out and reset and cw - len(line.cells) >= 3 + len(reset) + 1:
                    line.put(" · ", TRACK).put(reset, SOFT)  # room to keep the reset too
            lines.append(line.pad(cw))
    while len(lines) < 3:
        lines.append(Line().pad(cw))
    if live and view.anim:
        return ap.lit(lines, st, t)
    return [line.paint() for line in lines]


def compact_row(view, i, width, t, now):
    """One line per agent for short panes: ● Claude  5h ▰▰▱ 23%  wk ▰▱▱ 18%."""
    agent = view.agents[i]
    name, st = agent["name"], view.style(i)
    brand = THEMES[view.theme]["stops"] is None
    with LOCK:
        entry = USAGE.get(name)
        working = WORKING.get(name, 0)
    border_space = width >= 4
    if border_space:
        width -= 2
    t = t if view.anim else 0.4
    line = Line()
    dot = SPIN[int(t * 12) % len(SPIN)] if working else "●"
    line.put(dot, agent["color"] if brand else st["color"]).put(" ")
    name_w = min(11, max(4, width // 4))
    start = len(line.cells)
    put_name(line, agent, st, brand, t)
    del line.cells[start + name_w:]
    line.pad(start + name_w)
    if entry:
        windows = entry["windows"][:2]
        share = (width - len(line.cells)) // 2
        for w in windows:
            w = dict(w)
            if w.get("resets") and w["resets"] < now:
                w["pct"] = 0
            seg_start = len(line.cells)
            line.put(" " + w["label"].ljust(2) + " ", LABEL)
            bar = min(16, share - 9)
            if bar >= 2:
                draw_bar(line, w, bar, st, t if view.anim else 0, False, False, now, i * 0.13)
            pct = w["pct"]
            pcol = HOT if pct >= 90 else WARN if pct >= 70 else mix(st["color"], WHITE, 0.6)
            line.put(f"{pct:4.0f}%", pcol, bold=True)
            line.pad(max(len(line.cells), seg_start + share))
    else:
        line.put(" ··", SOFT)
    content = line.pad(width).paint()
    if border_space:
        if working:
            ring = ap.border_ring(agent, t, width)
            return ring[(1, 0)] + RESET + content + ring[(1, width + 1)] + RESET
        return " " + content + " "
    return content


GAP_X = 3
MIN_CW, MAX_CW = 24, 66


def plan(n, width, height, heights=None):
    """Arrange the cards in the fewest columns that fit: tall panes stack them,
    wide short panes put them side by side."""
    heights = heights or [3] * n
    for ncols in range(1, n + 1):
        cw = min(MAX_CW, (width - GAP_X * (ncols - 1)) // ncols)
        if cw < MIN_CW:
            return None
        nr = math.ceil(n / ncols)
        body_h = sum(max(heights[r * ncols:(r + 1) * ncols]) for r in range(nr))
        for gap in (2, 1, 0):
            if body_h + (nr - 1) * gap <= height - (2 if gap == 2 else 0):
                return ncols, cw, nr, gap
    return None


def overlay(cells, col, text, color, bold=False):
    for k, ch in enumerate(text):
        if 0 <= col + k < len(cells):
            cells[col + k] = f"{ap.fg(color)}{BOLD if bold else ''}{ch}{RESET}"


def working_card(view, i, width, t, now, lids=True, active=True):
    """Reserve the selector's border space, drawing it only while working."""
    content = card(view, i, width - 2, t, now)
    agent = view.agents[i]
    tick = t if view.anim else 0.4
    ring = ap.border_ring(agent, tick, width - 2, len(content)) if active else None
    lines = [(ring[(y + 1, 0)] + RESET if active else " ") + line
             + (ring[(y + 1, width - 1)] + RESET if active else " ")
             for y, line in enumerate(content)]
    if lids:
        lines.insert(0, "".join(ring[(0, c)] for c in range(width)) + RESET if active else " " * width)
        lines.append("".join(ring[(len(content) + 1, c)] for c in range(width)) + RESET
                     if active else " " * width)
    return lines


def render(view, cols, rows, t):
    """The frame's rows. A framed body row is (left edge, content, right edge), so
    the redraw can rewrite the animated border without resending the content."""
    now = time.time()
    n = len(view.agents)
    if not n:
        return [clip("usage: no agent CLIs installed", cols)]
    framed = cols >= 26 and rows >= n + 2
    if not framed:
        return [compact_row(view, i, cols, t, now) for i in range(n)][:rows]

    ring = ap.border_ring(view.frame_style(), t if view.anim else 0.4, cols - 2, rows - 2)
    top = [ring[(0, c)] for c in range(cols)]
    bottom = [ring[(rows - 1, c)] for c in range(cols)]

    # title, and the theme name (bright for a moment after switching)
    star = mix((225, 165, 40), (255, 222, 120), wave(0, t, speed=3.0)) if view.anim else (240, 195, 80)
    overlay(top, 2, " ", TEXT)
    overlay(top, 3, "✦", star)
    overlay(top, 4, " usage ", TEXT, bold=True)
    th = THEMES[view.theme]["name"]
    tag = f" ◂ {th} ▸ "
    if now < view.toast_until and cols >= 12 + len(tag) + 3:
        overlay(top, cols - 2 - len(tag), tag, TEXT, bold=True)

    with LOCK:
        active = [WORKING.get(a["name"], 0) > 0 for a in view.agents]
        working = sum(WORKING.values())
        herdr = HERDR_OK
    status = f" {working} working " if working else " idle " if herdr else " live "
    hints = next((h for h in (" t theme · r refresh · a anim · q quit ", " t theme · r refresh · q ",
                              " t theme · q ", "") if len(h) + max(13, len(status)) + 6 <= cols), "")
    overlay(bottom, 2, status, TEXT if working else SOFT)
    if hints:
        overlay(bottom, cols - 2 - len(hints), hints, SOFT)

    inner_w, inner_h = cols - 4, rows - 2
    body = []
    arrangement = plan(n, inner_w, inner_h, [5] * n)
    lids = arrangement is not None
    if arrangement is None:
        arrangement = plan(n, inner_w, inner_h)
    if arrangement:
        ncols, cw, nr, gap = arrangement
        grid_w = ncols * cw + (ncols - 1) * GAP_X
        left = (inner_w - grid_w) // 2
        cards = [working_card(view, i, cw, t, now, lids, active[i]) for i in range(n)]
        for r in range(nr):
            if r:
                body += [""] * gap
            row_cards = cards[r * ncols:(r + 1) * ncols]
            for y in range(max(len(c) for c in row_cards)):
                segs = [c[y] if y < len(c) else " " * cw for c in row_cards]
                text = (" " * GAP_X).join(segs)
                body.append(" " * left + text + " " * (inner_w - left - len(row_cards) * cw
                                                        - (len(row_cards) - 1) * GAP_X))
        pad_top = max(0, (inner_h - len(body)) // 2)
    else:
        body = [compact_row(view, i, inner_w, t, now) for i in range(n)]
        pad_top = max(0, (inner_h - len(body)) // 2)
    blank = " " * inner_w
    body = [blank] * pad_top + body
    body += [blank] * (inner_h - len(body))

    lines = ["".join(top) + RESET]
    for r, content in enumerate(body[:inner_h]):
        content = content or blank
        if not content.strip():
            content = blank
        lines.append((ring[(r + 1, 0)] + RESET, " " + content + " ", ring[(r + 1, cols - 1)] + RESET))
    lines.append("".join(bottom) + RESET)
    return lines


# ---- terminal ---------------------------------------------------------------

def run(view, providers):
    terminal = Terminal()
    start = time.monotonic()
    last_size, last_rows = None, []

    def draw():
        nonlocal last_size, last_rows
        cols, rows = shutil.get_terminal_size((80, 24))
        clear = ""
        if (cols, rows) != last_size:
            clear, last_size, last_rows = f"{ESC}[2J", (cols, rows), []
        lines = render(view, cols, rows, time.monotonic() - start)
        out = []
        for y, row in enumerate(lines):
            before = last_rows[y] if y < len(last_rows) else None
            if row == before:
                continue
            if isinstance(row, tuple):
                same = isinstance(before, tuple)
                if not same or row[0] != before[0]:
                    out.append(f"{ESC}[{y + 1};1H{row[0]}")
                if not same or row[1] != before[1]:
                    out.append(f"{ESC}[{y + 1};2H{row[1]}")
                if not same or row[2] != before[2]:
                    out.append(f"{ESC}[{y + 1};{cols}H{row[2]}")
            else:
                out.append(f"{ESC}[{y + 1};1H{row}{ESC}[K")
        last_rows = lines
        if out or clear:
            terminal.write(f"{ESC}[?2026h{clear}{''.join(out)}{ESC}[?2026l")

    def set_theme(i):
        view.theme = i % len(THEMES)
        view.toast_until = time.time() + 2.5
        save_pref("theme", THEMES[view.theme]["name"])

    sys.stdout.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[?7l")  # alt screen, hide cursor, no autowrap
    sys.stdout.flush()
    try:
        terminal.__enter__()
        frame_at = time.monotonic()
        while True:
            draw()
            # Fixed deadlines keep render time inside the 60 fps frame budget.
            step = ap.FRAME_TIME if view.anim else 1.0
            frame_at += step
            now = time.monotonic()
            if frame_at < now:
                frame_at = now
            if not terminal.wait(max(0, frame_at - now)):
                continue
            key = terminal.read_key()
            if key in ("t", "right", "l"):
                set_theme(view.theme + 1)
            elif key in ("T", "left", "h"):
                set_theme(view.theme - 1)
            elif key == "r":
                force_all(providers)
            elif key == "a":
                view.anim = not view.anim
                save_pref("animate", "1" if view.anim else "0")
            elif key in ("q", "esc", "\x03", "\x04"):
                return
    finally:
        if hasattr(terminal, "old") or hasattr(terminal, "modes"):
            terminal.__exit__()
        sys.stdout.write(f"{ESC}[?7h{ESC}[?25h{ESC}[?1049l")
        sys.stdout.flush()


def list_themes():
    current = load_pref("theme", "brand")
    for th in THEMES:
        style = BRAND_FRAME if th["stops"] is None else {"gradient": th["stops"]}
        swatch = "".join(f"{ap.fg(ap.grad(style, k / 16))}▰" for k in range(16))
        mark = "❯" if th["name"] == current else " "
        print(f" {mark} {swatch}{RESET}  {th['name']}")


def main():
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if "--themes" in args or "--list" in args:
        list_themes()
        return
    if "--theme" in args:
        try:
            name = args[args.index("--theme") + 1]
        except IndexError:
            sys.exit("usage: --theme needs a name (see --themes)")
        if name not in [th["name"] for th in THEMES]:
            sys.exit(f"usage: no theme named {name!r} (see --themes)")
        save_pref("theme", name)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("usage: needs an interactive terminal")

    agents = [a for a in ap.AGENTS if ap.installed(a) and a["name"] in ap.FETCHERS]
    providers = [a["name"] for a in agents]
    ap.load_usage()
    USAGE.update(ap.USAGE)
    load_history()
    stop = threading.Event()
    threading.Thread(target=watch_herdr, args=(stop,), daemon=True).start()
    threading.Thread(target=schedule, args=(providers, stop), daemon=True).start()
    try:
        run(View(agents), providers)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
