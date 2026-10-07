"""The usage screen: every coding agent's plan limits, live, made to sit in a
Herdr pane all day. Opened from the agent picker with u; the same command runs
as both agent and usage.

Limits are read with the picker's readers into the picker's own cache, so both
screens always agree. Refreshing follows what the agents are doing: Herdr is
asked every couple of seconds which panes are working, a provider with a working
agent is re-read often, and a provider is re-read a few seconds after one of its
agents finishes a turn (that is when the numbers move). Idle providers are read
rarely, so the usage endpoints are never hammered.

Each bar is lit up to what's been used. The track behind it is brighter up to
where an even pace through the window would be by now, and any part of the bar
past that point is drawn as is. A limit turns a muted red only when it is 70%
used with its reset still far off (see concerning()).

Keys: t / → next theme, T / ← previous, r refresh now, a toggle animation,
s settings, u or Esc back to the agents, q quit.
"""

import json
import math
import os
import subprocess
import threading
import time

import agent_picker as ap
import preferences

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
WARN = ap.WARN
CONCERN = (196, 118, 122)     # muted pastel red: a limit worth a glance, nothing louder
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def theme_index(name):
    return next((i for i, th in enumerate(THEMES) if th["name"] == name), 0)


def _legacy(name, default):
    """A choice saved by the standalone usage command, kept as the default."""
    try:
        return (STATE / name).read_text().strip()
    except Exception:
        return default


THEME = preferences.register(preferences.Setting(
    "usage.theme", "Usage panel", "Theme", [th["name"] for th in THEMES], _legacy("theme", "brand"),
    hint="Colors for the usage panel. t and T switch it there too."))
ANIMATE = preferences.register(preferences.Setting(
    "usage.animate", "Usage panel", "Animation", [True, False], _legacy("animate", "1") == "1",
    hint="Off redraws once a second and uses almost no CPU. a toggles it there too.",
    names={True: "on", False: "off"}))


# ---- live data --------------------------------------------------------------

LOCK = ap.USAGE_LOCK  # the picker's own cache, so both screens show the same numbers
USAGE = ap.USAGE     # provider -> {"windows": [...], "fetched": epoch}
INFLIGHT = ap.PENDING  # providers being read right now
PRESENT = {}         # provider -> its panes beside this pane: the cards drawn
WORKING = {}         # provider -> those panes that are working (drawn as working)
BUSY = {}            # provider -> its panes working anywhere (paces the reads)
SCOPE = "this tab"   # where PRESENT looks: this Herdr tab, this tmux window, or this computer
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
    """The profile a Herdr agent id or a process name belongs to (by its "detect" names)."""
    a = (agent or "").lower()
    for ext in (".exe", ".cmd", ".js", ".mjs", ".ps1"):
        if a.endswith(ext):
            a = a[:-len(ext)]
    if not a:
        return None
    for profile in ap.AGENTS:
        for key in profile["detect"]:
            if a == key or a.startswith(key + "-"):
                return profile["name"]
    return None


def from_argv(argv):
    """The profile a command line runs, looking past node/bun/python launchers."""
    for token in argv[:3]:
        name = provider_of(os.path.basename(token))
        if name:
            return name
    return None


def processes():
    """[(pid, ppid, argv)] for this user's processes; empty where ps doesn't exist."""
    out = subprocess.run(["ps", "-xo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5)
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2].split()))
    return rows


def agents_under(roots, procs):
    """Profiles running at or below the given pids, one per agent (not per subprocess)."""
    children = {}
    for pid, ppid, argv in procs:
        children.setdefault(ppid, []).append((pid, argv))
    found = {}
    stack = [(r, None) for r in roots]
    by_pid = {pid: argv for pid, _, argv in procs}
    while stack:
        pid, parent_agent = stack.pop()
        name = from_argv(by_pid.get(pid, []))
        if name and name != parent_agent:
            found[name] = found.get(name, 0) + 1
        for child, _ in children.get(pid, []):
            stack.append((child, name or parent_agent))
    return found


_pane_cache = {}  # Herdr pane id -> (read at, profile from its foreground process)


