"""Finder-style file kinds and pixel-art icons for the fs browser.

Pure functions over names and flags: no terminal I/O and nothing from the picker,
so the browser can classify a whole folder and redraw icons every frame cheaply.
Icons are rows of cells (ch, fg, bg, bold); a None bg is transparent, so the
browser's own background (the animated selection glow) shows around each shape.
Run this file to preview every kind at every size in true color.
"""

import colorsys
import functools
import os
import sys
import unicodedata
from pathlib import Path

WHITE, BLACK = (255, 255, 255), (0, 0, 0)
FOLDER_BLUE = (92, 172, 238)      # the macOS folder blue, softened for a dark screen
GIT_ORANGE = (240, 98, 62)
PAGE = (232, 230, 224)            # off-white paper, easier on the eyes than pure white
PAGE_EDGE = (206, 204, 198)
FOLD = (172, 171, 166)
RULE = (170, 170, 178)            # text lines on a plain page
TILE = (50, 52, 60)               # terminal window
TITLE_BAR = (92, 94, 104)
PROMPT = (120, 205, 145)
LIGHTS = ((240, 105, 95), (235, 185, 85), (120, 205, 145))
ALIAS_FG, ALIAS_BG = (30, 30, 34), (236, 236, 232)

# (cols, rows) of each icon size
SIZES = {"large": (8, 3), "small": (4, 2), "mini": (2, 1)}

_KIND_TABLE = (
    # kind, Finder-style label, brand color, badge
    ("folder", "Folder", FOLDER_BLUE, ""),
    ("folder-desktop", "Folder", FOLDER_BLUE, ""),
    ("folder-documents", "Folder", FOLDER_BLUE, ""),
    ("folder-downloads", "Folder", FOLDER_BLUE, ""),
    ("folder-applications", "Folder", FOLDER_BLUE, ""),
    ("folder-pictures", "Folder", FOLDER_BLUE, ""),
    ("folder-music", "Folder", FOLDER_BLUE, ""),
    ("folder-movies", "Folder", FOLDER_BLUE, ""),
    ("folder-library", "Folder", FOLDER_BLUE, ""),
    ("folder-public", "Folder", FOLDER_BLUE, ""),
    ("folder-git", "Git repository", FOLDER_BLUE, ""),
    ("home", "Folder", FOLDER_BLUE, ""),
    ("app", "Application", (122, 140, 250), ""),
    ("package", "Package", (200, 156, 104), ""),
    ("image", "Image", (110, 196, 130), ""),
    ("video", "Movie", (168, 120, 240), ""),
    ("audio", "Audio", (240, 108, 150), ""),
    ("pdf", "PDF document", (232, 86, 76), "PDF"),
    ("archive", "Archive", (204, 168, 112), ""),
    ("diskimage", "Disk Image", (176, 182, 196), ""),
    ("markdown", "Markdown document", (110, 160, 240), "MD"),
    ("text", "Plain Text", (170, 170, 180), ""),
    ("python", "Python script", (80, 140, 210), "PY"),
    ("javascript", "JavaScript", (236, 206, 72), "JS"),
    ("typescript", "TypeScript", (56, 132, 214), "TS"),
    ("rust", "Rust source", (222, 132, 92), "RS"),
    ("go", "Go source", (64, 186, 222), "GO"),
    ("c", "C source", (120, 150, 200), "C"),
    ("cpp", "C++ source", (234, 96, 138), "C++"),
    ("java", "Java source", (226, 140, 60), "JV"),
    ("kotlin", "Kotlin source", (168, 124, 255), "KT"),
    ("swift", "Swift source", (246, 112, 70), "SW"),
    ("ruby", "Ruby script", (222, 72, 78), "RB"),
    ("php", "PHP script", (130, 136, 204), "PHP"),
    ("csharp", "C# source", (164, 104, 210), "C#"),
    ("lua", "Lua script", (96, 116, 226), "LUA"),
    ("r", "R script", (70, 140, 220), "R"),
    ("shell", "Shell script", (128, 208, 112), "SH"),
    ("sql", "SQL", (226, 156, 72), "SQL"),
    ("dart", "Dart source", (44, 184, 200), "DRT"),
    ("html", "HTML document", (236, 106, 70), "</>"),
    ("css", "CSS", (150, 112, 230), "CSS"),
    ("json", "JSON", (232, 184, 84), "{}"),
    ("yaml", "YAML", (210, 104, 104), "YML"),
    ("toml", "TOML", (190, 116, 82), "TML"),
    ("xml", "XML document", (108, 172, 132), "XML"),
    ("csv", "CSV document", (100, 184, 120), "CSV"),
    ("config", "Configuration file", (150, 152, 168), "CFG"),
    ("notebook", "Jupyter notebook", (240, 136, 60), "NB"),
    ("document", "Document", (82, 140, 226), "DOC"),
    ("spreadsheet", "Spreadsheet", (72, 176, 106), "XLS"),
    ("presentation", "Presentation", (236, 120, 70), "PPT"),
    ("font", "Font", (196, 160, 230), "Aa"),
    ("lock", "Lock file", (182, 162, 112), "LCK"),
    ("license", "License", (204, 182, 120), "LIC"),
    ("makefile", "Makefile", (132, 176, 196), "MK"),
    ("executable", "Unix executable", PROMPT, ""),
    ("file", "Document", (160, 160, 170), ""),
)
KINDS = {kind: {"label": text, "color": rgb, "tag": tag} for kind, text, rgb, tag in _KIND_TABLE}

