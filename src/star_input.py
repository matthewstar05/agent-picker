"""StarCLI's input box: a small line editor in the style of Claude Code's.

The message is typed between two rules, with a hint line under them. While
typing it suggests as it goes:

- "/" opens a menu of commands, filtered as you type (prefix first, then
  fuzzy), with the ones you use most ranked higher; the best match shows as
  faint ghost text after the cursor. Tab or → accepts, ↑↓ pick, Enter runs.
- after a command that takes a value (/use, /tier, /resume ...) the menu offers
  its values.
- "@" completes file paths in the working folder.
- anything else is completed from history, fish style.

Keys: ←→ ⌥←→ home end, ctrl-a/e/b/f/w/u/k, ↑↓ history, shift with any of those
moves selects (backspace or delete removes the selection, typing replaces it), and with mouse=True a
drag selects too (click places the cursor, double-click picks a word), \\ or ⌥ before Enter for
a new line, pasted text keeps its line breaks, shift-tab calls on_mode,
ctrl-l clears the screen, ctrl-c clears the input (twice on an empty box quits),
ctrl-d on an empty box quits, esc closes the menu (twice clears the input).
"""

import os
import re
import select
import shutil
import subprocess
import sys
import time
import unicodedata

import termios
import tty

MENU_ROWS = 7
IMAGE = re.compile(r"\.(png|jpe?g|gif|webp|heic|bmp|tiff?)$", re.I)
TOKEN = re.compile(r"\[Image #(\d+)\]")
# shift+move extends a selection by the plain move
SELECT = {"shift-left": "left", "shift-right": "right", "shift-up": "up", "shift-down": "down",
          "shift-home": "home", "shift-end": "end", "shift-word-left": "word-left", "shift-word-right": "word-right"}


def image_paths(text):
    """Image files named in pasted or dropped text: quoted, backslash-escaped or plain paths."""
    found = []
    for match in re.finditer(r"'([^']+)'|\"([^\"]+)\"|((?:\\ |[^\s])+)", text.strip()):
        raw = next(g for g in match.groups() if g)
        path = os.path.expanduser(raw.replace("\\ ", " "))
        if path.startswith("file://"):
            path = path[7:]
        if IMAGE.search(path) and os.path.isfile(path):
            found.append(os.path.abspath(path))
    return found