def pane_agent(pane_id):
    """For panes Herdr doesn't recognize (OpenCode, Aider, ...): read the foreground process."""
    read_at, name = _pane_cache.get(pane_id, (0, None))
    if time.time() - read_at < 6:
        return name
    try:
        out = subprocess.run([HERDR, "pane", "process-info", "--pane", pane_id],
                             capture_output=True, text=True, timeout=5)
        info = json.loads(out.stdout)["result"]["process_info"]
        name = next((n for n in (from_argv(p.get("argv") or [p.get("name", "")])
                                 for p in info.get("foreground_processes", [])) if n), None)
    except Exception:
        name = None
    _pane_cache[pane_id] = (time.time(), name)
    return name


def herdr_sessions(me):
    """(agents in this pane's tab, working pane ids per profile anywhere)."""
    out = subprocess.run([HERDR, "pane", "list"], capture_output=True, text=True, timeout=5)
    panes = json.loads(out.stdout)["result"]["panes"]
    tab = next((p.get("tab_id") for p in panes if p.get("pane_id") == me), os.environ.get("HERDR_TAB_ID"))
    here, working = {}, {}
    for pane in panes:
        in_tab = pane.get("tab_id") == tab and pane.get("pane_id") != me
        name = provider_of(pane.get("agent"))
        if not name and in_tab:
            name = pane_agent(pane["pane_id"])
        if not name:
            continue
        busy = pane.get("agent_status") == "working"
        if busy:
            working.setdefault(name, set()).add(pane["pane_id"])
        if in_tab:
            slot = here.setdefault(name, {"panes": 0, "working": 0})
            slot["panes"] += 1
            slot["working"] += busy
    return here, working


def tmux_sessions():
    """Agents in this tmux window (tmux can't say which are working)."""
    out = subprocess.run(["tmux", "list-panes", "-F", "#{pane_pid}"], capture_output=True, text=True, timeout=5)
    roots = [int(p) for p in out.stdout.split() if p.isdigit()]
    return {name: {"panes": n, "working": 0} for name, n in agents_under(roots, processes()).items()}


def local_sessions():
    """Without a multiplexer: agents running anywhere on this computer."""
    procs = processes()
    roots = [pid for pid, ppid, _ in procs if ppid not in {p for p, _, _ in procs}]
    return {name: {"panes": n, "working": 0} for name, n in agents_under(roots, procs).items()}


def watch_sessions(stop):
    """Every two seconds: which agents run beside this pane (drawn) and which are
    working anywhere (read more often). A pane leaving "working" queues a re-read."""
    global HERDR_OK, SCOPE
    seen = {}  # provider -> set of working pane ids
    me = os.environ.get("HERDR_PANE_ID")
    while not stop.is_set():
        now_working, herdr = {}, False
        try:
            if me:
                here, now_working = herdr_sessions(me)
                scope, herdr = "this tab", True
            elif os.environ.get("TMUX"):
                here, scope = tmux_sessions(), "this window"
            else:
                here, scope = local_sessions(), "this computer"
        except Exception:
            here, scope = {}, SCOPE
        with LOCK:
            for name, before in seen.items():
                if before - now_working.get(name, set()):  # a turn just ended
                    FORCED[name] = min(FORCED.get(name, math.inf), time.time() + SETTLE)
            BUSY.clear()
            BUSY.update({k: len(v) for k, v in now_working.items()})
            PRESENT.clear()
            PRESENT.update({k: v["panes"] for k, v in here.items()})
            WORKING.clear()
            WORKING.update({k: v["working"] for k, v in here.items() if v["working"]})
            HERDR_OK, SCOPE = herdr, scope
        seen = now_working
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
    base = working if BUSY.get(name) else idle if HERDR_OK else unknown
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
        self.installed = agents   # every listed profile, in picker order
        self.toast_until = 0
        self.agents = self.present()  # the ones beside this pane; refreshed each frame

    def present(self):
        with LOCK:
            return [a for a in self.installed if a["name"] in PRESENT]

    @property
    def theme(self):
        return theme_index(preferences.get(THEME.key))

    @property
    def anim(self):
        return preferences.get(ANIMATE.key)

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


# A limit is flagged once it is 70% used while its reset is still further away
# than this; a monthly limit that resets within five days is never flagged.
CONCERN_PCT = 70
CONCERN_LEFT = {"5h": 2 * 3600, "wk": 2 * 86400}   # anything else is monthly: 5 days