_EXTS = {
    "image": "png jpg jpeg gif bmp tif tiff webp heic heif avif svg ico icns psd raw cr2 nef dng",
    "video": "mp4 m4v mov avi mkv webm wmv flv mpg mpeg 3gp",
    "audio": "mp3 m4a aac wav flac aif aiff ogg oga opus wma mid midi",
    "pdf": "pdf",
    "archive": "zip tar gz tgz bz2 tbz tbz2 xz txz 7z rar zst lz lz4 lzma jar cpio xip",
    "diskimage": "dmg iso img sparseimage toast vhd vhdx vmdk",
    "markdown": "md markdown mdx mkd",
    "text": "txt text log rst",
    "python": "py pyw pyi pyx",
    "javascript": "js mjs cjs jsx",
    "typescript": "ts tsx mts cts",
    "rust": "rs",
    "go": "go",
    "c": "c h m",
    "cpp": "cpp cc cxx hpp hh hxx mm ino",
    "java": "java class",
    "kotlin": "kt kts",
    "swift": "swift",
    "ruby": "rb rake gemspec erb",
    "php": "php",
    "csharp": "cs csx",
    "lua": "lua",
    "r": "r rmd",
    "shell": "sh bash zsh fish ksh csh tcsh ps1 psm1",
    "sql": "sql",
    "dart": "dart",
    "html": "html htm xhtml",
    "css": "css scss sass less",
    "json": "json jsonc json5 geojson webmanifest",
    "yaml": "yaml yml",
    "toml": "toml",
    "xml": "xml xsd xsl xslt",
    "csv": "csv tsv",
    "config": "ini conf cfg plist properties env",
    "notebook": "ipynb",
    "document": "doc docx pages rtf odt",
    "spreadsheet": "xls xlsx numbers ods",
    "presentation": "ppt pptx key odp",
    "font": "ttf otf woff woff2 ttc dfont",
    "lock": "lock lockb",
    "makefile": "mk cmake",
    "executable": "command exe bat cmd appimage",
    "package": "pkg mpkg msi",
}
EXT_KIND = {ext: kind for kind, exts in _EXTS.items() for ext in exts.split()}

# Folders that macOS treats as one item (Finder opens them, it doesn't browse them).
_BUNDLES = {
    "app": "app", "bundle": "package", "framework": "package", "xcodeproj": "package",
    "xcworkspace": "package", "pkg": "package", "mpkg": "package", "plugin": "package",
    "kext": "package", "appex": "package", "prefpane": "package", "qlgenerator": "package",
    "sparsebundle": "diskimage", "rtfd": "document", "pages": "document",
    "numbers": "spreadsheet", "key": "presentation",
}

