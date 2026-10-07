#!/usr/bin/env python3
"""agent: pick a coding agent with the arrow keys and start a new session.

Each entry launches its CLI on the frontier model with permission prompts
skipped. Extra arguments are passed through to the chosen CLI.
Keys: up/down or j/k move, 1-5 jump, Enter launches, q or Esc quits.
Draws on the alternate screen and fits itself to the terminal on every frame.
Each row shows the agent's plan limits (5-hour, weekly, or monthly) and when they reset.
"""

import base64
import colorsys
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from platform_runtime import Terminal, launch, resolve_argv, state_dir
from settings import ROOT, config_path, load_agents, update_checkout

try:
    AGENTS = load_agents()
except (OSError, ValueError) as exc:
    sys.exit(f"agent: invalid configuration: {exc}")

STATE = state_dir()

ESC = "\x1b"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"
DIM = f"{ESC}[2m"
MUTED = f"{ESC}[38;5;245m"
FAINT = f"{ESC}[38;5;247m"


def fg(rgb):
    return f"{ESC}[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def bg(rgb):
    return f"{ESC}[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def tint(rgb, amount):
    """Blend a brand color toward a dark background for the selection bar."""
    return (round(28 + (rgb[0] - 28) * amount),
            round(28 + (rgb[1] - 28) * amount),
            round(32 + (rgb[2] - 32) * amount))


def _write_json(path, data):
    """Atomic rewrite, so a CLI reading the file never sees half of it."""
    tmp = path.with_name(f".{path.name}.agent-picker.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def trust_claude(dirs):
    path = Path.home() / ".claude.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    projects = data.setdefault("projects", {})
    if all(projects.get(d, {}).get("hasTrustDialogAccepted") for d in dirs):
        return
    for d in dirs:
        projects.setdefault(d, {})["hasTrustDialogAccepted"] = True
    _write_json(path, data)


def trust_codex(dirs):
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    text = path.read_text() if path.exists() else ""
    for d in dirs:
        esc = d.replace("\\", "\\\\").replace('"', '\\"')
        header = f'[projects."{esc}"]'
        if header not in text.splitlines():
            text += f'\n{header}\ntrust_level = "trusted"\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def trust_antigravity(dirs):
    path = Path.home() / ".gemini/antigravity-cli/settings.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    trusted = data.setdefault("trustedWorkspaces", [])
    missing = [d for d in dirs if d not in trusted]
    if missing:
        trusted.extend(missing)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, data)


# Each CLI asks "do you trust this folder?" the first time it opens a
# directory. Record the answer the way the CLI itself would, so it starts
# straight into the session. Cursor takes --trust; Kiro has no folder prompt.
PRETRUST = {"Claude": trust_claude, "Codex": trust_codex, "Antigravity": trust_antigravity}


def pretrust(agent):
    trust = PRETRUST.get(agent["name"])
    if not trust:
        return
    cwd = os.getcwd()
    dirs = list(dict.fromkeys([cwd, os.path.realpath(cwd)]))
    try:
        trust(dirs)
    except Exception as exc:
        print(f"  {FAINT}couldn't pre-trust this folder for {agent['name']}: {exc}{RESET}")


def installed(agent):
    return resolve_argv(agent) is not None


# ---- usage limits -----------------------------------------------------------
# Each agent's plan limits, read from the same place the CLI itself reads them.
# Fetched in the background when the menu opens; the last result is cached so
# numbers appear instantly. Login tokens are only read, never refreshed, so the
# picker can't sign a CLI out; an expired token just keeps the cached numbers.

USAGE_CACHE = STATE / "usage.json"
USAGE_LOCK = threading.Lock()
USAGE = {}          # agent name -> {"windows": [...], "fetched": epoch}
PENDING = set()     # agent names still being fetched


def _keychain(service):
    if sys.platform != "darwin":
        return None
    out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                         capture_output=True, text=True, timeout=5)
    return out.stdout.strip() if out.returncode == 0 else None


def _http_json(url, headers, body=None):
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.load(resp)


