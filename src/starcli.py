"""StarCLI: one terminal for every AI provider, balanced across their usage limits.

Type a request and StarCLI picks who runs it: the task is classified locally
(no tokens spent on routing), every installed provider is scored on how well it
handles that kind of task and how much quota it can spare (star_router), and
the turn runs on that provider's own CLI headless, with its own tools and the
same bypass-permissions flags as its agent-picker profile (star_engines). Shift+
Tab switches to plan mode, where providers may read but not change anything.

A conversation stays with one provider while that is sensible, since its
session keeps the context cached; when a turn moves elsewhere, the new provider
gets a short recap of what it missed. A provider that fails is set aside for
the session and the turn reruns on the next best one. /goal keeps taking turns,
on whichever provider suits each step, until the goal is done.

Star CLIs in the same herdr tab form a mesh (star_mesh): each knows what the
others are doing, and they, or the agents inside them, hand each other tasks.
herdr-ai --setup star lays out a lead and four crew.

    star                          the interactive terminal
    star "prompt"                 start with a prompt
    star -p "prompt"              run one prompt, print the reply, exit
    star -c                       continue the last conversation
    star --goal "objective"       start working toward a goal
    star --route "prompt"         how a prompt would be routed; spends nothing
    star --usage                  the meters StarCLI balances
    star --peers                  the Star CLIs in this herdr tab
    star --send NAME "task" [--wait]   hand a peer a task (and wait for its reply)
    --name N --role lead|crew     this terminal's name and role in the mesh
    --use NAME / --tier T / --plan    pin a provider, a tier, or plan mode
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import star_sessions
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_picker as ap
import star_commands
import star_engines
import star_input
import star_mesh
import star_meter
import star_router

VERSION = "0.2"
STATE = ap.STATE / "star"
HISTORY = STATE / "history.jsonl"
COUNTS = STATE / "commands.json"
CONVERSATIONS = STATE / "conversations"
LAST = STATE / "last"
LIVE = STATE / "live"   # a file per running turn, so the usage panel can light up Star and its provider

# ---- palette -----------------------------------------------------------------

GOLD = star_meter.GRADIENT          # gold into a very light sky blue, as Star's profile in agents.json
GOLD_STYLE = {"gradient": GOLD, "color": GOLD[0]}
SKY = (196, 230, 255)
BRIGHT = (255, 232, 168)
DEEP = (214, 152, 44)
DIM = (150, 120, 62)
INK = (236, 230, 214)
SOFT = (146, 139, 126)
FAINT = (98, 94, 88)
CODE = (232, 210, 160)
BAD = (236, 112, 98)
PLAN = (150, 205, 250)
RESET, BOLD = ap.RESET, ap.BOLD
SHORT = {"Antigravity": "Gemini"}

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def fg(rgb):
    return ap.fg(rgb) if COLOR else ""


def paint(text, rgb, bold=False):
    if not COLOR:
        return text
    return fg(rgb) + (BOLD if bold else "") + text + RESET


def gold(text, t=0.0, bold=True, offset=0.0, shimmer=0.0):
    """Text in the gold gradient; shimmer > 0 rolls a highlight across it."""
    if not COLOR:
        return text
    out = []
    for i, ch in enumerate(text):
        c = ap.grad(GOLD_STYLE, offset + i / 28 - t * 0.12)
        if shimmer:
            c = ap.mix(c, (255, 250, 230), shimmer * ap.wave(i, t, 14.0, 7.0) ** 6)
        out.append(fg(c) + ch)
    return (BOLD if bold else "") + "".join(out) + RESET


def width():
    return max(40, shutil.get_terminal_size((100, 30)).columns)


def tilde(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def say(text, color=SOFT):
    print("  " + gold("✦", bold=False) + " " + paint(text, color))


# ---- the logo -----------------------------------------------------------------
#
# A twinkle: a four-point sparkle at the far left (where Claude Code keeps its
# crab). Its arms breathe, the core flares white, and stray glints wink around it.

STAR_BODY = ["    ▲    ", "   ◢█◣   ", "╴─━███━─╶", "   ◥█◤   ", "    ▼    "]
GLINTS = [(0, 1, 0.0), (0, 7, 2.1), (4, 1, 4.0), (4, 7, 1.2)]   # (row, col, phase) of the winking dots
AT = 1                                     # the star's first column
LOGO_W = AT + len(STAR_BODY[0]) + 1
LOGO_ROW = 2                               # the logo's first screen row once the screen is cleared


def logo_cells(t=0.0, shift=0, glow=0.0):
    """[[(char, rgb, bold)]] for the logo, slid `shift` columns left (for the
    entrance), twinkling at time t."""
    pulse = ap.wave(0, t, 1.0, 7.0) ** 2                       # the whole star breathes
    flare = ap.wave(0, t, 1.0, 3.1) ** 10                      # an occasional white-hot flash
    rows = []
    for y in range(5):
        cells = [(" ", None, False)] * LOGO_W
        for gy, gx, phase in GLINTS:
            if gy == y:
                lit = ap.wave(0, t + phase, 1.0, 9.0) ** 6
                cells[AT + gx] = ("✦" if lit > 0.6 else "·", ap.mix((70, 66, 58), (255, 244, 205), lit), False)
        for k, ch in enumerate(STAR_BODY[y]):
            if ch == " ":
                continue
            reach = max(abs(k - 4), abs(y - 2)) / 4              # 0 at the core, 1 at the tips
            c = ap.grad(GOLD_STYLE, (k + y * 1.6) / 22 - t * 0.12)
            c = ap.mix(c, (255, 251, 236), min(1.0, (0.2 + 0.5 * pulse + 0.6 * flare + glow) * (1 - reach) ** 0.7))
            c = ap.mix((90, 70, 30), c, 0.55 + 0.45 * pulse + 0.2 * flare * (1 - reach) if reach > 0.5 else 1.0)
            cells[AT + k] = (ch, c, True)
        rows.append(cells[shift:] + [(" ", None, False)] * shift if shift > 0 else cells)
    return rows


def paint_cells(cells):
    if not COLOR:
        return "".join(ch for ch, _, _ in cells)
    out, last = [], None
    for ch, c, b in cells:
        key = (c, b)
        if key != last:
            out.append(RESET + (fg(c) if c else "") + (BOLD if b else ""))
            last = key
        out.append(ch)
    return "".join(out) + RESET


def logo_text(cwd, names, mode, fade=1.0, t=0.0):
    providers = " · ".join(SHORT.get(n, n) for n in names) or "no providers installed"
    cwd = cwd if cwd.startswith(("~", "…")) else tilde(cwd)
    def faded(c):
        return ap.mix((40, 40, 46), c, fade)
    return ["",
            gold("Star CLI", t) + paint(f" v{VERSION}", faded(FAINT)) if fade >= 1 else paint("Star CLI", faded(BRIGHT), True),
            paint("auto routing", faded(SKY)) + paint("  " + providers, faded(SOFT)),
            paint(tilde(cwd), faded(FAINT)),
            paint(mode, faded(FAINT))]


def logo_lines(cwd, names, mode, t=0.0, shift=0, glow=0.0, fade=1.0):
    art = logo_cells(t, shift, glow)
    w = width()
    narrow = w < LOGO_W + 34
    room = w - 2 if narrow else w - LOGO_W - 5
    cwd = tilde(cwd)
    if len(cwd) > room:
        cwd = "…" + cwd[-(room - 1):]
    text = logo_text(cwd, [n for n in names][:max(1, (room - 14) // 9)] if len(" · ".join(names)) + 14 > room else names,
                     mode if len(mode) <= room else mode[:room - 1] + "…", fade, t)
    if narrow:
        return [" " + paint_cells(row) for row in art] + [" " + line for line in text[1:4]]
    return [" " + paint_cells(row) + "   " + text[k] for k, row in enumerate(art)]


def intro(cwd, names, mode, animate=True):
    """Clear the screen like Claude Code does, then fly the star in."""
    if not COLOR:
        print(f"Star CLI v{VERSION} · {tilde(cwd)}")
        return
    sys.stdout.write("\x1b[2J\x1b[3J\x1b[H\n")
    final = logo_lines(cwd, names, mode)
    sys.stdout.write("\n".join(final) + "\n\n")
    sys.stdout.flush()
    if not animate:
        return
    rows = len(final)
    try:
        start, length = time.time(), 1.25
        while True:
            e = time.time() - start
            if e >= length:
                break
            fly = min(1.0, e / 0.7)
            shift = round((1 - fly) ** 2.2 * LOGO_W)          # eases in from the left
            glow = max(0.0, 1 - abs(e - 0.8) / 0.35)          # a flash as it lands
            fade = max(0.0, min(1.0, (e - 0.55) / 0.5))
            frame = logo_lines(cwd, names, mode, t=e * 3, shift=shift, glow=glow, fade=fade)
            sys.stdout.write(f"\x1b7\x1b[{LOGO_ROW};1H" + "".join("\x1b[2K" + l + "\n" for l in frame)[:-1] + "\x1b8")
            sys.stdout.flush()
            time.sleep(1 / 40)
        sys.stdout.write(f"\x1b7\x1b[{LOGO_ROW};1H" + "".join("\x1b[2K" + l + "\n" for l in final)[:-1] + "\x1b8")
        sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    return rows


def twinkle(t):
    """Redraw only the art in place: the star pulses and its glints wink."""
    art = logo_cells(t * 1.8, glow=0.15 * ap.wave(0, t, 1.0, 2.0) ** 6)
    sys.stdout.write("\x1b7" + "".join(f"\x1b[{LOGO_ROW + k};2H" + paint_cells(row) for k, row in enumerate(art))
                     + "\x1b8")
    sys.stdout.flush()


# ---- meters -------------------------------------------------------------------

def installed():
    return {a["name"] for a in ap.AGENTS if ap.installed(a)}


def agent(name):
    return next(a for a in ap.AGENTS if a["name"] == name)


def bypass_enabled(name):
    settings = ap.provider_defaults.current(agent(name))
    return bool(settings and settings.get("bypass_permissions"))


def meter_bar(left, cells=5):
    shown = left if ap.depleting() else 100 - left
    filled = round(max(0, min(100, shown)) / 100 * cells)
    color = DEEP if left > 25 else (235, 160, 70) if left > 10 else BAD
    return paint("▰" * filled, color) + paint("▱" * (cells - filled), FAINT)


def star_summary():
    with ap.USAGE_LOCK:
        usage = dict(ap.USAGE)
    return star_meter.summary(usage, installed(), {a["name"]: a for a in ap.AGENTS})


def star_bar(info, cells):
    out, _ = star_meter.bar(info, cells, ap.depleting())
    return ("".join(fg(color) + glyph for glyph, color in out) + RESET) if COLOR else "".join(g for g, _ in out)


def refresh_meters(names, older_than=300, wait=0.0):
    """Re-read stale meters in the background; readers spend no model tokens."""
    now, threads = time.time(), []
    for name in names:
        with ap.USAGE_LOCK:
            entry = ap.USAGE.get(name) or {}
            busy = name in ap.PENDING
        if name in ap.FETCHERS and not busy and now - float(entry.get("fetched", 0) or 0) > older_than:
            with ap.USAGE_LOCK:
                ap.PENDING.add(name)
            thread = threading.Thread(target=ap._fetch, args=(name,), daemon=True)
            thread.start()
            threads.append(thread)
    deadline = time.monotonic() + wait
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))


def save_meter(name, windows):
    """A meter reported inside a turn's stream (Claude sends its limits live)."""
    ap.load_usage()
    with ap.USAGE_LOCK:
        old = (ap.USAGE.get(name) or {}).get("windows") or []
        merged = {w["label"]: w for w in old if isinstance(w, dict) and "label" in w}
        merged.update({w["label"]: w for w in windows})
        ap.USAGE[name] = {"windows": list(merged.values()), "fetched": time.time()}
        try:
            ap.USAGE.update(ap.usage_cache.write(ap.USAGE_CACHE, ap.USAGE))
        except OSError:
            pass


