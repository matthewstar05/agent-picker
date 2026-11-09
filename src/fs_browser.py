#!/usr/bin/env python3
"""fs: a Finder-style file browser for the terminal, drawn like the agent selector.

Icons in a grid or a details list. Arrow keys move, Enter opens a folder or
previews a file, and Shift+Enter moves every herdr-ai agent on this tab into the
selected folder (it runs herdr-ai's refresh there; a file means its folder). The
selected item glows in the theme's gradient the way a working agent's card does,
and brightens while Shift is held. Themes are the usage panel's list
(themes.json), so a theme added there shows up here too.

herdr-ai runs it in every setup's bottom-left pane in icon view (--herdr). There
it opens on the Desktop unless the "Herdr pane opens in" setting says otherwise.
Shift+Q, and nothing else, leaves it at the pane's shell; fs opens it again.

Usage: fs [DIR|FILE] [--icons|--details] [--hidden] [--sort name|kind|date|size]
          [--theme NAME] [--herdr]
       DIR opens that folder (the current directory by default). FILE opens its
       folder with the file selected. The options apply to this run only.

Keys:  ←↑↓→ move     ⏎ open or preview    ⇧⏎ move this tab's agents here
       esc, ⌫ or - back to the enclosing folder   [ ] back, forward   ~ home
       . hidden files   v icons or details   , sort   / filter   space Quick Look
       o open in app   y copy path   e edit   ! shell here   p pin for herdr-ai's pane
       r reload   t themes   s settings   ? every key   ⇧Q quit
"""

import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

if __name__ == "__main__":
    sys.modules.setdefault("fs_browser", sys.modules[__name__])

import agent_picker as ap
import fs_icons
import preferences
import themes
from platform_runtime import Terminal

ESC, RESET, BOLD = ap.ESC, ap.RESET, ap.BOLD
WHITE, TEXT = (255, 255, 255), (232, 232, 238)
LABEL, SOFT = ap.LABEL, ap.SOFT
OK, WARN, HOT = ap.OK, ap.WARN, ap.HOT
DARK = (24, 24, 28)                 # what dimmed cells fade toward (the panel's dark background)
BRAND_FRAME = {"color": (190, 190, 198)}
INACTIVE_RING, INACTIVE_BG = (112, 112, 120), (54, 54, 60)   # Finder greys a selection when its window loses focus
GOLD = ((225, 165, 40), (255, 222, 120))
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
HERE = Path(__file__).resolve().parent
HERDR_AI = HERE / "herdr_ai.py"

# ---- settings ---------------------------------------------------------------

SECTION = "File browser"
VIEW = preferences.register(preferences.Setting(
    "fs.view", SECTION, "View", ["icons", "details"], "icons",
    hint="How fs opens on its own: icons in a grid, or a list with dates, sizes and kinds. v switches.",
    names={"icons": "icons", "details": "details"}))
OPENS_IN = preferences.register(preferences.Setting(
    "fs.start", SECTION, "Herdr pane opens in", ["desktop", "home", "herdr", "pinned"], "desktop",
    hint="Where the file browser in herdr-ai's bottom pane starts. p in fs pins the folder it shows.",
    names={"desktop": "Desktop", "home": "home folder", "herdr": "herdr-ai's folder",
           "pinned": "pinned folder"}))
HIDDEN = preferences.register(preferences.Setting(
    "fs.hidden", SECTION, "Hidden files", [False, True], False,
    hint="Dotfiles and items Finder hides. . toggles them in fs.", names={False: "hidden", True: "shown"}))
SORT = preferences.register(preferences.Setting(
    "fs.sort", SECTION, "Sort by", ["name", "kind", "date", "size"], "name",
    hint="Names sort like Finder (file2 before file10); dates and sizes put the newest and largest first.",
    names={"name": "name", "kind": "kind", "date": "date modified", "size": "size"}))
FOLDERS_FIRST = preferences.register(preferences.Setting(
    "fs.folders_first", SECTION, "Folders on top", [True, False], True,
    hint="Keep folders above files in every sort order.", names={True: "on", False: "off"}))
ON_ENTER = preferences.register(preferences.Setting(
    "fs.enter", SECTION, "Enter on a file", ["preview", "quicklook", "app"], "preview",
    hint="Preview shows text, images, PDFs and movies right here. Space is always Quick Look, o the default app.",
    names={"preview": "preview here", "quicklook": "Quick Look", "app": "default app"}))
ZOOM = preferences.register(preferences.Setting(
    "fs.zoom", SECTION, "Zoom pane for previews", [True, False], True,
    hint="Inside herdr, a short pane fills the tab while it previews a file, and shrinks back after.",
    names={True: "on", False: "off"}))
THEME = preferences.register(preferences.Setting(
    "fs.theme", SECTION, "Theme", ["usage"], "usage",
    hint="t opens the theme menu. The themes are the usage panel's, from themes.json.",
    names={"usage": "match usage panel"}))
themes.track(THEME, extra=["usage"])
ANIMATE = preferences.register(preferences.Setting(
    "fs.animate", SECTION, "Animation", [True, False], True,
    hint="Off draws the highlight still. It also holds still while the pane is not focused.",
    names={True: "on", False: "off"}))
MOUSE = preferences.register(preferences.Setting(
    "fs.mouse", SECTION, "Mouse", [True, False], True,
    hint="Click selects, double-click opens, the wheel moves. Off leaves the mouse to the terminal.",
    names={True: "on", False: "off"}))
PINNED = "fs.pinned"   # the folder p pinned; a plain value, not a settings row

SETTINGS = [VIEW, OPENS_IN, HIDDEN, SORT, FOLDERS_FIRST, ON_ENTER, ZOOM, THEME, ANIMATE, MOUSE]

# ---- text and cells -----------------------------------------------------------
# A cell is (character, fg, bg, bold); fg and bg are RGB tuples or None. A wide
# character's second column is a cell with an empty character.

BLANK = (" ", None, None, False)
_CONTROL = {i: "?" for i in list(range(32)) + [127]}


def char_width(ch):
    if ch < "̀":
        return 1
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text):
    return sum(char_width(ch) for ch in text)


def clean(text):
    """One display row: a newline or tab in a file name shows as ?."""
    return text.translate(_CONTROL)


def take(text, width, from_end=False):
    """The longest start (or end) of text that fits in width columns."""
    out, used = [], 0
    for ch in (reversed(text) if from_end else text):
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(reversed(out) if from_end else out)


def fit(text, width):
    """text cut to width columns with an ellipsis."""
    if text_width(text) <= width:
        return text
    return take(text, width - 1) + "…" if width > 0 else ""


def middle(text, width):
    """Finder's long names: the start, an ellipsis, then the end with the extension."""
    if text_width(text) <= width:
        return text
    if width <= 3:
        return fit(text, width)
    keep = width - 1
    tail = keep // 2
    dot = text.rfind(".")
    if dot > 0 and text_width(text[dot:]) + 2 <= keep - 2:
        tail = max(tail, text_width(text[dot:]) + 2)
    tail = min(tail, keep - 1)
    end = take(text, tail, from_end=True)
    return take(text, keep - text_width(end)) + "…" + end


def cells_of(text, fg=None, bg=None, bold=False):
    out = []
    for ch in text:
        w = char_width(ch)
        if w == 0:
            if out:
                k = len(out) - 1 if out[-1][0] else len(out) - 2
                c = out[k]
                out[k] = (c[0] + ch, c[1], c[2], c[3])
            continue
        out.append((ch, fg, bg, bold))
        if w == 2:
            out.append(("", fg, bg, bold))
    return out


def put(row, x, cells):
    """Blit cells into a row at x, keeping wide characters whole at the edges."""
    width = len(row)
    for k, cell in enumerate(cells):
        at = x + k
        if 0 <= at < width:
            row[at] = cell
    if 0 < x < width and row[x][0] == "" and row[x - 1][0] and char_width(row[x - 1][0][0]) == 2:
        row[x - 1] = BLANK
    end = x + len(cells)
    if 0 < end < width and row[end][0] == "":
        row[end] = BLANK


_FG, _BG = {}, {}


def paint(cells):
    """Cells as one escape-coded string, a style code only where the style changes."""
    out, last = [], None
    for ch, fg, bg, bold in cells:
        if not ch:
            continue
        key = (fg, bg, bold)
        if key != last:
            code = RESET
            if fg:
                code += _FG.get(fg) or _FG.setdefault(fg, ap.fg(fg))
            if bg:
                code += _BG.get(bg) or _BG.setdefault(bg, ap.bg(bg))
            if bold:
                code += BOLD
            out.append(code)
            last = key
        out.append(ch)
    out.append(RESET)
    return "".join(out)


def dim_cell(cell, amount=0.55):
    ch, fg, bg, bold = cell
    return (ch, ap.mix(fg, DARK, amount) if fg else (88, 88, 96) if ch.strip() else None,
            ap.mix(bg, DARK, amount) if bg else None, False)


# ---- themes and the highlight --------------------------------------------------

def usage_theme():
    name = preferences.get("usage.theme")
    return name if name in themes.names() else themes.THEMES[0]["name"]


def resolve_theme(value):
    themes.reload()
    if value in (None, "usage") or value not in themes.names():
        return usage_theme()
    return value


def theme_style(name):
    """The theme's gradient as a style for grad(), or None for brand (each kind's own colors)."""
    stops = themes.stops(name)
    if stops is None:
        return None
    if stops == "rainbow":
        return {"gradient": "rainbow", "color": WHITE, "glow": 1.0}
    return {"gradient": stops, "color": ap.grad({"gradient": stops}, 0.3), "glow": 1.0}


def highlight_style(name, kind):
    style = theme_style(name)
    if style is None:
        return {"gradient": fs_icons.gradient(kind), "color": fs_icons.color(kind), "glow": 1.0}
    return style


def frame_style(name):
    return theme_style(name) or BRAND_FRAME


def title_color(style, pos, t):
    """The usage panel's title gradient, so both panes' titles flow alike."""
    phase = pos / 11 - t * 0.24
    if style.get("gradient") == "rainbow":
        return ap.rainbow_rgb(phase, 0.2)
    return ap.mix(ap.grad(style, phase), WHITE, 0.2)


_PATHS = {}


def ring_path(w, h):
    """(row, col, glyph) clockwise around a w x h box, as agent_picker.border_ring walks it."""
    path = _PATHS.get((w, h))
    if path is None:
        path = [(0, c, "╭" if c == 0 else "╮" if c == w - 1 else "─") for c in range(w)]
        path += [(r, w - 1, "│") for r in range(1, h - 1)]
        path += [(h - 1, c, "╯" if c == w - 1 else "╰" if c == 0 else "─") for c in range(w - 1, -1, -1)]
        path += [(r, 0, "│") for r in range(h - 2, 0, -1)]
        _PATHS[(w, h)] = path
    return path


