"""Keyboard scheduler views, rendered with the selector's own animated palette."""

import calendar
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import re
import textwrap
import time
from zoneinfo import ZoneInfo

import preferences
import prompt_scheduler as scheduler
import usage_panel
from platform_runtime import resolve_argv

CLOCK = preferences.register(preferences.Setting(
    "scheduler.clock", "Scheduler", "Time display", ["24h", "12h"], "24h",
    hint="24-hour military time or 12-hour AM/PM. Both formats work when typing.",
    names={"24h": "24-hour (14:30)", "12h": "12-hour (2:30 PM)"}))


def clean(text):
    """Terminal output is data, including CLI logs and pasted prompt text."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text))
    return "".join(c if c.isprintable() or c == "\n" else " " for c in text)


def clock(value):
    if preferences.get(CLOCK.key) == "12h":
        return value.strftime("%I:%M %p").lstrip("0")
    return value.strftime("%H:%M")


def when(job):
    if job.get("next_at") is None:
        return "complete"
    value = datetime.fromtimestamp(job["next_at"], ZoneInfo(job["timezone"]))
    return f"{value:%b %d, %Y} {clock(value)} {value:%Z}"


def status(job):
    if job.get("running"):
        return "RUNNING"
    if not job["enabled"]:
        return "PAUSED"
    if job["next_at"] is None:
        return (job.get("last") or {}).get("status", "complete").upper()
    if job["next_at"] <= time.time():
        return "DUE / catches up after wake"
    last = (job.get("last") or {}).get("status")
    return f"READY · last {last}" if last else "READY"


class Menu:
    def __init__(self, cwd=None):
        self.provider = 0
        self.index = 0
        self.mode = "list"
        self.jobs = []
        self.error = ""
        self.notice = ""
        self.loaded = scheduler.service_status()
        self.cwd = str(Path(cwd or Path.cwd()).resolve())
        self.profiles = scheduler.profiles()
        self.refresh_at = 0
        self.refresh()
        self.draft = None
        self.edit_id = None
        self.field = 0
        self.buffer = ""
        self.scroll = 0

    @property
    def name(self):
        return scheduler.PROVIDERS[self.provider]

    @property
    def agent(self):
        return self.profiles[self.name]

    @property
    def selected(self):
        return self.jobs[self.index] if self.index < len(self.jobs) else None

    def refresh(self):
        if self.mode != "list":
            return
        try:
            self.jobs = sorted([j for j in scheduler.read()["jobs"] if j["provider"] == self.name],
                               key=lambda j: (not j["enabled"], j["next_at"] or float("inf")))
            self.index = min(self.index, len(self.jobs))
            self.error = ""
        except (OSError, ValueError) as exc:
            self.error = str(exc)
        self.refresh_at = time.monotonic() + 2

    def editor(self, job=None):
        self.edit_id = job["id"] if job else None
        if job:
            self.draft = deepcopy(job)
        else:
            future = (datetime.now() + timedelta(hours=1)).replace(second=0, microsecond=0)
            self.draft = {"provider": self.name, "prompt": "", "cwd": self.cwd, "anchor": future.isoformat(),
                          "repeat": "once", "timezone": scheduler.local_zone()}
        if job and (job["next_at"] is None or job["next_at"] <= time.time()):
            future = datetime.now(ZoneInfo(job["timezone"])) + timedelta(hours=1)
            self.draft["anchor"] = future.replace(tzinfo=None, second=0, microsecond=0).isoformat()
        self.field = 0
        self.mode = "edit"
        self.error = ""

    def anchor(self):
        return datetime.fromisoformat(self.draft["anchor"])

    def set_anchor(self, value):
        self.draft["anchor"] = value.isoformat()

    def choose_field(self):
        if self.field in (0, 1):
            self.buffer = self.draft["prompt" if self.field == 0 else "cwd"]
            self.mode = "text"
        elif self.field == 2:
            self.day = self.anchor().date()
            self.date_buffer = ""
            self.mode = "date"
        elif self.field == 3:
            self.buffer = clock(self.anchor())
            self.mode = "time"
        elif self.field == 4:
            self.frequency = scheduler.REPEATS.index(self.draft["repeat"])
            self.mode = "repeat"
        elif self.field == 5:
            # Validate locally before starting the service or persisting anything.
            if resolve_argv(self.agent) is None:
                raise ValueError(f"{self.name} is not installed. See agent --picker-doctor.")
            if not self.draft["prompt"].strip():
                raise ValueError("Write a prompt first.")
            if not Path(self.draft["cwd"]).expanduser().is_dir():
                raise ValueError("The working directory does not exist.")
            if scheduler.wall_time(self.anchor().date(), self.anchor().time(), self.draft["timezone"]) <= time.time():
                raise ValueError("Choose a future date and time.")
            if not self.loaded:
                scheduler.install_service()
                self.loaded = True
            scheduler.save_job(self.draft, self.edit_id)
            self.mode, self.notice = "list", "Schedule saved locally."
            self.refresh()

    def handle(self, key):
        try:
            return self._handle(key)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error = str(exc)
        return None

    def _handle(self, key):
        enter = key in ("\r", "\n")
        back = key in ("esc", "\\")
        if key in ("\x03", "\x04"):
            return "quit"
        if self.mode == "text":
            if key == "esc":
                self.mode = "edit"
            elif enter:
                self.draft["prompt" if self.field == 0 else "cwd"] = self.buffer.strip()
                self.mode = "edit"
            elif key in ("\x7f", "\b"):
                self.buffer = self.buffer[:-1]
            elif key == "\x15":
                self.buffer = ""
            elif key == "\x0e" and self.field == 0:
                self.buffer += "\n"
            elif key.startswith("paste:"):
                self.buffer += clean(key[6:]).replace("\n", " " if self.field == 1 else "\n")
            elif len(key) == 1 and key.isprintable():
                self.buffer += key
        elif self.mode == "time":
            if key == "esc":
                self.mode = "edit"
            elif enter:
                value = scheduler.parse_time(self.buffer)
                self.set_anchor(datetime.combine(self.anchor().date(), value))
                self.mode = "edit"
                self.error = ""
            elif key in ("up", "down", "left", "right"):
                value = scheduler.parse_time(self.buffer)
                delta = {"up": 5, "down": -5, "left": -60, "right": 60}[key]
                value = datetime.combine(self.anchor().date(), value) + timedelta(minutes=delta)
                self.buffer = clock(value)
            elif key in ("\x7f", "\b"):
                self.buffer = self.buffer[:-1]
            elif key == "\x15":
                self.buffer = ""
            elif key.startswith("paste:"):
                self.buffer = clean(key[6:]).strip()
            elif len(key) == 1 and key in "0123456789: aApPmM":
                self.buffer += key
        elif self.mode == "date":
            if key == "esc":
                self.mode = "edit"
            elif enter:
                if self.date_buffer:
                    self.day = datetime.strptime(self.date_buffer, "%Y-%m-%d").date()
                self.set_anchor(datetime.combine(self.day, self.anchor().time()))
                self.mode, self.error = "edit", ""
            elif key in ("left", "right", "up", "down"):
                self.day += timedelta(days={"left": -1, "right": 1, "up": -7, "down": 7}[key])
                self.date_buffer = ""
            elif key in ("[", "]"):
                month = self.day.year * 12 + self.day.month - 1 + (-1 if key == "[" else 1)
                year, month = month // 12, month % 12 + 1
                self.day = self.day.replace(year=year, month=month, day=min(self.day.day, calendar.monthrange(year, month)[1]))
                self.date_buffer = ""
            elif key in ("\x7f", "\b"):
                self.date_buffer = self.date_buffer[:-1]
            elif len(key) == 1 and key in "0123456789-":
                self.date_buffer += key
        elif self.mode == "repeat":
            if key in ("up", "left", "k"):
                self.frequency = (self.frequency - 1) % len(scheduler.REPEATS)
            elif key in ("down", "right", "j", "\t"):
                self.frequency = (self.frequency + 1) % len(scheduler.REPEATS)
            elif enter:
                self.draft["repeat"] = scheduler.REPEATS[self.frequency]
                self.mode = "edit"
            elif back:
                self.mode = "edit"
        elif self.mode == "edit":
            if back:
                self.mode, self.error = "list", ""
                self.notice = "Draft discarded."
                self.refresh()
            elif key in ("up", "k"):
                self.field = (self.field - 1) % 6
            elif key in ("down", "j", "\t"):
                self.field = (self.field + 1) % 6
            elif enter or key == "right":
                self.choose_field()
        elif self.mode == "delete":
            if key in ("left", "right", "up", "down", "\t"):
                self.confirm_delete = not self.confirm_delete
            elif back:
                self.mode = "list"
            elif enter:
                if self.confirm_delete:
                    scheduler.manage(self.detail["id"], "delete")
                    self.notice = "Schedule removed. Existing logs remain local."
                self.mode = "list"
                self.refresh()
        elif self.mode == "log":
            if back or key == "q":
                self.mode = "detail"
            elif key in ("up", "k"):
                self.scroll = max(0, self.scroll - 1)
            elif key in ("down", "j"):
                self.scroll = min(max(0, len(self.log_lines) - 1), self.scroll + 1)
        elif self.mode == "detail":
            if back or key == "q":
                self.mode = "list"
                self.refresh()
            elif key == "e":
                self.editor(self.detail)
            elif key == "p":
                scheduler.manage(self.detail["id"], "toggle")
                self.mode = "list"
                self.refresh()
            elif key == "d":
                self.mode, self.confirm_delete = "delete", False
            elif key == "l":
                log = (self.detail.get("last") or {}).get("log")
                if not log or not Path(log).is_file():
                    raise ValueError("No run log yet.")
                with open(log, "rb") as handle:
                    handle.seek(max(0, Path(log).stat().st_size - 128000))
                    self.log_lines = clean(handle.read().decode("utf-8", errors="replace")).splitlines()
                self.scroll, self.mode = 0, "log"
        else:
            if key == "\t":
                self.provider = (self.provider + 1) % len(scheduler.PROVIDERS)
                self.index = 0
                self.refresh()
            elif key in ("up", "k"):
                self.index = (self.index - 1) % (len(self.jobs) + 1)
            elif key in ("down", "j"):
                self.index = (self.index + 1) % (len(self.jobs) + 1)
            elif back:
                return "agents"
            elif key == "q":
                return "quit"
            elif key == "s":
                return "settings"
            elif key in ("t", "T"):
                import usage_panel
                preferences.cycle(usage_panel.THEME.key, -1 if key == "T" else 1)
            elif key == "a":
                import usage_panel
                preferences.cycle(usage_panel.ANIMATE.key)
            elif key == "b":
                scheduler.install_service()
                self.loaded, self.notice = True, "Background dispatcher enabled."
            elif key == "n" or (enter and self.selected is None):
                self.editor()
            elif self.selected:
                if enter or key in ("e", "p", "d", "l"):
                    self.detail = deepcopy(self.selected)
                    self.mode = "detail"
                    if not enter:
                        self._handle(key)
        return None

    def content(self, width, height):
        """(text, selected) rows plus context-specific keyboard hints."""
        width = max(1, width)
        tabs = "   ".join(f"[{name}]" if i == self.provider else name for i, name in enumerate(scheduler.PROVIDERS))
        lines = [(tabs, False), ("", False)]
        hints = [" tab provider · ↑↓ move · ⏎ open · n new · s settings · \\ back ", " tab provider · ⏎ open · n new · s settings · \\ back ", " tab provider · n new · \\ back ", " \\ back ", ""]
        if self.mode == "list":
            ready = resolve_argv(self.agent)
            via = Path(ready[0]).name if ready else "not installed"
            lines += [(f"{self.name} via {via} · background {'on' if self.loaded else 'off (b enable)'}", False),
                      ("Missed times run after wake · repeats catch up once", False), ("", False)]
            for i, job in enumerate(self.jobs):
                lines += [(f"{'❯' if i == self.index else ' '} {clean(job['prompt']).splitlines()[0]}", i == self.index),
                          (f"  {when(job)} · {job['repeat']} · {status(job)}", False)]
            if not self.jobs:
                lines += [("No scheduled prompts for this provider yet.", False), ("", False)]
            lines += [("+ Schedule a prompt", self.index == len(self.jobs)), ("", False),
                      ("e edit · p pause/resume · d delete · l last log", False)]
        elif self.mode == "edit":
            values = [("Prompt", clean(self.draft["prompt"]).replace("\n", " ↵ ") or "write your prompt"),
                      ("Directory", self.draft["cwd"]), ("Date", f"{self.anchor():%a, %b %d, %Y}"),
                      ("Time", f"{clock(self.anchor())} · {self.draft['timezone']}"),
                      ("Repeat", self.draft["repeat"]), ("Save schedule", "")]
            lines += [("Edit schedule" if self.edit_id else "New scheduled prompt", False), ("", False)]
            lines += [(f"{label:14} {value}", self.field == i) for i, (label, value) in enumerate(values)]
            lines += [("", False), ("Uses this provider's current model and permissions at run time.", False),
                      ("Esc discards this draft. Saving enables the background service.", False)]
            hints = [" ↑↓ field · ⏎ edit/select · esc discard ", " ⏎ edit · esc discard ", ""]
        elif self.mode == "text":
            title = "Prompt" if self.field == 0 else "Working directory"
            lines += [(title, False), ("", False)]
            wrapped = []
            for paragraph in clean(self.buffer).split("\n"):
                wrapped += textwrap.wrap(paragraph, width=max(1, width - 2), replace_whitespace=False) or [""]
            room = max(2, height - 10)
            lines += [(line, False) for line in wrapped[-room:]]
            lines += [("▏", True)]
            hints = [" type/paste · ctrl+n newline · ctrl+u clear · ⏎ done · esc cancel ", " ctrl+n newline · ctrl+u clear · ⏎ done · esc cancel ", " ⏎ done · esc cancel ", ""]
        elif self.mode == "time":
            lines += [("Choose time", False), ("", False), (self.buffer + " ▏", True), ("", False),
                      ("↑↓ adjusts 5 minutes · ←→ adjusts 1 hour", False),
                      ("Type HH:MM, 1430, or 2:30 PM. Ctrl+U clears.", False)]
            hints = [" type or arrows · ctrl+u clear · ⏎ choose · esc cancel ", " ⏎ choose · esc cancel ", ""]
        elif self.mode == "date":
            lines += [(self.day.strftime("%B %Y"), False), (" Mo   Tu   We   Th   Fr   Sa   Su", False)]
            for week in calendar.Calendar().monthdayscalendar(self.day.year, self.day.month):
                line = " ".join(f"[{d:02}]" if d == self.day.day else f" {d:02} " if d else "    " for d in week)
                lines += [(line, self.day.day in week)]
            lines += [("", False), ("Typed date: " + self.date_buffer + " ▏", False),
                      (f"Selected: {self.day:%a %b %d, %Y}", False), ("[ / ] previous/next month · or type YYYY-MM-DD", False)]
            hints = [" arrows day/week · [ ] month · ⏎ choose · esc cancel ", " arrows move · ⏎ choose · esc cancel ", " ⏎ choose · esc cancel ", ""]
        elif self.mode == "repeat":
            lines += [("Frequency", False), ("", False)]
            lines += [(name, i == self.frequency) for i, name in enumerate(scheduler.REPEATS)]
            lines += [("", False), ("Daily/weekly keep local time through daylight-saving changes.", False),
                      ("Monthly uses the original day, clamped in shorter months.", False)]
            hints = [" ↑↓ frequency · ⏎ choose · esc cancel ", " ⏎ choose · esc cancel ", ""]
        elif self.mode == "delete":
            lines += [("Remove this schedule?", False), (clean(self.detail["prompt"]).splitlines()[0], False), ("", False),
                      ("Keep schedule", not self.confirm_delete), ("Delete schedule", self.confirm_delete)]
            hints = [" ←→ choose · ⏎ confirm · esc cancel ", " ⏎ confirm · esc cancel ", ""]
        elif self.mode == "log":
            lines += [("Last run log (last 128 KB)", False), ("", False)]
            lines += [(line, i == 0) for i, line in enumerate(self.log_lines[self.scroll:self.scroll + max(1, height - 8)])]
            hints = [" ↑↓ scroll · esc back ", " esc back ", ""]
        else:
            job = self.detail
            lines += [(f"{status(job)} · {job['repeat']}", False), (when(job), False), (job["cwd"], False), ("", False)]
            wrapped = textwrap.wrap(clean(job["prompt"]), width=width)
            lines += [(line, False) for line in wrapped[:max(2, height - 16)]]
            last = job.get("last") or {}
            if last:
                lines += [("", False), (f"Last run: {last['status']}", False)]
                if last.get("error"):
                    lines += [(clean(last["error"]), False)]
            lines += [("", False), ("e edit · p pause/resume · d delete · l last log", True)]
            hints = [" e edit · p pause · d delete · l log · esc back ", " e edit · p pause · l log · esc back ", " esc back ", ""]
        if self.notice and self.mode == "list":
            lines += [(self.notice, False)]
        if self.error:
            lines += [("", False)] + [(line, False) for line in textwrap.wrap(clean(self.error), width=width)]
        return lines, hints


def render(menu, cols, rows, t):
    import agent_picker as ap
    import usage_panel
    if not preferences.get(usage_panel.ANIMATE.key):
        t = 0
    if menu.mode == "list" and time.monotonic() >= menu.refresh_at:
        menu.refresh()
    width = max(1, min(ap.MAX_IW, cols - 2) - 7)
    lines, hints = menu.content(width, rows)
    stops = usage_panel.THEMES[usage_panel.theme_index(preferences.get(usage_panel.THEME.key))]["stops"]
    style = {"gradient": stops, "color": menu.agent["color"]} if stops else menu.agent
    def body(bw):
        entries = []
        for row, (text, selected) in enumerate(lines):
            text = ap.clip(clean(text).replace("\n", " "), max(1, bw - 1))
            line = ap.Line().put(" " + text, (235, 235, 242) if selected else (170, 170, 182), selected).pad(bw)
            if row == 0:
                line = ap.Line().put(" ")
                for i, name in enumerate(scheduler.PROVIDERS):
                    label = f"[{name}]" if i == menu.provider else name
                    agent = menu.profiles[name]
                    for x, char in enumerate(label):
                        color = ap.rainbow_rgb(x / 11 - t * 0.24, 0.2) if agent.get("pastel") == "rainbow" else agent["color"]
                        line.put(char, color, i == menu.provider)
                    line.put("   ")
                line.pad(bw)
            if selected:
                entries += ap.ringed(style, [line], t, bw)
            else:
                entries.append(("gap" if not text else "normal", "  " + line.paint(), bw + 2))
        return entries
    return ap.panel("prompt scheduler", body, hints, cols, rows, t, lines)