def _epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def usage_claude():
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    raw = _keychain("Claude Code-credentials") if not os.environ.get("CLAUDE_CONFIG_DIR") else None
    credential_file = config / ".credentials.json"
    if not raw and credential_file.exists():
        raw = credential_file.read_text(encoding="utf-8")
    creds = json.loads(raw or "{}").get("claudeAiOauth", {})
    if not creds.get("accessToken") or creds.get("expiresAt", 0) / 1000 < time.time():
        return None
    d = _http_json("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": f"Bearer {creds['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code",
    })
    return [{"label": label, "pct": d[key]["utilization"], "resets": _epoch(d[key]["resets_at"])}
            for label, key in (("5h", "five_hour"), ("wk", "seven_day")) if d.get(key)]


def usage_codex():
    # Codex records its rate limits in every session log; read the newest one.
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    logs = sorted(root.glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    for log in logs[:20]:
        with open(log, "rb") as handle:
            handle.seek(max(0, log.stat().st_size - 400_000))
            tail = handle.read().decode(errors="ignore")
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                limits = json.loads(line)["payload"]["rate_limits"]
            except Exception:
                continue
            if not limits or limits.get("limit_id") not in (None, "codex"):
                continue
            windows = []
            for key in ("primary", "secondary"):
                w = limits.get(key)
                if w:
                    label = "5h" if w["window_minutes"] <= 300 else "wk"
                    windows.append({"label": label, "pct": w["used_percent"], "resets": w["resets_at"]})
            if windows:
                return windows
    return None


def usage_cursor():
    token = _keychain("cursor-access-token")
    if not token:
        return None
    d = _http_json("https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
                   {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1"}, b"{}")
    plan = d.get("planUsage", {})
    # The monthly plan, split the way Cursor's own /usage splits it: "cr" is
    # Auto (Cursor's models), "ex" is API (external models picked by name).
    resets = int(d["billingCycleEnd"]) / 1000
    windows = [{"label": label, "pct": float(plan[key]), "resets": resets}
               for label, key in (("cr", "autoPercentUsed"), ("ex", "apiPercentUsed")) if key in plan]
    return windows or None


def usage_kiro():
    # Kiro's /usage slash command answers without sending a prompt to a model.
    agent = next(a for a in AGENTS if a["name"] == "Kiro")
    argv = resolve_argv(agent)
    if not argv or Path(argv[0]).stem.lower() not in ("kiro-cli", "kiro"):
        return None  # custom wrappers have no verified usage protocol
    out = subprocess.run([argv[0], "chat", "--no-interactive", "/usage"], capture_output=True,
                         text=True, timeout=30, stdin=subprocess.DEVNULL, start_new_session=True,
                         cwd=tempfile.gettempdir())
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.stdout + out.stderr)
    pct = re.search(r"\),\s*([\d.]+)%", text) or re.search(r"([\d.]+)%", text)
    date = re.search(r"resets on (\d{4}-\d{2}-\d{2})", text)
    if not pct:
        return None
    resets = datetime.strptime(date.group(1), "%Y-%m-%d").timestamp() if date else None
    return [{"label": "mo", "pct": float(pct.group(1)), "resets": resets}]


def usage_antigravity():
    raw = _keychain("gemini")
    if not raw:
        return None
    if raw.startswith("go-keyring-base64:"):
        raw = base64.b64decode(raw.split(":", 1)[1]).decode()
    token = json.loads(raw)["token"]
    if _epoch(token["expiry"]) < time.time():
        return None
    project_file = Path.home() / ".gemini/antigravity-cli/cache/default_project_id.txt"
    project = project_file.read_text().strip() if project_file.exists() else ""
    d = _http_json("https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                   {"Authorization": f"Bearer {token['access_token']}", "Content-Type": "application/json",
                    "User-Agent": "antigravity"},
                   json.dumps({"project": project} if project else {}).encode())
    for group in d.get("groups", []):
        if "gemini" not in group.get("displayName", "").lower():
            continue
        windows = []
        for bucket in group.get("buckets", []):
            label = {"5h": "5h", "weekly": "wk"}.get(bucket.get("window"))
            if label:
                windows.append({"label": label, "pct": 100 * (1 - bucket.get("remainingFraction", 1)),
                                "resets": _epoch(bucket["resetTime"])})
        return sorted(windows, key=lambda w: w["label"] != "5h") or None
    return None


FETCHERS = {"Claude": usage_claude, "Codex": usage_codex, "Cursor": usage_cursor,
            "Kiro": usage_kiro, "Antigravity": usage_antigravity}


def load_usage():
    try:
        USAGE.update(json.loads(USAGE_CACHE.read_text()))
    except Exception:
        pass


def _fetch(name):
    try:
        windows = FETCHERS[name]()
    except Exception:
        windows = None
    with USAGE_LOCK:
        PENDING.discard(name)
        if windows:
            USAGE[name] = {"windows": windows, "fetched": time.time()}
            try:
                STATE.mkdir(parents=True, exist_ok=True)
                tmp = USAGE_CACHE.with_suffix(".tmp")
                tmp.write_text(json.dumps(USAGE))
                os.replace(tmp, USAGE_CACHE)
            except Exception:
                pass


def refresh_usage(available):
    load_usage()
    for agent, ok in zip(AGENTS, available):
        if ok and agent["name"] in FETCHERS:
            PENDING.add(agent["name"])
            threading.Thread(target=_fetch, args=(agent["name"],), daemon=True).start()


def until(epoch):
    """Compact countdown: 42m, 5h 12m, 6d 4h, 18d."""
    if not epoch:
        return ""
    s = epoch - time.time()
    if s <= 0:
        return "now"
    m = int(s // 60)
    if m < 60:
        return f"{max(1, m)}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if d < 10 else f"{d}d"


# ---- drawing ----------------------------------------------------------------

BLOCKS = "▁▂▃▄▅▆▇█"
FRAME = (88, 88, 96)
MAX_IW = 68        # widest the panel gets (inside the outer frame)
FRAME_TIME = 1 / 60
OK, WARN, HOT = (120, 205, 145), (235, 185, 85), (240, 105, 95)
TRACK = (72, 72, 80)
LABEL = (150, 150, 160)
SOFT = (125, 125, 135)


def mix(a, b, amount):
    return (round(a[0] + (b[0] - a[0]) * amount),
            round(a[1] + (b[1] - a[1]) * amount),
            round(a[2] + (b[2] - a[2]) * amount))


def fade(amount):
    """Ease color changes with zero slope and curvature at each palette stop."""
    return amount * amount * amount * (amount * (amount * 6 - 15) + 10)


def rainbow_rgb(u, saturation=0.62):
    pos = (u % 1.0) * 6
    sector = int(pos)
    hue = (sector + fade(pos - sector)) / 6
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, saturation, 1.0))