# Folders directly inside home (Windows says Videos and keeps settings in AppData).
_SPECIAL = {
    "desktop": "folder-desktop", "documents": "folder-documents", "downloads": "folder-downloads",
    "applications": "folder-applications", "pictures": "folder-pictures", "music": "folder-music",
    "movies": "folder-movies", "videos": "folder-movies", "library": "folder-library",
    "public": "folder-public", "appdata": "folder-library",
}
_ROOT_SPECIAL = {"applications": "folder-applications", "library": "folder-library"}

_NAMES = {
    "makefile": "makefile", "gnumakefile": "makefile", "dockerfile": "makefile",
    "containerfile": "makefile", "justfile": "makefile", "cmakelists.txt": "makefile",
    "rakefile": "makefile", "procfile": "makefile", "vagrantfile": "makefile",
    "brewfile": "makefile", "meson.build": "makefile",
    "package-lock.json": "lock", "npm-shrinkwrap.json": "lock", "pnpm-lock.yaml": "lock",
    "yarn.lock": "lock", "go.sum": "lock", "package.resolved": "lock",
    "gemfile": "ruby", "podfile": "ruby",
    "readme": "text", "changelog": "text", "authors": "text", "contributors": "text",
    "notice": "text", "todo": "text",
    ".ds_store": "file", ".localized": "file",
}
_NAME_LABELS = {
    "makefile": "Makefile", "gnumakefile": "Makefile", "dockerfile": "Dockerfile",
    "containerfile": "Containerfile", "justfile": "Justfile", "cmakelists.txt": "CMake script",
    "rakefile": "Rakefile", "procfile": "Procfile", "vagrantfile": "Vagrantfile",
    "brewfile": "Brewfile", "meson.build": "Meson build file", "gemfile": "Gemfile",
    "podfile": "Podfile",
}
_NAME_TAGS = {"dockerfile": "DKR", "containerfile": "DKR", "justfile": "JST",
              "cmakelists.txt": "CMK", "meson.build": "MSN"}
_LICENSES = ("license", "licence", "copying", "unlicense", "copyright")
_LICENSE_EXTS = ("", "md", "txt", "rst", "markdown")

_EXT_LABELS = {
    "png": "PNG image", "jpg": "JPEG image", "jpeg": "JPEG image", "gif": "GIF image",
    "heic": "HEIF image", "heif": "HEIF image", "tif": "TIFF image", "tiff": "TIFF image",
    "webp": "WebP image", "svg": "SVG image", "bmp": "BMP image", "avif": "AVIF image",
    "psd": "Photoshop document", "ico": "Windows icon", "icns": "Apple icon image",
    "mp4": "MPEG-4 movie", "m4v": "MPEG-4 movie", "mov": "QuickTime movie", "avi": "AVI movie",
    "mkv": "Matroska video", "webm": "WebM movie", "mpg": "MPEG movie", "mpeg": "MPEG movie",
    "mp3": "MP3 audio", "m4a": "MPEG-4 audio", "aac": "AAC audio", "wav": "Waveform audio",
    "flac": "FLAC audio", "aif": "AIFF audio", "aiff": "AIFF audio", "ogg": "Ogg audio",
    "opus": "Opus audio", "mid": "MIDI audio", "midi": "MIDI audio",
    "zip": "ZIP archive", "gz": "Gzip archive", "tgz": "Gzip tar archive", "tar": "tar archive",
    "bz2": "Bzip2 archive", "xz": "XZ archive", "7z": "7-Zip archive", "rar": "RAR archive",
    "jar": "Java archive", "zst": "Zstandard archive", "xip": "XIP archive",
    "iso": "ISO disk image", "sparsebundle": "Sparse bundle disk image",
    "h": "C header", "m": "Objective-C source", "hpp": "C++ header", "hh": "C++ header",
    "hxx": "C++ header", "mm": "Objective-C++ source", "class": "Java class file",
    "ps1": "PowerShell script", "psm1": "PowerShell module",
    "scss": "Sass stylesheet", "sass": "Sass stylesheet", "less": "Less stylesheet",
    "tsv": "TSV document", "plist": "Property List", "log": "Log file",
    "doc": "Word document", "docx": "Word document", "pages": "Pages document",
    "rtf": "RTF document", "rtfd": "RTFD document", "odt": "OpenDocument text",
    "xls": "Excel workbook", "xlsx": "Excel workbook", "numbers": "Numbers spreadsheet",
    "ods": "OpenDocument spreadsheet", "ppt": "PowerPoint presentation",
    "pptx": "PowerPoint presentation", "key": "Keynote presentation",
    "odp": "OpenDocument presentation", "ttf": "TrueType font", "otf": "OpenType font",
    "woff": "Web font", "woff2": "Web font", "ttc": "TrueType collection",
    "exe": "Windows executable", "bat": "Batch file", "cmd": "Batch file",
    "command": "Terminal shell script", "framework": "Framework", "bundle": "Bundle",
    "xcodeproj": "Xcode project", "xcworkspace": "Xcode workspace", "pkg": "Installer package",
    "mpkg": "Installer package", "msi": "Windows Installer package", "plugin": "Plug-in",
    "kext": "Kernel extension", "appex": "App extension", "prefpane": "Preference pane",
    "cmake": "CMake script", "mk": "Makefile",
}
# Families whose unknown extensions still read naturally, e.g. "CR2 image".
_GENERIC = {"image": "{} image", "video": "{} movie", "audio": "{} audio", "archive": "{} archive"}


