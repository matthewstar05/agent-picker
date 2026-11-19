"""Codex-style input painting on the shared, tested terminal line editor."""

import queue
import re
import shutil
import sys
import textwrap
import threading
import time

from star_input import Editor, MENU_ROWS, Wake, cell_width, visible_width

RESET = "\x1b[0m"
GRAY = "\x1b[38;2;45;44;46m"
BACKGROUND = "\x1b[48;2;45;44;46m"
COLORS = {"text": (225, 225, 230), "mark": (185, 185, 195), "ghost": (125, 125, 135),
          "label": (200, 200, 210), "note": (135, 135, 145), "mode": (225, 165, 190),
          "select": (45, 44, 46), "pick": (245, 245, 250),
          "selection": (240, 240, 245), "selection_text": (30, 30, 34),   # selected prompt text: dark on white
          "goal": (240, 130, 175), "goal_text": (30, 18, 26), "rule": (70, 70, 76)}

# The command that keeps working toward an objective, highlighted wherever it shows.
GOAL_COMMAND = re.compile(r"/goal(?=\s|$)")


def fg(name, text, color=True):
    return f"\x1b[38;2;{';'.join(map(str, COLORS[name]))}m{text}{RESET}" if color else text


def chip(text, color=True):
    """A highlighted label, dark bold text on the goal's pink, as Codex marks a mode."""
    if not color:
        return text
    back, ink = COLORS["goal"], COLORS["goal_text"]
    return (f"\x1b[48;2;{';'.join(map(str, back))}m\x1b[38;2;{';'.join(map(str, ink))}m\x1b[1m"
            + text + RESET)