def grad(agent, u):
    """The agent's highlight gradient at u (wraps around, so it can loop)."""
    stops = agent.get("gradient") or [agent["color"]]
    u %= 1.0
    if stops == "rainbow":
        return rainbow_rgb(u)
    pos = u * len(stops)
    i = int(pos)
    a, b = stops[i % len(stops)], stops[(i + 1) % len(stops)]
    f = pos - i
    f = fade(f)
    return mix(a, b, f)


def wave(pos, t, length=9.0, speed=5.0):
    """0..1 brightness of a wave rolling along pos over time t."""
    return 0.5 + 0.5 * math.sin(pos * 2 * math.pi / length - t * speed)


def clip(text, width, least=1):
    """Fit text to width with an ellipsis; give up (empty) below `least` chars."""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…" if width - 1 >= least else ""


def heat(pct):
    return OK if pct < 50 else WARN if pct < 80 else HOT


class Line:
    """One row of cells (glyph, color, bold), so a row can be lit per cell."""

    def __init__(self):
        self.cells = []

    def put(self, text, color=None, bold=False):
        self.cells += [(ch, color, bold) for ch in text]
        return self

    def pad(self, width):
        self.cells += [(" ", None, False)] * max(0, width - len(self.cells))
        del self.cells[width:]
        return self

    def at(self, col, text, color, bold=False):
        self.pad(max(len(self.cells), col + len(text)))
        for i, ch in enumerate(text):
            self.cells[col + i] = (ch, color, bold)
        return self

    def paint(self, back=None):
        out = []
        for x, (ch, color, bold) in enumerate(self.cells):
            b = bg(back(x)) if back else ""
            out.append(f"{b}{fg(color or (0, 0, 0)) if color else ''}{BOLD if bold else ''}{ch}{RESET}")
        return "".join(out)


