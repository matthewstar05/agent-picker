#!/usr/bin/env python3
"""Agent selector: pick a coding agent or edit its local launch defaults.

Extra arguments are passed through to the chosen CLI.
Keys: up/down or j/k move, 1-5 jump, Enter launches, u usage panel, s settings,
q or Esc quits. The usage command runs this same program.
Draws on the alternate screen and fits itself to the terminal on every frame.
Each row shows the agent's plan limits (5-hour, weekly, or monthly) and when they reset.
"""

import provider_defaults
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

if __name__ == "__main__":
    # The usage screen imports this module by name; run as a script, make that
    # import find this copy instead of loading a second one.
    sys.modules.setdefault("agent_picker", sys.modules[__name__])

import preferences
import provider_defaults
import usage_cache
from platform_runtime import Terminal, launch, resolve_argv, state_dir
from settings import ROOT, config_path, load_agents, update_checkout

try:
    AGENTS = load_agents()
except (OSError, ValueError) as exc:
    sys.exit(f"agent: invalid configuration: {exc}")

STATE = state_dir()

START = preferences.register(preferences.Setting(
    "start", "General", "Opens on", ["agents", "usage"], "agents",
    hint="The screen agent opens on; usage always opens the usage panel. u switches anytime.",
    names={"agents": "agent selector", "usage": "usage panel"}))
# Registered by usage_panel after its own settings, so the Star section lists after them.
STAR_RESETS = preferences.Setting(
    "star.resets", "Star", "Provider cooldowns", [False, True], False,
    hint="Under Star's meter, a dot for each provider it balances with when that limit resets.",
    names={False: "hidden", True: "shown"})


def star_resets():
    return preferences.get(STAR_RESETS.key) is True

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
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def trust_claude(dirs):
    path = Path.home() / ".claude.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    projects = data.setdefault("projects", {})
    if all(projects.get(d, {}).get("hasTrustDialogAccepted") for d in dirs):
        return
    for d in dirs:
        projects.setdefault(d, {})["hasTrustDialogAccepted"] = True
    _write_json(path, data)


def trust_codex(dirs):
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for d in dirs:
        esc = d.replace("\\", "\\\\").replace('"', '\\"')
        header = f'[projects."{esc}"]'
        if header not in text.splitlines():
            text += f'\n{header}\ntrust_level = "trusted"\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def trust_antigravity(dirs):
    path = Path.home() / ".gemini/antigravity-cli/settings.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
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


def shown_setting(agent):
    """The settings row that hides an installed CLI from the agent selector."""
    return preferences.register(preferences.Setting(
        "shown." + agent["name"], "Agents", agent["name"], [True, False], True,
        hint="Hide a CLI from the selector. --picker-launch and herdr-ai still run it.",
        names={True: "shown", False: "hidden"}))


def shown(agents):
    """The agents the selector lists: all but the hidden ones, or all if every one is hidden."""
    return [a for a in agents if preferences.get(shown_setting(a).key)] or list(agents)


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
    # Go's RFC3339 timestamps trim trailing zeros and can contain nanoseconds.
    # Python 3.9 only accepts three or six fractional-second digits.
    iso = re.sub(r"\.(\d+)(?=Z|[+-]|$)",
                 lambda m: "." + m.group(1)[:6].ljust(6, "0"), iso)
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
                         text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL, start_new_session=True,
                         cwd=tempfile.gettempdir())
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.stdout + out.stderr)
    pct = re.search(r"\),\s*([\d.]+)%", text) or re.search(r"([\d.]+)%", text)
    date = re.search(r"resets on (\d{4}-\d{2}-\d{2})", text)
    if not pct:
        return None
    credits = re.search(r"([\d.]+) of ([\d.]+) covered", text)
    resets = datetime.strptime(date.group(1), "%Y-%m-%d").timestamp() if date else None
    window = {"label": "mo", "pct": float(pct.group(1)), "resets": resets}
    if credits:
        window.update(used=float(credits.group(1)), limit=float(credits.group(2)))
    return [window]


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
    with USAGE_LOCK:
        USAGE.update(usage_cache.merge(USAGE, usage_cache.read(USAGE_CACHE)))


def numeric_windows(windows):
    """Drop limits a provider reported without a number (a null utilization)."""
    return [w for w in windows or [] if isinstance(w.get("pct"), (int, float))]


def _fetch(name):
    try:
        windows = numeric_windows(FETCHERS[name]())
    except Exception:
        windows = None
    with USAGE_LOCK:
        PENDING.discard(name)
        if windows:
            USAGE[name] = {"windows": windows, "fetched": time.time()}
            try:
                USAGE.update(usage_cache.write(USAGE_CACHE, USAGE))
            except Exception:
                pass


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


# One setting for the bars in the agent selector, the usage panel and herdr-ai.
# The usage panel registers it beside its theme; herdr-ai lists it in its own
# settings. All three read preferences.json, so a change in one shows in the rest.
BARS = preferences.Setting(
    "usage.bars", "Usage panel", "Bars", ["left", "used"], "left",
    hint="Depleting bars start full and empty as a limit is used; filling bars grow toward 100%.",
    names={"left": "depleting", "used": "filling"})


def depleting():
    return preferences.get(BARS.key) != "used"