# ---- kinds -------------------------------------------------------------------

def _ext(name):
    """Lowercase last extension; a leading dot (".zshrc") is not one."""
    base = name.lower().lstrip(".")
    return base.rsplit(".", 1)[1] if "." in base else ""


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def classify(path, is_dir=False, is_link=False, executable=False, home=None):
    """The kind key for one entry. Only folders touch the disk (one exists() for .git).

    is_link marks an alias; the kind stays that of its target, the icon gets a badge."""
    name = os.path.basename(os.path.normpath(os.fspath(path))) or os.fspath(path)
    lower = name.lower()
    ext = _ext(name)
    if is_dir:
        if ext in _BUNDLES:
            return _BUNDLES[ext]
        full = _norm(path)
        root = _norm(Path.home() if home is None else home)
        if full == root:
            return "home"
        parent = os.path.dirname(full)
        if parent == root and lower in _SPECIAL:
            return _SPECIAL[lower]
        if parent == os.path.dirname(parent) and lower in _ROOT_SPECIAL:
            return _ROOT_SPECIAL[lower]
        if os.path.exists(os.path.join(os.fspath(path), ".git")):
            return "folder-git"
        return "folder"
    if lower in _NAMES:
        return _NAMES[lower]
    if lower.startswith(("dockerfile.", "containerfile.")):
        return "makefile"
    stem = lower.split(".", 1)[0]
    if ext in _LICENSE_EXTS and (stem in _LICENSES or stem.startswith(("license-", "licence-"))):
        return "license"
    if lower.startswith(".") and EXT_KIND.get(ext) in (None, "config"):
        return "config"     # .zshrc, .gitignore, .env.local
    if ext in EXT_KIND:
        return EXT_KIND[ext]
    if executable:
        return "executable"
    return "file"


def label(kind, name=""):
    """The Kind column text, refined by extension where Finder would say more."""
    lower = os.path.basename(os.fspath(name)).lower()
    ext = _ext(lower)
    if kind == "file":
        return f"{ext.upper()} file" if ext else "Document"
    if _NAMES.get(lower) == kind and lower in _NAME_LABELS:
        return _NAME_LABELS[lower]
    if kind == "makefile" and lower.startswith(("dockerfile.", "containerfile.")):
        return "Dockerfile"
    if ext in _EXT_LABELS and kind in (EXT_KIND.get(ext), _BUNDLES.get(ext)):
        text = _EXT_LABELS[ext]
        if kind == "archive" and ext != "tar" and _ext(lower[:-len(ext) - 1]) == "tar":
            text = text[:-len(" archive")] + " tar archive"    # name.tar.gz
        return text
    if kind in _GENERIC and EXT_KIND.get(ext) == kind:
        return _GENERIC[kind].format(ext.upper())
    return KINDS.get(kind, KINDS["file"])["label"]