def clipboard_image(dest):
    """Save the clipboard's image as a PNG at dest. True when there was one."""
    if sys.platform == "darwin":
        script = ['set f to POSIX file "%s"' % dest, "try", "set png to (the clipboard as «class PNGf»)",
                  "on error", 'return "none"', "end try", "set h to open for access f with write permission",
                  "set eof h to 0", "write png to h", "close access h", 'return "ok"']
        try:
            out = subprocess.run(["osascript", *sum((["-e", line] for line in script), [])], capture_output=True,
                                 text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return out.stdout.strip() == "ok" and os.path.isfile(dest) and os.path.getsize(dest) > 0
    return False


class Wake(Exception):
    """idle() returned something: read() stops, keeping what was typed in .text."""

    def __init__(self, payload, text):
        super().__init__("woken")
        self.payload, self.text = payload, text


def supported():
    """A real terminal where the line editor can run in raw mode."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def cell_width(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 0 if unicodedata.combining(ch) else 1


def fuzzy(query, text):
    """Score text against query: prefix beats substring beats subsequence; None when absent."""
    q, t = query.lower(), text.lower()
    if not q:
        return 1.0
    if t.startswith(q):
        return 3.0 - len(t) / 1000
    if q in t:
        return 2.0 - t.index(q) / 100
    pos, gaps = -1, 0
    for ch in q:
        nxt = t.find(ch, pos + 1)
        if nxt < 0:
            return None
        gaps += nxt - pos - 1
        pos = nxt
    return 1.0 - gaps / (len(t) + 10)


class Files:
    """Paths under a folder for @ completion, read once and refreshed now and then."""

    def __init__(self):
        self.root, self.paths, self.read_at = None, [], 0.0

    def list(self, root):
        if root != self.root or time.time() - self.read_at > 30:
            self.root, self.read_at = root, time.time()
            self.paths = self._scan(root)
        return self.paths

    @staticmethod
    def _scan(root):
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, capture_output=True,
                                 text=True, timeout=3)
            if out.returncode == 0 and out.stdout.strip():
                files = out.stdout.splitlines()[:20000]
                dirs = sorted({os.path.dirname(f) + "/" for f in files if os.path.dirname(f)})
                return dirs + files
        except (OSError, subprocess.SubprocessError):
            pass
        found = []
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            rel = os.path.relpath(base, root)
            for d in dirs:
                found.append(os.path.normpath(os.path.join(rel, d)) + "/")
            for f in files:
                found.append(os.path.normpath(os.path.join(rel, f)))
            if len(found) > 5000:
                break
        return found


class Editor:
    """commands: [(name, args hint, description)]. values(command) -> [(value, note)]
    for a command's argument. usage: {command: times used}, to rank the menu."""

    def __init__(self, commands, values=None, history=None, usage=None, colors=None, image_dir=None, mouse=False):
        self.commands = commands
        self.images = {}          # [Image #n] -> its file, for the whole session
        self.image_dir = image_dir
        self.values = values or (lambda command: [])
        self.history = history if history is not None else []
        self.usage = usage if usage is not None else {}
        self.files = Files()
        self.c = colors or {}
        self.cwd = os.getcwd()
        self.anchor = None        # the other end of a shift-selection, or None
        self.row_starts = []      # text index where each wrapped row begins
        self.row_lens = []
        self.mouse = mouse        # read clicks and drags while the box has text
        self.reading = self.tracking = self.dragging = self.awaiting = False
        self.origin = None        # screen row of the box's first line, from a cursor report
        self.text_line = 1        # the box line holding the first text row
        self.pending = []         # mouse events waiting for that report
        self.last_click = (0.0, -1)

    # ---- painting helpers
    def fg(self, key, text, bold=False):
        rgb = self.c.get(key)
        if not rgb:
            return text
        return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m" + ("\x1b[1m" if bold else "") + text + "\x1b[0m"

    def bg(self, key, text):
        rgb = self.c.get(key)
        return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m" + text + "\x1b[0m" if rgb else "\x1b[7m" + text + "\x1b[0m"

    def paint(self, row, piece):
        """A wrapped row's text, with the part inside the selection highlighted."""
        span = self.selection()
        start = self.row_starts[row] if row < len(self.row_starts) else 0
        a, b = (max(0, span[0] - start), min(len(piece), span[1] - start)) if span else (0, 0)
        if a >= b:
            return self.fg("text", piece) if self.c else piece
        mark = "selection" if "selection" in self.c else "select"
        ink = "selection_text" if "selection_text" in self.c else "pick"
        return (self.fg("text", piece[:a]) + self.bg(mark, self.fg(ink, piece[a:b]))
                + self.fg("text", piece[b:]))

    # ---- suggestions
    def token(self):
        """(start, word) of the word the cursor is in."""
        before = self.text[:self.pos]
        start = max(before.rfind(" "), before.rfind("\n")) + 1
        return start, before[start:]

    def menu_items(self):
        """[(insert, label, note)] for the menu under the box, or []."""
        text, before = self.text, self.text[:self.pos]
        if text.startswith("/") and "\n" not in text:
            head, sep, rest = before.partition(" ")
            if not sep:
                scored = []
                for name, args, desc in self.commands:
                    score = fuzzy(head[1:], name)
                    if score is not None:
                        scored.append((score + min(0.9, self.usage.get(name, 0) * 0.05), name, args, desc))
                scored.sort(key=lambda s: -s[0])
                return [("/" + name, "/" + name + (" " + args if args else ""), desc) for _, name, args, desc in scored]
            if " " not in rest:
                options = self.values(head[1:])
                scored = [(fuzzy(rest, v), v, note) for v, note in options]
                scored = sorted([s for s in scored if s[0] is not None], key=lambda s: -s[0])
                return [(head + " " + v, v, note) for _, v, note in scored]
            return []
        start, word = self.token()
        if word.startswith("@"):
            query = word[1:]
            scored = []
            for path in self.files.list(self.cwd):
                score = fuzzy(query, path)
                if score is None and "/" in path:
                    score = fuzzy(query, path.rsplit("/", 2)[-2 if path.endswith("/") else -1])
                if score is not None:
                    scored.append((score - path.count("/") * 0.01, path))
            scored.sort(key=lambda s: -s[0])
            return [(self.text[:start] + "@" + p, "@" + p, "") for _, p in scored[:50]]
        return []

    def ghost(self, items):
        """Faint text after the cursor that Tab or → would add."""
        if self.pos != len(self.text) or not self.text:
            return ""
        if items:
            pick = items[min(self.sel, len(items) - 1)][0]
            if pick.lower().startswith(self.text.lower()) and len(pick) > len(self.text):
                return pick[len(self.text):]
            return ""
        if len(self.text) >= 2 and not self.text.startswith("/"):
            for past in reversed(self.history):
                if past.startswith(self.text) and past != self.text and "\n" not in past:
                    return past[len(self.text):]
        return ""

    # ---- layout
    def wrap(self, prompt_w, w):
        """Visual rows [(prefix kind, text)] and the cursor's (row, col)."""
        rows, cur = [], (0, prompt_w)
        self.row_starts, self.row_lens = [], []
        index = 0
        for n, line in enumerate(self.text.split("\n")):
            room = max(10, w - prompt_w - 1)
            chunks, chunk, width = [], "", 0
            starts = [index]
            for ch in line:
                cw = cell_width(ch)
                if width + cw > room:
                    chunks.append(chunk)
                    chunk, width = "", 0
                    starts.append(index)
                chunk += ch
                width += cw
                index += 1
            chunks.append(chunk)
            for k, piece in enumerate(chunks):
                kind = "first" if n == 0 and k == 0 else "cont"
                row_start = starts[k]
                if row_start <= self.pos <= row_start + len(piece) and (k == len(chunks) - 1 or self.pos < row_start + len(piece)):
                    col = prompt_w + sum(cell_width(c) for c in self.text[row_start:self.pos])
                    cur = (len(rows), col)
                rows.append((kind, piece))
                self.row_starts.append(row_start)
                self.row_lens.append(len(piece))
            index += 1   # the newline
        return rows, cur

    def draw(self, final=False):
        self.track_mouse(final)
        w = max(20, shutil.get_terminal_size((100, 30)).columns)
        out = []
        if self.drawn_rows:
            out.append(f"\x1b[{self.cursor_row}A" if self.cursor_row else "")
        out.append("\r\x1b[J")
        rule = self.rule(w)
        items = [] if final else self.menu_items()
        if items:
            self.sel = max(0, min(self.sel, len(items) - 1))
        ghost = "" if final else self.ghost(items)
        rows, (crow, ccol) = self.wrap(2, w)
        lines = [] if final else [rule]
        mark = self.fg("mark", "› ", True)
        for k, (kind, piece) in enumerate(rows):
            prefix = mark if kind == "first" else "  "
            body = self.paint(k, piece)
            if k == len(rows) - 1 and ghost:
                room = w - 3 - sum(cell_width(ch) for ch in piece) - 2
                body += self.fg("ghost", ghost[:max(0, room)])
            lines.append(prefix + body)
        if final:
            text = "\n".join(lines) + "\n"
            sys.stdout.write("".join(out) + text)
            sys.stdout.flush()
            self.drawn_rows = self.cursor_row = 0
            return
        lines.append(rule)
        self.text_line = 1
        if items:
            top = max(0, min(self.sel - MENU_ROWS // 2, len(items) - MENU_ROWS))
            shown = items[top:top + MENU_ROWS]
            label_w = min(max(len(i[1]) for i in shown) + 2, max(12, w // 2))
            for k, (_, label, note) in enumerate(shown):
                label = label if len(label) < label_w else label[:label_w - 2] + "…"
                row = "  " + label.ljust(label_w) + note
                row = row[:w - 1]
                if top + k == self.sel:
                    lines.append(self.bg("select", self.fg("pick", row.ljust(w - 1), True)))
                else:
                    lines.append(self.fg("label", "  " + label.ljust(label_w)) + self.fg("note", note[:max(0, w - 3 - label_w)]))
            if top + len(shown) < len(items):
                lines.append(self.fg("note", f"  {len(items) - top - len(shown)} more · ↑↓ to look"))
        else:
            left, right = self.hint()
            if self.notice and time.time() < self.notice[1]:
                left = self.notice[0]
            room = w - 5
            if len(left) + len(right) > room:
                left = left.split(" (")[0]          # drop "(shift+tab to cycle)" first
            if len(left) + len(right) > room:
                right = right[:max(0, room - len(left) - 1)].rstrip(" ·")
            if len(left) > room:
                left = left[:room - 1] + "…"
            gap = max(2, w - 3 - len(left) - len(right))
            lines.append("  " + self.fg("note", left) + " " * gap + self.fg("mode", right))
        out.append("\r\n".join(lines))   # raw mode: a newline alone doesn't return the carriage
        # back up from the last line to the cursor
        up = len(lines) - 1 - (1 + crow)
        out.append(f"\x1b[{up}A" if up > 0 else "")
        out.append(f"\r\x1b[{ccol}C" if ccol else "\r")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drawn_rows = len(lines)
        self.cursor_row = 1 + crow

    def rule(self, w):
        a, b = self.c.get("rule", (150, 120, 62)), self.c.get("rule_end", (88, 84, 76))
        if not self.c:
            return "─" * (w - 1)
        out = []
        for x in range(w - 1):
            f = abs(x / max(1, w - 2) - 0.5) * 2
            rgb = tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3))
            out.append(f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m─")
        return "".join(out) + "\x1b[0m"

    # ---- images
    def attach(self, path):
        n = len(self.images) + 1
        self.images[n] = path
        token = f"[Image #{n}]"
        before = self.text[:self.pos]
        self.insert(("" if not before or before.endswith((" ", "\n")) else " ") + token + " ")
        return token

    def paste_image(self):
        folder = self.image_dir or os.path.join(os.path.expanduser("~"), ".cache", "starcli")
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, time.strftime("image-%Y%m%d-%H%M%S") + f"-{len(self.images) + 1}.png")
        if clipboard_image(dest):
            self.notice = (f"{self.attach(dest)} pasted from the clipboard", time.time() + 2.5)
        else:
            self.notice = ("no image on the clipboard (copy one, or drop an image file here)", time.time() + 3)

    def attached(self, message):
        """The image files a sent message refers to, by token or by path."""
        paths = [self.images[int(n)] for n in TOKEN.findall(message) if int(n) in self.images]
        return list(dict.fromkeys(paths + image_paths(message)))

    def token_span(self, start, end):
        """Widen start..end to cover any [Image #n] it touches, so an image is
        deleted whole, never left as stray text that sends instead of the image."""
        for match in TOKEN.finditer(self.text):
            if match.start() < end and start < match.end():
                start, end = min(start, match.start()), max(end, match.end())
        return start, end

    # ---- editing
    def insert(self, s):
        self.text = self.text[:self.pos] + s + self.text[self.pos:]
        self.pos += len(s)
        self.sel = 0

    def accept(self, items):
        if items:
            self.text = items[self.sel][0] + self.text[self.pos:]
            self.pos = len(items[self.sel][0])
            if self.text.startswith("/") and " " not in self.text and self.takes_args(self.text[1:]):
                self.insert(" ")
            elif self.text.startswith("/") is False and items[self.sel][0].split(" ")[-1].startswith("@") \
                    and not self.text.endswith("/"):
                self.insert(" ")
            self.sel = 0
            return True
        ghost = self.ghost(items)
        if ghost:
            self.insert(ghost)
            return True
        return False

    def selection(self):
        """(start, end) of the selected text, or None."""
        if self.anchor is None or self.anchor == self.pos:
            return None
        return min(self.anchor, self.pos), max(self.anchor, self.pos)

    def moved(self, move):
        """Where a plain cursor move (left, word-left, home, up ...) lands, within the text."""
        text, pos = self.text, self.pos
        if move == "left":
            return max(0, pos - 1)
        if move == "right":
            return min(len(text), pos + 1)
        if move == "word-left":
            return self.word_left()
        if move == "word-right":
            return self.word_right()
        if move == "home":
            return text.rfind("\n", 0, pos) + 1
        if move == "end":
            nl = text.find("\n", pos)
            return len(text) if nl < 0 else nl
        line_start = text.rfind("\n", 0, pos) + 1
        if move == "up":
            if line_start == 0:
                return 0
            prev = text.rfind("\n", 0, line_start - 1) + 1
            return min(line_start - 1, prev + pos - line_start)
        nxt = text.find("\n", pos)
        if nxt < 0:
            return len(text)
        end = text.find("\n", nxt + 1)
        return min(len(text) if end < 0 else end, nxt + 1 + pos - line_start)

    # ---- mouse
    def track_mouse(self, final=False):
        """Ask for mouse reports only while there is text to select, so an empty box
        leaves the wheel and the terminal's own selection alone."""
        want = self.mouse and self.reading and not final and bool(self.text)
        if want != self.tracking:
            sys.stdout.write("\x1b[?1002h\x1b[?1006h" if want else "\x1b[?1002l\x1b[?1006l")
            self.tracking, self.dragging = want, False

    def index_at(self, x, y, clamp=False):
        """The text index under screen cell (x, y), 1-based; None off the text unless clamp."""
        if self.origin is None or not self.row_starts:
            return None
        row = y - self.origin - self.text_line
        if not 0 <= row < len(self.row_starts):
            return None if not clamp else 0 if row < 0 else len(self.text)
        start, n, col = self.row_starts[row], self.row_lens[row], x - 3   # after the 2-cell prompt mark
        i = width = 0
        while i < n and width + cell_width(self.text[start + i]) <= col:
            width += cell_width(self.text[start + i])
            i += 1
        return start + i

    def on_mouse(self, kind, value):
        if kind == "report":            # where the cursor is on screen, so where the box is
            self.origin = value[0] - self.cursor_row
            pending, self.pending = self.pending, []
            for event in pending:
                self.on_mouse("mouse", event)
            return
        button, x, y, pressed = value
        if button & 64:
            return                       # the wheel
        if pressed and not button & 32 and self.origin is None or self.pending:
            self.pending.append(value)
            if not self.awaiting:
                self.awaiting = True
                sys.stdout.write("\x1b[6n")
                sys.stdout.flush()
            return
        if pressed and not button & 32:                     # a press
            at = self.index_at(x, y)
            self.dragging = at is not None
            if at is None:
                return
            now = time.time()
            if now - self.last_click[0] < 0.4 and self.last_click[1] == at:
                start = end = at                            # double-click: the word
                while start > 0 and self.text[start - 1].isalnum():
                    start -= 1
                while end < len(self.text) and self.text[end].isalnum():
                    end += 1
                self.anchor, self.pos = start, end
            elif button & 4:                                # shift-click extends
                self.anchor = self.pos if self.anchor is None else self.anchor
                self.pos = at
            else:
                self.anchor = self.pos = at
            self.last_click = (now, at)
        elif pressed and self.dragging:                     # a drag
            at = self.index_at(x, y, clamp=True)
            if at >= self.anchor and at < len(self.text) and self.text[at] != "\n":
                at += 1                                     # take the character under the pointer
            self.pos = at
        elif not pressed:
            self.dragging = False
            self.origin = None                              # find the box again on the next press

    def takes_args(self, name):
        return any(n == name and args for n, args, _ in self.commands)

    def word_left(self):
        p = self.pos
        while p > 0 and not self.text[p - 1].isalnum():
            p -= 1
        while p > 0 and self.text[p - 1].isalnum():
            p -= 1
        return p

    def word_right(self):
        p, n = self.pos, len(self.text)
        while p < n and not self.text[p].isalnum():
            p += 1
        while p < n and self.text[p].isalnum():
            p += 1
        return p

    def keys(self, fd, idle):
        """Yield key names and text from the terminal, calling idle() between reads."""
        buf = b""
        while True:
            if not buf:
                ready, _, _ = select.select([fd], [], [], 1 / 30)
                if not ready:
                    if idle:
                        woke = idle()
                        if woke:
                            yield ("wake", woke)
                    continue
                buf = os.read(fd, 4096)
                if not buf:
                    yield "eof"
                    return
            if buf.startswith(b"\x1b[200~"):        # bracketed paste
                end = buf.find(b"\x1b[201~")
                while end < 0:
                    buf += os.read(fd, 65536)
                    end = buf.find(b"\x1b[201~")
                yield ("paste", buf[6:end].decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n"))
                buf = buf[end + 6:]
                continue
            if buf.startswith(b"\x1b"):
                if len(buf) == 1:
                    ready, _, _ = select.select([fd], [], [], 0.03)
                    if ready:
                        buf += os.read(fd, 64)
                match = re.match(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", buf)
                if match:                                   # an SGR mouse report
                    buf = buf[match.end():]
                    yield ("mouse", (int(match.group(1)), int(match.group(2)), int(match.group(3)),
                                     match.group(4) == b"M"))
                    continue
                match = re.match(rb"\x1b\[(\d+);(\d+)R", buf) if self.awaiting else None
                if match:                                   # the answer to our cursor-position query
                    buf = buf[match.end():]
                    self.awaiting = False
                    yield ("report", (int(match.group(1)), int(match.group(2))))
                    continue
                seqs = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
                        b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left",
                        b"\x1b[H": "home", b"\x1b[F": "end", b"\x1bOH": "home", b"\x1bOF": "end",
                        b"\x1b[1~": "home", b"\x1b[4~": "end", b"\x1b[3~": "delete", b"\x1b[Z": "shift-tab",
                        b"\x1b[1;2D": "shift-left", b"\x1b[1;2C": "shift-right", b"\x1b[1;2A": "shift-up",
                        b"\x1b[1;2B": "shift-down", b"\x1b[1;2H": "shift-home", b"\x1b[1;2F": "shift-end",
                        b"\x1b[1;4D": "shift-word-left", b"\x1b[1;4C": "shift-word-right",
                        b"\x1b[1;6D": "shift-word-left", b"\x1b[1;6C": "shift-word-right",
                        b"\x1b[1;10D": "shift-word-left", b"\x1b[1;10C": "shift-word-right",
                        b"\x1b[1;3D": "word-left", b"\x1b[1;3C": "word-right", b"\x1bb": "word-left",
                        b"\x1bf": "word-right", b"\x1b[1;5D": "word-left", b"\x1b[1;5C": "word-right",
                        b"\x1b\r": "newline", b"\x1b\x7f": "kill-word"}
                for seq, name in seqs.items():
                    if buf.startswith(seq):
                        buf = buf[len(seq):]
                        yield name
                        break
                else:
                    match = re.match(rb"\x1b\[[0-9;]*[A-Za-z~]", buf)
                    if match:
                        buf = buf[match.end():]   # an escape we don't use
                        continue
                    buf = buf[1:]
                    yield "esc"
                continue
            byte = buf[0]
            if byte < 0x20 or byte == 0x7f:
                buf = buf[1:]
                yield {0x0d: "enter", 0x0a: "newline", 0x7f: "backspace", 0x08: "backspace", 0x09: "tab",
                       0x01: "home", 0x05: "end", 0x02: "left", 0x06: "right", 0x03: "ctrl-c", 0x04: "ctrl-d",
                       0x17: "kill-word", 0x15: "kill-line", 0x0b: "kill-end", 0x0c: "ctrl-l", 0x16: "ctrl-v",
                       0x10: "up", 0x0e: "down"}.get(byte, "ignore")
                continue
            # one UTF-8 character
            n = 1 if byte < 0x80 else 2 if byte < 0xe0 else 3 if byte < 0xf0 else 4
            while len(buf) < n:
                buf += os.read(fd, 4)
            yield ("text", buf[:n].decode("utf-8", "replace"))
            buf = buf[n:]

    def read(self, hint=lambda: ("", ""), on_mode=None, idle=None, cwd=None, initial="", on_ready=None,
             on_change=None):
        """The next message. Raises EOFError on ctrl-d (or a second ctrl-c) in an empty box,
        and Wake when idle() returns something. initial: text already typed."""
        self.text, self.pos, self.sel = initial or "", len(initial or ""), 0
        self.anchor, self.origin, self.pending, self.awaiting = None, None, [], False
        self.reading = True
        self.hint, self.notice, self.cwd = hint, None, cwd or os.getcwd()
        self.drawn_rows = self.cursor_row = 0
        browse, draft = len(self.history), ""
        last_ctrl_c, last_esc = 0.0, 0.0
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        sys.stdout.write("\x1b[?2004h")   # bracketed paste
        try:
            tty.setraw(fd)
            self.draw()
            if on_ready:
                on_ready(self.text)
            for key in self.keys(fd, idle):
                items = self.menu_items()
                if isinstance(key, tuple) and key[0] == "wake":
                    raise Wake(key[1], self.text)
                if isinstance(key, tuple) and key[0] in ("mouse", "report"):
                    self.on_mouse(*key)
                    self.draw()
                    continue
                if key in SELECT:
                    if self.anchor is None:
                        self.anchor = self.pos
                    self.pos = self.moved(SELECT[key])
                    self.draw()
                    continue
                span, self.anchor = self.selection(), None
                if span and (isinstance(key, tuple) or key in ("backspace", "delete", "ctrl-d")):
                    start, end = self.token_span(*span)       # typing or deleting replaces the selection
                    self.text, self.pos, self.sel = self.text[:start] + self.text[end:], start, 0
                    items = self.menu_items()
                    if not isinstance(key, tuple):
                        if on_change:
                            on_change(self.text)
                        self.draw()
                        continue
                elif span and key in ("left", "right"):
                    self.pos = span[0] if key == "left" else span[1]   # collapse to that edge
                    self.draw()
                    continue
                if isinstance(key, tuple):
                    kind, value = key
                    dropped = image_paths(value) if kind == "paste" else []
                    if dropped and len(" ".join(dropped)) >= len(value.strip()) - 4 * len(dropped):
                        for path in dropped:          # a dropped or pasted image file
                            self.attach(path)
                    else:
                        self.insert(value)
                elif key == "enter":
                    if self.text.endswith("\\") and self.pos == len(self.text):
                        self.text, self.pos = self.text[:-1], self.pos - 1
                        self.insert("\n")
                    elif items and self.text.startswith("/") and self.text.strip() != items[self.sel][0]:
                        self.accept(items)
                        if not self.takes_args(self.text.strip()[1:].split(" ")[0]) or " " in self.text.strip():
                            break
                    elif items and self.token()[1].startswith("@"):
                        self.accept(items)
                    else:
                        break
                elif key == "newline":
                    self.insert("\n")
                elif key == "tab":
                    self.accept(items)
                elif key == "right":
                    if self.pos == len(self.text):
                        self.accept([] if not items else items)
                    else:
                        self.pos += 1
                elif key == "left":
                    self.pos = max(0, self.pos - 1)
                elif key == "word-left":
                    self.pos = self.word_left()
                elif key == "word-right":
                    self.pos = self.word_right()
                elif key == "home":
                    self.pos = self.text.rfind("\n", 0, self.pos) + 1
                elif key == "end":
                    nl = self.text.find("\n", self.pos)
                    self.pos = len(self.text) if nl < 0 else nl
                elif key == "backspace":
                    if self.pos:
                        start, end = self.token_span(self.pos - 1, self.pos)
                        self.text, self.pos = self.text[:start] + self.text[end:], start
                        self.sel = 0
                elif key == "delete":
                    if self.pos < len(self.text):
                        start, end = self.token_span(self.pos, self.pos + 1)
                        self.text, self.pos = self.text[:start] + self.text[end:], start
                elif key == "kill-word":
                    p, end = self.token_span(self.word_left(), self.pos)
                    self.text, self.pos = self.text[:p] + self.text[end:], p
                elif key == "kill-line":
                    start = self.text.rfind("\n", 0, self.pos) + 1
                    self.text, self.pos = self.text[:start] + self.text[self.pos:], start
                elif key == "kill-end":
                    nl = self.text.find("\n", self.pos)
                    self.text = self.text[:self.pos] + ("" if nl < 0 else self.text[nl:])
                elif key in ("up", "down"):
                    if items:
                        self.sel = (self.sel + (1 if key == "down" else -1)) % len(items)
                    elif key == "up" and "\n" in self.text[:self.pos]:
                        line_start = self.text.rfind("\n", 0, self.pos)
                        prev = self.text.rfind("\n", 0, line_start) + 1
                        self.pos = min(line_start, prev + (self.pos - line_start - 1))
                    elif key == "down" and "\n" in self.text[self.pos:]:
                        line_start = self.text.rfind("\n", 0, self.pos) + 1
                        nxt = self.text.find("\n", self.pos) + 1
                        end = self.text.find("\n", nxt)
                        self.pos = min(len(self.text) if end < 0 else end, nxt + (self.pos - line_start))
                    elif self.history:
                        if browse == len(self.history):
                            draft = self.text
                        browse = max(0, browse - 1) if key == "up" else min(len(self.history), browse + 1)
                        self.text = self.history[browse] if browse < len(self.history) else draft
                        self.pos = len(self.text)
                elif key == "shift-tab" and on_mode:
                    self.notice = (on_mode(), time.time() + 2.5)
                elif key == "ctrl-v":
                    self.paste_image()
                elif key == "ctrl-l":
                    sys.stdout.write("\x1b[2J\x1b[H")
                    self.drawn_rows = self.cursor_row = 0
                elif key == "esc":
                    if items and self.text.startswith("/") and " " not in self.text:
                        pass   # the menu closes once the text no longer starts a command
                    if time.time() - last_esc < 0.6 and self.text:
                        self.text, self.pos = "", 0
                    last_esc = time.time()
                elif key == "ctrl-c":
                    if self.text:
                        self.text, self.pos = "", 0
                    elif time.time() - last_ctrl_c < 2:
                        raise EOFError
                    else:
                        last_ctrl_c = time.time()
                        self.notice = ("press ctrl-c again to quit", last_ctrl_c + 2)
                elif key in ("ctrl-d", "eof"):
                    if not self.text:
                        raise EOFError
                    self.text = self.text[:self.pos] + self.text[self.pos + 1:]
                if on_change:
                    on_change(self.text)
                self.draw()
        except (EOFError, Wake):
            self.text = ""
            raise
        finally:
            self.anchor, self.reading = None, False
            self.track_mouse()
            sys.stdout.write("\x1b[?2004l")
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            if self.drawn_rows:
                # leave the message as transcript, without the rules, menu and hint
                self.draw(final=True) if self.text.strip() else self._erase()
        message = self.text
        if message.strip() and (not self.history or self.history[-1] != message):
            self.history.append(message)
        return message

    def _erase(self):
        sys.stdout.write((f"\x1b[{self.cursor_row}A" if self.cursor_row else "") + "\r\x1b[J")
        sys.stdout.flush()
        self.drawn_rows = self.cursor_row = 0