def credits_text(w):
    """Kiro counts credits: '997.60/1000' left when depleting, '2.40/1000 used' when filling."""
    if not w.get("limit"):
        return None
    limit, used = w["limit"], min(w.get("used", 0), w["limit"])
    if w.get("resets") and w["resets"] < time.time():
        used = 0   # the window has rolled over since this was read
    return f"{limit - used:.2f}/{limit:.0f}" if depleting() else f"{used:.2f}/{limit:.0f} used"


def star_block(bar, reset):
    """StarCLI's two lines: its estimated limit as a bar split by provider (in
    reset order, each in its own colors), then when each provider resets."""
    import star_meter
    with USAGE_LOCK:
        usage = dict(USAGE)
    info = star_meter.summary(usage, {a["name"] for a in AGENTS if installed(a)}, {a["name"]: a for a in AGENTS})
    if not info:
        return [[("-  ", SOFT, False)], []]
    left = depleting()
    parts, sizes = [("∑  ", LABEL, False)], None
    if bar:
        cells, sizes = star_meter.bar(info, bar, left)
        parts += [(glyph, color, False) for glyph, color in cells] + [(" ", None, False)]
    shown = info["now"] if left else 100 - info["now"]
    parts.append((f"{shown:3.0f}%", star_meter.pct_color(), True))
    if reset and info["soonest"]:
        parts.append((f"  ↻ {until(info['soonest']['resets']):<7}", SOFT, False))
    if not star_resets():
        return [parts, []]
    room = usage_width(bar, reset) - 3
    second = [("   ", None, False)] + [(ch, color, False) for ch, color in star_meter.resets_row(info, room, sizes)]
    return [parts, second]


def usage_block(name, now_t, bar, reset):
    """Two lines of limit readouts for an agent: '5h ▰▰▱▱ 19%  ↻ 2h 31m'."""
    if name == "Star":
        return star_block(bar, reset)
    with USAGE_LOCK:
        entry = USAGE.get(name)
        pending = name in PENDING
    lines = []
    if not entry:
        dots = "·" * (1 + int(now_t * 3) % 3) if pending else "-"
        return [[(dots.ljust(3), SOFT, False)], []]
    stale = time.time() - entry.get("fetched", 0) > 3600 and not pending
    left = depleting()
    for w in entry["windows"][:2]:
        pct = w["pct"]
        if w.get("resets") and w["resets"] < time.time():
            pct = 0  # the window has rolled over since this was read
        color = mix(heat(pct), SOFT, 0.5) if stale else heat(pct)
        parts = [(w["label"] + " ", LABEL, False)]
        if bar and w["label"] != "mo":
            filled = round((100 - min(100, pct) if left else min(100, pct)) / 100 * bar)
            parts.append(("▰" * filled, color, False))
            parts.append(("▱" * (bar - filled) + " ", TRACK, False))
        elif bar:
            parts.append((" " * (bar + 1), None, False))
        parts.append((f"{max(0, 100 - pct) if left else pct:3.0f}%", color, True))
        if reset:
            parts.append((f"  ↻ {until(w.get('resets')):<7}", SOFT, False))
        lines.append(parts)
    credits = credits_text(entry["windows"][0]) if len(entry["windows"]) == 1 else None
    if credits:
        # a lone limit's spare line, ending under its percentage
        end = 3 + (bar + 1 if bar else 0) + 4
        lines.append([(credits.rjust(end), SOFT, False)])
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
    spark = spark and selected and not agent.get("aside")
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
        if ok and agent.get("aside"):
            # a row that is not one provider (herdr-ai's Standard) says what it is instead
            for line, text in zip((l1, l2), agent["aside"]):
                line.pad(col + DIVIDER).put(clip(text, max(1, width - col - DIVIDER - (12 if spark else 1))), SOFT)
        elif ok:
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


HINTS = [" ↑↓ move · → defaults · ⏎ launch · u usage · s settings · esc quit ",
         " → defaults · ⏎ launch · u usage · s settings · esc quit ",
         " → defaults · ⏎ launch · u · s · esc ", " → defaults · ⏎ launch ", ""]


def render(selected, available, t, cols, rows, sweep=None, agents=None, title="agent selector", hints=HINTS):
    """Lines for the current terminal size, each at most cols wide.

    agents: the profiles to list (the installed ones); every profile by default.
    title and hints let herdr-ai's setup menu reuse the same panel."""
    agents = AGENTS if agents is None else agents
    color = agents[selected]["color"]
    iw = min(MAX_IW, cols - 2)
    edge = fg(FRAME)

    if iw < 22 or rows < len(agents) + 2:
        # Too small for the panel: a bare one-line list, still with the glowing row.
        lines = []
        for i, agent in enumerate(agents):
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
    title = clip(title, iw - 6)
    top = f"{edge}╭─{RESET} {star} {BOLD}{title}{RESET} {edge}{'─' * max(0, iw - len(title) - 5)}╮{RESET}"

    # body entries: (kind, line). "lid" lines are dropped first when short on rows,
    # then "second" lines (each agent collapses to its first line).
    body = []
    for i, agent in enumerate(agents):
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

    hint = next((h for h in hints if len(h) <= iw - 2), "")
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