def usage_block(name, now_t, bar, reset):
    """Two lines of limit readouts for an agent: '5h ▰▰▱▱ 19%  ↻ 2h 31m'."""
    with USAGE_LOCK:
        entry = USAGE.get(name)
        pending = name in PENDING
    lines = []
    if not entry:
        dots = "·" * (1 + int(now_t * 3) % 3) if pending else "-"
        return [[(dots.ljust(3), SOFT, False)], []]
    stale = time.time() - entry.get("fetched", 0) > 3600 and not pending
    for w in entry["windows"][:2]:
        pct = w["pct"]
        if w.get("resets") and w["resets"] < time.time():
            pct = 0  # the window has rolled over since this was read
        color = mix(heat(pct), SOFT, 0.5) if stale else heat(pct)
        parts = [(w["label"] + " ", LABEL, False)]
        if bar and w["label"] != "mo":
            filled = round(min(100, pct) / 100 * bar)
            parts.append(("▰" * filled, color, False))
            parts.append(("▱" * (bar - filled) + " ", TRACK, False))
        elif bar:
            parts.append((" " * (bar + 1), None, False))
        parts.append((f"{pct:3.0f}%", color, True))
        if reset:
            parts.append((f"  ↻ {until(w.get('resets')):<7}", SOFT, False))
        lines.append(parts)
    while len(lines) < 2:
        lines.append([])
    return lines


def usage_width(bar, reset):
    return 3 + (bar + 1 if bar else 0) + 4 + (11 if reset else 0)


LEFT = 16           # agent column: name over model
DIVIDER = 3         # " │ " between the agent column and its limits

# Richest first. As space runs out the block wave goes first (it is pure
# decoration), then the subtitle (see render), then the bars, then the agent
# column narrows, then the reset countdowns.
LAYOUTS = ((LEFT, 8, True, True), (LEFT, 8, True, False), (LEFT, 0, True, False),
           (12, 0, True, False), (12, 0, False, False))


def layout(width, spark_ok):
    """Pick the richest arrangement that fits a row `width` cells wide."""
    for left, bar, reset, spark in LAYOUTS:
        need = 5 + left + DIVIDER + usage_width(bar, reset) + (11 if spark and spark_ok else 0) + 1
        if need <= width:
            return left, bar, reset, spark and spark_ok
    return width - 5, None, False, False


def agent_rows(agent, ok, width, t, selected):
    """An agent's two lines: name over model, a divider, then its limits.

    Every row uses the same layout (sized as if it had the block wave), so the
    columns line up; only the selected row draws the wave."""
    color = agent["color"]
    left, bar, reset, spark = layout(width, True)
    spark = spark and selected
    l1, l2 = Line(), Line()
    if selected:
        l1.put(" ❯ ", (255, 255, 255))
        l1.put("●", color).put(" ")  # brand dot: same color selected or not
        name_col, model_col = (245, 245, 245), (190, 190, 198)
        rule = mix(grad(agent, 0.5 - t * 0.15), (28, 28, 32), 0.35)
        model = agent["model"]
    else:
        dot = color if ok else SOFT
        l1.put("   ").put("●" if ok else "○", dot).put(" ")
        name_col, model_col, rule = ((225, 225, 230), SOFT, (64, 64, 72)) if ok else (SOFT, SOFT, (64, 64, 72))
        model = agent["short"] if ok else "not installed"
    name = clip(agent["name"], left)
    if selected and agent.get("pastel") == "rainbow":
        for i, ch in enumerate(name):
            l1.put(ch, rainbow_rgb(i / 11 - t * 0.24, 0.2), bold=True)
    elif selected:
        l1.put(name, agent.get("pastel", name_col), bold=True)
    else:
        l1.put(name, name_col)
    l2.put("     ").put(clip(model, left, least=4), model_col)
    if bar is not None:
        col = 5 + left
        for line in (l1, l2):
            line.at(col, " │ ", rule)
        if ok:
            for line, parts in zip((l1, l2), usage_block(agent["name"], t, bar, reset)):
                line.pad(col + DIVIDER)
                for text, c, b in parts:
                    line.put(text, c, b)
    if spark:
        for row, line in enumerate((l1, l2)):
            for i in range(10):
                block = BLOCKS[int(wave(i + row * 1.5, t, length=6.0, speed=9.0) * 7.99)]
                line.at(width - 11 + i, block, mix(grad(agent, i / 10 - t * 0.25), (255, 255, 255), 0.15))
    return [l1.pad(width), l2.pad(width)]