def badge(kind, name=""):
    """The text a large document icon carries; a generic file shows its extension."""
    lower = os.path.basename(os.fspath(name)).lower()
    if kind == "file":
        return _ext(lower)[:3].upper()
    if kind == "makefile":
        for prefix, tag in _NAME_TAGS.items():
            if lower == prefix or lower.startswith(prefix + "."):
                return tag
    return KINDS.get(kind, KINDS["file"])["tag"]


def color(kind):
    return KINDS.get(kind, KINDS["file"])["color"]


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(1.0, max(0.0, s)), min(1.0, max(0.0, v)))
    return (round(r * 255), round(g * 255), round(b * 255))


def gradient(kind):
    """Three stops (darker, base, lighter) for the brand theme's selection glow,
    with a slight hue drift so the flow reads as color, not just brightness."""
    base = color(kind)
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in base))
    return [_hsv(h - 0.025, s * 1.05, v * 0.72), base, _hsv(h + 0.025, s * 0.7, v * 1.12 + 0.06)]


# ---- drawing -----------------------------------------------------------------

def _mix(a, b, amount):
    return (round(a[0] + (b[0] - a[0]) * amount),
            round(a[1] + (b[1] - a[1]) * amount),
            round(a[2] + (b[2] - a[2]) * amount))