def ring(style, t, bw, height, boost=0.0, solid=None):
    """{(row, col): (glyph, rgb)} for a rounded box around a bw x height inside, lit by a
    wave that travels around it: agent_picker.border_ring as cells. boost (0 to 1)
    brightens and quickens it while Shift is held."""
    path = ring_path(bw + 2, height + 2)
    n = len(path)
    out = {}
    for i, (r, c, glyph) in enumerate(path):
        if solid is not None:
            out[(r, c)] = (glyph, solid)
            continue
        hue = ap.grad(style, i / n * 2 - t * 0.12)
        dim = ap.mix(hue, (24, 24, 28), 0.5 * (1 - 0.8 * boost))
        bright = ap.mix(hue, WHITE, 0.45 + 0.4 * boost)
        out[(r, c)] = (glyph, ap.mix(dim, bright, ap.wave(i, t, 16.0, 7.0 + 5.0 * boost) ** 2))
    return out


def glow(style, x, y, width, t, boost=0.0):
    """agent_picker.lit's rolling background, brighter with boost."""
    hue = ap.grad(style, x / max(1, width) * 0.9 - y * 0.04 - t * 0.12)
    strength = style.get("glow", 1.0) * (0.13 + 0.17 * boost
                                         + (0.17 + 0.2 * boost) * ap.wave(x - y * 2, t, 22.0, 4.0) ** 3)
    return ap.tint(hue, min(0.8, strength))


class Lit:
    """How the selected item is drawn this frame."""

    def __init__(self, style, t, boost=0.0, active=True, sweep=None):
        self.style, self.t, self.boost, self.active, self.sweep = style, t, boost, active, sweep

    def back(self, x, y, width):
        return glow(self.style, x, y, width, self.t, self.boost) if self.active else INACTIVE_BG

    def ring(self, bw, height):
        return ring(self.style, self.t, bw, height, self.boost, None if self.active else INACTIVE_RING)


def light(rows, lit, width_hint=None):
    """Glow behind every transparent cell of rows (in place), brighten the icon's pixels,
    and draw the launch sweep."""
    width = width_hint or (len(rows[0]) if rows else 1)
    for y, row in enumerate(rows):
        for x, (ch, fg, bg, bold) in enumerate(row):
            if bg is None:
                bg = lit.back(x, y, width)
            elif lit.boost:
                bg = ap.mix(bg, WHITE, 0.12 * lit.boost)
            if fg and lit.active and not bold:
                fg = ap.mix(fg, WHITE, 0.06 + 0.18 * lit.boost)
            row[x] = (ch, fg, bg, bold)
        if lit.sweep is not None:
            for x in range(len(row)):
                if abs(x + 0.5 - len(row) / 2) <= lit.sweep * len(row) / 2:
                    shade = ap.mix(ap.grad(lit.style, x / max(1, len(row)) - lit.t * 0.4), WHITE,
                                   0.2 * ap.wave(x, lit.t, 8.0, 12.0))
                    row[x] = ("█", shade, None, False)
    return rows


def boxed(rows, lit):
    """rows inside the ring: one more row above and below, one more column each side."""
    height, bw = len(rows), len(rows[0]) if rows else 0
    edge = lit.ring(bw, height)
    top = [(edge[(0, c)][0], edge[(0, c)][1], None, False) for c in range(bw + 2)]
    bottom = [(edge[(height + 1, c)][0], edge[(height + 1, c)][1], None, False) for c in range(bw + 2)]
    out = [top]
    for y, row in enumerate(rows):
        left, right = edge[(y + 1, 0)], edge[(y + 1, bw + 1)]
        out.append([(left[0], left[1], None, False)] + row + [(right[0], right[1], None, False)])
    return out + [bottom]


# ---- the folder ----------------------------------------------------------------

class Entry:
    __slots__ = ("name", "path", "is_dir", "is_link", "broken", "size", "mtime", "ctime", "mode",
                 "kind", "hidden", "label")

    def __init__(self, name, path, is_dir=False, is_link=False, broken=False, size=0, mtime=0.0, ctime=0.0,
                 mode=0, kind="file", hidden=False):
        self.name, self.path, self.is_dir, self.is_link, self.broken = name, path, is_dir, is_link, broken
        self.size, self.mtime, self.ctime, self.mode, self.kind, self.hidden = size, mtime, ctime, mode, kind, hidden
        self.label = None

    @property
    def opens(self):
        """Enter goes into it: a folder, not an app or a package Finder opens instead."""
        return self.is_dir and self.kind not in ("app", "package")

    def kind_text(self):
        if self.label is None:
            self.label = fs_icons.label(self.kind, self.name) + (" alias" if self.is_link else "")
        return self.label


def is_hidden(name, st):
    if name.startswith("."):
        return True
    if getattr(st, "st_flags", 0) & getattr(stat, "UF_HIDDEN", 0x8000):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2))


def scan(path):
    """(every entry in path, hidden ones included; an error message or None)."""
    try:
        listing = os.scandir(path)
    except OSError as exc:
        return [], exc.strerror or str(exc)
    entries = []
    with listing:
        for item in listing:
            try:
                link = item.is_symlink()
            except OSError:
                link = False
            broken = False
            try:
                st = item.stat()
            except OSError:
                try:
                    st = item.stat(follow_symlinks=False)
                    broken = True
                except OSError:
                    continue
            is_dir = stat.S_ISDIR(st.st_mode)
            executable = not is_dir and os.name != "nt" and bool(st.st_mode & 0o111)
            try:
                kind = fs_icons.classify(item.path, is_dir=is_dir, is_link=link, executable=executable)
            except Exception:
                kind = "folder" if is_dir else "file"
            try:
                own = item.stat(follow_symlinks=False) if link else st
            except OSError:
                own = st
            entries.append(Entry(item.name, Path(item.path), is_dir, link, broken, st.st_size, st.st_mtime,
                                 getattr(st, "st_birthtime", st.st_ctime), st.st_mode, kind,
                                 is_hidden(item.name, own)))
    return entries, None


def natural(name):
    """Sort key that puts file2 before file10, ignoring case, like Finder."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.casefold())]


def arrange(entries, by="name", folders_first=True):
    if by == "kind":
        key = lambda e: (e.kind_text().casefold(), natural(e.name))
    elif by == "date":
        key = lambda e: (-e.mtime, natural(e.name))
    elif by == "size":
        key = lambda e: (-(e.size if not e.is_dir else -1), natural(e.name))
    else:
        key = lambda e: natural(e.name)
    if folders_first:
        return sorted(entries, key=lambda e: (not e.opens, key(e)))
    return sorted(entries, key=key)


def human_size(n):
    """Finder's decimal sizes: 12 bytes, 4 KB, 1.2 MB, 340 GB."""
    if n < 1000:
        return f"{n} byte" + ("s" if n != 1 else "")
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1000
        if value < 999.5 or unit == "PB":
            return f"{value:.1f} {unit}" if value < 9.95 and unit != "KB" else f"{value:.0f} {unit}"
    return f"{n} bytes"


def when(epoch, width=24, now=None):
    """The Date Modified column, longest form that fits: Today at 3:04 PM, Nov 3, 2026, 11/3/26."""
    try:
        moment = datetime.fromtimestamp(epoch)
    except (OverflowError, OSError, ValueError):
        return "--"
    now = now or datetime.now()
    clock = moment.strftime("%I:%M %p").lstrip("0")
    if moment.date() == now.date():
        forms = (f"Today at {clock}", clock, "Today")
    elif moment.date() == (now - timedelta(days=1)).date():
        forms = (f"Yesterday at {clock}", "Yesterday")
    else:
        day = f"{moment.strftime('%b')} {moment.day}, {moment.year}"
        forms = (f"{day} at {clock}", day, f"{moment.month}/{moment.day}/{moment.year % 100:02d}")
    return next((f for f in forms if len(f) <= width), forms[-1][:max(0, width)])


def show_path(path):
    home = str(Path.home())
    text = str(path)
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def tail_path(path, width):
    """The path, cut from the left so the folder you are in stays readable."""
    text = clean(show_path(path))
    if text_width(text) <= width:
        return text
    parts = text.split(os.sep)
    for k in range(1, len(parts)):
        shorter = "…" + os.sep + os.sep.join(parts[k:])
        if text_width(shorter) <= width:
            return shorter
    return "…" + fit(parts[-1], max(1, width - 1))[-max(1, width - 1):]


def desktop():
    home = Path.home()
    candidates = [home / "Desktop"]
    if os.name == "nt":
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(var):
                candidates.insert(0, Path(os.environ[var]) / "Desktop")
    return next((p for p in candidates if p.is_dir()), home)


def herdr_start(herdr_dir):
    """Where the herdr-ai pane opens, per the Opens in setting; home if that folder is gone."""
    choice = preferences.get(OPENS_IN.key)
    if choice == "home":
        target = Path.home()
    elif choice == "herdr":
        target = Path(herdr_dir)
    elif choice == "pinned" and preferences.get(PINNED):
        target = Path(str(preferences.get(PINNED))).expanduser()
    else:
        target = desktop()
    return target if target.is_dir() else Path.home()


def inside_herdr():
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_PANE_ID"))


# ---- previews ------------------------------------------------------------------

TEXT_LIMIT = 512 * 1024
CODE = {"python", "javascript", "typescript", "rust", "go", "c", "cpp", "java", "kotlin", "swift", "ruby",
        "php", "csharp", "lua", "r", "shell", "sql", "dart", "html", "css", "json", "yaml", "toml", "xml",
        "config", "makefile", "lock"}
HASH_COMMENTS = {"python", "shell", "ruby", "yaml", "toml", "r", "config", "makefile"}
SLASH_COMMENTS = {"javascript", "typescript", "rust", "go", "c", "cpp", "java", "kotlin", "swift", "php",
                  "csharp", "dart", "css"}
DASH_COMMENTS = {"sql", "lua"}
PICTURE_KINDS = {"image", "pdf", "video", "document", "spreadsheet", "presentation", "font", "app", "diskimage",
                 "package"}
WRAPPED = {"text", "markdown", "license", "document", "file"}


def looks_like_text(path):
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(8192)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        if exc.start >= len(chunk) - 3:
            return True   # the sample cut a character in half
    odd = sum(1 for b in chunk if b < 9 or 13 < b < 32)
    return odd / len(chunk) < 0.02