GOLD = {"color": (225, 165, 40), "gradient": [(225, 165, 40), (255, 222, 120), (255, 190, 90)]}


def panel(title, body, hints, cols, rows, t, plain):
    """The selector's frame around any screen: a glinting star title, framed rows,
    and the key hints set into the bottom border.

    body(bw) returns (kind, content, visible) rows. When rows run short, "gap"
    rows go first, then "lid" rows, then the rest scroll around "selected".
    plain is the fallback for panes too small for a frame: (text, active) pairs."""
    iw = min(MAX_IW, cols - 2)
    if iw < 26 or rows < 5:
        lines = [(BOLD if active else FAINT) + clip(text, cols) + RESET for text, active in plain]
        at = next((i for i, (_, active) in enumerate(plain) if active), 0)
        first = min(max(0, at - rows // 2), max(0, len(lines) - rows))
        return lines[first:first + max(0, rows)]
    edge = fg(FRAME)
    bw = iw - 4

    def framed(content, visible):
        return f"{edge}│{RESET}{content}{' ' * max(0, iw - visible)}{edge}│{RESET}"

    star = fg(mix((225, 165, 40), (255, 222, 120), wave(0, t, speed=3.0))) + "✦" + RESET
    title = clip(title, iw - 6)
    top = f"{edge}╭─{RESET} {star} {BOLD}{title}{RESET} {edge}{'─' * max(0, iw - len(title) - 5)}╮{RESET}"
    hint = next((h for h in hints if len(h) <= iw - 2), "")
    bottom = f"{edge}╰─{RESET}{FAINT}{hint}{RESET}{edge}{'─' * (iw - 1 - len(hint))}╯{RESET}"

    entries = [(kind, framed(content, visible)) for kind, content, visible in body(bw)]
    lines = [top, framed("", 0)] + [line for _, line in entries] + [framed("", 0), bottom]
    if len(lines) > rows:
        lines = [top] + [line for kind, line in entries if kind != "gap"] + [bottom]
    if len(lines) > rows:
        kept = [(kind, line) for kind, line in entries if kind not in ("gap", "lid")]
        at = next((i for i, (kind, _) in enumerate(kept) if kind == "selected"), 0)
        room = max(1, rows - 2)
        first = min(max(0, at - room // 2), max(0, len(kept) - room))
        lines = [top] + [line for _, line in kept[first:first + room]] + [bottom]
    return lines[:rows]


def ringed(agent, lines, t, bw):
    """Rows inside the agent's glowing ring and lit by its gradient, as panel rows."""
    ring = border_ring(agent, t, bw, height=len(lines))
    lid = lambda r: ("lid", " " + "".join(ring[(r, c)] for c in range(bw + 2)) + RESET, bw + 3)
    out = [lid(0)]
    for y, line in enumerate(lit(lines, agent, t)):
        out.append(("selected", " " + ring[(y + 1, 0)] + RESET + line + ring[(y + 1, bw + 1)] + RESET, bw + 3))
    return out + [lid(len(lines) + 1)]


def flat(line, bw, kind="row"):
    return (kind, "  " + line.pad(bw).paint(), bw + 2)


def section(text, bw):
    return ("gap", "  " + fg(LABEL) + clip(text.upper(), bw) + RESET, 2 + min(len(text), bw))


def note(text, bw, color=SOFT, kind="note"):
    shown = clip(text, bw - 1)
    return (kind, "   " + fg(color) + shown + RESET, 3 + len(shown))


def buttons(agent, labels, choice, t, bw):
    """A row of buttons; the chosen one sits in the agent's glowing ring."""
    total = sum(len(label) + 4 for label in labels) + 3 * (len(labels) - 1)
    if total > bw:
        out = []
        for i, label in enumerate(labels):
            text = ("❯ " if i == choice else "  ") + label
            out.append(("selected" if i == choice else "row",
                        "  " + (BOLD if i == choice else FAINT) + clip(text, bw) + RESET,
                        2 + min(len(text), bw)))
        return out
    left = (bw - total) // 2
    parts = [" " * left, " " * left, " " * left]
    for i, label in enumerate(labels):
        inner = len(label) + 2
        if i == choice:
            ring = border_ring(agent, t, inner, height=1)
            face = lit([Line().put(f" {label} ", (255, 255, 255), bold=True)], agent, t)[0]
            parts[0] += "".join(ring[(0, c)] for c in range(inner + 2)) + RESET
            parts[1] += ring[(1, 0)] + RESET + face + ring[(1, inner + 1)] + RESET
            parts[2] += "".join(ring[(2, c)] for c in range(inner + 2)) + RESET
        else:
            parts[0] += " " * (inner + 2)
            parts[1] += f"  {fg(SOFT)}{label}{RESET}  "
            parts[2] += " " * (inner + 2)
        if i < len(labels) - 1:
            parts = [p + "   " for p in parts]
    visible = 2 + left + total
    return [("lid", "  " + parts[0], visible), ("selected", "  " + parts[1], visible), ("lid", "  " + parts[2], visible)]


def pair(label, value, bw, label_color, value_color, mark="   ", mark_color=None, bold=False):
    """'   Label ........ value' with the value right-aligned, as a Line bw wide."""
    line = Line().put(mark, mark_color or label_color).put(clip(label, bw // 2), label_color, bold=bold)
    shown = clip(value, max(1, bw - len(line.cells) - 3))
    line.at(max(len(line.cells) + 2, bw - len(shown) - 1), shown, value_color, bold=bold)
    return line.pad(bw)


def render_defaults(menu, cols, rows, t):
    """A provider's defaults, or its scrollable model list while browsing."""
    agent = menu.agent
    name = agent["name"]
    accent = tuple(agent.get("pastel")) if isinstance(agent.get("pastel"), list) else agent["color"]
    if menu.browsing:
        return render_models(menu, cols, rows, t, accent)
    if menu.original is None:
        message = "Defaults are managed by this provider."
        return panel(f"{name} defaults", lambda bw: [note(message, bw)], [" esc back ", ""], cols, rows, t,
                     [(f"{name} defaults", True), (message, False)])
    fields = menu.fields()
    menu.index = min(menu.index, len(fields) - 1)
    field = fields[menu.index]
    hint = {"model": "⏎ browse models", "effort": "←→ cycle effort"}.get(field, "←→ toggle")

    def body(bw):
        out = [("row", "  " + fg(agent["color"]) + "●" + RESET + " " + fg(accent) + BOLD + clip(name, bw - 2) + RESET,
                4 + min(len(name), bw - 2)), ("gap", "", 0)]
        for i, key in enumerate(fields):
            value = menu.shown(key, menu.values[key])
            moved = menu.values[key] != menu.original[key]
            if i == menu.index:
                shown = f"{value}  ⏎" if key == "model" else f"◂ {value} ▸"
                line = pair(menu.LABELS[key], shown, bw, (245, 245, 245), (255, 255, 255), " ❯ ", (255, 255, 255), True)
                out += ringed(agent, [line], t, bw)
            else:
                line = pair(menu.LABELS[key], value, bw, (205, 205, 212), accent if moved else (190, 190, 198),
                            " • " if moved else "   ", agent["color"])
                out.append(flat(line, bw))
        out.append(("gap", "", 0))
        if field == "fast_mode":
            out.append(note("Faster replies use more quota.", bw, accent))
        if menu.error:
            out.append(note(menu.error, bw, HOT))
        elif menu.changed:
            out.append(note("● changed · esc to review and save", bw, accent))
        else:
            out.append(note("✓ saved locally", bw, OK))
        return out

    plain = [(f"{name} defaults", False)] + [
        (("❯ " if i == menu.index else "  ") + menu.LABELS[k] + ": " + menu.shown(k, menu.values[k]), i == menu.index)
        for i, k in enumerate(fields)] + [(menu.error or hint, False)]
    hints = [f" ↑↓ move · {hint} · esc back ", f" {hint} · esc back ", " esc back ", ""]
    return panel(f"{name} defaults", body, hints, cols, rows, t, plain)


def render_models(menu, cols, rows, t, accent):
    agent = menu.agent
    models = menu.models()
    index = min(menu.model_index, len(models) - 1)
    current = menu.values["model"]

    def body(bw):
        room = max(1, rows - 8)
        first = min(max(0, index - room // 2), max(0, len(models) - room))
        out = [note(f"{index + 1} of {len(models)}", bw, LABEL, "gap")]
        for i, row in enumerate(models[first:first + room], first):
            label = row.get("name", row["id"])
            tag = "current" if row["id"] == current else ""
            if i == index:
                line = pair(label, tag, bw, (245, 245, 245), (255, 255, 255), " ❯ ", (255, 255, 255), True)
                out += ringed(agent, [line], t, bw)
            else:
                mark = " ● " if row["id"] == current else "   "
                out.append(flat(pair(label, tag, bw, (205, 205, 212), accent, mark, agent["color"]), bw))
        return out

    plain = [(("❯ " if i == index else "  ") + row.get("name", row["id"]), i == index) for i, row in enumerate(models)]
    hints = [" ↑↓ move · ⏎ choose · esc back ", " ⏎ choose · esc back ", " esc back ", ""]
    return panel(f"{agent['name']} models", body, hints, cols, rows, t, plain)


def render_back_confirmation(menu, choice, cols, rows, t=0):
    """Choose whether to save or discard pending provider settings."""
    agent = menu.agent
    accent = tuple(agent.get("pastel")) if isinstance(agent.get("pastel"), list) else agent["color"]
    selection, delta = menu.review()

    def body(bw):
        out = [section("selected", bw)]
        for label, value in selection:
            out.append(flat(pair(label, value, bw, (205, 205, 212), accent, bold=True), bw))
        out += [("gap", "", 0), section("changes", bw)]
        for label, old, new in delta:
            line = Line().put("   ").put(clip(label, bw // 2), (205, 205, 212))
            change = clip(f"{old} → {new}", max(1, bw - len(line.cells) - 3))
            col = max(len(line.cells) + 2, bw - len(change) - 1)
            split = change.find(" → ")
            if split < 0:
                line.at(col, change, accent, bold=True)
            else:
                line.at(col, change[:split], SOFT).at(col + split, " → ", LABEL)
                line.at(col + split + 3, change[split + 3:], accent, bold=True)
            out.append(flat(line, bw))
        return out + [("gap", "", 0)] + buttons(agent, ("Save settings", "Discard changes"), choice, t, bw)

    plain = [(f"Save {agent['name']} settings?", False)]
    plain += [(f"{label}: {value}", False) for label, value in selection]
    plain += [(f"{label}: {old} → {new}", False) for label, old, new in delta]
    plain += [("❯ " + ("Save settings" if choice == 0 else "Discard changes"), True)]
    hints = [" ←→ choose · ⏎ confirm · esc edit ", " ⏎ confirm · esc edit ", ""]
    return panel(f"Save {agent['name']} settings?", body, hints, cols, rows, t, plain)


def render_settings(menu, cols, rows, t, accent):
    """The settings screen: every registered setting, grouped by section, or
    the page opened from it."""
    items = menu.items
    index = min(menu.index, len(items) - 1)
    page = getattr(menu, "page", None)
    title = "settings" + (f" › {page.label.lower()}" if page else "")
    iw = min(MAX_IW, cols - 2)
    if iw < 26 or rows < 5:
        lines = []
        for i, setting in enumerate(items):
            mark = "❯ " if i == index else "  "
            text = f"{mark}{setting.label}: {setting.show(preferences.get(setting.key))}"
            lines.append(f"{BOLD if i == index else FAINT}{clip(text, cols)}{RESET}")
        first = min(max(0, index - rows // 2), max(0, len(lines) - rows))
        return lines[first:first + max(1, rows)]

    edge = fg(FRAME)
    bw = iw - 4

    def framed(content, visible):
        return f"{edge}│{RESET}{content}{' ' * max(0, iw - visible)}{edge}│{RESET}"

    star = fg(mix((225, 165, 40), (255, 222, 120), wave(0, t, speed=3.0))) + "✦" + RESET
    title = clip(title, iw - 8)
    top = f"{edge}╭─{RESET} {star} {BOLD}{title}{RESET} {edge}{'─' * (iw - 5 - len(title))}╮{RESET}"

    body = []   # (kind, line); kinds let short terminals drop the extras first
    section = None
    for i, setting in enumerate(items):
        if setting.section != section:
            if section is not None:
                body.append(("gap", framed("", 0)))
            section = setting.section
            body.append(("head", framed("  " + fg(LABEL) + clip(section.upper(), bw) + RESET, 2 + min(len(section), bw))))
        value = setting.show(preferences.get(setting.key))
        line = Line()
        opens = isinstance(setting, preferences.Page)
        if i == index:
            line.put(" ❯ ", (255, 255, 255)).put(clip(setting.label, bw - 8), (245, 245, 245), bold=True)
            shown = f"{value} ▸ " if opens else f"◂ {value} ▸ "
            line.at(max(len(line.cells) + 2, bw - len(shown)), clip(shown, bw // 2), (255, 255, 255), bold=True)
            painted = lit([line.pad(bw)], accent, t)[0]
        else:
            line.put("   ").put(clip(setting.label, bw - 8), (205, 205, 212))
            shown = f"{value} ▸ " if opens else f"{value}   "
            line.at(max(len(line.cells) + 2, bw - len(shown)), clip(shown, bw // 2), SOFT)
            painted = line.pad(bw).paint()
        body.append(("row" if i != index else "selected", framed("  " + painted, bw + 2)))
    hint = items[index].hint if items else ""
    if hint:
        body.append(("gap", framed("", 0)))
        body.append(("hint", framed("  " + FAINT + clip(hint, bw) + RESET, 2 + min(len(hint), bw))))

    if isinstance(items[index] if items else None, preferences.Page):
        hints = [" ↑↓ move · ⏎ open · esc back ", " ⏎ open · esc back ", " esc back ", ""]
    elif page:
        hints = [" ↑↓ move · ⏎ on/off · ←→ change · esc back ", " ⏎ on/off · ←→ change · esc back ",
                 " ←→ change · esc back ", " esc back ", ""]
    else:
        hints = [" ↑↓ move · ←→ change · esc back ", " ←→ change · esc back ", " esc back ", ""]
    hint_text = next(h for h in hints if len(h) <= iw - 2)
    bottom = f"{edge}╰─{RESET}{FAINT}{hint_text}{RESET}{edge}{'─' * (iw - 1 - len(hint_text))}╯{RESET}"

    lines = [top, framed("", 0)] + [line for _, line in body] + [framed("", 0), bottom]
    if len(lines) > rows:
        lines = [top] + [line for kind, line in body if kind != "gap"] + [bottom]
    if len(lines) > rows:   # scroll the settings around the selected one
        kept = [(kind, line) for kind, line in body if kind in ("row", "selected")]
        at = next(i for i, (kind, _) in enumerate(kept) if kind == "selected")
        room = max(1, rows - 2)
        first = min(max(0, at - room // 2), max(0, len(kept) - room))
        lines = [top] + [line for _, line in kept[first:first + room]] + [bottom]
    return lines[:rows]


class Screen:
    """Writes frames to the terminal, resending only the rows that changed.

    A row is a string drawn from the first column, or (left edge, content,
    right edge) so an animated border can be redrawn without its content.
    Each frame is one synchronized write, so a frame is never seen half drawn."""

    def __init__(self, terminal):
        self.terminal = terminal
        self.size = None
        self.rows = []

    def reset(self):
        self.size = None

    def show(self, frame, cols, rows):
        clear = ""
        if (cols, rows) != self.size:
            clear, self.size, self.rows = f"{ESC}[2J", (cols, rows), []
        out = []
        for y, row in enumerate(frame):
            before = self.rows[y] if y < len(self.rows) else None
            if row == before:
                continue
            if isinstance(row, tuple):
                same = isinstance(before, tuple)
                for part, col in ((0, 1), (1, 2), (2, cols)):
                    if not same or row[part] != before[part]:
                        out.append(f"{ESC}[{y + 1};{col}H{row[part]}")
            else:
                # Erase first: with autowrap off, a trailing erase would wipe a
                # row's last cell when that row fills the width.
                out.append(f"{ESC}[{y + 1};1H{ESC}[2K{row}")
        # A shorter frame (a card closed in a compact pane) leaves rows behind.
        out += [f"{ESC}[{y + 1};1H{ESC}[2K" for y in range(len(frame), min(len(self.rows), rows))]
        self.rows = frame
        if out or clear:
            self.terminal.write(f"{ESC}[?2026h{clear}{''.join(out)}{ESC}[?2026l")


def centered(lines, cols, rows):
    width = max((visible_len(line) for line in lines), default=0)
    left = " " * max(0, (cols - width) // 2)
    top = max(0, (rows - len(lines)) // 2)
    frame = [""] * rows
    for i, line in enumerate(lines[:rows - top]):
        frame[top + i] = left + line
    return frame


def load_last(agents=None):
    try:
        name = (STATE / "last").read_text().strip()
        return next(i for i, a in enumerate(agents or AGENTS) if a["name"] == name)
    except Exception:
        return 0


def save_last(index):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "last").write_text(AGENTS[index]["name"])
    except Exception:
        pass


def choose(screen=None, only=None):
    """Run the screens until an agent is picked (its index) or the user quits (None).

    only: provider names to show alone (a herdr-ai team's usage pane)."""
    import usage_panel   # registers its settings; imported here so it can import this module

    # Only installed CLIs are listed; the rest of agents.json stays in the catalog
    # until its CLI shows up on this computer.
    found = [a for a in AGENTS if installed(a) and (only is None or a["name"] in only)]
    if not found:
        raise RuntimeError("No supported CLI found. Run agent --picker-doctor and configure a provider.")
    # Hidden CLIs (settings) leave the selector only; usage still follows them.
    listed = shown(found)
    available = [True] * len(listed)
    providers = [a["name"] for a in found if a["name"] in FETCHERS]
    load_usage()
    usage_panel.start(providers)   # live readers feed both screens
    usage = usage_panel.View(found, pinned=only is not None)
    menu = preferences.Menu()
    screen = screen or preferences.get(START.key)
    came_from = screen               # where the settings screen returns to
    selected = load_last(listed)
    defaults_menu = None
    quit_choice = 0
    terminal = Terminal()
    writer = Screen(terminal)
    out = sys.stdout
    start = time.monotonic()

    def draw(sweep=None):
        # Sized to the terminal on every frame, so resizing or a narrow pane
        # can never wrap and smear.
        cols, rows = shutil.get_terminal_size((80, 24))
        t = time.monotonic() - start
        terminal.keyboard(screen == "usage")   # key releases, for holding Tab there
        if screen == "usage":
            frame = usage_panel.render(usage, cols, rows, t)
        elif screen == "settings":
            frame = centered(render_settings(menu, cols, rows, t, usage.frame_style()), cols, rows)
        elif screen == "defaults":
            frame = centered(render_defaults(defaults_menu, cols, rows, t), cols, rows)
        elif screen == "confirm_back":
            frame = centered(render_back_confirmation(defaults_menu, quit_choice, cols, rows, t), cols, rows)
        else:
            frame = centered(render(selected, available, t, cols, rows, sweep, listed), cols, rows)
        writer.show(frame, cols, rows)

    def go(name):
        nonlocal screen, came_from
        if name == "settings":
            came_from = screen
        screen = name
        writer.reset()

    def move(step):
        nonlocal selected
        selected = (selected + step) % len(listed)

    def relist():
        # Settings may have hidden or shown CLIs; keep the same one selected if it is still listed.
        nonlocal listed, available, selected
        current = listed[selected]["name"]
        listed = shown(found)
        available = [True] * len(listed)
        selected = next((i for i, a in enumerate(listed) if a["name"] == current), 0)

    out.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[?7l")  # alt screen, hide cursor, no autowrap
    out.flush()  # frames go straight to the fd after this
    try:
        terminal.__enter__()
        draw()
        frame_at = time.monotonic()
        while True:
            # Frames on a fixed clock, so drawing time doesn't slow the rate:
            # 60 fps for the picker, less for the screens that sit open.
            step = (FRAME_TIME if screen == "agents" else
                    usage_panel.frame_time(usage) if screen == "usage" else 1 / 30)
            frame_at += step
            wait = frame_at - time.monotonic()
            if wait < 0:
                frame_at, wait = time.monotonic(), 0
            if not terminal.wait(wait):
                draw()  # next animation frame
                continue
            key = terminal.read_key()
            if not key or (key.startswith("release:") and screen != "usage"):
                continue
            if screen == "usage":
                action = usage_panel.handle(usage, key, providers, getattr(terminal, "releases", False))
                if action == "quit":
                    return None
                if action:
                    go(action)
            elif screen == "settings":
                if key in ("\x03", "\x04"):
                    return None
                if menu.handle(key) == "close":
                    relist()
                    usage_panel.rescope()   # card settings may have changed
                    go(came_from)
            elif screen == "defaults":
                if key in ("\x03", "\x04"):
                    return None
                if defaults_menu.handle(key) == "save":
                    if defaults_menu.changed:
                        quit_choice = 0
                        go("confirm_back")
                    else:
                        go("agents")
            elif screen == "confirm_back":
                if key in ("left", "right", "up", "down", "\t"):
                    quit_choice = 1 - quit_choice
                elif key in ("s", "S", "y", "Y") or (key in ("\r", "\n", " ") and quit_choice == 0):
                    try:
                        provider_defaults.save(defaults_menu.agent, defaults_menu.values, config_path())
                        go("agents")
                    except (OSError, ValueError) as exc:
                        defaults_menu.error = f"Could not save: {exc}"
                        go("defaults")
                elif key in ("\r", "\n", " ", "d", "D", "n", "N"):
                    go("agents")
                elif key in ("esc", "q"):
                    go("defaults")
                elif key in ("\x03", "\x04"):
                    return None
            elif key == "right":
                defaults_menu = provider_defaults.Menu(listed[selected])
                go("defaults")
            elif key in ("up", "k"):
                move(-1)
            elif key in ("down", "j", "\t"):
                move(1)
            elif key.isdigit() and 1 <= int(key) <= len(listed):
                selected = int(key) - 1
            elif key in ("u", "U"):
                go("usage")
            elif key == "s":
                go("settings")
            elif key in ("\r", "\n"):
                launch = time.monotonic()
                while (elapsed := time.monotonic() - launch) < 0.21:
                    draw(sweep=min(1.0, (elapsed / 0.18) ** 1.6))
                    time.sleep(1 / 120)
                draw(sweep=1.0)
                return AGENTS.index(listed[selected])
            elif key in ("q", "esc", "\x03", "\x04"):
                return None
            draw()
    finally:
        terminal.keyboard(False)
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
    missing = []
    for agent in AGENTS:
        argv = resolve_argv(agent)
        if not argv:
            missing.append(agent["name"])
            continue
        hidden = "" if preferences.get(shown_setting(agent).key) else " (hidden in selector)"
        print(f"{agent['name']}: {argv[0]}{hidden}")
        print("  arguments: " + json.dumps(agent["argv"][1:]))
    if missing:
        print("Not installed (listed once their CLI is found): " + ", ".join(missing))
    print("Usage meters are best effort; unavailable credentials show '-'.")


def catalog():
    """Every profile in agents.json with its colors, installed or not."""
    for agent in AGENTS:
        argv = resolve_argv(agent)
        swatch = "".join(fg(grad(agent, k / 16)) + "▰" for k in range(16)) + RESET
        name = fg(agent["color"]) + "●" + RESET + " " + BOLD + agent["name"].ljust(12) + RESET
        state = f"{FAINT}{argv[0]}{RESET}" if argv else f"{FAINT}not installed · {agent['argv'][0]}{RESET}"
        print(f"  {name} {swatch}  {state}")

def print_launch_banner(agent):
    mascot = agent.get("mascot")
    if not mascot:
        print(f"  {fg(agent['color'])}●{RESET} {BOLD}{agent['name']}{RESET} {FAINT}{agent['detail']}{RESET}\n", flush=True)
        return

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    cwd_disp = "~" if cwd == home else (f"~/{os.path.relpath(cwd, home)}" if cwd.startswith(home + "/") else cwd)

    info_lines = [
        f"{BOLD}{agent['name']}{RESET} {FAINT}{agent.get('short', '')}{RESET}",
        f"{FAINT}{agent.get('detail', '')}{RESET}",
        f"{FAINT}permissions: bypass{RESET}",
        f"{FAINT}{cwd_disp}{RESET}"
    ]

    lines = []
    num_rows = len(mascot)
    for y, art_row in enumerate(mascot):
        colored_art = ""
        row_len = len(art_row)
        for x, ch in enumerate(art_row):
            if ch == " ":
                colored_art += " "
            else:
                u = (x / max(1, row_len) * 0.5 + y / max(1, num_rows) * 0.5)
                colored_art += f"{fg(grad(agent, u))}{ch}{RESET}"
        info = info_lines[y] if y < len(info_lines) else ""
        lines.append(f"  {colored_art}   {info}")

    print("\n" + "\n".join(lines) + "\n", flush=True)


def main():
    args = sys.argv[1:]
    # herdr-ai's workbench panes: quitting the agent reopens the selector, and
    # Esc there drops to the shell.
    returning = args[:1] == ["--picker-return"]
    if returning:
        args = args[1:]
    if args[:1] == ["--picker-relaunch"]:
        if len(args) != 2:
            raise ValueError("--picker-relaunch needs saved launch arguments")
        import session_refresh
        argv = session_refresh.decode(args[1])
        return launch_and_return(argv) if returning else launch(argv)
    if args[:1] == ["--picker-update"]:
        update_checkout()
        return 0
    if args[:1] == ["--picker-doctor"]:
        doctor()
        return 0
    if args[:1] == ["--picker-catalog"]:
        catalog()
        return 0
    if args[:1] == ["--picker-help"]:
        print("agent [CLI arguments]                 open picker; forward arguments")
        print("agent --picker-launch NAME [args]     launch a provider directly")
        print("agent --picker-launch NAME --picker-lead PANES|--picker-crew PANE")
        print("                                      launch as a herdr-ai team lead or worker")
        print("agent --picker-return [--picker-launch NAME]")
        print("                                      reopen the selector when the agent quits")
        print("agent --picker-launch NAME --picker-solo  launch with herdr-ai's usage and handoff rules")
        print("agent --picker-usage --picker-only NAMES  the usage panel for these providers (comma separated)")
        print("agent --picker-priority               explain the Cursor/Kiro workbench choice")
        print("agent --picker-update                 fast-forward this Git checkout")
        print("agent --picker-doctor                 show paths and launch profiles")
        print("agent --picker-catalog                every known CLI, its colors, and whether it is installed")
        print("agent --picker-help                   show this help")
        print("usage                                 opens on the usage panel; u switches screens")
        print("Prefix CLI args with -- to escape picker management commands.")
        return 0
    if args[:1] in (["--picker-launch-priority"], ["--picker-priority"]):
        import provider_priority
        name, reason = provider_priority.select(sys.modules[__name__])
        print(f"agent: workbench chose {name}: {reason}", flush=True)
        if args[0] == "--picker-priority":
            return 0
        args = ["--picker-launch", name, *args[1:]]
    launching = args[:1] == ["--picker-launch"]
    if launching:
        if len(args) < 2:
            raise ValueError("--picker-launch needs a provider name")
        index = next((i for i, a in enumerate(AGENTS) if a["name"].lower() == args[1].lower()), None)
        if index is None:
            raise ValueError(f"Unknown provider: {args[1]}")
        args = args[2:]
    else:
        start, only = None, None
        if args[:1] == ["--picker-usage"]:
            start, args = "usage", args[1:]
        if args[:1] == ["--picker-only"]:
            if len(args) < 2:
                raise ValueError("--picker-only needs a provider name")
            only = []
            for word in args[1].split(","):
                match = next((a for a in AGENTS if a["name"].lower() == word.strip().lower()), None)
                if match is None:
                    raise ValueError(f"Unknown provider: {word}")
                only.append(match["name"])
            args = args[2:]
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise RuntimeError("agent needs an interactive terminal; try --picker-doctor")
        try:
            index = choose(start, only)
        except KeyboardInterrupt:
            index = None
    if index is None:
        return 0
    if returning and not launching:
        # Re-exec so the pane's command line names the agent, which herdr-ai's
        # reroute restarts.
        # Inside herdr the chosen agent learns its usage rules like herdr-ai's own panes.
        # Star balances providers itself and takes no brief.
        solo = (["--picker-solo"] if os.environ.get("HERDR_ENV") == "1" and not args
                and AGENTS[index]["name"] != "Star" else [])
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()),
                                  "--picker-return", "--picker-launch", AGENTS[index]["name"], *solo, *args])
    agent = AGENTS[index]
    if args[:1] in (["--picker-lead"], ["--picker-crew"], ["--picker-solo"]):
        import team
        agent, args = team.prepare(agent, args)
    argv = resolve_argv(agent)
    if not argv:
        raise RuntimeError(f"{agent['name']} is not installed. Run --picker-doctor.")
    if args[:1] == ["--"]:
        args = args[1:]
    save_last(index)
    defaults = provider_defaults.current(agent)
    if os.environ.get("AGENT_PICKER_NO_PRETRUST") != "1" and (defaults is None or defaults["bypass_permissions"]):
        pretrust(agent)
    print_launch_banner(agent)
    if returning:
        return launch_and_return(argv + args)
    return launch(argv + args)


def launch_and_return(argv):
    """Run the agent, then reopen the selector in return mode once it quits."""
    import signal
    stopped = []
    # The agent handles Ctrl+C itself; a no-op handler (unlike SIG_IGN) is not inherited.
    handlers = {signal.SIGINT: lambda *_: None}
    # herdr-ai's reroute and a closing pane signal the whole process group.
    # Wait for the agent instead of dying first: the shell takes the terminal
    # back as soon as this process exits, and an agent still restoring its
    # tty modes after that leaves the shell's line editor unable to read Enter.
    # Ctrl+\ (SIGQUIT) reaches the group too; the agent decides what it means.
    handlers[signal.SIGTERM] = handlers[signal.SIGHUP] = handlers[signal.SIGQUIT] = \
        lambda sig, _: stopped.append(sig)
    previous = {sig: signal.signal(sig, handler) for sig, handler in handlers.items()}
    try:
        subprocess.call(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if stopped:
        return 128 + stopped[0]
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "--picker-return"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit(f"agent: {exc}")