def shimmer(text, t, color=True):
    """Codex's working text: a bright band sweeping across dim letters."""
    if not color:
        return text
    low, high, band = (120, 120, 130), (250, 250, 255), 3.0
    sweep = (t * 12) % (len(text) + 2 * band) - band
    out = []
    for i, ch in enumerate(text):
        glow = max(0.0, 1 - abs(i - sweep) / band)
        rgb = tuple(round(a + (b - a) * glow) for a, b in zip(low, high))
        out.append(f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{ch}")
    return "".join(out) + RESET


def goal_line(label, summary, width=None, color=True):
    """The highlighted /goal chip and the goal's first line, cut to fit the pane."""
    width = width or max(20, shutil.get_terminal_size((100, 30)).columns)
    room = width - 1 - cell_width_of(label) - 2
    summary = summary if cell_width_of(summary) <= room else fit_cells(summary, room)
    return chip(label, color) + ("  " + fg("note", summary, color) if room > 1 and summary else "")


def cell_width_of(text):
    return sum(cell_width(ch) for ch in text)


def fit_cells(text, room):
    """text cut to room cells, ending in … when cut."""
    if room <= 1:
        return ""
    out, used = "", 0
    for ch in text:
        if used + cell_width(ch) > room - 1:
            break
        out, used = out + ch, used + cell_width(ch)
    return out + "…"


def worked_rule(took, width, color=True):
    """Codex's divider after a turn: ─ Worked for 1m 05s · 3:04 PM ────────"""
    label = "─ Worked for " + elapsed(took) + " · " + time.strftime("%I:%M %p").lstrip("0") + " "
    return fg("note", label + "─" * max(0, width - 1 - len(label)), color)


def wrap(text, width, first="• ", rest="  "):
    """Codex-style reply block: a bullet, word wrap with a hanging indent,
    and code fences indented but never rewrapped."""
    width, lines, fenced = max(20, width - 1), [], False
    for line in text.splitlines() or [""]:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            lines.append(line)
        elif fenced or not line.strip():
            lines.append(line)
        else:
            indent = line[:len(line) - len(line.lstrip())]
            lines += textwrap.wrap(line, width - len(rest), subsequent_indent=indent,
                                   break_long_words=True, break_on_hyphens=False) or [""]
    return "\n".join((first if i == 0 else rest) + line for i, line in enumerate(lines))


def elapsed(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}h {seconds // 60 % 60:02d}m {seconds % 60:02d}s" if seconds >= 3600 \
        else f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Working:
    """One turn on a thread while the input box stays live, as in Codex: output prints
    above the box, Enter or Tab queues a message for after the turn, Esc interrupts."""

    def __init__(self, editor, out=None):
        self.editor, self.out = editor, out or sys.stdout
        self.lines, self.questions = queue.Queue(), queue.Queue()
        self.cancel, self.thread, self.error, self.drawn = threading.Event(), None, None, 0.0

    # ---- called from the turn's thread
    def emit(self, text):
        self.lines.put(text)

    def ask(self, prompt):
        reply = queue.Queue(1)
        self.questions.put((prompt, reply))
        return reply.get()

    def wait(self, seconds):
        if self.cancel.wait(seconds):
            raise KeyboardInterrupt

    # ---- the main thread
    def flush(self, raw):
        if self.lines.empty():
            return
        if raw:
            self.editor._erase()
        while not self.lines.empty():
            text = self.lines.get()
            self.out.write((text.replace("\n", "\r\n") if raw else text) + ("\r\n" if raw else "\n"))
        self.out.flush()
        if raw:
            self.editor.draw()

    def idle(self):
        self.flush(raw=True)
        if not self.questions.empty():
            return "ask"
        if not self.thread.is_alive() and self.lines.empty():
            return "done"
        if time.monotonic() - self.drawn > 0.1:
            self.drawn = time.monotonic()
            self.editor.draw()
        return None

    def run(self, job, initial="", **read):
        """Run job() to the end and return what is left typed in the box."""
        def target():
            try:
                job()
            except BaseException as exc:      # handed to the main thread
                self.error = exc
        self.cancel.clear()
        self.error, text = None, initial
        self.editor.working = time.monotonic()
        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        try:
            while True:
                try:
                    message = self.editor.read(initial=text, idle=self.idle, **read)
                    text = ""
                    if message.strip() == "/stop":
                        self.cancel.set()           # a stop is for this turn, not the queue
                    elif message.strip():
                        self.editor.queued.append(message.strip())
                except Wake as wake:
                    text = wake.text
                    if wake.payload == "done":
                        break
                    if wake.payload == "interrupt":
                        self.cancel.set()
                    elif wake.payload == "ask":
                        self.flush(raw=False)
                        prompt, reply = self.questions.get()
                        try:
                            reply.put(input(prompt))
                        except EOFError:
                            reply.put("")
                except EOFError:
                    self.cancel.set()     # ctrl-d twice on an empty box: stop this turn
        finally:
            self.thread.join()
            self.flush(raw=False)
            took, self.editor.working = time.monotonic() - self.editor.working, None
        self.out.write("\n" + worked_rule(took, max(20, shutil.get_terminal_size((100, 30)).columns)) + "\n\n")
        self.out.flush()
        return text


class CursorInput(Editor):
    """Codex-style composer: a shaded box with a › prompt, the working line and
    queued messages above it, and a footer or command menu below."""

    PLACEHOLDER = "Ask Grok Bot to do anything"

    def __init__(self, commands, **kwargs):
        super().__init__(commands, colors=COLORS, mouse=True, **kwargs)
        self.working = None     # when the running turn started; None between turns
        self.queued = []        # messages to send after it, in order
        self.badge = None       # "goal · step 3" while a /goal runs, shown on the working line

    def keys(self, fd, idle):
        for key in super().keys(fd, idle):
            if self.working is not None and not self.menu_items():
                if key == "tab":
                    key = "enter"                       # queue, as Codex's tab does
                elif key == "esc" or (key == "ctrl-c" and not self.text):
                    key = ("wake", "interrupt")
            yield key

    def paint(self, row, piece):
        """The row's text, with /goal at the start of the message highlighted."""
        match = GOAL_COMMAND.match(self.text) if row == 0 else None
        if not match or self.selection() or not piece.startswith(match.group()):
            return super().paint(row, piece)
        size = len(match.group())
        return chip(piece[:size]) + super().paint(row, piece[size:]) if piece[size:] else chip(piece[:size])

    def echo(self, text):
        """Show a sent message in the transcript the way a submitted prompt is left."""
        self.text, self.pos, self.drawn_rows, self.cursor_row = text, len(text), 0, 0
        self.draw(final=True)
        self.text, self.pos = "", 0

    def block(self, rows, width, ghost=""):
        """The shaded message block: half-block edges around › rows, each padded to width."""
        lines = [GRAY + "▄" * (width - 1) + RESET]
        for index, (kind, piece) in enumerate(rows):
            body = ("› " if kind == "first" else "  ") + piece
            occupied = sum(cell_width(ch) for ch in body)
            extra = ghost[:max(0, width - 1 - occupied)] if index == len(rows) - 1 else ""
            text = self.fg("mark", body[:2], True) + self.paint(index, piece) + self.fg("ghost", extra)
            text += " " * max(0, width - 1 - occupied - len(extra))
            lines.append(BACKGROUND + text.replace(RESET, RESET + BACKGROUND) + RESET)
        lines.append(GRAY + "▀" * (width - 1) + RESET)
        return lines

    def working_line(self, width):
        took = time.monotonic() - self.working
        line = self.fg("text", "• ") + shimmer("Working", took)
        line += self.fg("note", " (" + elapsed(took) + " • ") + self.fg("text", "esc") + self.fg("note", " to interrupt)")
        if self.badge:
            line = chip(" ◎ " + self.badge + " ") + " " + line
        return line

    def footer(self, width):
        left, right = self.hint()
        if self.working is not None and self.text.strip():
            left = "tab to queue message"
        if self.notice and time.time() < self.notice[1]:
            left = self.notice[0]
        room = max(0, width - len(right) - 4)
        if len(left) > room and "Goal " in left:
            left = left[left.index("Goal "):]      # the goal's state matters more than the model label
        left = left if len(left) <= room else left[:max(0, room - 1)] + "…"
        goal = left.find("Goal ")
        painted = (self.fg("note", left[:goal]) + self.fg("goal", left[goal:], True)) if goal >= 0 \
            else self.fg("note", left)
        return "  " + painted + " " * max(1, width - 3 - len(left) - len(right)) + self.fg("mode", right)

    def draw(self, final=False):
        self.track_mouse(final)
        width = self.columns()
        out = [self.rewind() + "\x1b[J"]
        rows, (cursor_row, cursor_col) = self.wrap(2, width)
        if final and self.working is not None:
            sys.stdout.write("".join(out))     # a queued message shows above the box, not as sent
            sys.stdout.flush()
            self.drawn_rows = self.cursor_row = 0
            return
        if final:
            sys.stdout.write("".join(out) + "\r\n".join(self.block(rows, width)) + "\r\n")
            sys.stdout.flush()
            self.drawn_rows = self.cursor_row = 0
            return
        items = self.menu_items()
        self.sel = max(0, min(self.sel, len(items) - 1)) if items else 0
        ghost = self.ghost(items) if self.text else self.PLACEHOLDER
        head = []   # the working line and queued messages, above the box
        if self.working is not None:
            head += ["", self.working_line(width)]
            for message in self.queued[-4:]:
                first = message.splitlines()[0]
                head.append(self.fg("ghost", "  ↳ " + first[:width - 8] + (" …" if len(first) > width - 8 or "\n" in message else "")))
            if len(self.queued) > 4:
                head.append(self.fg("ghost", f"    and {len(self.queued) - 4} more queued"))
            head.append("")
        lines = head + self.block(rows, width, ghost)
        self.text_line = len(head) + 1
        if items:
            top = max(0, min(self.sel - MENU_ROWS // 2, len(items) - MENU_ROWS))
            for index, (_, label, note) in enumerate(items[top:top + MENU_ROWS], top):
                text = ("  " + label.ljust(20) + note)[:width - 1]
                if index == self.sel:
                    lines.append(self.bg("select", self.fg("pick", text.ljust(width - 1))))
                elif GOAL_COMMAND.match(label):
                    lines.append("  " + self.fg("goal", label.ljust(20), True) + self.fg("label", note[:max(0, width - 23)]))
                else:
                    lines.append(self.fg("label", text))
        else:
            lines.append(self.footer(width))
        out.append("\r\n".join(lines))
        up = len(lines) - 1 - (len(head) + 1 + cursor_row)
        if up > 0:
            out.append(f"\x1b[{up}A")
        out.append("\r" + (f"\x1b[{cursor_col}C" if cursor_col else ""))
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drew(lines, len(head) + 1 + cursor_row, cursor_col, shutil.get_terminal_size((100, 30)).columns)
