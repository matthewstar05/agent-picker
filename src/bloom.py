"""Bloom: a pixel-art backdrop behind Herdr, drawn by the Ghostty terminal.

Turning it on (Settings, Bloom backdrop) does three reversible things:

- writes a Ghostty include file with a palette and a background image, and adds
  one optional `config-file` line to Ghostty's config so it is loaded;
- adds a marked `[theme.custom]` block to Herdr's config.toml, so panes and the
  sidebar let the picture through;
- repaints the scene (blossoms, wisteria, glows, and the sky tint) in the usage
  panel's theme colors, and again whenever that theme changes.

Turning it off removes the include file and the marked block. The scenes are
small PNGs in bloom/scenes; any PNG added there becomes a choice. Only the
standard library is used: the PNG reader and writer below are enough for flat
pixel art. herdr-ai's status watcher calls sync() once a second, so a setting
changed on any screen applies within about a second; the settings screens and
`agent --picker-bloom` call it directly as well.
"""

import colorsys
import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import preferences
import settings

SUPPORTED = sys.platform != "win32"     # Ghostty runs on macOS and Linux
SCENES = settings.ROOT / "bloom" / "scenes"
SCALE = 8                               # screen pixels per art pixel in the image Ghostty loads
KEEP = 12                               # repainted images kept in the cache
BEGIN, END = "# >>> bloom backdrop (managed by agent-picker)", "# <<< bloom backdrop"
INCLUDE_NOTE = "# agent-picker bloom backdrop: this file exists only while the setting is on"
ACCENT = "#f2a7c3"

# Scene colors that follow the theme, lightest first. Foliage, bark and the warm
# lantern light stay as drawn.
GROUPS = {
    "blossom": ["#f2a7c3", "#e77fa8", "#b85c8a", "#7a3f66", "#4a2c4c"],
    "wisteria": ["#c9a0f0", "#9a7fe0", "#6a56b0", "#3d3470", "#2a2548"],
    "spark": ["#8ff0d2", "#7fa8f0", "#4fb8a0", "#3d9c8c"],
}
SKY = ["#0f0e19", "#14121f", "#1b1a2e", "#232640", "#2d3358", "#4a5484"]

# Terminal and Herdr colors. The ones in TINTED lean toward the theme with the sky.
# The overlays are the sidebar's quiet text; they are kept light enough to read
# with nothing solid behind them, since the sidebar is see-through.
GHOSTTY = {"background": "#14121f", "foreground": "#ece4f5", "cursor-color": ACCENT, "cursor-text": "#14121f",
           "selection-background": "#3a3260", "selection-foreground": "#fbf6ff"}
PALETTE = ["#1b1a2e", "#f0788c", "#8fc27a", "#ffd98a", "#7fa8f0", "#c9a0f0", "#8ff0d2", "#ece4f5",
           "#807ba6", "#ff9aa8", "#b4e09a", "#ffe6ad", "#a4c2ff", "#dcbcff", "#b6f7e3", "#fbf6ff"]
HERDR = {"panel_bg": "reset", "sidebar_bg": "reset", "active_row_bg": "#2a2440", "selection_bg": "#3a3260",
         "surface0": "#1b1a2e", "surface1": "#2d3358", "surface_dim": "#14121f", "overlay0": "#8f8ab3",
         "overlay1": "#aca6cf", "text": "#ece4f5", "subtext0": "#cfc6e4", "accent": ACCENT,
         "mauve": "#c9a0f0", "green": "#8fc27a", "yellow": "#ffd98a", "red": "#f0788c", "blue": "#7fa8f0",
         "teal": "#8ff0d2", "peach": "#ffb38a"}
TINTED = {"background", "selection-background", "active_row_bg", "selection_bg", "surface0", "surface1",
          "surface_dim", "overlay0", "overlay1"}


def scene_names():
    try:
        return sorted(path.stem for path in SCENES.glob("*.png"))
    except OSError:
        return []


def _summary(children):
    if not preferences.get(ENABLED.key):
        return "colors only" if preferences.get(COLORS.key) else "off"
    return f"on · {preferences.get(SCENE.key)}"


_ON_OFF = {False: "off", True: "on"}
_SECTION = "Bloom backdrop"
PAGE = preferences.Page(
    "bloom", "General", "Bloom backdrop",
    hint="A pixel-art backdrop behind Herdr in the Ghostty terminal. ⏎ opens.", summary=_summary)
ENABLED = preferences.Setting(
    "bloom.enabled", _SECTION, "Backdrop", [False, True], False, names=_ON_OFF,
    hint="Draws the scene behind every pane. Off restores your normal look, unless Herdr colors is on.")
