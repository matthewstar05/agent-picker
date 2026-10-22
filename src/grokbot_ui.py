"""Cursor-like input painting on the shared, tested terminal line editor."""

import contextlib
import os
import select
import shutil
import sys
import termios
import textwrap
import time

from star_input import Editor, MENU_ROWS, cell_width

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


class TurnScreen:
    """While a turn runs: no raw echo of keystrokes, a one-line working indicator,
    typed text kept for the next prompt, and Esc to interrupt (as in Claude Code)."""

    def __init__(self, stream=None, stdin=None, color=True):
        self.out, self.stdin, self.color = stream or sys.stdout, stdin or sys.stdin, color
        self.typed, self.saved, self.shown, self.frame = "", None, False, 0
        self.started = time.monotonic()

    def __enter__(self):
        self.started, self.typed = time.monotonic(), ""
        try:
            fd = self.stdin.fileno()
            self.saved = termios.tcgetattr(fd)
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~(termios.ECHO | termios.ICANON)     # keep ISIG: ctrl-c still interrupts
            attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except (OSError, ValueError, termios.error):
            self.saved = None
        return self

    def __exit__(self, *exc):
        self.clear()
        if self.saved is not None:
            termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self.saved)
            self.saved = None
        return False

    def clear(self):
        if self.shown:
            self.out.write("\r\x1b[2K")
            self.out.flush()
            self.shown = False

    def draw(self):
        width = max(20, shutil.get_terminal_size((100, 30)).columns)
        seconds = int(time.monotonic() - self.started)
        line = SPINNER[self.frame % len(SPINNER)] + " Working… (" + str(seconds) + "s · esc to interrupt)"
        if self.typed:
            line += " · next: " + self.typed.replace("\n", " ")[-30:]
        self.frame += 1
        self.out.write("\r\x1b[2K" + fg("mode", line[:width - 1], self.color))
        self.out.flush()
        self.shown = True

    def keys(self, data):
        """Keep typing for the next prompt; Esc on its own interrupts the turn."""
        if data == "\x1b":
            raise KeyboardInterrupt
        if data.startswith("\x1b"):
            return      # arrows and other sequences mean nothing while working
        for ch in data:
            if ch in "\x7f\b":
                self.typed = self.typed[:-1]
            elif ch in "\r\n":
                self.typed += "\n" if self.typed else ""
            elif ch == "\x15":                 # ctrl-u
                self.typed = ""
            elif ch >= " ":
                self.typed += ch

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.draw()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            if self.saved is None:
                time.sleep(min(0.1, left))
                continue
            ready, _, _ = select.select([self.stdin], [], [], min(0.1, left))
            if ready:
                data = os.read(self.stdin.fileno(), 1024).decode("utf-8", "replace")
                self.keys(data)

    @contextlib.contextmanager
    def paused(self):
        """Normal echoing line input for a question asked mid-turn (an approval)."""
        self.clear()
        saved = self.saved
        if saved is not None:
            current = termios.tcgetattr(self.stdin.fileno())
            termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, saved)
        try:
            yield
        finally:
            if saved is not None:
                termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, current)

    def take(self):
        typed, self.typed = self.typed.strip("\n"), ""
        return typed


class CursorInput(Editor):
    def __init__(self, commands, **kwargs):
        super().__init__(commands, colors=COLORS, **kwargs)

    def draw(self, final=False):
        width = max(20, shutil.get_terminal_size((100, 30)).columns)
        out = []
        if self.drawn_rows:
            out.append(f"\x1b[{self.cursor_row}A" if self.cursor_row else "")
        out.append("\r\x1b[J")
        rows, (cursor_row, cursor_col) = self.wrap(2, width)
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
        lines = [GRAY + "▄" * (width - 1) + RESET]
        for index, (kind, piece) in enumerate(rows):
            body = ("→ " if kind == "first" else "  ") + piece
            occupied = sum(cell_width(ch) for ch in body)
            extra = ghost[:max(0, width - 1 - occupied)] if index == len(rows) - 1 else ""
            text = self.fg("mark", body[:2]) + self.fg("text", piece)
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
            if self.notice:
                import time
                if time.time() < self.notice[1]:
                    left = self.notice[0]
            left = left[:max(0, width - len(right) - 4)]
            lines.append("  " + self.fg("note", left) + " " * max(1, width - 3 - len(left) - len(right))
                         + self.fg("mode", right))
        out.append("\r\n".join(lines))
        up = len(lines) - 1 - (1 + cursor_row)
        if up > 0:
            out.append(f"\x1b[{up}A")
        out.append("\r" + (f"\x1b[{cursor_col}C" if cursor_col else ""))
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drawn_rows, self.cursor_row = len(lines), 1 + cursor_row