def lit(lines, agent, t, sweep=None):
    """Paint the selected rows: the agent's gradient flowing across them under a
    rolling glow, or the launch bars."""
    width = len(lines[0].cells)
    out = []
    for y, line in enumerate(lines):
        if sweep is not None:
            for x in range(width):
                if abs(x + 0.5 - width / 2) <= sweep * width / 2:
                    # launch: two bars of blocks grow outward from the center
                    shade = mix(grad(agent, x / width - t * 0.4), (255, 255, 255), 0.2 * wave(x, t, 8.0, 12.0))
                    line.cells[x] = ("█", shade, False)

        def back(x, y=y):
            hue = grad(agent, x / width * 0.9 - y * 0.04 - t * 0.12)
            strength = agent.get("glow", 1.0)
            return tint(hue, strength * (0.13 + 0.17 * wave(x - y * 2, t, 22.0, 4.0) ** 3))

        out.append(line.paint(back))
    return out


def border_ring(agent, t, bw, height=1, solid=None):
    """Box border glyphs in the agent's gradient, lit by a wave that travels
    around the perimeter."""
    w, h = bw + 2, height + 2
    ring = []  # (row, col, glyph) clockwise from the top-left corner
    ring += [(0, c, "╭" if c == 0 else "╮" if c == w - 1 else "─") for c in range(w)]
    ring += [(r, w - 1, "│") for r in range(1, h - 1)]
    ring += [(h - 1, c, "╯" if c == w - 1 else "╰" if c == 0 else "─") for c in range(w - 1, -1, -1)]
    ring += [(r, 0, "│") for r in range(h - 2, 0, -1)]
    cells = {}
    for i, (r, c, g) in enumerate(ring):
        if solid is not None:
            cells[(r, c)] = fg(solid) + g
            continue
        hue = grad(agent, i / len(ring) * 2 - t * 0.12)
        dim, bright = mix(hue, (24, 24, 28), 0.5), mix(hue, (255, 255, 255), 0.45)
        cells[(r, c)] = fg(mix(dim, bright, wave(i, t, length=16.0, speed=7.0) ** 2)) + g
    return cells