COLORS = preferences.Setting(
    "bloom.colors", _SECTION, "Herdr colors", [False, True], False, names=_ON_OFF,
    hint="Keeps the Bloom colors, following the usage theme, while the backdrop is off.")
SCENE = preferences.Setting(
    "bloom.scene", _SECTION, "Scene", scene_names() or ["elder-tree"], (scene_names() or ["elder-tree"])[0],
    hint="Which picture is drawn. PNG files in bloom/scenes are listed here.")
DIM = preferences.Setting(
    "bloom.dim", _SECTION, "Strength", [0, 15, 25, 35, 45, 55, 70, 85, 100], 45,
    names={0: "0% (colors only)", **{n: f"{n}%" for n in (15, 25, 35, 45, 55, 70, 85, 100)}},
    hint="How strongly the picture shows through. 0% hides it and keeps the Bloom colors.")
SIDE = preferences.Setting(
    "bloom.side", _SECTION, "Tree side", ["right", "left"], "right",
    hint="Left mirrors the picture so the tree stands by the sidebar.")
SHIFT = preferences.Setting(
    "bloom.shift", _SECTION, "Tree offset", [0, 8, 16, 26, 36, 48], 0,
    names={0: "none", 8: "8", 16: "16", 26: "26", 36: "36", 48: "48"},
    hint="Slides the picture toward the tree's edge, in art pixels. 26 puts the trunk behind the sidebar.")
FOLLOW = preferences.Setting(
    "bloom.follow", _SECTION, "Theme colors", [True, False], True, names=_ON_OFF,
    hint="Blossoms, wisteria and glows take the usage panel theme's colors.")
TINT = preferences.Setting(
    "bloom.tint", _SECTION, "Theme tint", [True, False], True, names=_ON_OFF,
    hint="Backgrounds lean toward the theme's first color. brand, rainbow and mono get black and white.")
SETTINGS = (ENABLED, COLORS, SCENE, DIM, SIDE, SHIFT, FOLLOW, TINT)


def register():
    """List the Bloom page on the settings screens; nothing where Ghostty can't run.
    Returns the page, or None when unsupported."""
    if not SUPPORTED:
        return None
    page = preferences.register(PAGE)
    for setting in SETTINGS:
        preferences.register(setting, parent=page)
    names = scene_names()
    if names:
        SCENE.choices[:] = names
        if SCENE.default not in names:
            SCENE.default = names[0]
    return page


# ---- colors ----

def rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def hexa(color):
    return "#%02x%02x%02x" % tuple(color)


def luma(color):
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def lift(color, floor=150):
    """A dark theme color still has to read as a flower on a night sky."""
    top = max(color)
    if top >= floor or top == 0:
        return tuple(color)
    return tuple(min(255, round(v * floor / top)) for v in color)


def tinted(color, toward):
    """color with the hue of toward, keeping its brightness. A gray theme grays it."""
    _, saturation, value = colorsys.rgb_to_hsv(*(c / 255 for c in color))
    hue, theme_saturation, _ = colorsys.rgb_to_hsv(*(c / 255 for c in toward))
    saturation = min(1.0, saturation * max(0.15, min(1.15, theme_saturation / 0.5)))
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, saturation, value))