def print_usage(names):
    now = time.time()
    info = star_summary()
    if info:
        shown = (info["now"], info["long"]) if ap.depleting() else (100 - info["now"], 100 - info["long"])
        print(f"  {gold('✦')} {gold('Star'.ljust(12))}{paint('∑  ', SOFT)}{star_bar(info, 30)} "
              f"{paint(f'{shown[0]:3.0f}%', SKY, True)}" + paint(f"  now · {shown[1]:.0f}% without 5h windows", FAINT))
        row = star_meter.resets_row(info, max(30, width() - 20), None, now)
        print(" " * 18 + "".join(paint(ch, color) if color else ch for ch, color in row))
        print()
    for name in names:
        with ap.USAGE_LOCK:
            entry = ap.USAGE.get(name)
        found = star_router.windows(entry, now)
        head = f"  {paint('●', tuple(agent(name)['color']))} {paint(name.ljust(12), INK)}"
        if not found:
            print(head + paint("no reading", FAINT))
            continue
        cells = []
        for label, pct, resets, _ in found:
            left = 100 - pct
            number = left if ap.depleting() else pct
            w = next((w for w in entry["windows"] if w.get("label") == label), {})
            credits = ap.credits_text(w) if w.get("limit") else None
            cells.append(paint(label.ljust(3), SOFT) + meter_bar(left, 10) + " "
                         + paint(f"{number:3.0f}%", BRIGHT) + paint(f" ↻ {ap.until(resets)}".ljust(10), FAINT)
                         + (paint(credits + "  ", SOFT) if credits else ""))
        print(head + "  ".join(cells))
    print("  " + paint("bars " + ("deplete: the number is what's left" if ap.depleting()
                                  else "fill: the number is what's used"), FAINT))


# ---- reply rendering ------------------------------------------------------------

INLINE = [
    (re.compile(r"`([^`]+)`"), lambda m: paint(m.group(1), CODE) + fg(INK)),
    (re.compile(r"\*\*([^*]+)\*\*"), lambda m: (BOLD if COLOR else "") + m.group(1) + (RESET + fg(INK) if COLOR else "")),
]


def style_inline(text):
    if not COLOR:
        return re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    for pattern, repl in INLINE:
        text = pattern.sub(repl, text)
    return fg(INK) + text + RESET


class Printer:
    """Writes a reply line by line in StarCLI's style, under a spinner while waiting."""

    def __init__(self, label="thinking"):
        self.lock = threading.Lock()
        self.buffer = ""
        self.code = False
        self.label = label
        self.started = time.time()
        self.spinning = sys.stdout.isatty()
        self.wrote = False
        self.stop = threading.Event()
        self.spinner = threading.Thread(target=self._spin, daemon=True)
        if self.spinning:
            self.spinner.start()

    def _spin(self):
        frames = "✶✸✹✺✹✸"
        while not self.stop.wait(1 / 12):
            with self.lock:
                if not self.spinning:
                    continue
                t = time.time() - self.started
                text = f"{self.label} {int(t)}s"
                sys.stdout.write("\r\x1b[2K  " + gold(frames[int(t * 8) % len(frames)], t, shimmer=0.5)
                                 + " " + gold(text, t * 2, bold=False, shimmer=0.9)
                                 + paint("  esc to interrupt", FAINT))
                sys.stdout.flush()

    def _clear(self):
        if self.spinning:
            sys.stdout.write("\r\x1b[2K")

    def thinking(self, label):
        with self.lock:
            self.label = label

    def emit(self, line):
        """Print one finished line above the spinner."""
        with self.lock:
            self._clear()
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def tool(self, label, error=False):
        if self.buffer.strip():
            self.flush_text()
        self.emit("  " + paint("⎿ ", DIM) + paint(ap.clip(label, width() - 6), BAD if error else SOFT))

    def text(self, chunk):
        self.buffer += chunk
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.line(line)

    def flush_text(self):
        if self.buffer:
            line, self.buffer = self.buffer, ""
            self.line(line)

    def line(self, raw):
        if not self.wrote:
            if not raw.strip():
                return
            self.emit("")
            self.wrote = True
        w = width() - 4
        if raw.strip().startswith("```"):
            self.code = not self.code
            lang = raw.strip()[3:].strip()
            self.emit("  " + paint("▁" * 3 + (" " + lang if lang else ""), DIM) if self.code else "  " + paint("▔" * 3, DIM))
            return
        if self.code:
            self.emit("  " + paint("▏ ", DIM) + paint(raw, CODE))
            return
        heading = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if heading:
            self.emit("  " + gold(heading.group(2).replace("**", ""), bold=True))
            return
        bullet = re.match(r"^(\s*)([-*•]|\d+[.)])\s+(.*)$", raw)
        if bullet:
            indent, mark, body = bullet.groups()
            mark = "•" if mark in "-*•" else mark
            lead = "  " + indent + paint(mark, DEEP) + " "
            pieces = textwrap.wrap(body, max(20, w - len(indent) - len(mark) - 1)) or [""]
            self.emit(lead + style_inline(pieces[0]))
            for piece in pieces[1:]:
                self.emit("  " + indent + " " * (len(mark) + 1) + style_inline(piece))
            return
        if not raw.strip():
            self.emit("")
            return
        lead = len(raw) - len(raw.lstrip())
        for piece in textwrap.wrap(raw.strip(), max(20, w - lead)) or [""]:
            self.emit("  " + " " * lead + style_inline(piece))

    def close(self):
        self.flush_text()
        self.stop.set()
        with self.lock:
            self._clear()
            self.spinning = False
            sys.stdout.flush()


def show_text(text, limit=40):
    """Render a finished reply (a peer's, or a summary) in StarCLI's style."""
    printer = Printer()
    printer.spinning = False
    printer.stop.set()
    lines = text.strip().splitlines()
    for line in lines[:limit]:
        printer.line(line)
    if len(lines) > limit:
        printer.emit("  " + paint(f"… {len(lines) - limit} more lines", FAINT))
    printer.close()


# ---- conversations ----------------------------------------------------------------

BRIDGE_TURNS = 6
BRIDGE_REPLY = 700
BRIDGE_TOTAL = 4000


_seq = 0


def new_id():
    """Unique even when two conversations start in the same second."""
    global _seq
    _seq += 1
    return time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 10000:04d}-{_seq:02d}"