def _luma(rgb):
    def linear(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@functools.lru_cache(maxsize=256)
def _ink(rgb):
    """The color darkened until it reads on the page (about 3:1), so a yellow JS
    or a pale theme accent still makes legible text."""
    out = rgb
    for step in range(13):
        out = _mix(rgb, (20, 20, 24), step / 12)
        if _luma(out) <= 0.2:
            break
    return out


def _half(top, bottom):
    """One cell for two stacked pixels; an empty pixel stays transparent."""
    if top is None and bottom is None:
        return (" ", None, None, False)
    if bottom is None:
        return ("▀", top, None, False)
    if top is None:
        return ("▄", bottom, None, False)
    if top == bottom:
        return ("█", top, None, False)
    return ("▀", top, bottom, False)


def _render(grid, colors):
    """Cells for a grid of letters, two pixel rows per cell row; '.' is empty."""
    return [[_half(colors.get(a), colors.get(b)) for a, b in zip(grid[y], grid[y + 1])]
            for y in range(0, len(grid), 2)]


def _overlay(grid, mark):
    return tuple("".join(m if m != "." else g for g, m in zip(row, over))
                 for row, over in zip(grid, mark))


def _text(row, start, text, fg, back):
    """Put real characters over a cell row, bold on the shape's own color."""
    for i, ch in enumerate(text):
        if 0 <= start + i < len(row):
            row[start + i] = (ch, fg, back, True)


def _clean(text):
    """Keep only printable single-width characters, so a caller's tag can't widen a row."""
    return "".join(ch for ch in str(text) if ch.isprintable() and not unicodedata.combining(ch)
                   and unicodedata.east_asian_width(ch) not in ("W", "F"))


_FOLDER = {
    "large": ("ttt.....",
              "11111111",
              "22222222",
              "33333333",
              "44444444",
              ".555555."),
    "small": ("tt..",
              "1111",
              "3333",
              "5555"),
}

# Large emblems sit in the folder body (rows 2-5, a stem may reach row 1).
_EMBLEMS = {
    "folder-desktop": ("........", "........", "..eeee..", "..eeee..", "...ee...", "..eeee.."),
    "folder-documents": ("........", "........", "..eeee..", "........", "..eeee..", "........"),
    "folder-downloads": ("........", "...ee...", "...ee...", ".eeeeee.", "..eeee..", "...ee..."),
    "folder-applications": ("........", "........", "...ee...", "..e..e..", "..eeee..", "..e..e.."),
    "folder-pictures": ("........", "........", "...e....", "..eee.e.", ".eeeeeee", "........"),
    "folder-music": ("........", "....ee..", "....e...", "....e...", "..eee...", "..ee...."),
    "folder-movies": ("........", "...e....", "...ee...", "...eee..", "...ee...", "...e...."),
    "folder-library": ("........", "........", ".eeeeee.", "..e..e..", "..e..e..", ".eeeeee."),
    "folder-public": ("........", "........", "...ee...", "........", "..eeee..", "..eeee.."),
    "folder-git": ("........", "........", "...oo...", "..oooo..", "...oo...", "........"),
}


def _folder_colors(base):
    return {"t": _mix(base, WHITE, 0.30), "1": _mix(base, WHITE, 0.14), "2": _mix(base, WHITE, 0.05),
            "3": base, "4": _mix(base, BLACK, 0.07), "5": _mix(base, BLACK, 0.15),
            "e": _mix(base, BLACK, 0.40), "o": GIT_ORANGE}


def _folder(kind, size, accent):
    base = accent or FOLDER_BLUE
    if size == "mini":
        return [[("▙", base, None, False), ("▄", base, None, False)]]
    grid = _FOLDER[size]
    if kind in _EMBLEMS:
        if size == "large":
            grid = _overlay(grid, _EMBLEMS[kind])
        else:   # too small to tell emblems apart: a mark says "special"
            dot = "o" if kind == "folder-git" else "e"
            grid = _overlay(grid, ("....", "....", "." + dot * 2 + ".", "...."))
    return _render(grid, _folder_colors(base))


_HOUSE = {
    "large": ("...rr...",
              "..rrrr..",
              "rrrrrrrr",
              ".wwwwww.",
              ".wwddww.",
              ".wwddww."),
    "small": (".rr.",
              "rrrr",
              "wwww",
              "wddw"),
}


def _house(size, accent):
    base = accent or FOLDER_BLUE
    if size == "mini":
        return [[("▟", base, None, False), ("▙", base, None, False)]]
    colors = {"r": _mix(base, BLACK, 0.12), "w": _mix(base, WHITE, 0.28), "d": _mix(base, BLACK, 0.45)}
    return _render(_HOUSE[size], colors)


_PAGE = {
    "large": (".ppppf..",
              ".pppppf.",
              ".pppppp.",
              ".pppppp.",
              ".pppppp.",
              ".qqqqqq."),
    "small": ("ppp.",
              "pppf",
              "pppp",
              "pppp"),
}
_PAGE_COLORS = {"p": PAGE, "q": PAGE_EDGE, "f": FOLD, "l": RULE}
_LINES = {
    "large": ("........", "........", "..llll..", "........", "..lll...", "........"),
    "small": ("....", "....", ".ll.", "...."),
}


def _document(kind, size, accent, tag):
    tint = accent or color(kind)
    if size == "mini":
        return [[("▙", tint, None, False), (" ", None, None, False)]]
    text = KINDS[kind]["tag"] if tag is None else _clean(tag)
    grid = _PAGE[size]
    if kind == "text" and not text:
        grid = _overlay(grid, _LINES[size])
    elif size == "small" and text:
        grid = _overlay(grid, ("....", "....", "bbbb", "...."))
    rows = _render(grid, dict(_PAGE_COLORS, b=tint))
    if size == "large" and text:
        text = text[:6]
        _text(rows[1], 1 + (6 - len(text)) // 2, text, _ink(tint), PAGE)
    return rows


def _image(size):
    colors = {"k": (132, 190, 240), "y": (250, 212, 100), "h": (104, 186, 112), "g": (70, 150, 96)}
    if size == "mini":
        return [[("▀", colors["k"], colors["h"], False), ("▀", colors["y"], colors["h"], False)]]
    grid = {"large": ("kkkkkyyk",
                      "kkkkkyyk",
                      "kkkhkkkk",
                      "kkhhhkkk",
                      "khhhhhkg",
                      "hhhhhhgg"),
            "small": ("kkky",
                      "kkkk",
                      "khkk",
                      "hhhg")}[size]
    return _render(grid, colors)


def _video(size):
    body = color("video")
    colors = {"w": (236, 236, 240), "d": (44, 44, 52), "v": body, "u": _mix(body, BLACK, 0.18)}
    if size == "mini":
        return [[("▀", colors["w"], body, False), ("▀", colors["d"], body, False)]]
    grid = {"large": ("wwddwwdd",
                      "vvvwvvvv",
                      "vvvwwvvv",
                      "vvvwwwvv",
                      "vvvwwvvv",
                      "uuuwuuuu"),
            "small": ("wdwd",
                      "vwvv",
                      "vwwv",
                      "uwuu")}[size]
    return _render(grid, colors)


def _audio(size):
    base = color("audio")
    if size == "mini":
        return [[("♪", base, None, True), (" ", None, None, False)]]
    grid = {"large": ("..lllll.",
                      "..lllll.",
                      "..n...n.",
                      "..n...n.",
                      "nnn.nnn.",
                      "nn..nn.."),
            "small": ("..nl",
                      "..n.",
                      "nnn.",
                      "nn..")}[size]
    return _render(grid, {"n": base, "l": _mix(base, WHITE, 0.2)})


def _archive(size):
    base = color("archive")
    if size == "mini":
        return [[("▙", base, None, False), (" ", None, None, False)]]
    colors = dict(_PAGE_COLORS, Z=_mix(base, WHITE, 0.15), z=_mix(base, BLACK, 0.3),
                  x=_mix(base, BLACK, 0.45))
    grid = {"large": (".ppZzf..",
                      ".ppzZpf.",
                      ".ppZzpp.",
                      ".ppzZpp.",
                      ".ppxxpp.",
                      ".qqqqqq."),
            "small": ("pZz.",
                      "pzZf",
                      "pZzp",
                      "pxxp")}[size]
    return _render(grid, colors)


def _disk(size):
    colors = {"l": (228, 230, 236), "s": (192, 196, 206), "d": (142, 147, 160), "g": PROMPT}
    if size == "mini":
        return [[("▀", colors["s"], colors["d"], False), ("▀", colors["s"], colors["g"], False)]]
    grid = {"large": ("........",
                      ".llllll.",
                      "ssssssss",
                      "ssssssss",
                      "ddddgddd",
                      ".dddddd."),
            "small": ("....",
                      "llll",
                      "ssss",
                      "ddgd")}[size]
    return _render(grid, colors)


def _app(size):
    base = color("app")
    colors = {"T": _mix(base, WHITE, 0.18), "M": base, "B": _mix(base, BLACK, 0.2), "g": (246, 246, 250)}
    if size == "mini":
        return [[("▀", colors["T"], colors["B"], False)] * 2]
    grid = {"large": (".TTTTTT.",
                      "TTTggTTT",
                      "MMgMMgMM",
                      "MMggggMM",
                      "BgBBBBgB",
                      ".BBBBBB."),
            "small": (".TT.",
                      "TggT",
                      "MggM",
                      ".BB.")}[size]
    return _render(grid, colors)


def _package(size):
    base = color("package")
    colors = {"l": _mix(base, WHITE, 0.22), "L": _mix(base, WHITE, 0.08), "b": base,
              "d": _mix(base, BLACK, 0.18), "t": (236, 216, 172)}
    if size == "mini":
        return [[("▀", colors["l"], base, False), ("▀", colors["t"], base, False)]]
    grid = {"large": ("llllllll",
                      "LLLttLLL",
                      "bbbttbbb",
                      "bbbttbbb",
                      "bbbbbbbb",
                      "dddddddd"),
            "small": ("lttl",
                      "bttb",
                      "bbbb",
                      "dddd")}[size]
    return _render(grid, colors)


def _terminal(size):
    if size == "mini":
        return [[(">", PROMPT, TILE, True), ("_", PROMPT, TILE, True)]]
    colors = {"k": TILE, "h": TITLE_BAR, "R": LIGHTS[0], "Y": LIGHTS[1], "G": LIGHTS[2]}
    grid = {"large": (".RYGhhh.",
                      "kkkkkkkk",
                      "kkkkkkkk",
                      "kkkkkkkk",
                      "kkkkkkkk",
                      ".kkkkkk."),
            "small": (".hh.",
                      "kkkk",
                      "kkkk",
                      ".kk.")}[size]
    rows = _render(grid, colors)
    _text(rows[1], 1, ">_", PROMPT, TILE)
    return rows


_ART = {"image": _image, "video": _video, "audio": _audio, "archive": _archive,
        "diskimage": _disk, "app": _app, "package": _package, "executable": _terminal}


def _with_badge(rows, size):
    """Finder's alias arrow: a small light tile, bottom-left (the right cell on
    mini icons, whose left cell carries the whole shape)."""
    rows = [list(row) for row in rows]
    col = 1 if size == "mini" else 0
    rows[-1][col] = ("▸", ALIAS_FG, ALIAS_BG, True)
    return rows


@functools.lru_cache(maxsize=4096)
def _icon(kind, size, accent, link, tag):
    if kind == "home":
        rows = _house(size, accent)
    elif kind.startswith("folder"):
        rows = _folder(kind, size, accent)
    elif kind in _ART:
        rows = _ART[kind](size)
    else:
        rows = _document(kind, size, accent, tag)
    if link:
        rows = _with_badge(rows, size)
    return tuple(tuple(row) for row in rows)


def icon(kind, size="large", accent=None, link=False, tag=None):
    """The kind's icon as rows of (ch, fg, bg, bold) cells, exactly SIZES[size].

    accent tints folders and a document's tag or band (a theme color); pages and
    pictures keep their natural colors. link adds the alias badge. tag replaces
    the text on a large document. Outside the shape, cells are transparent."""
    kind = kind if kind in KINDS else "file"
    size = size if size in SIZES else "large"
    accent = None if accent is None else tuple(int(c) for c in tuple(accent)[:3])
    tag = None if tag is None else str(tag)
    return [list(row) for row in _icon(kind, size, accent, bool(link), tag)]


# ---- preview -----------------------------------------------------------------

def _paint(cells, back, plain=False):
    if plain:
        return "".join(ch for ch, _, _, _ in cells)
    out = []
    for ch, fg, bg, bold in cells:
        b = bg or back
        out.append(f"\x1b[48;2;{b[0]};{b[1]};{b[2]}m")
        if fg:
            out.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
        out.append(("\x1b[1m" if bold else "") + ch + "\x1b[0m")
    return "".join(out)


def _preview(argv):
    """Every kind at every size on a dark background (--plain: glyphs only)."""
    plain = "--plain" in argv
    back, slot = (28, 28, 32), 14
    gap = "" if plain else f"\x1b[48;2;{back[0]};{back[1]};{back[2]}m"
    reset = "" if plain else "\x1b[0m"
    variants = [(kind, {}) for kind in KINDS]
    variants += [("folder", {"link": True}), ("python", {"link": True}),
                 ("folder", {"accent": (240, 130, 175)}), ("python", {"accent": (240, 130, 175)}),
                 ("file", {"tag": badge("file", "data.bin")})]
    per = max(1, (os.get_terminal_size(sys.stdout.fileno()).columns if sys.stdout.isatty() else 112) // slot)
    for start in range(0, len(variants), per):
        group = variants[start:start + per]
        for size in SIZES:
            cols, height = SIZES[size]
            icons = [icon(kind, size, **opts) for kind, opts in group]
            for y in range(height):
                line = "".join(_paint(rows[y], back, plain) + gap + " " * (slot - cols) for rows in icons)
                print(gap + line + reset)
            print(gap + " " * (slot * len(group)) + reset)
        names = []
        for kind, opts in group:
            note = kind + ("*" if opts else "")
            names.append(note[:slot - 1].ljust(slot))
        print(gap + "".join(names) + reset)
        print(gap + " " * (slot * len(group)) + reset)


if __name__ == "__main__":
    _preview(sys.argv[1:])
