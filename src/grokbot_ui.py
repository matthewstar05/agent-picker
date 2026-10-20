"""Cursor-like input painting on the shared, tested terminal line editor."""

import shutil
import sys

from star_input import Editor, MENU_ROWS, cell_width

RESET = "\x1b[0m"
GRAY = "\x1b[38;2;45;44;46m"
BACKGROUND = "\x1b[48;2;45;44;46m"
COLORS = {"text": (225, 225, 230), "mark": (185, 185, 195), "ghost": (125, 125, 135),
          "label": (200, 200, 210), "note": (135, 135, 145), "mode": (225, 165, 190),
          "select": (45, 44, 46), "pick": (245, 245, 250)}


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
