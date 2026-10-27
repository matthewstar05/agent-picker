"""Cursor-like input painting on the shared, tested terminal line editor."""

import queue
import shutil
import sys
import textwrap
import threading
import time

from star_input import Editor, MENU_ROWS, Wake, cell_width

RESET = "\x1b[0m"
GRAY = "\x1b[38;2;45;44;46m"
BACKGROUND = "\x1b[48;2;45;44;46m"
COLORS = {"text": (225, 225, 230), "mark": (185, 185, 195), "ghost": (125, 125, 135),
          "label": (200, 200, 210), "note": (135, 135, 145), "mode": (225, 165, 190),
          "select": (45, 44, 46), "pick": (245, 245, 250)}


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def fg(name, text, color=True):
    return f"\x1b[38;2;{';'.join(map(str, COLORS[name]))}m{text}{RESET}" if color else text


def wrap(text, width, first="⏺ ", rest="  "):
    """Claude Code-style reply block: a marker, word wrap with a hanging indent,
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
        self.out.write("\n" + fg("note", "─ Worked for " + elapsed(took) + " · "
                                  + time.strftime("%-I:%M %p") + " ─") + "\n\n")
        self.out.flush()
        return text


class CursorInput(Editor):
    def __init__(self, commands, **kwargs):
        super().__init__(commands, colors=COLORS, **kwargs)
        self.working = None     # when the running turn started; None between turns
        self.queued = []        # messages to send after it, in order

    def keys(self, fd, idle):
        for key in super().keys(fd, idle):
            if self.working is not None and not self.menu_items():
                if key == "tab":
                    key = "enter"                       # queue, as Codex's tab does
                elif key == "esc" or (key == "ctrl-c" and not self.text):
                    key = ("wake", "interrupt")
            yield key

    def echo(self, text):
        """Show a sent message in the transcript the way a submitted prompt is left."""
        self.text, self.pos, self.drawn_rows, self.cursor_row = text, len(text), 0, 0
        self.draw(final=True)
        self.text, self.pos = "", 0

    def draw(self, final=False):
        width = max(20, shutil.get_terminal_size((100, 30)).columns)
        out = []
        if self.drawn_rows:
            out.append(f"\x1b[{self.cursor_row}A" if self.cursor_row else "")
        out.append("\r\x1b[J")
        rows, (cursor_row, cursor_col) = self.wrap(2, width)
        if final and self.working is not None:
            sys.stdout.write("".join(out))     # a queued message shows above the box, not as sent
            sys.stdout.flush()
            self.drawn_rows = self.cursor_row = 0
            return
        if final:
            lines = [("→ " if kind == "first" else "  ") + self.fg("text", piece) for kind, piece in rows]
            sys.stdout.write("".join(out) + "\n".join(lines) + "\n")
            sys.stdout.flush()
            self.drawn_rows = self.cursor_row = 0
            return
        items = self.menu_items()
        self.sel = max(0, min(self.sel, len(items) - 1)) if items else 0
        ghost = self.ghost(items)
        if not self.text:
            ghost = "Plan, search, build anything"
        head = []   # the working line and queued messages, above the box
        if self.working is not None:
            took = time.monotonic() - self.working
            head += ["", self.fg("mode", SPINNER[int(took * 10) % len(SPINNER)] + " ") + self.fg("note", "Working (" + elapsed(took))
                    + self.fg("note", " · ") + self.fg("text", "esc") + self.fg("note", " to interrupt)")]
            for message in self.queued[-4:]:
                first = message.splitlines()[0]
                head.append(self.fg("ghost", "  ↳ " + first[:width - 8] + (" …" if len(first) > width - 8 or "\n" in message else "")))
            if len(self.queued) > 4:
                head.append(self.fg("ghost", f"    and {len(self.queued) - 4} more queued"))
            head.append("")
        lines = head + [GRAY + "▄" * (width - 1) + RESET]
        for index, (kind, piece) in enumerate(rows):
            body = ("→ " if kind == "first" else "  ") + piece
            occupied = sum(cell_width(ch) for ch in body)
            extra = ghost[:max(0, width - 1 - occupied)] if index == len(rows) - 1 else ""
            text = self.fg("mark", body[:2]) + self.paint(index, piece)
            text += self.fg("ghost", extra)
            text += " " * max(0, width - 1 - occupied - len(extra))
            lines.append(BACKGROUND + text.replace(RESET, RESET + BACKGROUND) + RESET)
        lines.append(GRAY + "▀" * (width - 1) + RESET)
        if items:
            top = max(0, min(self.sel - MENU_ROWS // 2, len(items) - MENU_ROWS))
            for index, (_, label, note) in enumerate(items[top:top + MENU_ROWS], top):
                text = ("  " + label.ljust(20) + note)[:width - 1]
                lines.append(self.bg("select", self.fg("pick", text.ljust(width - 1))) if index == self.sel
                             else self.fg("label", text))
        else:
            left, right = self.hint()
            if self.working is not None and self.text.strip():
                left = "tab to queue message"
            if self.notice:
                if time.time() < self.notice[1]:
                    left = self.notice[0]
            left = left[:max(0, width - len(right) - 4)]
            lines.append("  " + self.fg("note", left) + " " * max(1, width - 3 - len(left) - len(right))
                         + self.fg("mode", right))
        out.append("\r\n".join(lines))
        up = len(lines) - 1 - (len(head) + 1 + cursor_row)
        if up > 0:
            out.append(f"\x1b[{up}A")
        out.append("\r" + (f"\x1b[{cursor_col}C" if cursor_col else ""))
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drawn_rows, self.cursor_row = len(lines), len(head) + 1 + cursor_row