class Conversation:
    """Turns so far, each provider's own session id, and what each provider has seen."""

    def __init__(self, data=None):
        data = data or {}
        self.id = data.get("id") or new_id()
        self.turns = data.get("turns", [])        # [{"user", "reply", "provider"}]
        self.requests = data.get("requests", [t["user"] for t in self.turns
                                               if not t["user"].startswith("[StarCLI")])
        self.sessions = data.get("sessions", {})  # provider -> its session id
        self.seen = data.get("seen", {})          # provider -> turns it holds in context
        self.cwd = data.get("cwd") or os.getcwd()
        self.goal = data.get("goal", "")
        self.goal_state = data.get("goal_state") if self.goal and data.get("goal_state") in {"running", "blocked"} \
            else "paused" if self.goal else ""
        self.inflight = data.get("inflight") if isinstance(data.get("inflight"), dict) else None
        self.summary = data.get("summary", "")    # from /compact: what came before these turns
        self.notes = data.get("notes", [])        # peer replies that reach the next turn
        self.mesh_tasks = data.get("mesh_tasks", {})  # durable receipts for tasks accepted from peers
        self.mesh_replies = data.get("mesh_replies", [])  # reply ids already folded into notes
        self.briefed = data.get("briefed", {})    # provider -> the mesh brief it last got
        self.routing = data.get("routing") if isinstance(data.get("routing"), dict) else None
        self.before_save = None
        self.created = data.get("created") or time.time()

    @property
    def current(self):
        return self.turns[-1]["provider"] if self.turns else None

    @property
    def title(self):
        first = next((t["user"] for t in self.turns if not t["user"].startswith("[StarCLI")), "")
        return (first or self.goal or "(empty)").strip().splitlines()[0][:70]

    def prompt_for(self, provider, prompt, brief=""):
        """The prompt, with whatever this provider has not seen: the mesh brief,
        the compacted summary, a recap of turns it missed, and peer replies."""
        parts = []
        resumed = bool(self.sessions.get(provider))
        if brief and (not resumed or self.briefed.get(provider) != brief):
            parts.append(brief)
        if self.summary and (not resumed or self.seen.get(provider) is None):
            parts.append("[StarCLI] Summary of this conversation so far:\n\n" + self.summary)
        missed = self.turns[self.seen.get(provider, 0) if resumed else 0:]
        if not missed and not resumed and self.requests:
            parts.append("[StarCLI] Earlier user requests and constraints, in order. Preserve these unless "
                         "a later user message supersedes them:\n\n" + "\n\n".join(self.requests))
        if missed:
            recap, total = [], 0
            for turn in reversed(missed[-BRIDGE_TURNS:]):
                reply = turn["reply"].strip()
                if len(reply) > BRIDGE_REPLY:
                    reply = reply[:BRIDGE_REPLY] + " […]"
                block = f"User: {turn['user'].strip()}\n{turn['provider']}: {reply}"
                if total + len(block) > BRIDGE_TOTAL and recap:
                    break
                recap.insert(0, block)
                total += len(block)
            skipped = len(missed) - len(recap)
            recent_requests = {t["user"] for t in missed[len(missed) - len(recap):]}
            earlier = ([t["user"] for t in missed[:skipped] if not t["user"].startswith("[StarCLI")]
                       if resumed else [request for request in self.requests if request not in recent_requests])
            if earlier:
                parts.append("[StarCLI] Earlier user requests and constraints, in order. Preserve these unless "
                             "a later user message supersedes them:\n\n" + "\n\n".join(earlier))
            parts.append("[StarCLI] You are joining a conversation other agents were handling in this same "
                         "working directory. Recent turns you did not see"
                         + (f" ({skipped} older ones omitted)" if skipped else "") + ", oldest first:\n\n"
                         + "\n\n".join(recap))
        if self.notes:
            parts.append("[StarCLI] Replies from peer Star CLIs since the last turn:\n\n" + "\n\n".join(self.notes))
        if not parts:
            return prompt
        return "\n\n".join(parts) + "\n\n[StarCLI] The user's new message:\n\n" + prompt

    def record(self, provider, user, reply, session, brief="", status="ok", error=None):
        self.inflight = None
        turn = {"user": user, "reply": reply, "provider": provider, "at": time.time()}
        if status != "ok":
            turn.update(status=status, error=error)
        self.turns.append(turn)
        if not user.startswith("[StarCLI") and (not self.requests or self.requests[-1] != user):
            self.requests.append(user)
        if session:
            self.sessions[provider] = session
        self.seen[provider] = len(self.turns)
        if brief:
            self.briefed[provider] = brief
        if status == "ok":
            self.notes = []

    def data(self):
        keep = self.turns[-60:]
        dropped = len(self.turns) - len(keep)
        out = {"id": self.id, "turns": [dict(t, reply=t["reply"][:6000]) for t in keep], "requests": self.requests,
               "sessions": self.sessions, "cwd": self.cwd, "goal": self.goal, "goal_state": self.goal_state,
               "inflight": self.inflight,
               "summary": self.summary,
               "notes": self.notes, "briefed": self.briefed, "created": self.created,
               "mesh_tasks": dict(list(self.mesh_tasks.items())[-100:]), "mesh_replies": self.mesh_replies[-200:],
               "seen": {k: max(0, v - dropped) for k, v in self.seen.items()}}
        if isinstance(self.routing, dict):
            out["routing"] = {"pinned": self.routing.get("pinned"), "tier": self.routing.get("tier"),
                              "plan": bool(self.routing.get("plan"))}
        return out

    def save(self, force=False):
        if self.before_save:
            self.before_save()
        empty = not self.turns and not self.goal and not self.notes and not self.mesh_tasks and not self.inflight
        if empty and not force:
            return True
        try:
            CONVERSATIONS.mkdir(parents=True, exist_ok=True)
            path = CONVERSATIONS / f"{self.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data()), encoding="utf-8")
            os.replace(tmp, path)
            if not empty:
                LAST.write_text(self.id, encoding="utf-8")
            return True
        except OSError:
            return False

    @classmethod
    def load(cls, ident):
        if not star_sessions.valid_id(ident):
            return None
        try:
            data = json.loads((CONVERSATIONS / f"{ident}.json").read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("id") != ident:
                return None
            if not star_sessions.valid_conversation(data):
                return None
            convo = cls(data)
            convo._loaded = True
            return convo
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    @classmethod
    def load_last(cls):
        try:
            return cls.load(LAST.read_text(encoding="utf-8").strip())
        except OSError:
            return None


def saved_conversations(limit=30):
    """[(id, title, turns, updated, cwd)], newest first."""
    out = []
    paths = []
    for path in CONVERSATIONS.glob("*.json") if CONVERSATIONS.is_dir() else ():
        try:
            paths.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for updated, path in sorted(paths, reverse=True):
        convo = Conversation.load(path.stem)
        if convo is None:
            continue
        out.append((convo.id, convo.title, len(convo.turns), updated, convo.cwd))
        if len(out) >= limit:
            break
    return out


# ---- keys during a turn -----------------------------------------------------------

class Approvals:
    """Put each permission request a turn leaves in its folder to the user:
    y allows it, a allows that tool for the rest of the session, n or Esc
    denies it (Esc also stops the turn, as in Claude Code)."""

    ANSWERS = {"y": "allow", "a": "always", "n": "deny", "esc": "deny"}

    def __init__(self, folder, provider, keys, allowed, waiting=None, printer=None):
        self.folder, self.provider, self.keys, self.allowed = folder, provider, keys, allowed
        self.waiting = waiting or (lambda label: None)
        # Lines go above the turn's spinner, under its lock, so neither garbles the other.
        self.printer = printer
        self.stop = threading.Event()
        self.choice = None
        self.answered = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.answered.set()
        self.thread.join(2)
        return False

    def key(self, ch):
        choice = self.ANSWERS.get(ch if ch == "esc" else ch.lower())
        if not choice:
            return False
        self.choice = choice
        self.answered.set()
        return True

    def say(self, line):
        if self.printer:
            self.printer.emit(line)
        else:
            print(line, flush=True)

    def _run(self):
        done = set()
        while not self.stop.is_set():
            for request in sorted(self.folder.glob("*.req")):
                if request.name not in done:
                    done.add(request.name)
                    self._answer(request)
            self.stop.wait(.1)

    def _answer(self, request):
        try:
            data = json.loads(request.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        tool = str(data.get("tool_name") or "a tool")
        label = star_engines.describe(tool, data.get("input"))
        decision = {"behavior": "deny", "message": "The user denied this action."}
        if tool in self.allowed:
            decision = {"behavior": "allow"}
        elif not self.keys.active:
            decision["message"] = "StarCLI has no terminal to ask the user for approval."
            self.say("  " + paint(f"✗ {self.provider} asked to {label}: no terminal to approve it", BAD))
        else:
            self.say("  " + paint("? ", GOLD[0], True) + paint(f"{self.provider} wants to ", SOFT) + paint(label, INK)
                     + paint("   y allow · a always allow " + tool + " · n deny · esc stop", FAINT))
            self.choice = None
            self.answered.clear()
            self.keys.question = self
            self.waiting(label)
            if self.printer:
                self.printer.thinking("waiting for your answer")
            self.answered.wait()
            self.keys.question = None
            if self.printer:
                self.printer.thinking("working")
            self.waiting(None)
            if self.choice in ("allow", "always"):
                decision = {"behavior": "allow"}
                if self.choice == "always":
                    self.allowed.add(tool)
            self.say("  " + paint(("✓ allowed" if decision["behavior"] == "allow" else "✗ denied") + f": {label}",
                                  FAINT))
        reply = request.with_suffix(".res")
        tmp = reply.with_name("." + reply.name + ".tmp")
        try:
            tmp.write_text(json.dumps(decision), encoding="utf-8")
            os.replace(tmp, reply)
        except OSError:
            pass


class KeyWatch:
    """While a turn runs: Esc interrupts it, and anything typed is kept for the
    next prompt (as Claude Code queues a message typed mid-turn)."""

    def __init__(self, on_escape):
        self.on_escape = on_escape
        self.typed = ""
        self.question = None   # a pending Approval takes the next y, a, n or Esc
        self.active = star_input.supported()
        self.stop = threading.Event()

    def __enter__(self):
        if not self.active:
            return self
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)   # keys arrive one by one; ctrl-c still interrupts
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        return self

    def _watch(self):
        import codecs
        import select
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while not self.stop.is_set():
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if not ready:
                continue
            data = os.read(self.fd, 1024)
            if data == b"\x1b":
                more, _, _ = select.select([self.fd], [], [], 0.03)
                if not more:
                    self._escape()
                    continue
                data += os.read(self.fd, 64)
            text = re.sub(r"\x1b\[[0-9;]*[A-Za-z~]|\x1b.", "", decoder.decode(data))
            for ch in text:
                if self.question and self.question.key(ch):
                    continue
                if ch in "\x7f\x08":
                    self.typed = self.typed[:-1]
                elif ch in "\r\n":
                    self.typed += " " if self.typed and not self.typed.endswith(" ") else ""
                elif ch >= " ":
                    self.typed += ch

    def _escape(self):
        if self.question:
            self.question.key("esc")
        self.on_escape()

    def __exit__(self, *exc):
        if self.active:
            self.stop.set()
            self.thread.join(0.3)
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        return False


# ---- the session ------------------------------------------------------------------

def announce(provider, child, model=None):
    """Tell the usage panel a turn is running: StarCLI (this pid) is driving provider."""
    path = LIVE / f"{os.getpid()}-{child}.json"
    try:
        LIVE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "child": child, "provider": provider, "model": model,
                                    "pane": os.environ.get("HERDR_PANE_ID"), "started": time.time()}),
                        encoding="utf-8")
    except OSError:
        pass
    return path


RESUME = None   # () -> the argv that reopens this StarCLI's conversation after a herdr restart