def concerning(w, now):
    if w["pct"] < CONCERN_PCT or not w.get("resets"):
        return False
    return w["resets"] - now > CONCERN_LEFT.get(w["label"], 5 * 86400)


def pct_color(w, st, now):
    return CONCERN if concerning(w, now) else mix(st["color"], WHITE, 0.6)


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


def put_name(line, agent, t, bold=True):
    name = agent["name"]
    if agent.get("pastel") == "rainbow":
        for k, ch in enumerate(name):
            line.put(ch, ap.rainbow_rgb(k / 11 - t * 0.24, 0.2), bold=bold)
    else:
        line.put(name, agent.get("pastel") or agent["color"], bold=bold)


def draw_bar(line, w, bar, st, t, live, stale, now, offset, pulses=1):
    """pulses: how many bright waves run along the bar, one per working instance."""
    pct = min(100.0, w["pct"])
    concern = concerning(w, now)
    frac, _ = pace(w, now)
    filled = round(pct / 100 * bar)
    if pct >= 0.5 and filled == 0:
        filled = 1
    ahead = round(frac * bar) if frac is not None else 0
    for x in range(bar):
        if x < filled:
            c = ap.grad(st, x / max(1, bar) * st.get("bar_span", 0.5) + offset - t * 0.08)
            if concern:
                c = mix(c, CONCERN, 0.45)
            if live:
                glint = max(wave(x + k * 12.0 / pulses, t, 12.0, 7.0) for k in range(pulses)) ** 6
                c = mix(c, WHITE, 0.5 * glint)
            if stale:
                c = mix(c, SOFT, 0.55)
            line.put("▰", c)
        else:
            line.put("▱", AHEAD if x < ahead else TRACK)


CARDS = {}  # still cards, keyed by everything they show; moving ones are drawn every frame


