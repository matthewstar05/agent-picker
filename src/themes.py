"""Color themes shared by every screen, so a theme added once shows in the usage
panel, the file browser, and anything drawn later.

The built-in themes live in themes.json in the checkout. A themes.json beside the
local config (see --picker-doctor) adds more: an entry with a built-in's name
replaces it in place, and new names follow the built-ins in file order. Each
entry is {"name": ..., "stops": ...}, where stops is null (each agent in its own
colors, like brand), "rainbow", or 1 to 8 colors as [r, g, b] or "#rrggbb".
Both files are reread when they change, so a running screen picks up an edit
within about a second. Loading never raises: a bad entry is skipped, and a file
that can't be read keeps what it loaded last, with the reason in problems().
"""

import json
import re
import time

import settings

BUILTIN = settings.ROOT / "themes.json"
RECHECK = 1.0       # seconds between looks; screens ask every frame
MAX_STOPS = 8
FALLBACK = [{"name": "brand", "stops": None}]   # only if the built-in file never loads
HEX = re.compile(r"#([0-9a-fA-F]{6})")

THEMES = []         # the live list; reload() changes it in place
_files = {}         # "builtin" / "local" -> (path, stamp, themes, problems) from its last read
_checked = None     # monotonic time of the last look
_tracked = []       # (setting, extra) pairs whose choices follow the themes


def local_path():
    """The user's own themes, beside the local config."""
    return settings.config_path().parent / "themes.json"


def _stamp(path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _color(value):
    """(r, g, b) from [r, g, b] or "#rrggbb", or None for anything else."""
    if isinstance(value, str):
        match = HEX.fullmatch(value.strip())
        return tuple(int(match.group(1)[k:k + 2], 16) for k in (0, 2, 4)) if match else None
    if isinstance(value, list) and len(value) == 3 and all(
            isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in value):
        return tuple(value)
    return None


def _theme(entry):
    """(theme, None) for a usable file entry, or (None, why it was skipped)."""
    if not isinstance(entry, dict):
        return None, "not an object"
    name = entry.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name or not name.isprintable():
        return None, "it needs a name"
    if "stops" not in entry:
        return None, f"{name} needs stops"
    stops = entry["stops"]
    if stops is None or stops == "rainbow":
        return {"name": name, "stops": stops}, None
    if not isinstance(stops, list) or not 1 <= len(stops) <= MAX_STOPS:
        return None, f'{name}: stops must be null, "rainbow", or a list of 1 to {MAX_STOPS} colors'
    colors = [_color(c) for c in stops]
    if None in colors:
        bad = json.dumps(stops[colors.index(None)])
        return None, f'{name}: {bad} is not [r, g, b] with 0-255 values or "#rrggbb"'
    return {"name": name, "stops": colors}, None


def _read(path, last, required):
    """(themes, problems) from one file. A missing optional file has none; a file
    that can't be read or parsed keeps last."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if not required:
            return [], []
        return last, [f"{path}: missing"]
    except Exception as error:
        return last, [f"{path}: can't be read ({error}), keeping the themes it had"]
    if isinstance(data, dict):
        data = data.get("themes")
    if not isinstance(data, list):
        return last, [f'{path}: expected a list of themes or {{"themes": [...]}}, keeping the themes it had']
    found, problems = [], []
    for n, entry in enumerate(data, 1):
        theme, reason = _theme(entry)
        if theme:
            found.append(theme)
        else:
            problems.append(f"{path}: theme {n} skipped, {reason}")
    return found, problems


def _merge(*lists):
    """Later themes replace earlier ones of the same name in place; new names go last."""
    merged = []
    for themes in lists:
        for theme in themes:
            at = next((i for i, t in enumerate(merged) if t["name"] == theme["name"]), None)
            if at is None:
                merged.append(dict(theme))
            else:
                merged[at] = dict(theme)
    return merged or [dict(t) for t in FALLBACK]


def reload(force=False):
    """Reread whichever theme file changed; True when the list of themes changed.
    Looks at most once per RECHECK seconds unless force."""
    global _checked
    now = time.monotonic()
    if not force and _checked is not None and now - _checked < RECHECK:
        return False
    _checked = now
    try:
        local = local_path()
    except Exception:
        local = None
    reread = False
    for role, path in (("builtin", BUILTIN), ("local", local)):
        stamp = _stamp(path) if path else None
        last = _files.get(role)
        if last and last[0] == path and last[1] == stamp:
            continue
        previous = last[2] if last and last[0] == path else FALLBACK if role == "builtin" else []
        found, problems = _read(path, previous, role == "builtin") if path else ([], [])
        _files[role] = (path, stamp, found, problems)
        reread = True
    if not reread:
        return False
    merged = _merge(_files["builtin"][2], _files["local"][2])
    if merged == THEMES:
        return False
    THEMES[:] = merged
    for setting, extra in _tracked:
        setting.choices[:] = extra + names()
    return True


def names():
    return [theme["name"] for theme in THEMES]


def index(name):
    """Where a theme is in THEMES; 0 for a name that isn't there."""
    return next((i for i, theme in enumerate(THEMES) if theme["name"] == name), 0)


def get(name):
    return THEMES[index(name)]


def stops(name):
    return get(name)["stops"]


def track(setting, extra=()):
    """Keep a setting's choices at extra plus every theme's name, now and after
    each reload that changes the themes, so a new theme can be picked straight
    away. Returns the setting."""
    extra = list(extra)
    _tracked[:] = [pair for pair in _tracked if pair[0] is not setting]
    _tracked.append((setting, extra))
    setting.choices[:] = extra + names()
    return setting


def sources():
    """[(path, exists)] for the built-in and local theme files."""
    paths = [BUILTIN]
    try:
        paths.append(local_path())
    except Exception:
        pass
    return [(path, _stamp(path) is not None) for path in paths]


def problems():
    """Why entries or files were skipped when each file was last read."""
    return [p for role in ("builtin", "local") if role in _files for p in _files[role][3]]


reload(force=True)