def render(selected, available, t, cols, rows, sweep=None):
    """Lines for the current terminal size, each at most cols wide."""
    color = AGENTS[selected]["color"]
    iw = min(MAX_IW, cols - 2)
    edge = fg(FRAME)

    if iw < 22 or rows < len(AGENTS) + 2:
        # Too small for the panel: a bare one-line list, still with the glowing row.
        lines = []
        for i, agent in enumerate(AGENTS):
            if cols < 18:
                mark = "❯ " if i == selected else "  "
                tone = fg(grad(agent, -t * 0.15)) if i == selected else FAINT
                lines.append(f"{tone}{clip(mark + agent['name'], cols)}{RESET}")
                continue
            first = agent_rows(agent, available[i], min(cols, MAX_IW), t, i == selected)[0]
            lines.append(lit([first], agent, t, sweep)[0] if i == selected else first.paint())
        first = min(max(0, selected - rows // 2), max(0, len(lines) - rows))
        return lines[first:first + max(1, rows)]

    bw = iw - 4

    def framed(content, visible):
        return f"{edge}│{RESET}{content}{' ' * max(0, iw - visible)}{edge}│{RESET}"

    star = fg(mix((225, 165, 40), (255, 222, 120), wave(0, t, speed=3.0))) + "✦" + RESET  # gold, glinting
    sub = "new session · permissions skipped"
    # the subtitle goes after the block wave and before the bars
    show_sub = iw >= 11 + len(sub) + 6 and layout(bw, True) in LAYOUTS[:2]
    fill = iw - 10 - (len(sub) + 2 if show_sub else 0)  # "╭─ ✦ agent " + "╮" is 12 wide
    top = f"{edge}╭─{RESET} {star} {BOLD}agent{RESET} {edge}{'─' * fill}{RESET}"
    top += (f" {FAINT}{sub}{RESET} " if show_sub else "") + f"{edge}╮{RESET}"

    # body entries: (kind, line). "lid" lines are dropped first when short on rows,
    # then "second" lines (each agent collapses to its first line).
    body = []
    for i, agent in enumerate(AGENTS):
        rows_i = agent_rows(agent, available[i], bw, t, i == selected)
        if i == selected:
            ring = border_ring(agent, t, bw, height=2)
            painted = lit(rows_i, agent, t, sweep)
            body.append(("lid", framed(" " + "".join(ring[(0, c)] for c in range(bw + 2)) + RESET, bw + 3)))
            for y, line in enumerate(painted):
                kind = "boxed" if y == 0 else "second"
                body.append((kind, framed(" " + ring[(y + 1, 0)] + RESET + line + ring[(y + 1, bw + 1)] + RESET,
                                          bw + 3)))
            body.append(("bare", framed("  " + painted[0], bw + 2)))  # first line without the box
            body.append(("lid", framed(" " + "".join(ring[(3, c)] for c in range(bw + 2)) + RESET, bw + 3)))
        else:
            for y, line in enumerate(rows_i):
                body.append(("first" if y == 0 else "second", framed("  " + line.paint(), bw + 2)))

    hints = [" ↑↓ move · ⏎ launch · q quit ", " ↑↓ · ⏎ launch · q quit ", " ⏎ launch · q quit ", " ⏎ launch ", ""]
    hint = next(h for h in hints if len(h) <= iw - 2)
    bottom = f"{edge}╰─{RESET}{FAINT}{hint}{RESET}{edge}{'─' * (iw - 1 - len(hint))}╯{RESET}"

    every = [line for kind, line in body if kind != "bare"]
    lines = [top, framed("", 0)] + every + [framed("", 0), bottom]
    if len(lines) > rows:   # drop the breathing room, then the box lids, then second lines
        lines = [top] + every + [bottom]
    if len(lines) > rows:
        lines = [top] + [line for kind, line in body if kind not in ("lid", "bare")] + [bottom]
    if len(lines) > rows:
        lines = [top] + [line for kind, line in body if kind in ("first", "bare")] + [bottom]
    return lines[:rows]


def load_last():
    try:
        name = (STATE / "last").read_text().strip()
        return next(i for i, a in enumerate(AGENTS) if a["name"] == name)
    except Exception:
        return 0


def save_last(index):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "last").write_text(AGENTS[index]["name"])
    except Exception:
        pass


def choose():
    available = [installed(a) for a in AGENTS]
    if not any(available):
        raise RuntimeError("No supported CLI found. Run agent --picker-doctor and configure a provider.")
    refresh_usage(available)
    selected = load_last()
    if not available[selected]:
        selected = next((i for i, ok in enumerate(available) if ok), 0)
    terminal = Terminal()
    out = sys.stdout
    start = time.monotonic()
    last_size = None
    last_frame = None

    def draw(sweep=None):
        # Full-screen frame on the alternate screen, sized to the terminal on
        # every frame, so resizing or a narrow pane can never wrap and smear.
        # Nothing is erased between frames: every cell is overwritten in place,
        # and the frame is one synchronized write, so high-refresh displays
        # never catch a half-drawn or blank frame.
        nonlocal last_size, last_frame
        cols, rows = shutil.get_terminal_size((80, 24))
        clear = ""
        if (cols, rows) != last_size:
            clear, last_size, last_frame = f"{ESC}[2J", (cols, rows), None
        lines = render(selected, available, time.monotonic() - start, cols, rows, sweep)
        width = max((visible_len(line) for line in lines), default=0)
        left = max(0, (cols - width) // 2)
        top = max(0, (rows - len(lines)) // 2)
        body = "".join(
            f"{ESC}[{top + i + 1};1H{' ' * left}{line}{' ' * max(0, cols - left - visible_len(line))}"
            for i, line in enumerate(lines)
        )
        if body == last_frame:
            return
        last_frame = body
        frame = f"{ESC}[?2026h{clear}{body}{ESC}[?2026l"
        terminal.write(frame)

    def move(step):
        nonlocal selected
        for _ in AGENTS:
            selected = (selected + step) % len(AGENTS)
            if available[selected]:
                return

    out.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[?7l")  # alt screen, hide cursor, no autowrap
    out.flush()  # frames go straight to the fd after this
    try:
        terminal.__enter__()
        draw()
        frame_at = time.monotonic()
        while True:
            # Frames on a fixed 60 fps clock, so drawing time doesn't slow the rate.
            frame_at += FRAME_TIME
            wait = frame_at - time.monotonic()
            if wait < 0:
                frame_at, wait = time.monotonic(), 0
            if not terminal.wait(wait):
                draw()  # next animation frame
                continue
            key = terminal.read_key()
            if key in ("up", "k"):
                move(-1)
            elif key in ("down", "j", "\t"):
                move(1)
            elif key.isdigit() and 1 <= int(key) <= len(AGENTS) and available[int(key) - 1]:
                selected = int(key) - 1
            elif key in ("\r", "\n"):
                launch = time.monotonic()
                while (elapsed := time.monotonic() - launch) < 0.21:
                    draw(sweep=min(1.0, (elapsed / 0.18) ** 1.6))
                    time.sleep(1 / 120)
                draw(sweep=1.0)
                return selected
            elif key in ("q", "esc", "\x03", "\x04"):
                return None
            draw()
    finally:
        if hasattr(terminal, "old") or hasattr(terminal, "modes"):
            terminal.__exit__()
        out.write(f"{ESC}[?7h{ESC}[?25h{ESC}[?1049l")
        out.flush()


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def visible_len(text):
    return len(_ANSI.sub("", text))


def doctor():
    print(f"Checkout: {ROOT}")
    print(f"Local config: {config_path()}")
    print(f"Local state: {STATE}")
    for agent in AGENTS:
        argv = resolve_argv(agent)
        print(f"{agent['name']}: {argv[0] if argv else 'not installed'}")
        print("  arguments: " + json.dumps(agent["argv"][1:]))
    print("Usage meters are best effort; unavailable credentials show '-'.")


def main():
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if args[:1] == ["--picker-update"]:
        update_checkout()
        return 0
    if args[:1] == ["--picker-doctor"]:
        doctor()
        return 0
    if args[:1] == ["--picker-help"]:
        print("agent [CLI arguments]                 open picker; forward arguments")
        print("agent --picker-launch NAME [args]     launch a provider directly")
        print("agent --picker-update                 fast-forward this Git checkout")
        print("agent --picker-doctor                 show paths and launch profiles")
        print("agent --picker-help                   show this help")
        print("Prefix CLI args with -- to escape picker management commands.")
        return 0
    if args[:1] == ["--picker-launch"]:
        if len(args) < 2:
            raise ValueError("--picker-launch needs a provider name")
        index = next((i for i, a in enumerate(AGENTS) if a["name"].lower() == args[1].lower()), None)
        if index is None:
            raise ValueError(f"Unknown provider: {args[1]}")
        args = args[2:]
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise RuntimeError("agent needs an interactive terminal; try --picker-doctor")
        try:
            index = choose()
        except KeyboardInterrupt:
            index = None
    if index is None:
        return 0
    agent = AGENTS[index]
    argv = resolve_argv(agent)
    if not argv:
        raise RuntimeError(f"{agent['name']} is not installed. Run --picker-doctor.")
    if args[:1] == ["--"]:
        args = args[1:]
    save_last(index)
    if os.environ.get("AGENT_PICKER_NO_PRETRUST") != "1":
        pretrust(agent)
    print(f"  {fg(agent['color'])}●{RESET} {BOLD}{agent['name']}{RESET} {FAINT}{agent['detail']}{RESET}\n", flush=True)
    return launch(argv + args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit(f"agent: {exc}")