def report_herdr(state, message=""):
    """Tell herdr this pane's agent is working or idle, so its sidebar and the usage
    panel see StarCLI the way they see the providers' own CLIs. Fire and forget."""
    pane, herdr = os.environ.get("HERDR_PANE_ID"), os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not (pane and herdr and os.environ.get("HERDR_ENV") == "1"):
        return
    if state == "release":
        argv = [herdr, "pane", "release-agent", "--source", "starcli", "--agent", "star", pane]
    else:
        argv = [herdr, "pane", "report-agent", "--source", "starcli", "--agent", "star", "--state", state, pane]
    if message:
        argv[-1:-1] = ["--message", message[:120]]
    # herdr ignores a report whose seq is not above the last one it accepted
    argv[-1:-1] = ["--seq", str(time.time_ns() // 1000)]
    if state != "release" and RESUME:
        # herdr 0.10+ reruns this in the pane after a restart; older versions ignore it
        words = RESUME()
        argv[-1:-1] = ["--agent-session-id", words[2]]
        argv += ["--"] + words
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError:
        pass


def tokens(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


PLAN_NOTE = ("[StarCLI plan mode] Read and analyze only: do not modify files, and run only commands that "
             "change nothing. Reply with a concrete plan: the steps, the files involved, and the risks.")


def goal_verdict(reply):
    """Only the final plain status line controls a goal, never quoted examples."""
    lines = reply.rstrip().splitlines()
    if not lines:
        return "CONTINUES", ""
    # A marker inside a Markdown code block is output or an example, not a verdict.
    fence = None
    for line in lines[:-1]:
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            token = match.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
    if fence:
        return "CONTINUES", ""
    match = re.fullmatch(r"GOAL (COMPLETE|BLOCKED|CONTINUES)(?::[ \t]*(.*))?", lines[-1])
    return (match.group(1), match.group(2) or "") if match else ("CONTINUES", "")


def goal_prompt(goal, step, crew=()):
    rule = ("End your reply with exactly one of these lines: `GOAL COMPLETE` once the goal is fully done and "
            "verified; `GOAL BLOCKED: <what you need from the user>` if you cannot go on without them; or "
            "`GOAL CONTINUES: <the next step>` otherwise. Write the status as the final plain line, without "
            "quotes, Markdown formatting, or a code block. Before reporting complete, check every requirement "
            "against current files and runtime evidence, integrate and verify crew work, and resolve any "
            "outstanding tasks. A plan, partial change, or unverified claim is not completion.")
    delegate = ""
    if crew:
        delegate = (f"Idle crew Star CLIs you can delegate to: {', '.join(crew)}. Hand them searches, test runs, "
                    f"and well specified edits with `star --send <name> \"task\" --wait` (several at once, then "
                    f"wait). Keep planning, decisions, and the final check yourself. ")
    if step == 1:
        return (f"[StarCLI goal] Work autonomously toward this goal until it is fully done:\n\n{goal}\n\n"
                f"Do as much as you can in this turn: make the changes and run the checks. {delegate}{rule}")
    return (f"[StarCLI goal, step {step}] Keep going toward the goal: {goal}\n\n"
            f"Pick up where the last step left off (its reply is above). {delegate}{rule}")


class Star:
    def __init__(self, conversation=None, pinned=None, tier=None, plan=False, member=None, lease=None):
        self.config = star_router.load_config()
        self.installed = installed()
        self.names = [n for n in self.config if n in self.installed]
        self.engines = {n: star_engines.ENGINES[n](agent(n)) for n in self.names if n in star_engines.ENGINES}
        self.pinned, self.tier, self.plan = pinned, tier, plan
        self.member = member
        self.down = {}            # provider -> why it failed this session
        self.approved = set()     # tools the user allowed for the rest of this session ("a")
        self.quota_failures = {}  # known quota windows, so reset pools can rejoin
        self.last = None          # (task, choices, skipped) of the last routing
        self.totals = {}          # provider -> {"turns", "input", "cached", "output", "credits", "seconds"}
        self.running = None       # the Turn in progress, for Esc
        self.typed = ""           # typed during the last turn, for the next prompt
        self.lease = lease
        self.bind(conversation or Conversation())

    def bind(self, conversation):
        """Follow a conversation, so every save records this session's pin, tier, and plan."""
        if self.lease:
            def reload_saved():
                latest = Conversation.load(conversation.id)
                if latest is None:
                    raise star_sessions.ConversationBusy("saved conversation disappeared or became invalid")
                return latest
            loaded = getattr(conversation, "_loaded", False)
            latest = self.lease.claim(conversation.id, prepare=reload_saved if loaded else None)
            if latest is not None:
                conversation = latest
        self.convo = conversation
        self.convo.before_save = self._stamp_routing
        if self.member:
            apply_saved_routing(self)
            self.convo.save(force=True)
            self.show_goal_state()

    def _stamp_routing(self):
        self.convo.routing = {"pinned": self.pinned, "tier": self.tier, "plan": bool(self.plan)}
        if self.member:
            self.member.beat(conversation=self.convo.id, cwd=self.convo.cwd)

    def show_goal_state(self):
        if self.member:
            blocked = bool(self.convo.goal and self.convo.goal_state == "blocked")
            state, task = ("blocked", self.convo.goal[:120]) if blocked else ("idle", "")
            self.member.beat(status=state, task=task, goal=self.convo.goal)
            report_herdr(state, task)

    # routing
    def route(self, text, images=()):
        tier = self.tier
        if tier is None and self.member and self.member.role == "lead":
            tier = "heavy"
        task = star_router.classify(text, tier)
        if self.tier is None and tier == "heavy":
            task.reasons[-1] = "mesh lead uses a strong orchestration model"
        with ap.USAGE_LOCK:
            usage = dict(ap.USAGE)
        self.recover_quota_windows(usage)
        choices, skipped = star_router.rank(task, usage, set(self.engines), self.config,
                                            current=self.convo.current, pinned=self.pinned, down=self.down,
                                            images=bool(images))
        self.last = (task, choices, skipped)
        return task, choices, skipped

    def mark_failure(self, choice, error):
        name = choice.provider
        self.down[name] = error
        self.quota_failures.pop(name, None)
        if not re.search(r"quota|rate.?limit|usage.?limit|limit (?:reached|exceeded)|too many requests|"
                         r"(?:out of|exhausted) credits|credits exhausted", error, re.I):
            return
        now = time.time()
        with ap.USAGE_LOCK:
            found = star_router.windows(ap.USAGE.get(name), now, choice.pool)
        if found:
            # A failure can arrive before a meter reaches its routing cutoff.
            # Track the tightest observed window, without inventing a reset.
            tight = max(w[1] for w in found)
            self.quota_failures[name] = {"at": now, "pool": choice.pool,
                                         "windows": [w for w in found if w[1] == tight]}

    def recover_quota_windows(self, usage):
        now = time.time()
        for name, failed in list(self.quota_failures.items()):
            if name not in self.down:
                self.quota_failures.pop(name, None)
                continue
            windows = failed["windows"]
            reset = all(w[2] and w[2] <= now for w in windows)
            entry = usage.get(name) or {}
            current = star_router.windows(entry, now, failed["pool"])
            try:
                newer = float(entry.get("fetched", 0)) > failed["at"]
            except (TypeError, ValueError):
                newer = False
            recovered = newer and current and not star_router.blocked(current, now) and all(
                any(new[0] == old[0] and new[1] < old[1] for new in current) for old in windows)
            if reset or recovered:
                self.down.pop(name, None)
                self.quota_failures.pop(name, None)

    def route_line(self, task, choice, note=""):
        dot = paint("◆", tuple(agent(choice.provider)["color"]))
        model = choice.model + (f" · {choice.effort}" if choice.effort else "")
        meter = f"   {paint(choice.meter, FAINT)}" if choice.meter else ""
        plan = paint("  plan", PLAN) if self.plan else ""
        return (f"  {dot} {gold(choice.provider)} {paint(model, SKY)}   "
                f"{paint(task.kind + ' · ' + choice.tier, SOFT)}{plan}{meter}" + (f"  {paint(note, DIM)}" if note else ""))

    def explain(self, prompt=None):
        if prompt:
            self.route(prompt)
        if not self.last:
            say("nothing routed yet; try /route <prompt>")
            return
        task, choices, skipped = self.last
        print("  " + gold(f"{task.kind} · {task.tier}") + paint("   " + "; ".join(task.reasons), SOFT))
        for k, c in enumerate(choices):
            mark = gold("✦") if k == 0 else paint("·", FAINT)
            print(f"  {mark} {paint(f'{c.score:.2f}', BRIGHT if k == 0 else SOFT)}  "
                  f"{paint(c.provider.ljust(11), INK)} {paint((c.model + ' ' + c.effort).ljust(28), SOFT)} "
                  f"{paint(c.why, FAINT)}")
        for name, why in skipped.items():
            print(f"  {paint('×', FAINT)}       {paint(name.ljust(11), FAINT)} {paint(why, FAINT)}")

    # running
    def routing_failure(self):
        skipped = self.last[2] if self.last else {}
        details = "; ".join(f"{name}: {why}" for name, why in skipped.items())
        return "no provider can take this task" + (": " + details if details else "")

    def run(self, prompt, route_text=None, images=()):
        """One turn on the best provider, rerouted on failure. Returns (reply, status):
        status is "ok", "interrupted", "blocked", "failed" or "none" (nobody could take it).
        images: attached image files; only providers that can see them take the turn."""
        if self.convo.inflight:
            pending = self.convo.inflight
            previous = pending.get("provider", "unknown")
            recovery = ("[StarCLI recovery] The previous Star process stopped during this request. "
                        "Commands, edits, or external actions may already have happened. Inspect the "
                        "current files, native session and external state before repeating any action.")
            self.convo.record(previous, pending.get("prompt", ""),
                              recovery,
                              self.convo.sessions.get(previous), status="interrupted", error="Star process stopped")
            self.convo.notes.append(recovery + "\nInterrupted request:\n" + pending.get("prompt", ""))
            if not self.save_progress():
                return "", "blocked"
        if self.convo.goal_state == "blocked":
            self.convo.goal_state = "paused"
            self.show_goal_state()
        ap.load_usage()
        refresh_meters(self.names, older_than=60, wait=1.5)
        task, choices, skipped = self.route(route_text or prompt, images)
        if images:
            # every provider gets the paths; Codex also gets them attached
            for n, path in enumerate(images, start=1):
                prompt = prompt.replace(f"[Image #{n}]", f"[Image #{n}: {path}]")
            prompt += ("\n\n[StarCLI] Attached image" + ("s" if len(images) > 1 else "") + " (look at "
                       + ("them" if len(images) > 1 else "it") + " with your file-reading tool if you cannot "
                       "see it already): " + ", ".join(images))
        if not choices:
            print("  " + paint("✗ " + self.routing_failure(), BAD))
            return "", "none"
        note = ""
        if not self.plan:
            # A profile that requires approvals runs only where StarCLI can ask them.
            unable = [c.provider for c in choices
                      if not bypass_enabled(c.provider) and not self.engines[c.provider].asks]
            choices = [c for c in choices if c.provider not in unable]
            if unable and not choices:
                reason = (f"{unable[0]}'s picker profile requires approvals. This headless adapter cannot "
                          f"show its approval UI; use /open {unable[0]} for native approval controls, "
                          "or use plan mode for read-only analysis.")
                say(reason, BAD)
                self.convo.notes.append("[StarCLI] This request was not executed because native approval is "
                                        "required:\n" + prompt)
                self.convo.save()
                return "", "blocked"
            if unable:
                note = f"{', '.join(unable)} need{'s' * (len(unable) == 1)} native approvals"
        for attempt, choice in enumerate(choices):
            self.convo.inflight = {"provider": choice.provider, "prompt": prompt, "started": time.time()}
            if not self.save_progress():
                return "", "blocked"
            reply, session, error, interrupted, brief = self.turn(task, choice, prompt, note, images)
            if interrupted:
                self.convo.record(choice.provider, prompt, reply + "\n[interrupted by the user]", session, brief,
                                  status="interrupted")
                if not self.save_progress():
                    return reply, "blocked"
                return reply, "interrupted"
            if error:
                self.mark_failure(choice, error)
                print("  " + paint(f"✗ {choice.provider}: {error}", BAD))
                partial = (f"[StarCLI] This attempt failed: {error}. Work may already have changed files or run "
                           "commands. Inspect the current state before continuing; do not assume this task is done.\n\n"
                           + reply)
                self.convo.record(choice.provider, prompt, partial, session, brief, status="failed", error=error)
                if not self.save_progress():
                    return reply, "blocked"
                if attempt + 1 >= len(choices):
                    return reply, "failed"
                note = f"after {choice.provider} failed"
                continue
            self.convo.record(choice.provider, prompt, reply, session, brief)
            if not self.save_progress():
                return reply, "blocked"
            return reply, "ok"
        return "", "failed"

    def save_progress(self):
        if self.convo.save():
            return True
        say("couldn't save conversation progress; stopping here. Restore access to the state directory before retrying.", BAD)
        return False

    def turn(self, task, choice, prompt, note="", images=()):
        engine = self.engines[choice.provider]
        print(self.route_line(task, choice, note))
        session = self.convo.sessions.get(choice.provider)
        brief = self.member.brief() if self.member else ""
        text = (PLAN_NOTE + "\n\n" + prompt) if self.plan else prompt
        argv = engine.argv(self.convo.prompt_for(choice.provider, text, brief), choice.model, choice.effort,
                           session, plan=self.plan, images=images, tier=choice.tier,
                           needs_mcp=task.needs_mcp,
                           mesh=bool(self.member and (self.member.role in ("lead", "crew") or self.member.peers())))
        folder = None
        if not self.plan and not bypass_enabled(choice.provider):
            folder = STATE / "approvals" / f"{os.getpid()}-{time.time_ns()}"
            try:
                folder.mkdir(parents=True)
            except OSError as exc:
                return "", session, f"couldn't prepare approvals: {exc}", False, brief
            argv = engine.with_approvals(argv, folder)
        printer = Printer("thinking")
        started = time.time()
        try:
            run = star_engines.Turn(engine, argv, cwd=self.convo.cwd, lease=self.lease)
        except OSError as exc:
            printer.close()
            if folder:
                shutil.rmtree(folder, ignore_errors=True)
            return "", session, str(exc), False, brief
        live = announce(choice.provider, run.proc.pid, choice.model)
        task_line = prompt.strip().splitlines()[0][:120] if prompt.strip() else ""
        if self.member:
            self.member.beat(status="working", task=task_line, provider=choice.provider, ready=False, draft=False)
        report_herdr("working", f"{choice.provider}: {task_line}")
        self.running = run
        def waiting(label):
            # Like a native pane awaiting permission, the pane shows it needs the user.
            state, text = ("blocked", f"approve: {label}") if label else ("working", f"{choice.provider}: {task_line}")
            if self.member:
                self.member.beat(status=state, task=text[:120])
            report_herdr(state, text)

        try:
            with KeyWatch(run.interrupt) as keys:
                if folder:
                    with Approvals(folder, choice.provider, keys, self.approved, waiting, printer):
                        result = self._stream(run, printer, choice, session, started)
                else:
                    result = self._stream(run, printer, choice, session, started)
            self.typed = (self.typed + keys.typed) if keys.active else self.typed
            return result + (brief,)
        finally:
            run.close()
            if folder:
                shutil.rmtree(folder, ignore_errors=True)
            self.running = None
            if self.member:
                self.member.beat(status="idle", task="", provider="")
            report_herdr("idle")
            try:
                live.unlink()
            except OSError:
                pass

    def _stream(self, run, printer, choice, session, started):
        reply, error, usage = [], None, {}
        try:
            for event in run:
                kind = event["kind"]
                if kind == "text":
                    reply.append(event["text"])
                    printer.text(event["text"])
                elif kind == "tool":
                    printer.tool(event["label"])
                    printer.thinking("working")
                elif kind == "tool_out":
                    printer.tool(event["text"], error=event.get("error"))
                elif kind == "thinking":
                    printer.thinking("thinking")
                elif kind == "session" and event.get("id"):
                    session = event["id"]
                    if self.convo.sessions.get(choice.provider) != session:
                        self.convo.sessions[choice.provider] = session
                        if not self.save_progress():
                            error = "couldn't save the provider session"
                            run.interrupt()
                            break
                elif kind == "usage":
                    for key, value in event.items():
                        if key != "kind":
                            usage[key] = usage.get(key, 0) + (value or 0)
                elif kind == "meter":
                    save_meter(choice.provider, event["windows"])
                elif kind == "error":
                    error = event["text"]
        except KeyboardInterrupt:
            run.interrupt()
        printer.close()
        self.count(choice.provider, usage, time.time() - started)
        if run.interrupted:
            print("  " + paint("⏹ interrupted", DIM))
            return "".join(reply), session, None, True
        if "".join(reply).strip():
            print()
        if "".join(reply).strip() or not error:
            self.footer(choice, usage, time.time() - started)
        refresh_meters([choice.provider], older_than=90)
        return "".join(reply), session, error, False

    def count(self, provider, usage, seconds):
        row = self.totals.setdefault(provider, {"turns": 0, "input": 0, "cached": 0, "output": 0,
                                                "credits": 0.0, "seconds": 0.0})
        row["turns"] += 1
        row["seconds"] += seconds
        for key in ("input", "cached", "output", "credits"):
            row[key] += usage.get(key, 0) or 0

    def footer(self, choice, usage, seconds):
        bits = [f"{seconds:.1f}s"]
        if usage.get("input"):
            cached = usage.get("cached", 0) / max(1, usage["input"])
            bits.append(f"{tokens(usage['input'])} in" + (f" · {cached:.0%} cached" if cached >= 0.05 else ""))
        if usage.get("output"):
            bits.append(f"{tokens(usage['output'])} out")
        if usage.get("credits"):
            bits.append(f"{usage['credits']:.2f} credits")
        print("  " + gold("✦", bold=False) + " " + paint(SHORT.get(choice.provider, choice.provider), SOFT)
              + paint(" · " + " · ".join(bits), FAINT))

    # goals
    def await_crew(self):
        """Wait for delegated work without spending model turns on polling."""
        if not self.member or not self.member.outstanding():
            return True
        stop = threading.Event()
        missing_since = None
        printer = Printer("waiting for crew")
        keys = KeyWatch(stop.set)
        self.member.beat(status="working", task="waiting for crew", provider="")
        report_herdr("working", "waiting for crew")
        try:
            with keys:
                while self.member.outstanding():
                    if stop.is_set() or keys.typed.strip():
                        say("goal paused while waiting for crew; /goal resume continues it", DIM)
                        return False
                    peers = {m["name"] for m in self.member.peers()}
                    missing = [name for name, _ in self.member.outstanding() if name not in peers]
                    missing_since = (missing_since or time.monotonic()) if missing else None
                    if missing_since and time.monotonic() - missing_since >= 5:
                        say("goal paused: waiting for " + ", ".join(sorted(set(missing)))
                            + " to reconnect. Tasks remain queued; /goal resume tries again", BAD)
                        return False
                    time.sleep(0.1)
            return True
        except KeyboardInterrupt:
            say("goal paused while waiting for crew; /goal resume continues it", DIM)
            return False
        finally:
            self.typed += keys.typed
            printer.close()
            self.member.beat(status="idle", task="", provider="")
            report_herdr("idle")

    def pursue(self, goal, start=1, steps=None):
        """Take turns toward goal until a provider says it is done or blocked."""
        self.convo.goal = goal
        self.convo.goal_state = "running"
        if not self.save_progress():
            self.convo.goal_state = "blocked"
            self.show_goal_state()
            return "blocked"
        if self.member:
            self.member.beat(goal=goal)
        outcome = "paused"
        import itertools
        turns = itertools.count(start) if steps is None else range(start, start + steps)
        for step in turns:
            self.collect_replies()
            if not self.await_crew():
                break
            self.collect_replies()
            if self.member and self.member.role == "crew" and self.pending_tasks():
                say("pausing the goal for a task from the lead; /goal resume continues it", DIM)
                break
            print("  " + gold(f"◎ goal · step {step}") + paint("   " + ap.clip(goal.splitlines()[0], width() - 24), SOFT))
            crew_line = self.crew_hint()
            if crew_line:
                print("  " + paint(crew_line, FAINT))
            idle = [m["name"] for m in self.member.peers() if m.get("status") not in {"working", "blocked"}] \
                if self.member and self.member.role == "lead" else []
            reply, status = self.run(goal_prompt(goal, step, idle), route_text=goal)
            if status == "none":
                say(self.routing_failure() + "; /goal resume retries routing", BAD)
                outcome = status
                break
            if status != "ok":
                say(f"goal paused ({status}); /goal resume picks it up", DIM)
                outcome = status
                break
            verdict, detail = goal_verdict(reply)
            late_replies = self.collect_replies()
            if verdict == "COMPLETE":
                if self.member and (self.member.outstanding() or late_replies):
                    say("crew work needs integration; checking it before completing the goal", DIM)
                    continue
                # Saved before it is announced: a Star stopped in between must
                # not reopen a finished goal and spend another turn on it.
                self.convo.goal, self.convo.goal_state = "", ""
                self.convo.save()
                say("goal complete", BRIGHT)
                outcome = "complete"
                break
            if verdict == "BLOCKED":
                say(f"goal blocked: {detail.strip() or 'the agent needs you'}; answer, then /goal resume", BAD)
                outcome = "blocked"
                break
            if self.typed.strip():
                say("you typed during the goal; pausing so you can send it (/goal resume to go on)", DIM)
                break
            print()
        else:
            say(f"paused after {steps} steps; /goal resume keeps going", DIM)
        self.convo.goal_state = "blocked" if outcome == "blocked" else "paused" if self.convo.goal else ""
        self.convo.save()
        self.show_goal_state()
        return outcome

    def collect_replies(self):
        """Fold crew replies that arrived during a goal into the next step."""
        if not self.member:
            return 0
        count = 0
        for message in list(self.member.inbox()):
            if message.get("kind") == "reply":
                before = len(self.convo.notes)
                self.handle(message)
                count += len(self.convo.notes) - before
        return count

    def pending_tasks(self):
        if not self.member:
            return []
        return [m for m in self.member.inbox() if m.get("kind") != "reply"]

    # mesh
    def handle(self, message):
        """A message from the mesh: run a peer's task and answer it, or show a reply."""
        sender = message.get("from", "a peer")
        if message.get("kind") == "reply":
            if message.get("id") and message["id"] in self.convo.mesh_replies:
                if self.member:
                    self.member.take(message)
                return
            print("  " + paint("↩ ", SKY, True) + gold(sender) + paint(" replied to: ", SOFT)
                  + paint(ap.clip(message.get("task", ""), width() - 30), FAINT))
            show_text(message.get("text", ""))
            self.convo.notes.append(f"{sender} replied to \"{message.get('task', '')[:200]}\":\n{message.get('text', '')[:3000]}")
            if message.get("id"):
                self.convo.mesh_replies.append(message["id"])
            if not self.convo.save():
                raise OSError("couldn't save the crew reply; it remains in the inbox")
            if self.member:
                self.member.take(message)
            return
        mid = message.get("id")
        previous = self.convo.mesh_tasks.get(mid) if mid else None
        if previous and previous.get("status") == "answered":
            star_mesh.reply(message, previous["reply"], self.member.name if self.member else "star")
            if self.member:
                self.member.take(message)
            return
        print("  " + paint("◂ ", SKY, True) + gold(sender) + paint(" asks:", SOFT))
        show_text(message.get("text", ""), limit=12)
        text = message.get("text", "")
        word, _, rest = text[1:].partition(" ") if text.startswith("/") else ("", "", "")
        custom = star_commands.find(self.convo.cwd).get(word.lower()) if word else None
        if custom:
            # A lead can name a command or skill, as it would type it into a native pane.
            try:
                text = star_commands.expand(*custom, rest)
            except OSError:
                pass
        framed = (f"[StarCLI mesh] Task from {sender}, a peer StarCLI in this herdr tab:\n\n{text}\n\n"
                  "Do it, then finish with a short report (at most 15 lines): what you did or found, files "
                  "changed, open questions.")
        if previous:
            framed += ("\n\n[StarCLI recovery] An earlier attempt at this same task stopped before its reply was "
                       "delivered. Inspect the current files and conversation before continuing. Commands or "
                       "edits may already have happened; verify their effects before repeating them.")
        if mid:
            self.convo.mesh_tasks[mid] = {"status": "running"}
            if not self.convo.save():
                raise OSError("couldn't save the task receipt; it remains in the inbox")
        reply, status = self.run(framed, route_text=message.get("text", ""))
        if status == "none":
            reply = f"[{self.member.name if self.member else 'star'} stopped: {self.routing_failure()}]\n{reply}"
        elif status != "ok":
            reply = f"[{self.member.name if self.member else 'star'} could not finish this task: {status}]\n{reply}"
        reply = reply.strip()[-6000:]
        if mid:
            self.convo.mesh_tasks[mid] = {"status": "answered", "reply": reply}
            if not self.convo.save():
                raise OSError("couldn't save the task result; it remains in the inbox")
        star_mesh.reply(message, reply, self.member.name if self.member else "star")
        if self.member:
            self.member.take(message)
        say(f"answered {sender}", DIM)

    # the input box
    def hint(self):
        if self.plan:
            left = ("⏸ plan mode on", PLAN)
        else:
            left = ("⏵⏵ " + self.permissions_label(), DIM)
        bits = [f"pinned to {self.pinned}" if self.pinned else "auto routing"]
        if self.tier:
            bits.append(self.tier)
        if self.convo.current:
            bits.append(f"with {self.convo.current}")
        if self.member and self.member.role != "solo":
            m = self.member
            bits.append(m.name if m.name == m.role else f"{m.name} · {m.role}")
        crew = self.crew_hint()
        if crew:
            bits.append(crew)
        if self.convo.goal:
            bits.append("goal blocked" if self.convo.goal_state == "blocked" else "goal paused")
        return left[0] + " (shift+tab to cycle)", " · ".join(bits)

    def crew_hint(self):
        """How the lead's crew is occupied, for the hint line. Empty when this pane is not leading."""
        if not self.member or self.member.role != "lead":
            return ""
        peers = [m for m in self.member.peers() if m.get("role") != "lead"]
        if not peers:
            return ""
        counts = {state: sum(m.get("status") == state for m in peers) for state in ("working", "blocked")}
        counts["idle"] = len(peers) - counts["working"] - counts["blocked"]
        return "crew " + " · ".join(f"{counts[state]} {state}" for state in ("idle", "working", "blocked")
                                    if counts[state])

    def toggle_plan(self):
        self.plan = not self.plan
        self.convo.save()
        return "plan mode: providers read and propose, nothing changes" if self.plan else self.permissions_label()

    def permissions_label(self):
        return "bypass permissions on" if all(bypass_enabled(n) for n in self.names) else "provider permission settings"


# ---- commands -------------------------------------------------------------------

COMMANDS = [
    ("help", "", "every command and key"),
    ("goal", "<objective> | resume | clear", "keep working, provider after provider, until it's done"),
    ("plan", "", "toggle plan mode (also shift+tab)"),
    ("status", "", "mode, routing, conversation, sessions and peers"),
    ("usage", "", "refresh and show every limit, and Star's combined estimate"),
    ("cost", "", "tokens, credits and time this session, per provider"),
    ("route", "<prompt>", "how a prompt would be routed, without running it"),
    ("why", "", "the scores behind the last turn"),
    ("use", "<provider>", "pin every turn to one provider (/auto undoes it)"),
    ("model", "<provider> [tier]", "pin a provider and, optionally, its tier"),
    ("tier", "<light|standard|heavy>", "pin the model tier (/tier alone resets it)"),
    ("auto", "", "route every turn automatically again"),
    ("models", "", "each provider's light, standard and heavy model"),
    ("crew", "[n]", "open n crew StarCLIs beside this one (herdr) and lead them"),
    ("panel", "", "open the usage panel beside this one (herdr)"),
    ("peers", "", "the other Star CLIs in this herdr tab and what they're doing"),
    ("send", "<star> <task>", "hand a peer Star CLI a task; its reply shows up here"),
    ("open", "[provider]", "continue in that CLI's own interface"),
    ("new", "", "start a new conversation"),
    ("clear", "", "clear the screen and start a new conversation"),
    ("compact", "[focus]", "summarize the conversation and carry on from the summary"),
    ("resume", "[n]", "pick up an earlier conversation"),
    ("export", "[file]", "save this conversation as Markdown"),
    ("init", "", "have an agent write AGENTS.md for this project"),
    ("review", "[focus]", "review the uncommitted changes"),
    ("memory", "", "edit AGENTS.md (# <note> adds a line to it)"),
    ("doctor", "", "check providers, meters and the mesh"),
    ("cd", "<dir>", "move to another folder (new conversation)"),
    ("quit", "", "leave StarCLI"),
    ("exit", "", "leave StarCLI"),
]


def load_counts():
    try:
        return json.loads(COUNTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_counts(counts):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        COUNTS.write_text(json.dumps(counts), encoding="utf-8")
    except OSError:
        pass


def load_history(limit=500):
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]
    except (OSError, ValueError):
        return []


def add_history(message):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(HISTORY, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(message) + "\n")
    except OSError:
        pass


def memory_file(cwd):
    for name in ("AGENTS.md", "CLAUDE.md"):
        if (Path(cwd) / name).exists():
            return Path(cwd) / name
    return Path(cwd) / "AGENTS.md"


class Commands:
    def __init__(self, star):
        self.star = star

    def values(self, command):
        """Suggestions for a command's argument, for the input box's menu."""
        star = self.star
        if command in ("use", "model", "open"):
            out = []
            for name in star.names:
                found = star_router.windows(ap.USAGE.get(name), time.time())
                note = ""
                if found:
                    label, pct, resets, _ = max(found, key=lambda w: w[1])
                    note = f"{100 - pct:.0f}% of {label} left"
                out.append((name, note))
            return out
        if command == "tier":
            return [("light", "cheap models for small things"), ("standard", "the default"),
                    ("heavy", "the strongest models")]
        if command == "goal":
            return [("resume", f"continue: {star.convo.goal[:50]}" if star.convo.goal else "continue the paused goal"),
                    ("clear", "drop the goal")]
        if command == "resume":
            return [(str(k + 1), f"{title}  ·  {turns} turns  ·  {tilde(cwd)}")
                    for k, (_, title, turns, _, cwd) in enumerate(saved_conversations(20))]
        if command == "send":
            return [(m["name"], f"{m.get('role', '')} · {m.get('status', '')}") for m in
                    (star.member.peers() if star.member else [])]
        return []

    def __call__(self, line):
        """Handle a /command. Returns False to quit."""
        star = self.star
        word, _, rest = line[1:].partition(" ")
        word, rest = word.lower(), rest.strip()
        handler = getattr(self, "do_" + word, None)
        if word in ("q", "quit", "exit"):
            return False
        if handler is None:
            custom = star_commands.find(star.convo.cwd).get(word)
            if custom:
                # A provider's own command or skill, followed by whichever provider takes the turn.
                try:
                    prompt = star_commands.expand(*custom, rest)
                except OSError as exc:
                    say(f"couldn't read /{word}: {exc}", BAD)
                    return True
                say(f"/{word}: {tilde(str(custom[1]))}")
                star.run(prompt)
                return True
            close = [n for n, _, _ in COMMANDS if star_input.fuzzy(word, n) is not None][:3]
            say(f"unknown command /{word}" + (f"; did you mean {', '.join('/' + c for c in close)}?" if close else ""), BAD)
            return True
        handler(rest)
        return True

    def do_help(self, rest):
        print()
        print("  " + gold("Star CLI") + paint("  type a request; StarCLI picks the provider and model for each turn", SOFT))
        print()
        for name, args, desc in COMMANDS:
            if name == "exit":
                continue
            label = "/" + name + (" " + args if args else "")
            print("  " + paint(label.ljust(34), BRIGHT) + paint(desc, SOFT))
        print()
        found = [n for n in star_commands.find(self.star.convo.cwd) if n not in {c[0] for c in COMMANDS}]
        if found:
            print("  " + paint("/<name> [request]".ljust(34), BRIGHT)
                  + paint(f"a provider command or skill ({len(found)} here, e.g. /{sorted(found)[0]})", SOFT))
            print()
        for keys, desc in (("!<command>", "run a shell command"), ("# <note>", "add a line to AGENTS.md"),
                           ("@path", "mention a file (tab completes)"),
                           ("ctrl+v / drop a file", "attach an image ([Image #1]); goes to Claude or Codex"),
                           ("shift+tab", "bypass permissions ⇄ plan mode"),
                           ("esc", "interrupt a turn · twice clears the input"), ("\\ then enter", "a new line"),
                           ("↑ ↓", "history, or the suggestion menu"), ("ctrl-c twice / ctrl-d", "quit")):
            print("  " + paint(keys.ljust(34), DEEP) + paint(desc, SOFT))
        print()

    def do_goal(self, rest):
        star = self.star
        if rest in ("", "status"):
            say(f"goal: {star.convo.goal}" if star.convo.goal else "no goal; /goal <objective> starts one")
            return
        if rest == "clear":
            star.convo.goal = ""
            star.convo.goal_state = ""
            star.convo.save()
            star.show_goal_state()
            say("goal cleared")
            return
        if rest == "resume":
            if not star.convo.goal:
                say("no goal to resume")
                return
            steps = sum(1 for t in star.convo.turns if t["user"].startswith("[StarCLI goal"))
            star.down.clear()  # an explicit retry may follow a quota reset or a repaired login
            star.pursue(star.convo.goal, start=steps + 1)
            return
        star.pursue(rest)

    def do_plan(self, rest):
        say(self.star.toggle_plan(), PLAN if self.star.plan else SOFT)

    def do_status(self, rest):
        star, convo = self.star, self.star.convo
        rows = [("version", f"Star CLI {VERSION}"), ("folder", tilde(convo.cwd)),
                ("mode", "plan (read only)" if star.plan else star.permissions_label()),
                ("routing", f"pinned to {star.pinned}" if star.pinned else "automatic"),
                ("tier", star.tier or "chosen per prompt"),
                ("conversation", f"{convo.title}  ·  {len(convo.turns)} turns  ·  {convo.id}"),
                ("goal", convo.goal or "none"),
                ("sessions", ", ".join(f"{p} {s[:8]}…" for p, s in convo.sessions.items()) or "none yet"),
                ("providers", ", ".join(star.names) + (f"  (set aside: {', '.join(star.down)})" if star.down else ""))]
        if star.member:
            peers = star.member.peers()
            rows.append(("mesh", f"{star.member.name} ({star.member.role})" +
                         (f"  ·  peers: {', '.join(m['name'] for m in peers)}" if peers else "  ·  no peers")))
        for label, value in rows:
            print("  " + paint(label.ljust(14), SOFT) + paint(value, INK))

    def do_usage(self, rest):
        refresh_meters(self.star.names, older_than=60, wait=4.0)
        print_usage(self.star.names)

    def do_cost(self, rest):
        if not self.star.totals:
            say("no turns yet this session")
            return
        for name, row in self.star.totals.items():
            bits = [f"{row['turns']} turns", f"{row['seconds']:.0f}s"]
            if row["input"]:
                bits.append(f"{tokens(row['input'])} in ({row['cached'] / max(1, row['input']):.0%} cached)")
            if row["output"]:
                bits.append(f"{tokens(row['output'])} out")
            if row["credits"]:
                bits.append(f"{row['credits']:.2f} credits")
            print(f"  {paint('●', tuple(agent(name)['color']))} {paint(name.ljust(12), INK)}{paint(' · '.join(bits), SOFT)}")

    def do_route(self, rest):
        self.star.explain(rest or None)

    def do_why(self, rest):
        self.star.explain()

    def provider(self, word):
        return next((n for n in self.star.names if n.lower() == word.lower()
                     or SHORT.get(n, "").lower() == word.lower()), None)

    def do_use(self, rest):
        name = self.provider(rest.split(" ")[0]) if rest else None
        if not name:
            say("use one of: " + ", ".join(self.star.names))
            return
        self.star.pinned = name
        self.star.down.pop(name, None)
        self.star.convo.save()
        say(f"every turn runs on {name} until /auto")

    def do_model(self, rest):
        words = rest.split()
        self.do_use(words[0] if words else "")
        if len(words) > 1:
            self.do_tier(words[1])

    def do_tier(self, rest):
        self.star.tier = rest if rest in star_router.TIERS else None
        self.star.convo.save()
        say(f"tier {self.star.tier or 'chosen per prompt'}")

    def do_auto(self, rest):
        self.star.pinned, self.star.tier = None, None
        self.star.down.clear()
        self.star.convo.save()
        say("routing every turn automatically")

    def do_models(self, rest):
        for name in self.star.names:
            tiers = self.star.config[name]["tiers"]
            print(f"  {paint('●', tuple(agent(name)['color']))} {paint(name.ljust(12), INK)}"
                  + "  ".join(paint(t[:1], DIM) + " " + paint(tiers[t]["model"], SOFT) for t in tiers))

    def terminal_ids(self):
        """pane id -> herdr terminal id, for recording a layout. Empty when herdr can't be asked."""
        try:
            listed = self.herdr("pane", "list", "--workspace", os.environ.get("HERDR_WORKSPACE_ID", ""))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return {}
        return {p["pane_id"]: p.get("terminal_id") for p in listed.get("panes", [])
                if isinstance(p, dict) and p.get("pane_id")}

    def herdr(self, *args):
        binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
        out = subprocess.run([binary, *args], capture_output=True, text=True, timeout=15)
        if out.returncode:
            raise RuntimeError((out.stderr or out.stdout).strip() or f"herdr {args[0]} failed")
        try:
            return json.loads(out.stdout).get("result", {})
        except ValueError:
            return {}

    def split(self, pane, direction, ratio, line):
        made = self.herdr("pane", "split", pane, "--direction", direction, "--ratio", str(ratio),
                          "--cwd", self.star.convo.cwd, "--no-focus")
        new = (made.get("pane") or {}).get("pane_id")
        if new:
            self.herdr("pane", "run", new, line)
        return new

    def do_crew(self, rest):
        """Lay out crew StarCLIs beside this one, as herdr-ai --setup star does, and lead them."""
        star, me = self.star, os.environ.get("HERDR_PANE_ID")
        if not (me and os.environ.get("HERDR_ENV") == "1"):
            say("/crew opens panes with herdr; run StarCLI inside herdr (or use herdr-ai --setup star)", BAD)
            return
        n = max(1, min(6, int(rest))) if rest.isdigit() else 3
        taken = {m["name"] for m in star.member.peers()} | {star.member.name}
        names = []
        k = 1
        while len(names) < n:
            if f"crew{k}" not in taken:
                names.append(f"crew{k}")
            k += 1
        import herdr_ai
        try:
            column = self.split(me, "right", 0.5, herdr_ai.picker_command(herdr_ai.star_member_args(names[0], "crew")))
            panes = [column]
            for k, name in enumerate(names[1:], start=1):
                # each split takes an even share of what is left of the column
                panes.append(self.split(panes[-1], "down", round(1 - 1 / (n - k + 1), 3),
                                        herdr_ai.picker_command(herdr_ai.star_member_args(name, "crew"))))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            say(f"couldn't open the crew: {exc}", BAD)
            return
        star.member.role = "lead"
        star.member.beat(role="lead")
        opened = dict(zip(names, panes))
        reason = herdr_ai.note_star_panes(me, opened, self.terminal_ids())
        if reason:
            say(f"opened {', '.join(names)}; {reason}, so herdr-ai refresh leaves them as they are", DIM)
        else:
            say(f"opened {', '.join(names)}; this StarCLI now leads them, and its agents will delegate to them")

    def do_panel(self, rest):
        me = os.environ.get("HERDR_PANE_ID")
        if not (me and os.environ.get("HERDR_ENV") == "1"):
            say("/panel opens a pane with herdr; outside herdr run: usage", BAD)
            return
        import herdr_ai
        try:
            pane = self.split(me, "down", 0.72, herdr_ai.picker_command(["--picker-usage"]))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            say(f"couldn't open the usage panel: {exc}", BAD)
            return
        if pane:
            herdr_ai.note_star_panes(me, {}, self.terminal_ids(), usage=pane, create=False)
        say("usage panel opened below; Star's card lights up while it drives a provider")

    def do_peers(self, rest):
        for line in star_mesh.describe(self.star.member.name if self.star.member else None):
            print("  " + paint(line, SOFT))

    def do_send(self, rest):
        name, _, task = rest.partition(" ")
        if not name or not task.strip():
            say("/send <star> <task>")
            return
        try:
            star_mesh.send(name, task.strip(), sender=self.star.member.name if self.star.member else "you",
                           sender_is_star=bool(self.star.member))
        except LookupError as exc:
            say(str(exc), BAD)
            return
        say(f"sent to {name}; its reply will show up here")

    def do_open(self, rest):
        star = self.star
        if star.plan:
            say("the native interface uses its picker permission profile; leave Star plan mode with /plan before /open", BAD)
            return
        name = self.provider(rest) if rest else star.convo.current
        if not name:
            say("nothing to open yet; /open <provider> starts a fresh one")
            return
        argv = star.engines[name].native(star.convo.sessions.get(name))
        say(f"opening {name}; exit it to come back to StarCLI")
        if star.member:
            star.member.beat(status="working", provider=name, task="native interface", ready=False, draft=False)
        report_herdr("working", f"{name}: native interface")
        try:
            subprocess.call(argv, cwd=star.convo.cwd, env=star_engines.child_env())
        except (OSError, KeyboardInterrupt):
            pass
        finally:
            if star.member:
                star.member.beat(provider="", task="", ready=False)
            star.show_goal_state()
        say("back in StarCLI. Turns taken there stay in that provider's session.")

    def do_new(self, rest):
        cwd = self.star.convo.cwd
        self.star.convo.save()
        self.star.bind(Conversation({"cwd": cwd}))
        self.star.down.clear()
        say("new conversation")

    def do_clear(self, rest):
        self.do_new(rest)
        self.star.cleared = True

    def do_compact(self, rest):
        star = self.star
        if not star.convo.turns:
            say("nothing to compact yet")
            return
        ask = ("[StarCLI] Write a compact summary of this conversation for whoever continues it: the user's goals, "
               "decisions made, what was done (files, commands), what is still open, and anything to watch out "
               "for. Plain prose and short lists, at most 40 lines." + (f" Focus on: {rest}" if rest else ""))
        reply, status = star.run(ask, route_text="summarize this conversation")
        if status != "ok" or not reply.strip():
            say("couldn't compact; the conversation is unchanged", BAD)
            return
        old = star.convo
        old.save()
        star.bind(Conversation({"cwd": old.cwd, "goal": old.goal, "goal_state": old.goal_state,
                                "requests": old.requests,
                                "summary": ((old.summary + "\n\n") if old.summary else "") + reply.strip()}))
        say("compacted: new turns start from the summary")

    def do_resume(self, rest):
        saved = saved_conversations(20)
        if not saved:
            say("no saved conversations")
            return
        if not rest:
            for k, (_, title, turns, updated, cwd) in enumerate(saved):
                print(f"  {paint(str(k + 1).rjust(2), BRIGHT)}  {paint(ap.clip(title, 50).ljust(50), INK)}  "
                      f"{paint(f'{turns} turns', SOFT)}  {paint(tilde(cwd), FAINT)}")
            say("/resume <n> picks one")
            return
        pick = saved[int(rest) - 1] if rest.isdigit() and 0 < int(rest) <= len(saved) else \
            next((s for s in saved if s[0] == rest), None)
        convo = Conversation.load(pick[0]) if pick else None
        if not convo:
            say("no such conversation", BAD)
            return
        self.star.convo.save()
        try:
            self.star.bind(convo)
        except star_sessions.ConversationBusy as exc:
            say(str(exc), BAD)
            return
        convo = self.star.convo
        apply_saved_routing(self.star)
        if Path(convo.cwd).is_dir():
            os.chdir(convo.cwd)
        say(f"resumed: {convo.title} ({len(convo.turns)} turns)")
        if convo.turns:
            last = convo.turns[-1]
            print("  " + paint("› ", GOLD[0], True) + paint(ap.clip(last["user"].splitlines()[0], width() - 6), INK))
            show_text(last["reply"], limit=12)

    def do_export(self, rest):
        convo = self.star.convo
        path = Path(rest).expanduser() if rest else Path(convo.cwd) / f"star-{convo.id}.md"
        lines = [f"# {convo.title}", ""]
        if convo.summary:
            lines += ["## Summary of earlier turns", "", convo.summary, ""]
        for turn in convo.turns:
            lines += [f"## You", "", turn["user"], "", f"## {turn['provider']}", "", turn["reply"], ""]
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            say(f"couldn't write {path}: {exc}", BAD)
            return
        say(f"saved {tilde(str(path))}")

    def do_init(self, rest):
        self.star.run("Study this repository and create or update AGENTS.md at its root: what the project is, how "
                      "it is laid out, how to build, test and run it, the conventions to follow, and anything an AI "
                      "agent working here should know. Keep it concise and accurate; keep what is already there "
                      "if it is still right.", route_text="write documentation for the codebase AGENTS.md")

    def do_review(self, rest):
        self.star.run("Review the uncommitted changes in this repository (git diff and git diff --staged, plus new "
                      "untracked files) for bugs, regressions, security problems and unclear code. List findings "
                      "by severity with file and line; say plainly if there is nothing worth changing."
                      + (f" Focus on: {rest}" if rest else ""), route_text="review my diff")

    def do_memory(self, rest):
        path = memory_file(self.star.convo.cwd)
        if not path.exists():
            path.write_text("# Notes for AI agents\n\n", encoding="utf-8")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        try:
            subprocess.call([*editor.split(), str(path)])
        except OSError as exc:
            say(f"couldn't open {editor}: {exc}", BAD)

    def do_doctor(self, rest):
        star = self.star
        now = time.time()
        for name in star.config:
            engine = star.engines.get(name)
            with ap.USAGE_LOCK:
                entry = ap.USAGE.get(name)
            if not engine:
                print(f"  {paint('·', FAINT)} {paint(name.ljust(12), FAINT)}{paint('not installed', FAINT)}")
                continue
            age = now - float((entry or {}).get("fetched", 0) or 0)
            meter = "no meter reading (is it signed in?)" if not entry else f"meter read {ap.until(now + age) or 'just now'} ago"
            state = paint("✓", (120, 205, 145)) if entry and name not in star.down else paint("!", (235, 185, 85))
            print(f"  {state} {paint(name.ljust(12), INK)}{paint(tilde(engine.exe), SOFT)}  {paint(meter, FAINT)}"
                  + (paint(f"  set aside: {star.down[name]}", BAD) if name in star.down else ""))
        overrides = star_engines.codex_compat_overrides()
        if overrides:
            print("  " + paint("! Codex config has values this codex rejects; StarCLI passes: " + " ".join(overrides[1::2]), (235, 185, 85)))
        print("  " + paint("  herdr " + ("found" if shutil.which("herdr") else "not found (needed for herdr-ai --setup star)"), SOFT))
        if star.member:
            print("  " + paint(f"  mesh: {star.member.name} ({star.member.role}), {len(star.member.peers())} peers", SOFT))

    def do_cd(self, rest):
        target = Path(rest or "~").expanduser().resolve()
        if not target.is_dir():
            say(f"no such folder: {target}", BAD)
            return
        self.star.convo.save()
        self.star.bind(Conversation({"cwd": str(target)}))
        os.chdir(target)
        if self.star.member:
            self.star.member.beat(cwd=str(target))
        say(f"{tilde(str(target))} (new conversation)")


# ---- the interactive terminal ---------------------------------------------------------

def mode_line(star):
    return "shift+tab: plan mode · /help · esc interrupts"


def plain_input(star):
    """Without a terminal for the input box: a simple prompt."""
    try:
        return input("› ")
    except KeyboardInterrupt:
        print()
        return ""


def interactive(star, first=None, goal=None):
    commands = Commands(star)
    counts = load_counts()
    history = load_history()
    colors = {"mark": GOLD[0], "text": INK, "ghost": (110, 104, 92), "rule": DIM, "rule_end": (70, 66, 60),
              "note": FAINT, "mode": SOFT, "label": BRIGHT, "pick": (30, 26, 18), "select": (240, 200, 110)} if COLOR else {}
    builtin = {n for n, _, _ in COMMANDS}
    custom = [(n, "[request]", star_commands.describe(kind, path))
              for n, (kind, path) in sorted(star_commands.find(star.convo.cwd).items()) if n not in builtin]
    editor = star_input.Editor([(n, a, d) for n, a, d in COMMANDS] + custom, commands.values, [h for h in history if isinstance(h, str)],
                               counts, colors, image_dir=str(STATE / "images")) if star_input.supported() else None
    sane_terminal()
    star.cleared = True
    logo_live = False
    pending = [first] if first else []
    if goal:
        pending.append("/goal " + goal)
    elif star.convo.goal and star.convo.goal_state == "running":
        pending.append("/goal resume")
    last_mail, last_beat = 0.0, 0.0
    start = time.time()

    def ready(text):
        if star.member:
            star.member.beat(ready=True, draft=bool(text))
        star.show_goal_state()

    def changed(text):
        if star.member and star.member.state.get("draft") != bool(text):
            star.member.beat(draft=bool(text))

    def idle():
        nonlocal last_mail, last_beat
        now = time.time()
        if logo_live and COLOR:
            twinkle(now - start)
        if star.member and now - last_beat > 5:
            last_beat = now
            star.member.beat()
        if star.member and now - last_mail > 1:
            last_mail = now
            waiting = star.member.inbox()
            if waiting and not editor.text:
                return waiting[0]
        return None

    try:
        while True:
            if star.cleared:
                star.cleared = False
                intro(star.convo.cwd, star.names, mode_line(star))
                logo_live = True
                if star.convo.turns:
                    say(f"continuing: {star.convo.title} · {len(star.convo.turns)} turns, last with "
                        f"{star.convo.current}", FAINT)
                    logo_live = False
            if star.member:
                waiting = star.member.inbox()
                if waiting:
                    logo_live = False
                    star.handle(waiting[0])
                    print()
                    continue
            if pending:
                message = pending.pop(0)
                star_input_echo(message)
            elif editor:
                try:
                    message = editor.read(star.hint, on_mode=star.toggle_plan, idle=idle, cwd=star.convo.cwd,
                                          initial=star.typed, on_ready=ready, on_change=changed)
                    if star.member:
                        star.member.beat(ready=False, draft=False)
                    star.typed = ""
                except star_input.Wake as wake:
                    if star.member:
                        star.member.beat(ready=False, draft=bool(wake.text))
                    star.typed = wake.text
                    logo_live = False
                    star.handle(wake.payload)
                    print()
                    continue
            else:
                message = plain_input(star)
            logo_live = False
            message = message.strip()
            if not message:
                continue
            add_history(message)
            if message.startswith("/"):
                name = message[1:].split(" ")[0].lower()
                counts[name] = counts.get(name, 0) + 1
                save_counts(counts)
                if not commands(message):
                    break
            elif message.startswith("!"):
                subprocess.call(message[1:], shell=True, cwd=star.convo.cwd)
            elif message.startswith("#"):
                path = memory_file(star.convo.cwd)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(("" if not path.exists() or path.read_text(encoding="utf-8").endswith("\n") else "\n")
                                 + "- " + message[1:].strip() + "\n")
                say(f"noted in {path.name}")
            else:
                star.run(message, images=editor.attached(message) if editor else star_input.image_paths(message))
            print()
    except EOFError:
        pass
    finally:
        star.convo.save()


def sane_terminal():
    """Undo what a killed TUI can leave behind in this pane before drawing: raw tty
    flags (lines would stair-step), mouse reporting and keyboard protocols."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    try:
        subprocess.run(["stty", "sane"], stdin=sys.stdin, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass
    sys.stdout.write("\x1bc")   # full reset: modes, keyboard protocol, mouse reporting
    sys.stdout.flush()


def shlex_join(words):
    return " ".join(shlex.quote(w) for w in words)


def apply_saved_routing(star):
    """A resumed conversation keeps the pin, tier, and plan mode from that session.

    Launch flags such as --plan and --use describe how the pane was first opened.
    Once the session has saved its own routing, that is what comes back."""
    saved = star.convo.routing if isinstance(star.convo.routing, dict) else None
    if not saved:
        return False
    star.pinned = saved.get("pinned") or None
    tier = saved.get("tier")
    star.tier = tier if tier in star_router.TIERS else None
    star.plan = bool(saved.get("plan"))
    return True


def star_input_echo(message):
    lines = message.split("\n")
    print(paint("› ", GOLD[0], True) + paint(lines[0], INK))
    for line in lines[1:]:
        print("  " + paint(line, INK))


# ---- command line -------------------------------------------------------------------

def main(argv=None):
    with ExitStack() as stack:
        lease = stack.enter_context(star_sessions.Lease(CONVERSATIONS / "locks"))
        try:
            return cli_main(argv, lease)
        except star_sessions.ConversationBusy as exc:
            sys.exit(f"star: {exc}")


def cli_main(argv=None, lease=None):
    import argparse
    parser = argparse.ArgumentParser(prog="star", description="StarCLI: one terminal for every provider, balanced limits.")
    parser.add_argument("prompt", nargs="*", help="a first message")
    parser.add_argument("-p", "--print", dest="once", action="store_true", help="run the prompt and exit")
    parser.add_argument("-c", "--continue", dest="resume", action="store_true", help="continue the last conversation")
    parser.add_argument("--resume-id", metavar="ID", help="continue a saved conversation (as herdr does after a restart)")
    parser.add_argument("--goal", metavar="OBJECTIVE", help="start working toward a goal")
    parser.add_argument("--route", action="store_true", help="show how the prompt would be routed and exit")
    parser.add_argument("--usage", action="store_true", help="show the meters and exit")
    parser.add_argument("--use", metavar="PROVIDER", help="pin every turn to one provider")
    parser.add_argument("--tier", choices=star_router.TIERS, help="pin the model tier")
    parser.add_argument("--plan", action="store_true", help="start in plan mode (read only)")
    parser.add_argument("--name", help="this terminal's name in the mesh")
    parser.add_argument("--role", choices=("lead", "crew", "solo"), default="solo", help="its role in the mesh")
    parser.add_argument("--peers", action="store_true", help="list the Star CLIs in this herdr tab")
    parser.add_argument("--send", metavar="NAME", help="hand the prompt to that Star CLI as a task")
    parser.add_argument("--wait", action="store_true", help="with --send: wait for the reply and print it")
    parser.add_argument("--timeout", type=float, default=1800, help="with --send --wait: seconds to wait")
    args = parser.parse_args(argv)

    prompt = " ".join(args.prompt).strip()
    # --goal carries its own objective; reading stdin then would block a shell
    # tool whose stdin is a pipe that never closes.
    if not prompt and not args.goal and (args.once or args.route or args.send) and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()   # star -p < brief.md

    if args.peers:
        for line in star_mesh.describe(os.environ.get("STAR_NAME")):
            print(line)
        return 0
    if args.send:
        if not prompt:
            sys.exit("star --send needs a task")
        sender = os.environ.get("STAR_NAME") or "terminal"
        try:
            result = star_mesh.send(args.send, prompt, sender=sender, wait=args.wait, timeout=args.timeout,
                                    sender_is_star=bool(os.environ.get("STAR_NAME")) and not args.wait)
        except LookupError as exc:
            sys.exit(f"star: {exc}")
        if args.wait:
            if result is None:
                sys.exit(f"star: {args.send} did not answer within {args.timeout:.0f}s")
            print(result)
        else:
            print(f"sent to {args.send}" + ("; the reply will reach your StarCLI" if os.environ.get("STAR_NAME") else ""))
        return 0

    ap.load_usage()
    convo = Conversation.load(args.resume_id) if args.resume_id else \
        Conversation.load_last() if args.resume else Conversation()
    if convo is None:
        sys.exit("star: saved conversation is missing or invalid; start without resume flags for a new one")
    if (args.resume or args.resume_id) and convo.cwd and Path(convo.cwd).is_dir():
        os.chdir(convo.cwd)
    star = Star(convo, tier=args.tier, plan=args.plan, lease=None if args.usage or args.route else lease)
    resumed = bool(args.resume or args.resume_id) and apply_saved_routing(star)
    if args.use and not resumed:
        if args.usage or args.route:
            star.pinned = Commands(star).provider(args.use)
            if not star.pinned:
                sys.exit("star: unknown provider " + args.use)
        else:
            Commands(star)("/use " + args.use)
    if not star.names:
        sys.exit("star: no supported provider CLI is installed (Claude, Codex, Cursor, Kiro, Antigravity).")

    if args.usage:
        refresh_meters(star.names, older_than=60, wait=4.0)
        print_usage(star.names)
        return 0
    if args.route:
        if not prompt:
            sys.exit("star --route needs a prompt")
        refresh_meters(star.names, wait=2.0)
        star.explain(prompt)
        return 0
    refresh_meters(star.names, wait=1.5 if args.once else 0.0)
    if args.once:
        if not prompt and not args.goal:
            sys.exit("star -p needs a prompt")
        if args.name or args.role != "solo":
            star.member = star_mesh.Member(args.name, args.role, star.convo.cwd)
            os.environ["STAR_NAME"] = star.member.name
        try:
            if args.goal:
                status = star.pursue(args.goal)
            else:
                _, status = star.run(prompt)
        finally:
            star.convo.save()
            if star.member:
                star.member.leave()
            report_herdr("release")
        return {"ok": 0, "complete": 0, "interrupted": 130, "blocked": 2, "paused": 3}.get(status, 1)

    star.member = star_mesh.Member(args.name, args.role, star.convo.cwd)
    star.convo.save(force=True)
    os.environ["STAR_NAME"] = star.member.name   # so the agents' own `star --send` knows who is asking
    global RESUME
    RESUME = lambda: ["starcli", "--resume-id", star.convo.id, "--name", star.member.name,
                      "--role", star.member.role]
    report_herdr("blocked" if star.convo.goal_state == "blocked" else "unknown")
    try:
        interactive(star, prompt or None, args.goal)
    finally:
        star.member.leave()
        report_herdr("release")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