def card(view, i, cw, t, now, rows=3):
    """The agent and its status, then a bar per limit, centered in `rows` lines
    (three at the least; a working card's glow fills all of them).

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
    key = (i, name, cw, rows, view.theme, view.anim, int(now), entry and entry["fetched"], fetching,
           int(t * 3) % 3 if fetching else 0, working, len(HISTORY.get(name) or ()), step)
    if CARDS.get(i, (None,))[0] != key:
        CARDS[i] = (key, _card(view, i, cw, step, now, entry, fetching, working, dots=key[9], rows=rows))
    return CARDS[i][1]


def _card(view, i, cw, t, now, entry, fetching, working, dots=None, rows=3):
    agent = view.agents[i]
    name, st = agent["name"], view.style(i)
    live = working > 0
    dots = int(t * 3) % 3 if dots is None else dots
    stale = bool(entry) and now - entry["fetched"] > 20 * 60 and not fetching

    head = Line().put(" ")
    if live:
        head.put(SPIN[int(t * 6) % len(SPIN)], agent["color"], bold=True)
    else:
        head.put("●", agent["color"])
    head.put(" ")
    put_name(head, agent, t)
    shimmer = None
    if live:
        status, scol = (f"{working} working" if cw >= 30 else f"{working}×"), TEXT
        if working > 1:
            shimmer = len(status)
            if cw >= 34:   # one spinner per instance, out of step with each other
                status = "".join(SPIN[(int(t * 10) + k * 3) % len(SPIN)] for k in range(min(working, 4))) + " " + status
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
    if shimmer:
        # several instances: the agent's gradient flows through the text with a
        # bright glint sweeping across it, and the count pulses
        lead = len(status) - shimmer
        for k, ch in enumerate(status):
            hue = ap.grad(st, k / len(status) * 0.7 - t * 0.45)
            glow = wave(k, t, 9.0, 8.0) ** 4
            if k < lead:
                color = mix(hue, WHITE, 0.25)
            elif ch.isdigit():
                color = mix(hue, WHITE, 0.55 + 0.45 * wave(0, t, speed=6.0))
            else:
                color = mix(mix(hue, WHITE, 0.35), WHITE, 0.6 * glow)
            head.at(right + k, ch, color, bold=ch.isdigit())
    else:
        head.at(right, status, scol)
    lines = [head.pad(cw)]

    if not entry:
        if name not in ap.FETCHERS:
            msg = "no usage reader for this CLI yet"
        else:
            msg = ("reading limits" + "·" * (1 + dots)) if fetching else f"no data · is {name} signed in?"
        lines.append(Line().put("   ").put(clip(msg, cw - 4), SOFT).pad(cw))
    else:
        for k, w in enumerate(entry["windows"][:2]):
            w = dict(w)
            if w.get("resets") and w["resets"] < now:
                w["pct"] = 0  # rolled over since this was read
            pct = w["pct"]
            line = Line().put("   ").put(w["label"].ljust(2), LABEL).put(" ")
            tail = f"↻ {until(w.get('resets'))}" if w.get("resets") else ""
            tail_w = 12 if cw - 6 - 5 - 12 >= 6 else 0
            bar = min(40, cw - 6 - 5 - tail_w - 1)
            if bar >= 3:
                draw_bar(line, w, bar, st, t if view.anim else 0, live and view.anim, stale, now, i * 0.13,
                         pulses=max(1, min(working, 4)))
            pcol = pct_color(w, st, now)
            line.put(f" {pct:3.0f}%", mix(pcol, SOFT, 0.5) if stale else pcol, bold=True)
            if tail_w and tail:
                line.put("  " + tail, CONCERN if concerning(w, now) else SOFT)
            lines.append(line.pad(cw))
    while len(lines) < 3:
        lines.append(Line().pad(cw))
    if rows >= 7:
        lines.insert(1, Line().pad(cw))   # room to breathe between the name and its bars
    top = max(0, (rows - len(lines)) // 2)
    lines = [Line().pad(cw) for _ in range(top)] + lines
    lines += [Line().pad(cw) for _ in range(rows - len(lines))]
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
    line.put(dot, agent["color"]).put(" ")
    name_w = min(11, max(4, width // 4))
    start = len(line.cells)
    put_name(line, agent, t)
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
            line.put(f"{w['pct']:4.0f}%", pct_color(w, st, now), bold=True)
            line.pad(max(len(line.cells), seg_start + share))
    else:
        line.put(" ··", SOFT)
    content = line.pad(width).paint()
    if border_space:
        if working:
            ring = ap.border_ring(agent if brand else st, t, width)
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


def working_card(view, i, width, t, now, lids=True, active=True, height=None):
    """Reserve the selector's border space, drawing it only while working.
    height: total lines including lids; the card grows to fill it."""
    inner = 3 if height is None else max(3, height - (2 if lids else 0))
    content = card(view, i, width - 2, t, now, rows=inner)
    agent = view.agents[i]
    style = agent if THEMES[view.theme]["stops"] is None else view.style(i)
    tick = t if view.anim else 0.4
    ring = ap.border_ring(style, tick, width - 2, len(content)) if active else None
    lines = [(ring[(y + 1, 0)] + RESET if active else " ") + line
             + (ring[(y + 1, width - 1)] + RESET if active else " ")
             for y, line in enumerate(content)]
    if lids:
        lines.insert(0, "".join(ring[(0, c)] for c in range(width)) + RESET if active else " " * width)
        lines.append("".join(ring[(len(content) + 1, c)] for c in range(width)) + RESET
                     if active else " " * width)
    return lines


def empty_state(width, height):
    """Shown until an agent is open beside this pane."""
    title = f"no agents in {SCOPE}"
    lines = [(title, TEXT, True), ("", None, False)]
    hint = "open one with agent and its limits show up here"
    if height >= 4:
        lines.append((hint, SOFT, False))
    out = []
    for text, color, bold in lines:
        text = clip(text, width)
        left = (width - len(text)) // 2
        out.append(" " * left + (ap.fg(color) + (BOLD if bold else "") + text + RESET if text else "")
                   + " " * (width - left - len(text)))
    return out


def render(view, cols, rows, t):
    """The frame's rows. A framed body row is (left edge, content, right edge), so
    the redraw can rewrite the animated border without resending the content."""
    now = time.time()
    view.agents = view.present()   # one consistent list for the whole frame
    n = len(view.agents)
    framed = cols >= 26 and rows >= max(n, 4) + 2
    if not n and not framed:
        return [clip(f"no agents in {SCOPE}", cols)]
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
    hints = next((h for h in (" t theme · r refresh · s settings · u agents · q quit ",
                              " t theme · s settings · u agents · q ", " t theme · s · u · q ", "")
                  if len(h) + max(13, len(status)) + 6 <= cols), "")
    overlay(bottom, 2, status, TEXT if working else SOFT)
    if hints:
        overlay(bottom, cols - 2 - len(hints), hints, SOFT)

    inner_w, inner_h = cols - 4, rows - 2
    body = []
    # Three ways to lay the cards out, best first:
    #  uniform  every card reserves border space, so nothing moves when an
    #           agent starts or stops working;
    #  fitted   only working cards get a border (5 rows, idle cards 3), for
    #           panes a little too short for uniform;
    #  bare     no lids at all, for very short panes.
    fitted = [5 if live else 3 for live in active]
    arrangement = plan(n, inner_w, inner_h, [5] * n)
    mode = "uniform"
    if arrangement is None and any(active):
        arrangement, mode = plan(n, inner_w, inner_h, fitted), "fitted"
    if arrangement is None:
        arrangement, mode = plan(n, inner_w, inner_h), "bare"
    lids = mode != "bare"
    if arrangement:
        ncols, cw, nr, gap = arrangement
        # Cards tile the whole panel: bordered cards take every column and their
        # share of the height, so neighbouring working cards' borders touch.
        gap_x = 0 if lids else GAP_X
        if lids:
            cw = inner_w // ncols
        grid_w = ncols * cw + (ncols - 1) * gap_x
        left = (inner_w - grid_w) // 2
        gap = 0 if lids else min(gap, 1)
        need = [max(fitted[r * ncols:(r + 1) * ncols]) if mode == "fitted" else 0 for r in range(nr)]
        spare = inner_h - (nr - 1) * gap - sum(need)
        # Every row of cards gets the same extra height; rows that don't divide
        # evenly go above and below the cards rather than into one card.
        slots = [need[r] + spare // nr for r in range(nr)]
        cards = [working_card(view, i, cw, t, now, lids and (mode == "uniform" or active[i]), active[i],
                              height=slots[i // ncols])
                 for i in range(n)]
        for r in range(nr):
            if r:
                body += [""] * gap
            row_cards = cards[r * ncols:(r + 1) * ncols]
            for y in range(max(len(c) for c in row_cards)):
                segs = [c[y] if y < len(c) else " " * cw for c in row_cards]
                text = (" " * gap_x).join(segs)
                body.append(" " * left + text + " " * (inner_w - left - len(row_cards) * cw
                                                        - (len(row_cards) - 1) * gap_x))
        pad_top = max(0, (inner_h - len(body)) // 2)
    elif n:
        body = [compact_row(view, i, inner_w, t, now) for i in range(n)]
        pad_top = max(0, (inner_h - len(body)) // 2)
    else:
        body = empty_state(inner_w, inner_h)
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


# ---- screen -----------------------------------------------------------------

_stop = None


def start(providers):
    """Start the live readers once; they keep the cache fresh for both screens."""
    global _stop
    if _stop:
        return
    _stop = threading.Event()
    merge_cache()
    load_history()
    now = time.time()
    with LOCK:
        for name in providers:
            entry = USAGE.get(name)
            if entry and now - entry["fetched"] < 60:
                LAST_TRY[name] = entry["fetched"]  # just read; no need to read it again yet
    threading.Thread(target=watch_sessions, args=(_stop,), daemon=True).start()
    threading.Thread(target=schedule, args=(providers, _stop), daemon=True).start()


def frame_time(view):
    return ap.FRAME_TIME if view.anim else 1.0   # once a second keeps the countdowns current


def handle(view, key, providers):
    """Apply a key on the usage screen. Returns "agents", "settings", "quit", or None."""
    if key in ("t", "right", "l", "T", "left", "h"):
        preferences.cycle(THEME.key, -1 if key in ("T", "left", "h") else 1)
        view.toast_until = time.time() + 2.5
    elif key == "r":
        force_all(providers)
    elif key == "a":
        preferences.cycle(ANIMATE.key)
    elif key == "s":
        return "settings"
    elif key in ("u", "U", "esc", "\x7f", "\b"):
        return "agents"
    elif key in ("q", "\x03", "\x04"):
        return "quit"
    return None