def recolor_table(stops, tint):
    """{scene color: new color} for a list of theme stops."""
    stops = [tuple(stop) for stop in stops]
    lifted = [lift(stop) for stop in stops]
    goal = {"blossom": lifted[-1], "wisteria": lifted[0], "spark": lifted[len(lifted) // 2]}
    table = {}
    for group, colors in GROUPS.items():
        top = luma(rgb(colors[0]))
        for color in map(rgb, colors):
            scale = min(1.0, luma(color) / top)
            table[color] = tuple(min(255, round(v * scale)) for v in goal[group])
    if tint:
        for color in map(rgb, SKY):
            table[color] = tinted(color, stops[0])
    return table


def neutral(stops):
    """Themes with no color of their own to lend: brand, rainbow, and grays like mono."""
    if not isinstance(stops, list) or not stops:
        return True
    return all(colorsys.rgb_to_hsv(*(c / 255 for c in stop))[1] < 0.12 for stop in stops)


def gray(value):
    return hexa((round(luma(rgb(value))),) * 3)


STATUS = {"green", "yellow", "red", "teal", "peach"}    # these carry meaning in the sidebar


def interface(stops, tint):
    """(ghostty colors, 16-color palette, herdr tokens) for the theme stops. A
    colored theme lends its hues; a neutral one gets black and white."""
    ghostty, palette, herdr = dict(GHOSTTY), list(PALETTE), dict(HERDR)
    plain = neutral(stops)
    if not plain:
        ghostty["cursor-color"] = herdr["accent"] = hexa(lift(stops[-1], 190))
        herdr["mauve"] = hexa(lift(stops[0], 190))
        herdr["blue"] = hexa(lift(stops[len(stops) // 2], 190))
    if tint and plain:
        ghostty = {key: gray(value) for key, value in ghostty.items()}
        herdr = {key: value if key in STATUS or value == "reset" else gray(value) for key, value in herdr.items()}
        ghostty["cursor-color"] = herdr["accent"] = "#f4f4f4"
        for index in (0, 7, 8, 15):
            palette[index] = gray(palette[index])
    elif tint:
        shade = lambda value: hexa(tinted(rgb(value), stops[0]))
        ghostty = {key: shade(value) if key in TINTED else value for key, value in ghostty.items()}
        herdr = {key: shade(value) if key in TINTED else value for key, value in herdr.items()}
        palette[0], palette[8] = shade(palette[0]), shade(palette[8])
    return ghostty, palette, herdr


# ---- PNG: 8-bit RGB or RGBA, not interlaced ----

def read_png(path):
    """(width, height, rows) where each row is a list of (r, g, b)."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    at, header, packed = 8, None, []
    while at + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[at:at + 8])
        body = data[at + 8:at + 8 + size]
        at += 12 + size
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            packed.append(body)
        elif kind == b"IEND":
            break
    if not header:
        raise ValueError(f"{path}: no header")
    width, height, depth, color, _, _, interlace = header
    if depth != 8 or color not in (2, 6) or interlace:
        raise ValueError(f"{path}: save it as an 8-bit RGB PNG without interlacing")
    step = 3 if color == 2 else 4
    stride = width * step
    raw = zlib.decompress(b"".join(packed))
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 255
        elif kind == 3:
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                up, corner = previous[i], previous[i - step] if i >= step else 0
                guess = left + up - corner
                a, b, c = abs(guess - left), abs(guess - up), abs(guess - corner)
                line[i] = (line[i] + (left if a <= b and a <= c else up if b <= c else corner)) & 255
        elif kind:
            raise ValueError(f"{path}: unknown row filter {kind}")
        rows.append([tuple(line[i:i + 3]) for i in range(0, stride, step)])
        previous = line
    return width, height, rows


def write_png(path, rows, scale=1):
    """Save rows of (r, g, b), each art pixel drawn scale by scale."""
    blocks, raw = {}, []
    for row in rows:
        line = b"\x00" + b"".join(
            blocks.get(pixel) or blocks.setdefault(pixel, bytes(pixel) * scale) for pixel in row)
        raw.append(line * scale)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)

    header = struct.pack(">IIBBBBB", len(rows[0]) * scale, len(rows) * scale, 8, 2, 0, 0, 0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                          + chunk(b"IDAT", zlib.compress(b"".join(raw), 6)) + chunk(b"IEND", b""))
    os.replace(temporary, path)


def repaint(rows, stops, tint=True, mirror=False, shift=0):
    """The scene's rows in the theme colors, slid toward the tree and mirrored."""
    width = len(rows[0])
    if stops == "rainbow":
        members = {rgb(c): (offset, luma(rgb(colors[0])))
                   for (colors, offset) in zip(GROUPS.values(), (0.0, 0.33, 0.66)) for c in colors}
        painted = []
        for row in rows:
            line = []
            for x, pixel in enumerate(row):
                hit = members.get(pixel)
                if hit:
                    offset, top = hit
                    scale = min(1.0, luma(pixel) / top)
                    pixel = tuple(round(c * 255 * scale)
                                  for c in colorsys.hsv_to_rgb((x / width + offset) % 1, 0.55, 1.0))
                line.append(pixel)
            painted.append(line)
        rows = painted
    elif isinstance(stops, list) and stops:
        table = recolor_table(stops, tint)
        rows = [[table.get(pixel, pixel) for pixel in row] for row in rows]
    shift = max(0, min(int(shift), width - 1))
    if shift:
        # The tree is drawn on the right. The strip of sky and meadow this opens
        # on the far side is filled with its own reflection.
        rows = [row[:shift][::-1] + row[:width - shift] for row in rows]
    if mirror:
        rows = [row[::-1] for row in rows]
    return rows


# ---- files ----

def home():
    """Beside preferences.json, so what was applied is recorded with the settings
    that asked for it."""
    return preferences.PATH.parent / "bloom"


def active_path():
    """The Ghostty include file; it exists only while the backdrop is on."""
    return home() / "active.ghostty"


def herdr_config_path():
    if os.environ.get("HERDR_CONFIG_PATH"):
        return Path(os.environ["HERDR_CONFIG_PATH"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base or str(Path.home() / ".config")) / "herdr/config.toml"


def ghostty_config_path():
    """Ghostty's config file, or None when Ghostty has never been set up here."""
    if os.environ.get("AGENT_PICKER_GHOSTTY_CONFIG"):
        return Path(os.environ["AGENT_PICKER_GHOSTTY_CONFIG"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "ghostty"
    folders = [base]
    if sys.platform == "darwin":
        folders.append(Path.home() / "Library/Application Support/com.mitchellh.ghostty")
    for folder in folders:
        for name in ("config.ghostty", "config"):
            if (folder / name).is_file():
                return folder / name
    for folder in folders:
        if folder.is_dir():
            return folder / "config.ghostty"
    return None


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def include_line():
    return f"config-file = ?{active_path()}"


def with_include(text):
    """Ghostty's config with our optional include as its last word."""
    line = include_line()
    if line in text.splitlines():
        return text
    kept = [l for l in text.splitlines() if l.strip() != INCLUDE_NOTE
            and not (l.startswith("config-file = ?") and l.rstrip().endswith("bloom/active.ghostty"))]
    return "\n".join(kept).rstrip("\n") + ("\n" if kept else "") + f"{INCLUDE_NOTE}\n{line}\n"


def without_block(text):
    stripped = re.sub(r"\n*" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "\n", text, flags=re.S)
    return stripped if stripped == text else stripped.rstrip("\n") + "\n"


def with_block(text, tokens):
    """Herdr's config with our [theme.custom] block. A [theme.custom] of your own
    is left alone, since TOML allows only one."""
    text = without_block(text)
    if re.search(r"(?m)^\[theme\.custom\]", text):
        return text
    lines = "\n".join(f'{key} = "{value}"' for key, value in tokens.items())
    return text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"{BEGIN}\n[theme.custom]\n{lines}\n{END}\n"


def ghostty_text(image, dim, mirror, colors, palette):
    lines = ["# Written by agent-picker (Settings, Bloom backdrop). Edits here are overwritten."]
    lines += [f"{key} = {value}" for key, value in colors.items()]
    lines += [f"palette = {i}={value}" for i, value in enumerate(palette)]
    if image:
        lines += [f"background-image = {image}", "background-image-fit = cover",
                  f"background-image-position = bottom-{'left' if mirror else 'right'}",
                  f"background-image-opacity = {dim / 100:.2f}"]
    return "\n".join(lines) + "\n"


# ---- applying ----

def spec():
    """What the settings ask for now, or None while the backdrop and its colors
    are both off. Colors alone are the backdrop at no strength."""
    if not SUPPORTED:
        return None
    register()
    picture = bool(preferences.get(ENABLED.key))
    if not picture and not preferences.get(COLORS.key):
        return None
    names = scene_names()
    if not names:
        return None
    scene = preferences.get(SCENE.key)
    stops = None
    if preferences.get(FOLLOW.key):
        import themes   # loaded only while the backdrop follows a theme
        themes.reload()
        stops = themes.stops(preferences.get("usage.theme") or "brand")
        stops = [list(stop) for stop in stops] if isinstance(stops, list) else stops
    return {"scene": scene if scene in names else names[0], "dim": preferences.get(DIM.key) if picture else 0,
            "mirror": preferences.get(SIDE.key) == "left", "shift": preferences.get(SHIFT.key),
            "stops": stops, "tint": bool(preferences.get(TINT.key))}


def image_for(want):
    """The PNG Ghostty loads for these settings, painted once and cached."""
    source = SCENES / f"{want['scene']}.png"
    stamp = source.stat()
    key = json.dumps([want["scene"], want["stops"], want["tint"], want["mirror"], want["shift"],
                      stamp.st_mtime_ns, stamp.st_size, SCALE], sort_keys=True)
    target = home() / "cache" / f"{want['scene']}-{hashlib.sha1(key.encode()).hexdigest()[:12]}.png"
    if not target.exists():
        _, _, rows = read_png(source)
        write_png(target, repaint(rows, want["stops"], want["tint"], want["mirror"], want["shift"]), SCALE)
        old = sorted((p for p in target.parent.glob("*.png") if p != target),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        for path in old[KEEP - 1:]:
            try:
                path.unlink()
            except OSError:
                pass
    return target


def reload_herdr(call=None):
    """Ask Herdr to reread config.toml; False when it refused the file."""
    try:
        if call:
            return call("server.reload_config", {}).get("status") not in ("failed", "partial")
        binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
        if binary:
            subprocess.run([binary, "server", "reload-config"], capture_output=True, timeout=10)
    except Exception:
        pass
    return True


def reload_ghostty():
    """Ghostty rereads its config on SIGUSR2."""
    usr2 = getattr(signal, "SIGUSR2", None)
    if usr2 is None:
        return
    try:
        found = subprocess.run(["pgrep", "-x", "ghostty"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for pid in found.split():
        try:
            os.kill(int(pid), usr2)
        except (OSError, ValueError):
            pass


def _signature(want):
    return json.dumps(want, sort_keys=True)


def _applied():
    try:
        return json.loads((home() / "applied.json").read_text(encoding="utf-8")).get("signature")
    except (OSError, ValueError, AttributeError):
        return None


def sync(call=None):
    """Make Ghostty and Herdr match the settings. Cheap when nothing changed, so
    it can run every second. Returns True when something was written."""
    if not SUPPORTED:
        return False
    want = spec()
    config = herdr_config_path()
    herdr_text = read_text(config)
    active = active_path()
    if want is None:
        if _applied() is None:
            return False       # nothing of ours is in place, as far as these settings know
        try:
            active.unlink()
        except OSError:
            pass
        cleaned = without_block(herdr_text)
        if cleaned != herdr_text:
            write_text(config, cleaned)
            reload_herdr(call)
        write_text(home() / "applied.json", json.dumps({"signature": None}) + "\n")
        reload_ghostty()
        return True
    ghostty = ghostty_config_path()
    if ghostty is None:
        return False       # Ghostty isn't set up here; the setting waits
    signature = _signature(want)
    colors, palette, tokens = interface(want["stops"], want["tint"])
    wanted_herdr = with_block(herdr_text, tokens) if config.exists() else herdr_text
    ghostty_before = read_text(ghostty)
    if (signature == _applied() and active.exists() and wanted_herdr == herdr_text
            and include_line() in ghostty_before.splitlines()):
        return False
    image = image_for(want) if want["dim"] else None     # 0%: colors only, no picture
    write_text(active, ghostty_text(image, want["dim"], want["mirror"], colors, palette))
    included = with_include(ghostty_before)
    if included != ghostty_before:
        backup = home() / "ghostty-config-original"
        if not backup.exists() and ghostty.exists():
            write_text(backup, ghostty_before)
        write_text(ghostty, included)
    if wanted_herdr != herdr_text:
        write_text(config, wanted_herdr)
        if not reload_herdr(call):
            write_text(config, herdr_text)     # Herdr refused it: keep the picture, drop the block
            reload_herdr(call)
    write_text(home() / "applied.json", json.dumps({"signature": signature}) + "\n")
    reload_ghostty()
    return True


def status():
    """Lines for `agent --picker-bloom status` and the doctor."""
    if not SUPPORTED:
        return ["Bloom backdrop: not available on this platform (it needs the Ghostty terminal)."]
    register()
    ghostty = ghostty_config_path()
    on = bool(preferences.get(ENABLED.key))
    state = "on" if on else "off, Herdr colors kept" if preferences.get(COLORS.key) else "off"
    lines = [f"Bloom backdrop: {state}"
             + ("" if ghostty else " (Ghostty config not found, so nothing is drawn)")]
    lines.append("  scene {} · strength {}% · tree {} · offset {} · theme colors {} · tint {}".format(
        preferences.get(SCENE.key), preferences.get(DIM.key), preferences.get(SIDE.key),
        preferences.get(SHIFT.key), _ON_OFF[bool(preferences.get(FOLLOW.key))],
        _ON_OFF[bool(preferences.get(TINT.key))]))
    if ghostty:
        lines.append(f"  Ghostty config: {ghostty}")
    return lines


def command(args):
    """agent --picker-bloom [on|off|toggle|status]"""
    action = args[0] if args else "status"
    if action not in ("on", "off", "toggle", "status"):
        print("agent --picker-bloom [on|off|toggle|status]", file=sys.stderr)
        return 2
    if not SUPPORTED:
        print(status()[0])
        return 0 if action == "status" else 1
    register()
    if action != "status":
        now = bool(preferences.get(ENABLED.key))
        preferences.put(ENABLED.key, (not now) if action == "toggle" else action == "on")
        sync()
    print("\n".join(status()))
    return 0