def read_bmp(data):
    """(width, height, pixel(x, y) -> (r, g, b, a)) from an uncompressed 24 or 32-bit BMP,
    the format sips writes, or None."""
    if len(data) < 54 or data[:2] != b"BM":
        return None
    word = lambda at, n=4, signed=False: int.from_bytes(data[at:at + n], "little", signed=signed)
    offset, header = word(10), word(14)
    width, height = word(18, signed=True), word(22, signed=True)
    bpp, compression = word(28, 2), word(30)
    if bpp not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        return None
    top_down, height = height < 0, abs(height)
    stride = (width * bpp + 31) // 32 * 4
    if offset + stride * height > len(data):
        return None
    masks = None
    if compression == 3 and len(data) >= 66:
        masks = [word(54), word(58), word(62), word(66) if header >= 56 else 0]
    step = bpp // 8

    def channel(value, mask):
        if not mask:
            return 255
        shift = (mask & -mask).bit_length() - 1
        top = mask >> shift
        return ((value & mask) >> shift) * 255 // max(1, top)

    def pixel(x, y):
        row = y if top_down else height - 1 - y
        i = offset + row * stride + x * step
        if bpp == 24:
            return data[i + 2], data[i + 1], data[i], 255
        if masks:
            value = int.from_bytes(data[i:i + 4], "little")
            return tuple(channel(value, m) for m in masks)
        return data[i + 2], data[i + 1], data[i], data[i + 3]

    # Some writers leave alpha at zero everywhere; that means opaque, not invisible.
    samples = [pixel(width * k // 8, height * j // 8)[3] for k in range(8) for j in range(8)]
    if not any(samples):
        plain = pixel
        pixel = lambda x, y: plain(x, y)[:3] + (255,)
    return width, height, pixel


def rasterize(path, kind, size=480):
    """An image of any file macOS can show: sips for pictures, Quick Look's thumbnail for
    PDFs, movies, documents, fonts and apps. None elsewhere or when neither can."""
    if sys.platform != "darwin" or not shutil.which("sips"):
        return None

    def run(argv, timeout=20):
        try:
            return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=timeout).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    with tempfile.TemporaryDirectory(prefix="fs-preview-") as tmp:
        out = os.path.join(tmp, "picture.bmp")
        done = kind == "image" and run(["sips", "-s", "format", "bmp", "-Z", str(size), str(path), "--out", out])
        if not (done and os.path.exists(out)):
            if not shutil.which("qlmanage"):
                return None
            run(["qlmanage", "-t", "-s", str(size), "-o", tmp, str(path)], timeout=25)
            thumbs = [n for n in os.listdir(tmp) if n.lower().endswith(".png")]
            if not thumbs or not run(["sips", "-s", "format", "bmp", os.path.join(tmp, thumbs[0]), "--out", out]):
                return None
        try:
            with open(out, "rb") as handle:
                return read_bmp(handle.read())
        except OSError:
            return None


def picture_rows(image, width, height):
    """The image fitted into width x height cells, two pixels per cell (▀ over its background)."""
    iw, ih, pixel = image
    scale = min(width / iw, height * 2 / ih)
    tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
    left, top = (width - tw) // 2, (height * 2 - th) // 2

    def sample(px, py):
        if not (0 <= px - left < tw and 0 <= py - top < th):
            return None
        x0, y0 = (px - left) / scale, (py - top) / scale
        span = max(1.0, 1 / scale)
        total, seen = [0, 0, 0], 0
        for dx in (0.25, 0.75):
            for dy in (0.25, 0.75):
                r, g, b, a = pixel(min(iw - 1, int(x0 + dx * span)), min(ih - 1, int(y0 + dy * span)))
                if a >= 128:
                    total[0] += r
                    total[1] += g
                    total[2] += b
                    seen += 1
        return tuple(v // seen for v in total) if seen >= 2 else None

    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            upper, lower = sample(x, 2 * y), sample(x, 2 * y + 1)
            if upper is None and lower is None:
                row.append(BLANK)
            elif upper is None:
                row.append(("▄", lower, None, False))
            else:
                row.append(("▀", upper, lower, False))
        rows.append(row)
    return rows


class Preview:
    """A file shown full screen: its text, a picture of it, or its details."""

    def __init__(self, entry, theme):
        self.entry, self.theme = entry, theme
        self.top = self.left = 0
        self.lines, self.note, self.image, self.fenced = [], "", None, []
        self.wrap = entry.kind in WRAPPED
        self.mode = "info"
        self.loading = False
        self.cache = {}
        self.zoomed = False
        if entry.broken or entry.is_dir and entry.kind not in ("app", "package"):
            return
        picture = entry.kind in PICTURE_KINDS and sys.platform == "darwin"
        if not picture and looks_like_text(entry.path):
            self.load_text()
        elif picture:
            self.mode, self.loading = "picture", True
            threading.Thread(target=self.load_picture, daemon=True).start()

    def load_text(self):
        try:
            with open(self.entry.path, "rb") as handle:
                data = handle.read(TEXT_LIMIT + 1)
        except OSError as exc:
            self.note = exc.strerror or str(exc)
            return
        if len(data) > TEXT_LIMIT:
            data = data[:TEXT_LIMIT]
            self.note = f"the first {TEXT_LIMIT // 1024} KB"
        self.mode = "text"
        text = data.decode("utf-8", errors="replace")
        self.lines = [line.expandtabs(4).translate(_CONTROL) for line in text.splitlines()] or [""]
        # Markdown code fences, worked out once so a block stays shaded wherever it scrolls.
        self.fenced, inside = [], False
        for line in self.lines:
            fence = self.entry.kind == "markdown" and line.lstrip().startswith("```")
            self.fenced.append(inside or fence)
            inside = inside != fence

    def load_picture(self):
        image = rasterize(self.entry.path, self.entry.kind)
        if image is None and looks_like_text(self.entry.path):
            self.load_text()
        elif image is None:
            self.mode, self.note = "info", "no preview for this file · space opens Quick Look"
        else:
            self.image, self.note = image, f"{image[0]} × {image[1]}" if self.entry.kind == "image" else ""
        self.loading = False

    def display(self, width):
        """[(line number or None, text)] for the text at this width, wrapped when wrapping."""
        key = (width, self.wrap)
        if key not in self.cache:
            out = []
            for n, line in enumerate(self.lines, 1):
                if not self.wrap or text_width(line) <= width:
                    out.append((n, line))
                    continue
                first = True
                while line:
                    piece, used = [], 0
                    for ch in line:
                        w = char_width(ch)
                        if used + w > width:
                            break
                        piece.append(ch)
                        used += w
                    piece = "".join(piece) or line[0]
                    space = piece.rfind(" ")
                    if len(piece) < len(line) and space > len(piece) // 2:
                        piece = piece[:space + 1]   # break between words
                    out.append((n if first else None, piece.rstrip(" ") if not first or n else piece))
                    line, first = line[len(piece):], False
            self.cache = {key: out}
        return self.cache[key]

    def handle(self, key, page):
        if self.mode != "text":
            return
        total = len(self.display(page[0]))
        rows = page[1]
        moves = {"down": 1, "j": 1, "up": -1, "k": -1, " ": rows - 1, "b": -(rows - 1),
                 "g": -total, "G": total}
        if key in moves:
            self.top = max(0, min(max(0, total - rows), self.top + moves[key]))
        elif key in ("right", "l") and not self.wrap:
            self.left += 8
        elif key in ("left", "h") and not self.wrap:
            self.left = max(0, self.left - 8)
        elif key == "w":
            self.wrap, self.top, self.left = not self.wrap, 0, 0


# ---- layout ------------------------------------------------------------------------

ICON_SIZE = {"large": (8, 3), "small": (4, 2), "mini": (2, 1)}
# (icon size, name lines, tile width, ring space), best first; see App.grid.
TILES = (("large", 2, 16, True), ("large", 1, 16, True), ("small", 1, 13, True),
         ("mini", 1, 26, True), ("mini", 1, 26, False))


class Grid:
    def __init__(self, size, lines, tile_w, lids, width, height, n):
        self.size, self.lines, self.tile_w, self.lids = size, lines, tile_w, lids
        icon_h = ICON_SIZE[size][1]
        self.inline = size == "mini"
        self.tile_h = (1 if self.inline else icon_h + lines) + (2 if lids else 0)
        self.ncols = max(1, (width - 2) // tile_w)
        spare = width - 2 - self.ncols * tile_w
        self.gap = min(6, spare // max(1, self.ncols)) if self.ncols > 1 else 0
        self.left = 1 + (spare - self.gap * (self.ncols - 1)) // 2 if n < self.ncols else 1 + self.gap // 2
        if n < self.ncols:
            self.left = 1 + self.gap // 2
        self.rows_fit = max(1, height // self.tile_h)
        self.nrows = max(1, math.ceil(n / self.ncols))
        self.rows_vis = min(self.rows_fit, self.nrows)
        extra = height - self.rows_vis * self.tile_h
        self.row_gap = 1 if self.rows_vis > 1 and extra >= self.rows_vis - 1 else 0
        used = self.rows_vis * self.tile_h + (self.rows_vis - 1) * self.row_gap
        self.top_pad = 0   # top-aligned like Finder, whatever room is left over

    @staticmethod
    def choose(width, height, n):
        fits = [height // ((1 if s == "mini" else ICON_SIZE[s][1] + lines) + (2 if lids else 0))
                for s, lines, _, lids in TILES]
        # Two rows of the biggest tiles, else one row of them (pane width gives plenty of
        # columns), then smaller tiles, then a list of names.
        if fits[0] >= 2 or (fits[0] >= 1 and fits[1] < 2):
            pick = 0
        elif fits[1] >= 1:
            pick = 1
        else:
            pick = next((k for k in range(2, len(TILES)) if fits[k] >= 1), len(TILES) - 1)
        size, lines, tile_w, lids = TILES[pick]
        if size == "mini":
            tile_w = max(18, min(tile_w, width - 2))
        return Grid(size, lines, tile_w, lids, width, height, n)


def tile_name(name, width, lines):
    """A name under an icon: whole if it fits, else broken onto a second line at a space,
    dash, dot or underscore, the last line shortened in the middle like Finder's."""
    name = clean(name)
    if text_width(name) <= width:
        return [name]
    if lines < 2:
        return [middle(name, width)]
    cut, used = 0, 0
    for k, ch in enumerate(name):
        used += char_width(ch)
        if used > width:
            break
        cut = k + 1
    breaks = [k + 1 for k in range(1, cut) if name[k] in " -_."]
    head = name[:breaks[-1] if breaks else cut]
    rest = name[len(head):].lstrip(" ")
    return [head.rstrip(" "), middle(rest, width)]


class Region:
    """A rectangle of content drawn fresh every frame: the selected item."""

    def __init__(self, x, y, w, h, draw):
        self.x, self.y, self.w, self.h, self.draw = x, y, w, h, draw


# ---- screens -----------------------------------------------------------------------

class ThemeMenu:
    def __init__(self, current):
        self.items = ["usage"] + themes.names()
        self.index = self.items.index(current) if current in self.items else 0
        self.original = current

    @property
    def value(self):
        return self.items[self.index]

    def handle(self, key):
        if key in ("up", "k"):
            self.index = (self.index - 1) % len(self.items)
        elif key in ("down", "j", "\t"):
            self.index = (self.index + 1) % len(self.items)
        elif key in ("\r", "\n", " ", "shift-enter"):
            return "apply"
        elif key in ("esc", "q", "t", "\x7f", "\b"):
            return "cancel"
        return None


class Confirm:
    def __init__(self, kind, target):
        self.kind, self.target, self.choice = kind, Path(target), 0


HELP = [("←↑↓→", "move"), ("⏎", "open a folder, preview a file"),
        ("⇧⏎", "move this tab's agents here (herdr-ai)"), ("esc  ⌫", "back to the enclosing folder"),
        ("[  ]", "back, forward"), ("~", "home folder"), ("v", "icons or details"),
        (",", "sort by name, kind, date, size"), (".", "show or hide hidden files"), ("/", "filter by name"),
        ("space", "Quick Look"), ("o", "open with the default app"), ("e", "edit in $EDITOR"),
        ("y", "copy the path"), ("!", "a shell in this folder"), ("p", "pin this folder for herdr-ai"),
        ("r", "reload"), ("t", "themes"), ("s", "settings"), ("⇧Q", "quit to the shell")]


class SegmentScreen:
    """agent_picker.Screen for rows made of segments. A row is a list of (column, painted
    text); when a row's segments start at the same columns as last frame, only the ones
    that changed are resent, so the glow on one tile costs that tile, not the whole row."""

    def __init__(self, terminal):
        self.terminal, self.size, self.rows = terminal, None, []

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
            if before is not None and len(before) == len(row) and all(a[0] == b[0] for a, b in zip(row, before)):
                out += [f"{ESC}[{y + 1};{col}H{text}" for (col, text), (_, old) in zip(row, before) if text != old]
            else:
                out.append(f"{ESC}[{y + 1};1H{ESC}[2K" + "".join(f"{ESC}[{y + 1};{col}H{text}" for col, text in row))
        out += [f"{ESC}[{y + 1};1H{ESC}[2K" for y in range(len(frame), min(len(self.rows), rows))]
        self.rows = frame
        if out or clear:
            self.terminal.write(f"{ESC}[?2026h{clear}{''.join(out)}{ESC}[?2026l")


def strings_frame(lines, cols, rows):
    """Plain lines (agent_picker's panels) as segment rows."""
    return [[(1, line)] for line in ap.centered(lines, cols, rows)]


# ---- the browser ---------------------------------------------------------------------

class App:
    def __init__(self, start, select=None, view=None, hidden=None, sort=None, theme=None, herdr=False,
                 herdr_dir=None):
        self.cwd = Path(start)
        self.view_override, self.hidden_override, self.sort_override = view, hidden, sort
        self.theme_override = theme
        self.herdr, self.herdr_dir = herdr, Path(herdr_dir or os.getcwd())
        self.all, self.entries, self.error = [], [], None
        self.index, self.top = 0, 0
        self.memory, self.back, self.forward = {}, [], []
        self.query, self.typing = None, False
        self.screen = "browse"
        self.preview = self.theme_menu = self.confirm = self.settings = None
        self.preview_theme = None
        self.toast = None              # (text, color, until)
        self.theme_toast = 0.0
        self.routing = None            # {"target", "started", "result"}
        self.sweep_at = None
        self.focused = True
        self.bare_shift = False        # the terminal reports Shift on its own, so its release is seen
        self.shift_until = 0.0
        self.boost = 0.0
        self.last_frame = time.monotonic()
        self.ncols, self.hits, self.page = 1, [], 1
        self.click = (None, 0.0)
        self.stamp, self.checked = None, 0.0
        self.free = (None, 0.0, None)
        self.quicklook = None
        self.terminal = None
        self.start = time.monotonic()
        self.static = None
        self.load(select=select)

    # -- state --

    @property
    def view(self):
        return self.view_override or preferences.get(VIEW.key)

    @property
    def show_hidden(self):
        return preferences.get(HIDDEN.key) if self.hidden_override is None else self.hidden_override

    @property
    def sort(self):
        return self.sort_override or preferences.get(SORT.key)

    def theme(self):
        if self.preview_theme is not None:
            return resolve_theme(self.preview_theme)
        return resolve_theme(self.theme_override or preferences.get(THEME.key))

    def selected(self):
        return self.entries[self.index] if self.entries and 0 <= self.index < len(self.entries) else None

    def animating(self):
        return bool(preferences.get(ANIMATE.key)) and self.focused

    def shift_held(self):
        held = bool(getattr(self.terminal, "shift", False))
        if self.bare_shift:
            return held
        return held and time.monotonic() < self.shift_until

    def stamp_of(self, path):
        try:
            st = os.stat(path)
            return st.st_mtime_ns, st.st_ino
        except OSError:
            return None

    def load(self, select=None):
        """Read the folder again, keeping the selection on the same name."""
        current = select or (self.selected().name if self.selected() else self.memory.get(str(self.cwd)))
        self.all, self.error = scan(self.cwd)
        self.stamp, self.checked = self.stamp_of(self.cwd), time.monotonic()
        self.refilter(current)

    def refilter(self, select=None):
        current = select or (self.selected().name if self.selected() else None)
        shown = [e for e in self.all if self.show_hidden or not e.hidden]
        if self.query:
            q = self.query.casefold()
            shown = [e for e in shown if q in e.name.casefold()]
        self.entries = arrange(shown, self.sort, preferences.get(FOLDERS_FIRST.key))
        names = [e.name for e in self.entries]
        self.index = names.index(current) if current in names else min(self.index, max(0, len(names) - 1))
        if current not in names and self.query:
            self.index = 0
        self.static = None

    def go(self, path, select=None, record=True):
        path = Path(path)
        if not path.is_dir():
            self.say(f"can't open {path.name or path}", HOT)
            return False
        if self.selected():
            self.memory[str(self.cwd)] = self.selected().name
        if record and path != self.cwd:
            self.back.append(self.cwd)
            self.forward.clear()
        self.cwd, self.query, self.typing, self.index, self.top = path, None, False, 0, 0
        self.load(select=select or self.memory.get(str(path)))
        return True

    def up(self):
        parent = self.cwd.parent
        if parent != self.cwd:
            self.go(parent, select=self.cwd.name)

    def say(self, text, color=TEXT, seconds=3.5):
        self.toast = (text, color, time.monotonic() + seconds)

    def watch(self):
        """Pick up files added, removed or renamed in this folder, about once a second."""
        now = time.monotonic()
        if now - self.checked < 1.0:
            return
        self.checked = now
        if not self.cwd.is_dir():
            parent = next((p for p in self.cwd.parents if p.is_dir()), Path.home())
            self.say(f"{self.cwd.name} is gone", WARN)
            self.go(parent, record=False)
            return
        stamp = self.stamp_of(self.cwd)
        if stamp != self.stamp:
            self.load()

    def free_space(self):
        path, at, text = self.free
        if path != self.cwd or time.monotonic() - at > 10:
            try:
                text = human_size(shutil.disk_usage(self.cwd).free)
            except OSError:
                text = None
            self.free = (self.cwd, time.monotonic(), text)
        return self.free[2]

    # -- drawing --

    def frame(self, cols, rows, t):
        if self.screen == "preview" and self.preview:
            return self.preview_frame(cols, rows, t)
        overlay = self.overlay_lines(cols, rows, t)
        if overlay is not None:
            return self.with_overlay(cols, rows, t, overlay)
        return self.browse_frame(cols, rows, t)

    def boost_now(self):
        now = time.monotonic()
        dt, self.last_frame = now - self.last_frame, now
        target = 1.0 if self.shift_held() or self.routing else 0.0
        self.boost += (target - self.boost) * min(1.0, dt * 14)
        if abs(self.boost - target) < 0.01:
            self.boost = target
        return self.boost

    def lit_for(self, entry, t, x_frac=0.5):
        style = highlight_style(self.theme(), entry.kind if entry else "folder")
        sweep = None
        if self.sweep_at is not None:
            elapsed = time.monotonic() - self.sweep_at
            if elapsed < 0.3:
                sweep = min(1.0, (elapsed / 0.18) ** 1.6)
            else:
                self.sweep_at = None
        return Lit(style, t if self.animating() else 0.4, self.boost, self.focused, sweep)

    def accent(self, x_frac):
        style = theme_style(self.theme())
        return None if style is None else ap.grad(style, x_frac * 0.85)

    def content(self, W, H, t):
        """(rows of static cells W x H, animated regions, hit boxes relative to the content)."""
        rows = [[BLANK] * W for _ in range(H)]
        if self.error:
            self.center(rows, [(f"Can't open this folder: {self.error}", HOT, True),
                               ("⌫ goes to the enclosing folder", SOFT, False)])
            return rows, [], []
        if not self.entries:
            hidden = sum(1 for e in self.all if e.hidden) if not self.show_hidden else 0
            lines = ([(f'No items match "{self.query}"', TEXT, True), ("esc clears the filter", SOFT, False)]
                     if self.query else [("Empty folder", SOFT, True)] +
                     ([(f"{hidden} hidden item{'s' * (hidden != 1)} · . shows them", SOFT, False)] if hidden else []))
            self.center(rows, lines)
            return rows, [], []
        if self.view == "details" and W >= 30:
            return self.details(rows, W, H, t)
        return self.icons(rows, W, H, t)

    @staticmethod
    def center(rows, lines):
        H, W = len(rows), len(rows[0]) if rows else 0
        top = max(0, (H - len(lines)) // 2)
        for k, (text, color, bold) in enumerate(lines):
            if top + k < H:
                text = fit(clean(text), W - 2)
                put(rows[top + k], max(0, (W - text_width(text)) // 2), cells_of(text, color, None, bold))

    def icons(self, rows, W, H, t):
        n = len(self.entries)
        g = Grid.choose(W, H, n)
        self.ncols, self.page = g.ncols, g.rows_vis
        row_of = self.index // g.ncols
        if row_of < self.top:
            self.top = row_of
        elif row_of >= self.top + g.rows_vis:
            self.top = row_of - g.rows_vis + 1
        self.top = max(0, min(self.top, g.nrows - g.rows_vis))
        regions, hits = [], []
        for vr in range(g.rows_vis):
            for c in range(g.ncols):
                i = (self.top + vr) * g.ncols + c
                if i >= n:
                    break
                x = g.left + c * (g.tile_w + g.gap)
                y = g.top_pad + vr * (g.tile_h + g.row_gap)
                if x + g.tile_w > W or y + g.tile_h > H:
                    continue
                entry, frac = self.entries[i], (c + 0.5) / g.ncols
                hits.append((x, y, g.tile_w, g.tile_h, i))
                if i == self.index:
                    regions.append(Region(x, y, g.tile_w, g.tile_h,
                                          lambda t, e=entry, f=frac: self.tile(e, g, self.accent(f), self.lit_for(e, t))))
                else:
                    for k, line in enumerate(self.tile(entry, g, self.accent(frac))):
                        put(rows[y + k], x, line)
        self.scrollbar = (self.top, g.rows_vis, g.nrows)
        return rows, regions, hits

    def tile(self, entry, g, accent, lit=None):
        """One icon tile's cells: ring space, the icon centred, its name under it."""
        side = 1 if g.lids else 0
        iw, ih = g.tile_w - 2 * side, g.tile_h - 2 * side
        inner = [[BLANK] * iw for _ in range(ih)]
        name_color = WHITE if lit else SOFT if entry.hidden else TEXT
        try:
            icon = fs_icons.icon(entry.kind, g.size, accent, entry.is_link)
        except Exception:
            icon = [[BLANK] * ICON_SIZE[g.size][0] for _ in range(ICON_SIZE[g.size][1])]
        if entry.hidden and not lit:
            icon = [[dim_cell(c, 0.45) if c[1] or c[2] else c for c in row] for row in icon]
        if g.inline:
            put(inner[0], 1, icon[0])
            put(inner[0], 4, cells_of(middle(clean(entry.name), iw - 5), name_color, None, bool(lit)))
        else:
            x = (iw - len(icon[0])) // 2
            for y, row in enumerate(icon):
                put(inner[y], x, row)
            names = tile_name(entry.name, iw, g.lines)
            # A one-line name takes the last row, leaving a row between it and the icon,
            # so the icon and name sit evenly inside the highlight's ring.
            first = len(icon) + g.lines - len(names)
            for k, line in enumerate(names):
                put(inner[first + k], (iw - text_width(line)) // 2, cells_of(line, name_color, None, bool(lit)))
        if lit is None:
            if not side:
                return inner
            pad = [BLANK] * g.tile_w
            return [list(pad)] + [[BLANK] + row + [BLANK] for row in inner] + [list(pad)]
        light(inner, lit)
        return boxed(inner, lit) if side else inner

    def details(self, rows, W, H, t):
        """Finder's list view: Name, Date Modified, Size and Kind, the columns that fit. Rows
        sit two columns in, so the selected one's ring takes the margin, as in the selector."""
        n = len(self.entries)
        inner = W - 4
        size_w = 9 if inner >= 44 else 0
        kind_w = min(24, max(12, inner // 5)) if inner >= 64 else 0
        date_w = 22 if inner >= 92 else 13 if inner >= 56 else 0
        name_w = inner - 6 - sum(w + 2 for w in (date_w, size_w, kind_w) if w)
        lids = H >= 8
        columns, x = [("name", 5, name_w)], 5 + name_w
        for key, width in (("date", date_w), ("size", size_w), ("kind", kind_w)):
            if width:
                columns.append((key, x + 2, width))
                x += 2 + width
        header = [BLANK] * inner
        titles = {"name": "Name", "date": "Date Modified", "size": "Size", "kind": "Kind"}
        for key, at, width in columns:
            label = fit(titles[key] + (" ▾" if self.sort == key else ""), width)
            put(header, at + (width - text_width(label) if key == "size" else 0),
                cells_of(label, LABEL, None, self.sort == key))
        put(rows[0], 2, header)
        room = max(1, H - 1 - (2 if lids else 0))
        self.page = room
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + room:
            self.top = self.index - room + 1
        self.top = max(0, min(self.top, max(0, n - room)))
        regions, hits, y = [], [], 1

        def line(entry, selected):
            cells = [BLANK] * inner
            try:
                put(cells, 1, fs_icons.icon(entry.kind, "mini", self.accent(0.3), entry.is_link)[0])
            except Exception:
                pass
            main = WHITE if selected else SOFT if entry.hidden else TEXT
            put(cells, 5, cells_of(middle(clean(entry.name), name_w), main, None, selected))
            side = TEXT if selected else SOFT
            for key, at, width in columns[1:]:
                if key == "date":
                    put(cells, at, cells_of(when(entry.mtime, width), side))
                elif key == "size":
                    text = "--" if entry.is_dir else human_size(entry.size)
                    put(cells, at + width - text_width(text), cells_of(text, side))
                else:
                    put(cells, at, cells_of(fit(entry.kind_text(), width), side))
            return cells

        for i in range(self.top, min(n, self.top + room)):
            entry = self.entries[i]
            if i == self.index:
                h = 3 if lids else 1
                if y + h > H:
                    break
                regions.append(Region(1, y, W - 2, h, lambda t, e=entry: self.lit_row(line(e, True), e, t, lids)))
                hits.append((0, y, W, h, i))
                y += h
            else:
                if y >= H:
                    break
                put(rows[y], 2, line(entry, False))
                hits.append((0, y, W, 1, i))
                y += 1
        self.ncols = 1
        self.scrollbar = (self.top, room, n)
        return rows, regions, hits

    def lit_row(self, cells, entry, t, lids):
        lit = self.lit_for(entry, t)
        light([cells], lit)
        if lids:
            return boxed([cells], lit)
        return [[BLANK] + cells + [BLANK]]

    def chrome(self, cols, rows, t, still=False, title=None, tag=None, status=None, hints=None):
        """(top row, bottom row, left edges, right edges) as cells: the usage panel's ring,
        the folder (or title) set into the top, the status and key hints into the bottom."""
        theme = self.theme()
        style = frame_style(theme)
        animate = self.animating() and not still
        tick = t if animate else 0.4
        edge = ring(style, tick, cols - 2, rows - 2)
        top = [(edge[(0, c)][0], edge[(0, c)][1], None, False) for c in range(cols)]
        bottom = [(edge[(rows - 1, c)][0], edge[(rows - 1, c)][1], None, False) for c in range(cols)]
        left = [(edge[(r, 0)][0], edge[(r, 0)][1], None, False) for r in range(1, rows - 1)]
        right = [(edge[(r, cols - 1)][0], edge[(r, cols - 1)][1], None, False) for r in range(1, rows - 1)]
        themed = theme_style(theme)
        star = ap.mix(*GOLD, ap.wave(0, t, speed=3.0)) if animate else (240, 195, 80)
        tag = self.top_tag() if tag is None else tag
        room = cols - 8 - (text_width(tag) + 3 if tag else 0)
        title = tail_path(self.cwd, max(4, room)) if title is None else fit(clean(title), max(4, room))
        put(top, 2, cells_of(" ", TEXT))
        put(top, 3, [("✦", title_color(themed, 0, tick) if themed else star, None, False)])
        put(top, 4, cells_of(" ", TEXT))
        flow = cells_of(title, TEXT, None, True)
        if themed:
            flow = [(c[0], title_color(themed, k, tick), None, True) for k, c in enumerate(flow)]
        put(top, 5, flow + [BLANK])
        if tag and cols >= 6 + text_width(title) + text_width(tag) + 3:
            words = cells_of(tag, TEXT, None, True)
            if themed and self.theme_toast > time.monotonic():
                words = [(c[0], title_color(themed, k, tick), None, True) for k, c in enumerate(words)]
            put(top, cols - 2 - len(words), words)
        status, color = self.status() if status is None else status
        hints = next((h for h in (self.hints() if hints is None else hints)
                      if len(h) + text_width(status) + 6 <= cols), "")
        if status:
            status = fit(status, max(0, cols - 6 - len(hints)))
            put(bottom, 2, cells_of(status, color, None, color not in (SOFT, LABEL)))
        if hints:
            put(bottom, cols - 2 - len(hints), cells_of(hints, SOFT))
        if not still:
            scroll = getattr(self, "scrollbar", None)
            if scroll and scroll[2] > scroll[1] and rows - 2 >= 3 and self.screen == "browse":
                first, shown, total = scroll
                span = rows - 2
                size = max(1, round(span * shown / total))
                at = round((span - size) * first / max(1, total - shown))
                for r in range(at, min(span, at + size)):
                    right[r] = ("┃", ap.mix(right[r][1] or TEXT, WHITE, 0.5), None, False)
        return top, bottom, left, right

    def top_tag(self):
        if self.theme_toast > time.monotonic():
            return f" ◂ {self.theme()} ▸ "
        if self.query is not None and (self.query or self.typing):
            return f" {len(self.entries)} of {len([e for e in self.all if self.show_hidden or not e.hidden])} "
        return ""

    def status(self):
        """The bottom border's left text and its color."""
        now = time.monotonic()
        if self.typing:
            return f" / {self.query or ''}▏ ", WHITE
        if self.routing:
            spin = SPIN[int((now - self.routing['started']) * 10) % len(SPIN)]
            return f" {spin} moving agents to {self.routing['target'].name or self.routing['target']}… ", WHITE
        if self.toast and self.toast[2] > now:
            return f" {self.toast[0]} ", self.toast[1]
        if self.shift_held() and self.screen == "browse":
            target = self.route_target()
            what = "this tab's agents" if inside_herdr() else "herdr-ai"
            return f" ⇧⏎ {'moves' if inside_herdr() else 'opens'} {what} {'to' if inside_herdr() else 'in'} {target.name or target} ", WHITE
        if self.query and not self.typing:
            return f" /{self.query} · esc clears ", LABEL
        n = len(self.entries)
        free = self.free_space()
        return f" {n} item{'s' * (n != 1)}" + (f" · {free} available " if free else " "), SOFT

    def hints(self):
        route = "⇧⏎ move agents" if inside_herdr() else "⇧⏎ herdr-ai"
        if self.typing:
            return [" type to filter · ⏎ keep · esc clear ", " ⏎ keep · esc clear ", ""]
        return [f" ⏎ open · esc back · {route} · v view · t theme · s settings · ? keys · ⇧Q quit ",
                f" ⏎ open · {route} · v view · t theme · s settings · ? keys · ⇧Q quit ",
                f" ⏎ open · {route} · v view · t theme · s settings · ? keys ",
                f" ⏎ open · {route} · t theme · s settings · ? keys ",
                f" {route} · t theme · s · ? keys ", " ? keys ", ""]

    def browse_parts(self, cols, rows, t):
        """Everything the browser shows, as cells and regions, or None for a pane too small to frame."""
        if cols < 24 or rows < 4:
            return None
        W, H = cols - 2, rows - 2
        key = (cols, rows, self.cwd, id(self.entries), self.index, self.view, self.theme(), self.focused,
               self.sort, len(self.entries), self.top, self.error)
        if self.static is None or self.static[0] != key:
            content, regions, hits = self.content(W, H, t)
            self.static = (key, content, regions, hits, {})
        return self.static

    def browse_frame(self, cols, rows, t):
        self.boost_now()
        parts = self.browse_parts(cols, rows, t)
        if parts is None:
            return self.tiny_frame(cols, rows, t)
        _, content, regions, hits, painted = parts
        self.hits = [(x + 2, y + 2, w, h, i) for x, y, w, h, i in hits]
        top, bottom, left, right = self.chrome(cols, rows, t)
        live = {}
        for region in regions:
            for k, row in enumerate(region.draw(t)):
                live.setdefault(region.y + k, []).append((region.x, row))
        frame = [[(1, paint(top))]]
        W = cols - 2
        for y, row in enumerate(content):
            segs = [(1, paint([left[y]]))]
            x = 0
            for rx, cells in sorted(live.get(y, []), key=lambda item: item[0]):
                if rx > x:
                    key = (y, x, rx)
                    if key not in painted:
                        painted[key] = paint(row[x:rx])
                    segs.append((2 + x, painted[key]))
                segs.append((2 + rx, paint(cells[:W - rx])))
                x = rx + len(cells)
            if x < W:
                key = (y, x, W)
                if key not in painted:
                    painted[key] = paint(row[x:W])
                segs.append((2 + x, painted[key]))
            segs.append((cols, paint([right[y]])))
            frame.append(segs)
        frame.append([(1, paint(bottom))])
        return frame

    def browse_cells(self, cols, rows, t):
        """The browser as one grid of cells, held still, for drawing panels over it."""
        parts = self.browse_parts(cols, rows, t)
        if parts is None:
            return [[BLANK] * cols for _ in range(rows)]
        _, content, regions, _, _ = parts
        top, bottom, left, right = self.chrome(cols, rows, 0.4, still=True)
        grid = [top] + [[left[y]] + list(row) + [right[y]] for y, row in enumerate(content)] + [bottom]
        for region in regions:
            for k, row in enumerate(region.draw(0.4)):
                put(grid[1 + region.y + k], 1 + region.x, row[:cols - 2 - region.x])
        return grid

    def tiny_frame(self, cols, rows, t):
        """A pane too small for the frame: one name per row, the selected one lit."""
        out = []
        n = len(self.entries)
        rows = max(1, rows)
        first = min(max(0, self.index - rows // 2), max(0, n - rows))
        self.ncols, self.page, self.hits = 1, rows, []
        for i in range(first, min(n, first + rows)):
            e = self.entries[i]
            cells = [BLANK] * cols
            put(cells, 0, cells_of(("❯ " if i == self.index else "  ") + fit(clean(e.name), cols - 2),
                                   WHITE if i == self.index else TEXT, None, i == self.index))
            if i == self.index:
                light([cells], self.lit_for(e, t))
            self.hits.append((1, len(out) + 1, cols, 1, i))
            out.append([(1, paint(cells))])
        if not out:
            out.append([(1, paint(cells_of(fit("empty folder", cols), SOFT)))])
        return out

    # -- panels over the browser --

    def overlay_lines(self, cols, rows, t):
        if self.screen == "themes" and self.theme_menu:
            return self.theme_panel(cols, rows, t)
        if self.screen == "settings" and self.settings:
            accent = highlight_style(self.theme(), "folder")
            return ap.render_settings(self.settings, cols, rows, t, accent)
        if self.screen == "help":
            return self.help_panel(cols, rows, t)
        if self.screen == "confirm" and self.confirm:
            return self.confirm_panel(cols, rows, t)
        return None

    def with_overlay(self, cols, rows, t, lines):
        """A panel centred over the browser, which shows dimmed around it (in the theme being
        previewed, while the theme menu is open)."""
        key = ("under", cols, rows, self.theme(), self.static and self.static[0])
        if getattr(self, "under", (None,))[0] != key:
            grid = [[dim_cell(c) for c in row] for row in self.browse_cells(cols, rows, t)]
            self.under = (key, grid, {})
        _, grid, painted = self.under
        width = max((ap.visible_len(line) for line in lines), default=0)
        lines = [line + " " * (width - ap.visible_len(line)) for line in lines]
        x0, y0 = max(0, (cols - width) // 2), max(0, (rows - len(lines)) // 2)
        frame = []
        for y in range(rows):
            row = grid[y] if y < len(grid) else [BLANK] * cols
            k = y - y0
            if 0 <= k < len(lines):
                if ("l", y) not in painted:
                    painted[("l", y)] = paint(row[:x0])
                    painted[("r", y)] = paint(row[x0 + width:])
                segs = [(1, painted[("l", y)])] if x0 else []
                segs.append((x0 + 1, lines[k]))
                if x0 + width < cols:
                    segs.append((x0 + width + 1, painted[("r", y)]))
                frame.append(segs)
            else:
                if ("all", y) not in painted:
                    painted[("all", y)] = paint(row)
                frame.append([(1, painted[("all", y)])])
        return frame

    def theme_panel(self, cols, rows, t):
        menu = self.theme_menu
        current = self.theme_override or preferences.get(THEME.key)

        def swatch(name, width, moving):
            style = theme_style(name)
            if style is None:
                colors = [fs_icons.color(k) for k in ("folder", "python", "javascript", "markdown", "image",
                                                       "rust", "json", "shell")]
                return [colors[k * len(colors) // width] for k in range(width)]
            return [ap.grad(style, k / width - (t * 0.1 if moving else 0)) for k in range(width)]

        def body(bw):
            out = [ap.note(f"{len(menu.items) - 1} themes, shared with the usage panel", bw, LABEL, "gap"),
                   ("gap", "", 0)]
            for i, value in enumerate(menu.items):
                name = usage_theme() if value == "usage" else value
                chosen = i == menu.index
                line = ap.Line()
                label = "match usage panel" if value == "usage" else value
                if chosen:
                    line.put(" ❯ ", WHITE).put(label, (245, 245, 245), bold=True)
                else:
                    line.put(" • " if value == current else "   ", LABEL).put(label, (205, 205, 212))
                if value == "usage":
                    line.put(f"  {name}", SOFT)
                width = min(16, max(4, bw - len(line.cells) - 6))
                for k, color in enumerate(swatch(name, width, chosen)):
                    line.at(bw - width - 1 + k, "▰", color)
                line.pad(bw)
                if chosen:
                    out += ap.ringed(highlight_style(name, "folder"), [line], t, bw)
                else:
                    out.append(ap.flat(line, bw))
            local = next((str(p) for p, _ in themes.sources()[1:]), "")
            out += [("gap", "", 0), ap.note("add your own: " + show_path(local), bw, SOFT, "gap")]
            return out

        plain = [(("❯ " if i == menu.index else "  ") + v, i == menu.index) for i, v in enumerate(menu.items)]
        hints = [" ↑↓ preview · ⏎ apply · esc cancel ", " ⏎ apply · esc cancel ", " esc ", ""]
        return ap.panel("themes", body, hints, cols, rows, t, plain)

    def help_panel(self, cols, rows, t):
        accent = highlight_style(self.theme(), "folder")["color"]

        def body(bw):
            out = []
            for key, what in HELP:
                line = ap.Line().put("   ").put(key.ljust(8), accent, bold=True).put(ap.clip(what, bw - 12), TEXT)
                out.append(ap.flat(line, bw))
            return out

        plain = [(f"{key}  {what}", False) for key, what in HELP]
        return ap.panel("fs keys", body, [" any key closes ", ""], cols, rows, t, plain)

    def confirm_panel(self, cols, rows, t):
        c = self.confirm
        style = highlight_style(self.theme(), "folder")
        where = show_path(c.target)
        if c.kind == "layout":
            title, lines, yes = "herdr-ai", ["This tab has no herdr-ai layout yet.",
                                             f"Lay herdr-ai out here, in {where}?"], "Lay out"
        else:
            title, lines, yes = "herdr-ai", ["fs is not running inside herdr.",
                                             f"Open a herdr-ai workspace in {where}?"], "Open herdr-ai"

        def body(bw):
            out = [ap.note(line, bw, TEXT) for line in lines] + [("gap", "", 0)]
            return out + ap.buttons(style, (yes, "Cancel"), c.choice, t, bw)

        plain = [(line, False) for line in lines] + [("❯ " + (yes if c.choice == 0 else "Cancel"), True)]
        return ap.panel(title, body, [" ←→ choose · ⏎ confirm · esc cancel ", " ⏎ confirm ", ""],
                        cols, rows, t, plain)

    # -- preview --

    def preview_frame(self, cols, rows, t):
        p = self.preview
        entry = p.entry
        if cols < 24 or rows < 4:
            line = fit(clean(entry.name) + " · esc back", cols)
            return [[(1, paint(cells_of(line, TEXT)))]]
        W, H = cols - 2, rows - 2
        key = ("preview", cols, rows, p.mode, p.top, p.left, p.wrap, p.loading, id(p.image), self.theme())
        if getattr(self, "previewed", (None,))[0] != key:
            self.previewed = (key, self.preview_content(W, H, t), {})
        _, content, painted = self.previewed
        size = "" if entry.is_dir else human_size(entry.size)
        tag = " · ".join(x for x in (p.note, entry.kind_text(), size) if x)
        tag = " " + fit(tag, max(0, cols // 2 - 4)) + " " if tag else ""
        if p.mode == "text":
            hints = [" ↑↓ scroll · space page · w wrap · o open · e edit · esc back ",
                     " ↑↓ scroll · w wrap · esc back ", " esc back ", ""]
        else:
            more = " · ⏎ show contents" if entry.is_dir else ""
            hints = [f" space Quick Look · o open{more} · esc back ", " esc back ", ""]
        if p.loading:
            status = (f" {SPIN[int(t * 10) % len(SPIN)]} drawing a preview… ", TEXT)
        elif p.mode == "text":
            shown = p.display(max(4, W - len(str(len(p.lines))) - 4))[p.top:p.top + H]
            numbers = [n for n, _ in shown if n] or [1]
            status = (f" lines {numbers[0]}-{numbers[-1]} of {len(p.lines)} ", SOFT)
        else:
            status = ("", SOFT)
        top, bottom, left, right = self.chrome(cols, rows, t, title=entry.name, tag=tag, status=status,
                                               hints=hints)
        frame = [[(1, paint(top))]]
        for y, row in enumerate(content):
            if y not in painted:
                painted[y] = paint(row)
            frame.append([(1, paint([left[y]])), (2, painted[y]), (cols, paint([right[y]]))])
        frame.append([(1, paint(bottom))])
        return frame

    def preview_content(self, W, H, t):
        p = self.preview
        rows = [[BLANK] * W for _ in range(H)]
        theme = self.theme()
        accent = highlight_style(theme, p.entry.kind)["color"]
        if p.mode == "text":
            numbers = len(str(len(p.lines)))
            width = max(4, W - numbers - 4)
            shown = p.display(width)
            p.top = max(0, min(p.top, max(0, len(shown) - H)))
            kind = p.entry.kind
            line_no = 1
            for y, (number, text) in enumerate(shown[p.top:p.top + H]):
                line_no = number or line_no
                row = rows[y]
                if number is not None:
                    put(row, 1, cells_of(str(number).rjust(numbers), (96, 96, 106)))
                body = text if p.wrap else text[p.left:]
                stripped = body.lstrip()
                color, bold = TEXT, False
                if kind == "markdown":
                    if p.fenced[line_no - 1]:
                        color = SOFT if stripped.startswith("```") else (200, 200, 210)
                    elif stripped.startswith("#"):
                        color, bold = accent, True
                elif kind in HASH_COMMENTS and stripped.startswith("#") or \
                        kind in SLASH_COMMENTS and stripped.startswith(("//", "/*", "*")) or \
                        kind in DASH_COMMENTS and stripped.startswith("--"):
                    color = SOFT
                put(row, numbers + 3, cells_of(fit(body, width), color, None, bold))
            return rows
        if p.mode == "picture" and p.image:
            return [[BLANK] + r + [BLANK] for r in picture_rows(p.image, W - 2, H)]
        if p.mode == "picture":
            self.center(rows, [("drawing a preview…", SOFT, False)])
            return rows
        return self.info_rows(rows, W, H)

    def info_rows(self, rows, W, H):
        """Finder's Get Info, short: the icon, then what the file is."""
        e = self.preview.entry
        try:
            icon = fs_icons.icon(e.kind, "large", self.accent(0.4), e.is_link)
        except Exception:
            icon = [[BLANK] * 8 for _ in range(3)]
        facts = [(clean(e.name), WHITE, True), (e.kind_text(), TEXT, False)]
        if not e.is_dir:
            facts.append((f"{human_size(e.size)} ({e.size:,} bytes)", TEXT, False))
        facts += [(f"Modified  {when(e.mtime, 40)}", SOFT, False), (f"Created   {when(e.ctime, 40)}", SOFT, False),
                  (f"Where     {show_path(e.path.parent)}", SOFT, False),
                  (f"Mode      {stat.filemode(e.mode)}", SOFT, False)]
        if e.is_link:
            try:
                facts.append((f"Original  {show_path(os.readlink(e.path))}", SOFT, False))
            except OSError:
                pass
        if self.preview.note:
            facts.append((self.preview.note, LABEL, False))
        block = max(len(facts), len(icon))
        top = max(0, (H - block) // 2)
        x = max(2, W // 2 - 26)
        for k, row in enumerate(icon):
            if top + k < H:
                put(rows[top + k], x, row)
        for k, (text, color, bold) in enumerate(facts):
            if top + k < H:
                put(rows[top + k], x + 12, cells_of(fit(text, max(4, W - x - 14)), color, None, bold))
        return rows

    # -- actions --

    def route_target(self):
        entry = self.selected()
        return entry.path if entry and entry.is_dir else self.cwd

    def route(self):
        """Shift+Enter: herdr-ai's refresh in the selected folder, so every agent on this tab
        restarts there (idle ones keep their conversations when the provider allows)."""
        target = self.route_target()
        self.sweep_at = time.monotonic()
        if not inside_herdr():
            self.confirm, self.screen = Confirm("herdr", target), "confirm"
            return
        if self.routing:
            self.say("agents are still moving", WARN)
            return
        job = {"target": target, "started": time.monotonic(), "result": None}
        self.routing = job

        def run():
            try:
                out = subprocess.run([sys.executable, str(HERDR_AI), "--refresh"], cwd=str(target),
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=180)
                job["result"] = (out.returncode, (out.stdout or "") + (out.stderr or ""))
            except (OSError, subprocess.SubprocessError) as exc:
                job["result"] = (1, f"herdr-ai: {exc}")

        threading.Thread(target=run, daemon=True).start()

    def routed(self):
        """Report a finished refresh, and follow the agents into their folder."""
        job = self.routing
        if not job or job["result"] is None:
            return
        self.routing = None
        code, output = job["result"]
        target = job["target"]
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        if code == 3:
            self.confirm, self.screen = Confirm("layout", target), "confirm"
            return
        if code:
            last = lines[-1] if lines else f"herdr-ai exited with {code}"
            self.say(last.replace("herdr-ai: ", "", 1), HOT, 8)
            return
        self.say(summarize(lines, target), OK, 6)
        if target != self.cwd:
            self.go(target)

    def suspend(self, action):
        """Hand the terminal to another program (a shell, an editor, herdr-ai), then come back."""
        term = self.terminal
        self.leave()
        previous = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            return action()
        finally:
            signal.signal(signal.SIGINT, previous)
            self.enter()
            self.load()

    def run_foreground(self, argv, cwd):
        def action():
            try:
                return subprocess.call(argv, cwd=str(cwd))
            except OSError as exc:
                print(f"fs: {exc}")
                return 127
        return self.suspend(action)

    def confirmed(self):
        c = self.confirm
        self.confirm, self.screen = None, "browse"
        argv = [sys.executable, str(HERDR_AI)] + (["--no-fs"] if c.kind == "layout" else [])
        code = self.run_foreground(argv, c.target)
        if code == 0 and c.kind == "layout" and c.target != self.cwd:
            self.go(c.target)
        self.say("herdr-ai finished" if code == 0 else f"herdr-ai exited with {code}", OK if code == 0 else WARN)

    def open_entry(self, how=None):
        entry = self.selected()
        if entry is None:
            return
        how = how or ("enter" if entry.opens else preferences.get(ON_ENTER.key))
        if how == "enter":
            self.go(entry.path)
        elif how == "quicklook":
            self.quick_look(entry)
        elif how == "app":
            self.open_app(entry)
        else:
            self.open_preview(entry)

    def open_preview(self, entry):
        self.preview, self.screen = Preview(entry, self.theme()), "preview"
        self.zoom(True)

    def close_preview(self):
        self.zoom(False)
        self.preview, self.screen = None, "browse"
        self.previewed = (None,)

    def zoom(self, on):
        """A short herdr pane fills the tab while it previews, then goes back to its place."""
        if not (inside_herdr() and preferences.get(ZOOM.key)) or not self.preview:
            return
        pane = os.environ["HERDR_PANE_ID"]
        herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
        if not herdr:
            return
        try:
            if on:
                if shutil.get_terminal_size((80, 24)).lines >= 20:
                    return
                out = subprocess.run([herdr, "pane", "layout", "--pane", pane], capture_output=True, text=True,
                                     timeout=3)
                if json.loads(out.stdout or "{}").get("result", {}).get("layout", {}).get("zoomed"):
                    return
                subprocess.run([herdr, "pane", "zoom", "--pane", pane, "--on"], capture_output=True, timeout=3)
                self.preview.zoomed = True
            elif self.preview.zoomed:
                subprocess.run([herdr, "pane", "zoom", "--pane", pane, "--off"], capture_output=True, timeout=3)
                self.preview.zoomed = False
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    def quick_look(self, entry):
        if self.quicklook and self.quicklook.poll() is None:
            self.quicklook.terminate()
            self.quicklook = None
            return
        if sys.platform == "darwin" and shutil.which("qlmanage"):
            self.quicklook = subprocess.Popen(["qlmanage", "-p", str(entry.path)], stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              start_new_session=True)
            self.say(f"Quick Look · space closes it", SOFT)
        elif self.screen != "preview":
            self.open_preview(entry)

    def open_app(self, entry):
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(entry.path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif os.name == "nt":
                os.startfile(str(entry.path))
            else:
                subprocess.Popen(["xdg-open", str(entry.path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.say(f"opened {clean(entry.name)}", OK)
        except OSError as exc:
            self.say(f"can't open it: {exc.strerror or exc}", HOT)

    def copy_path(self):
        entry = self.selected()
        path = str(entry.path if entry else self.cwd)
        if copy_text(path, self.terminal):
            self.say(f"copied {show_path(path)}", OK)
        else:
            self.say("no clipboard here", WARN)

    def edit(self):
        entry = self.selected()
        if entry is None or entry.is_dir:
            self.say("e edits a file", SOFT)
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
        self.run_foreground(shlex.split(editor, posix=os.name != "nt") + [str(entry.path)], self.cwd)

    def shell(self):
        shell = os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else os.environ.get("SHELL", "/bin/sh")

        def action():
            print(f"fs: a shell in {show_path(self.cwd)}. exit returns to fs.", flush=True)
            try:
                return subprocess.call([shell], cwd=str(self.cwd))
            except OSError as exc:
                print(f"fs: {exc}")
        self.suspend(action)

    def pin(self):
        preferences.put(PINNED, str(self.cwd))
        preferences.put(OPENS_IN.key, "pinned")
        self.say(f"herdr-ai's pane now opens in {show_path(self.cwd)}", OK)

    def toggle_view(self):
        view = "details" if self.view == "icons" else "icons"
        if self.view_override:
            self.view_override = view
        else:
            preferences.put(VIEW.key, view)
        self.top, self.static = 0, None

    def toggle_hidden(self):
        if self.hidden_override is not None:
            self.hidden_override = not self.hidden_override
        else:
            preferences.put(HIDDEN.key, not preferences.get(HIDDEN.key))
        self.refilter()
        self.say("hidden files shown" if self.show_hidden else "hidden files hidden", SOFT)

    def cycle_sort(self):
        order = SORT.choices
        value = order[(order.index(self.sort) + 1) % len(order)]
        if self.sort_override:
            self.sort_override = value
        else:
            preferences.put(SORT.key, value)
        self.refilter()
        self.say(f"sorted by {SORT.show(value)}", SOFT)

    def move(self, key):
        n = len(self.entries)
        if not n:
            return
        cols = self.ncols if self.view == "icons" or self.ncols > 1 else 1
        i = self.index
        if key in ("right", "l") and cols > 1:
            i = min(n - 1, i + 1)
        elif key in ("left", "h") and cols > 1:
            i = max(0, i - 1)
        elif key in ("down", "j"):
            if i + cols < n:
                i += cols
            elif i // cols < (n - 1) // cols:
                i = n - 1
        elif key in ("up", "k"):
            i = i - cols if i - cols >= 0 else i
        self.index = i

    def handle_mouse(self, key):
        try:
            _, button, x, y, kind = key.split(":")
            button, x, y = int(button), int(x), int(y)
        except ValueError:
            return
        if not preferences.get(MOUSE.key) or self.screen != "browse":
            return
        if button & 64:   # the wheel moves the selection a row at a time
            self.move("down" if button & 1 else "up")
            return
        if kind != "press" or button & 3 or button & 32:
            return
        for hx, hy, w, h, i in self.hits:
            if hx <= x < hx + w and hy <= y < hy + h:
                last, at = self.click
                self.index = i
                if last == i and time.monotonic() - at < 0.45:
                    self.click = (None, 0.0)
                    self.open_entry()
                else:
                    self.click = (i, time.monotonic())
                return

    def handle(self, key):
        """Apply one key. Returns "quit" to leave."""
        if key == "shift":
            self.bare_shift = True
            return None
        if key == "release:shift":
            self.bare_shift = True
            return None
        if key.startswith("release:"):
            return None
        if key in ("focus-in", "focus-out"):
            self.focused = key == "focus-in"
            self.static = None
            return None
        if getattr(self.terminal, "shift", False):
            self.shift_until = time.monotonic() + 0.6
        if key.startswith("mouse:"):
            self.handle_mouse(key)
            return None
        if key == "Q" and not self.typing:   # the only way out; q, Esc and Ctrl+C never quit
            return "quit"
        screen = self.screen
        if screen == "preview":
            return self.handle_preview(key)
        if screen == "themes":
            action = self.theme_menu.handle(key)
            if action == "apply":
                preferences.put(THEME.key, self.theme_menu.value)
                self.theme_override = None
                self.theme_toast = time.monotonic() + 2.5
            if action:
                self.theme_menu, self.preview_theme, self.screen = None, None, "browse"
            else:
                self.preview_theme = self.theme_menu.value
            self.static = None
            return None
        if screen == "settings":
            before = [preferences.get(s.key) for s in SETTINGS]
            if self.settings.handle(key) == "close":
                self.settings, self.screen = None, "browse"
            if [preferences.get(s.key) for s in SETTINGS] != before:
                self.refilter()
                self.terminal.mouse(bool(preferences.get(MOUSE.key)))
            self.static = None
            return None
        if screen == "help":
            self.screen = "browse"
            return None
        if screen == "confirm":
            c = self.confirm
            if key in ("left", "right", "up", "down", "\t"):
                c.choice = 1 - c.choice
            elif key in ("\r", "\n", " ") and c.choice == 0 or key in ("y", "Y"):
                self.confirmed()
            elif key in ("\r", "\n", " ", "esc", "q", "n", "N"):
                self.confirm, self.screen = None, "browse"
            return None
        if self.typing:
            return self.handle_typing(key)
        return self.handle_browse(key)

    def handle_typing(self, key):
        if key in ("\r", "\n"):
            self.typing = False
            if not self.query:
                self.query = None
        elif key == "esc":
            self.query, self.typing = None, False
        elif key in ("\x7f", "\b"):
            self.query = (self.query or "")[:-1]
        elif key in ("up", "down", "left", "right"):
            self.typing = False
            self.move(key)
            return None
        elif key.startswith("paste:"):
            self.query = (self.query or "") + clean(key[6:])
        elif len(key) == 1 and key.isprintable():
            self.query = (self.query or "") + key
        else:
            return None
        self.refilter()
        return None

    def handle_browse(self, key):
        if key == "esc":
            # Back one step: a filter clears first, then each Esc goes to the enclosing folder.
            if self.query is not None:
                self.query = None
                self.refilter()
            else:
                self.up()
            return None
        if key in ("up", "down", "left", "right", "j", "k"):
            if self.view == "details" and key == "left":
                self.up()
            elif self.view == "details" and key == "right":
                if self.selected() and self.selected().opens:
                    self.go(self.selected().path)
            else:
                self.move(key)
        elif key in ("\r", "\n"):
            self.open_entry()
        elif key == "shift-enter":
            self.route()
        elif key in ("\x7f", "\b", "-"):
            self.up()
        elif key == "[" and self.back:
            self.forward.append(self.cwd)
            self.go(self.back.pop(), record=False)
        elif key == "]" and self.forward:
            self.back.append(self.cwd)
            self.go(self.forward.pop(), record=False)
        elif key == "~":
            self.go(Path.home())
        elif key == "v":
            self.toggle_view()
        elif key == ".":
            self.toggle_hidden()
        elif key == ",":
            self.cycle_sort()
        elif key == "/":
            self.query, self.typing = self.query or "", True
        elif key == " ":
            if self.selected():
                self.quick_look(self.selected())
        elif key == "o":
            if self.selected():
                self.open_app(self.selected())
        elif key == "y":
            self.copy_path()
        elif key == "e":
            self.edit()
        elif key == "!":
            self.shell()
        elif key == "p":
            self.pin()
        elif key == "r":
            self.load()
            self.say("reloaded", SOFT, 1.5)
        elif key == "t":
            self.theme_menu = ThemeMenu(self.theme_override or preferences.get(THEME.key))
            self.preview_theme, self.screen = self.theme_menu.value, "themes"
        elif key == "s":
            self.settings, self.screen = preferences.Menu(settings_rows()), "settings"
        elif key == "?":
            self.screen = "help"
        self.static = None if key not in ("up", "down", "left", "right", "j", "k") else self.static
        return None

    def handle_preview(self, key):
        p = self.preview
        if key in ("esc", "\x7f", "\b", "\r", "\n") and not (key in ("\r", "\n") and p.entry.is_dir):
            self.close_preview()
        elif key in ("\r", "\n"):
            entry = p.entry
            self.close_preview()
            self.go(entry.path)
        elif key == " " and p.mode != "text":
            self.quick_look(p.entry)
        elif key == "o":
            self.open_app(p.entry)
        elif key == "e":
            self.edit()
        elif key == "y":
            self.copy_path()
        elif key == "shift-enter":
            self.close_preview()
            self.route()
        else:
            cols, rows = shutil.get_terminal_size((80, 24))
            p.handle(key, (max(4, cols - 2 - len(str(len(p.lines))) - 4), max(1, rows - 2)))
        return None

    # -- the loop --

    def enter(self):
        out = sys.stdout
        out.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[?7l")
        out.flush()
        self.terminal.__enter__()
        self.terminal.keyboard(True, flags=15)
        self.terminal.focus_events(True)
        self.terminal.mouse(bool(preferences.get(MOUSE.key)))
        self.writer.reset()
        self.static = None

    def leave(self):
        term = self.terminal
        try:
            term.mouse(False)
            term.focus_events(False)
            term.keyboard(False)
        finally:
            if hasattr(term, "old") or hasattr(term, "modes"):
                term.__exit__()
            sys.stdout.write(f"{ESC}[?7h{ESC}[?25h{ESC}[?1049l")
            sys.stdout.flush()

    def frame_step(self):
        now = time.monotonic()
        busy = (self.routing or self.sweep_at or (self.toast and self.toast[2] > now) or self.theme_toast > now
                or (self.preview and self.preview.loading) or self.boost not in (0.0, 1.0) or self.typing)
        if self.animating() or busy:
            return 1 / 30
        return 1.0

    def run(self):
        quiet = signal.signal(signal.SIGINT, lambda *_: None)   # Ctrl+C does not quit; Shift+Q does
        try:
            return self.loop()
        finally:
            signal.signal(signal.SIGINT, quiet)

    def loop(self):
        self.terminal = Terminal()
        self.terminal.extended = True
        self.writer = SegmentScreen(self.terminal)
        self.enter()
        try:
            frame_at = time.monotonic()
            while True:
                cols, rows = shutil.get_terminal_size((80, 24))
                t = time.monotonic() - self.start
                self.writer.show(self.frame(cols, rows, t), cols, rows)
                frame_at += self.frame_step()
                wait = frame_at - time.monotonic()
                if wait < 0:
                    frame_at, wait = time.monotonic(), 0
                if not self.terminal.wait(wait):
                    self.tick()
                    continue
                key = self.terminal.read_key()
                if key and self.handle(key) == "quit":
                    return 0
                self.tick()
        except KeyboardInterrupt:
            return 0
        finally:
            if self.preview:
                self.zoom(False)
            if self.quicklook and self.quicklook.poll() is None:
                self.quicklook.terminate()
            self.leave()

    def tick(self):
        if self.screen in ("browse", "preview"):
            self.watch()
        self.routed()


def summarize(lines, target):
    """herdr-ai's refresh report in one line: who moved, who kept running."""
    where = target.name or str(target)
    kept = [l for l in lines if "kept " in l]
    moved = next((l for l in lines if "refreshed " in l), None)
    names = []
    if moved:
        names = [n.strip() for n in moved.split("refreshed ", 1)[1].split(" in ", 1)[0].split(",")
                 if n.strip() and n.strip() != "usage"]   # the usage panel reloads too; it is not an agent
    text = f"{len(names)} agent{'s' * (len(names) != 1)} now in {where}" if names else f"no agents moved to {where}"
    if kept:
        text += f" · {len(kept)} kept running"
    return "✓ " + text


def copy_text(text, terminal=None):
    """The system clipboard, else the terminal's (OSC 52, which herdr and most terminals pass on)."""
    commands = [["pbcopy"]] if sys.platform == "darwin" else [["clip"]] if os.name == "nt" else \
        [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for argv in commands:
        if shutil.which(argv[0]):
            try:
                data = text.encode("utf-16le") if argv[0] == "clip" else text.encode("utf-8")
                subprocess.run(argv, input=data, check=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError):
                pass
    if terminal is not None:
        import base64
        terminal.write(f"{ESC}]52;c;{base64.b64encode(text.encode('utf-8')).decode()}\a")
        return True
    return False


def settings_rows():
    """fs's settings, then herdr-ai's Terminal pane setting when it is available."""
    rows = list(SETTINGS)
    try:
        import team
        terminal = getattr(team, "TERMINAL", None)
        if terminal is not None:
            rows.append(preferences.register(terminal))
    except Exception:
        pass
    return rows


# ---- command line --------------------------------------------------------------------

VIEWS = {"icon": "icons", "icons": "icons", "grid": "icons", "details": "details", "detail": "details",
         "list": "details"}


def parse(argv):
    """The options as a dict, or a usage error string."""
    opts = {"path": None, "view": None, "hidden": None, "sort": None, "theme": None, "herdr": False, "help": False}
    args = list(argv)
    rest = False
    while args:
        arg = args.pop(0)
        value = None
        if not rest and arg.startswith("--") and "=" in arg:
            arg, value = arg.split("=", 1)
        if rest or not arg.startswith("-") or arg == "-":
            if opts["path"] is not None:
                return "fs: takes one folder or file; quote names with spaces"
            opts["path"] = arg
        elif arg == "--":
            rest = True
        elif arg in ("-h", "--help"):
            opts["help"] = True
        elif arg in ("-i", "--icons"):
            opts["view"] = "icons"
        elif arg in ("-l", "--details", "--list"):
            opts["view"] = "details"
        elif arg in ("-a", "--hidden"):
            opts["hidden"] = True
        elif arg == "--herdr":
            opts["herdr"] = True
        elif arg in ("--view", "--sort", "--theme"):
            if value is None:
                if not args:
                    return f"fs: {arg} needs a value"
                value = args.pop(0)
            if arg == "--view":
                if value.lower() not in VIEWS:
                    return "fs: --view takes icon or details"
                opts["view"] = VIEWS[value.lower()]
            elif arg == "--sort":
                if value.lower() not in SORT.choices:
                    return "fs: --sort takes name, kind, date or size"
                opts["sort"] = value.lower()
            else:
                names = themes.names()
                match = next((n for n in names if n.lower() == value.lower()), None)
                if value.lower() != "usage" and match is None:
                    return f"fs: no theme named {value}; themes: " + ", ".join(names)
                opts["theme"] = match or "usage"
        else:
            return f"fs: unknown option {arg}; see fs --help"
    return opts


def main(argv=None):
    opts = parse(sys.argv[1:] if argv is None else argv)
    if isinstance(opts, str):
        print(opts, file=sys.stderr)
        return 2
    if opts["help"]:
        print(__doc__.strip())
        return 0
    select = None
    if opts["path"]:
        target = Path(opts["path"]).expanduser()
        if not target.exists():
            print(f"fs: {opts['path']}: no such file or folder", file=sys.stderr)
            return 2
        target = target.resolve() if not target.is_absolute() else target
        if not target.is_dir():
            target, select = target.parent, target.name
        start = target
    elif opts["herdr"]:
        start = herdr_start(os.getcwd())
    else:
        start = Path(os.getcwd())
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("fs: needs an interactive terminal", file=sys.stderr)
        return 2
    app = App(start, select=select, view=opts["view"], hidden=opts["hidden"], sort=opts["sort"],
              theme=opts["theme"], herdr=opts["herdr"], herdr_dir=os.getcwd())
    return app.run()


if __name__ == "__main__":
    from platform_runtime import configure_stdio
    configure_stdio()
    sys.exit(main())
