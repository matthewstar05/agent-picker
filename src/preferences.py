"""Preferences behind the settings screen, shared by every view of the command.

Each setting is one Setting in SETTINGS. A feature registers its own with
register() and reads the current value with get(); the settings screen (press s)
lists every registered setting, grouped by section, with no other wiring. A Page
is a row that opens its own list of settings (register a child with parent=page).
Values are saved to preferences.json in the local state directory. Every
command shares that file and rereads it when it changes, so a setting changed in
one (the agent selector, the usage panel, herdr-ai) shows in the others.
"""

import json
import os
import time

from platform_runtime import state_dir

PATH = state_dir() / "preferences.json"
SETTINGS = []
_values = None
_stamp = None       # the file's (mtime, size) when _values was read
_checked = 0.0      # when the file was last looked at
RECHECK = 0.25      # seconds between looks; get() runs many times a frame


class Setting:
    def __init__(self, key, section, label, choices, default, hint="", names=None, off=None):
        self.key = key
        self.section = section      # heading it is listed under
        self.label = label
        self.choices = list(choices)
        self.default = default
        self.hint = hint            # one line shown while the setting is selected
        self.names = names or {}    # display text for a value, when it differs from the value
        self.off = off              # a choice that Enter toggles to and back from, if any

    def show(self, value):
        return self.names.get(value, str(value))


class Page:
    """A settings row that opens its own list of settings."""

    def __init__(self, key, section, label, hint="", summary=None):
        self.key = key
        self.section = section
        self.label = label
        self.hint = hint
        self.children = []
        self.summary = summary      # children -> the text shown as the row's value

    def show(self, value=None):
        return self.summary(self.children) if self.summary else f"{len(self.children)} settings"


def register(setting, parent=None):
    """Add a setting (once), after the rest of its section; returns it so a
    module can keep a handle. Settings registered later still list under their
    section's heading instead of starting a second one. With parent, it is
    listed on that Page instead of the top level."""
    existing = find(setting.key)
    if existing:
        return existing
    items = SETTINGS if parent is None else parent.children
    last = max((i for i, s in enumerate(items) if s.section == setting.section), default=len(items) - 1)
    items.insert(last + 1, setting)
    return setting


def _walk(items):
    for item in items:
        yield item
        if isinstance(item, Page):
            yield from _walk(item.children)


def find(key):
    return next((s for s in _walk(SETTINGS) if s.key == key), None)


def _file_stamp():
    try:
        st = PATH.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _load(fresh=False):
    """The saved values, reread when another command has changed the file."""
    global _values, _stamp, _checked
    now = time.monotonic()
    if _values is not None and not fresh and now - _checked < RECHECK:
        return _values
    _checked = now
    stamp = _file_stamp()
    if _values is None or stamp != _stamp:
        try:
            values = json.loads(PATH.read_text(encoding="utf-8"))
            _values, _stamp = (values if isinstance(values, dict) else {}), stamp
        except Exception:
            # Missing, or unreadable: keep what we had; a changed file is read next look.
            _values = {} if _values is None else _values
    return _values


def get(key):
    setting = find(key)
    if isinstance(setting, Page):
        return None
    value = _load().get(key, setting.default if setting else None)
    if setting and value not in setting.choices:
        return setting.default
    return value


def put(key, value):
    global _stamp
    _load(fresh=True)[key] = value   # fresh: keep what other commands saved meanwhile
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_name(f".preferences.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(_values, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, PATH)
        _stamp = _file_stamp()
    except Exception:
        pass


def toggle(key):
    """Flip a setting with an off choice between off and the value it had before."""
    setting = find(key)
    current = get(key)
    if current == setting.off:
        back = _load().get(key + ".on", setting.default)
        put(key, back if back in setting.choices and back != setting.off else setting.default)
    else:
        _load(fresh=True)[key + ".on"] = current
        put(key, setting.off)


def cycle(key, step=1):
    setting = find(key)
    current = get(key)
    index = setting.choices.index(current) if current in setting.choices else 0
    put(key, setting.choices[(index + step) % len(setting.choices)])


class Menu:
    """Cursor state for the settings screen; drawing lives in the shared renderer."""

    def __init__(self, items=None):
        self.index = 0
        self.items = SETTINGS if items is None else list(items)
        self.stack = []             # (items, index, page) for each page opened, innermost last

    @property
    def page(self):
        """The Page being shown, or None at the top level."""
        return self.stack[-1][2] if self.stack else None

    def handle(self, key):
        """Apply a key. Returns "close" when the screen should be left."""
        if not self.items:
            return self.back()
        self.index = min(self.index, len(self.items) - 1)
        item = self.items[self.index]
        if key in ("up", "k"):
            self.index = (self.index - 1) % len(self.items)
        elif key in ("down", "j", "\t"):
            self.index = (self.index + 1) % len(self.items)
        elif isinstance(item, Page):
            if key in ("right", "l", "\r", "\n", " ") and item.children:
                self.stack.append((self.items, self.index, item))
                self.items, self.index = item.children, 0
            elif key in ("esc", "s", "q", "\x7f", "\b"):
                return self.back()
        elif key in ("left", "h"):
            cycle(item.key, -1)
        elif key in ("\r", "\n", " ") and item.off is not None:
            toggle(item.key)
        elif key in ("right", "l", "\r", "\n", " "):
            cycle(item.key, 1)
        elif key in ("esc", "s", "q", "\x7f", "\b"):
            return self.back()
        return None

    def back(self):
        """Leave the page being shown, or the settings screen at the top level."""
        if not self.stack:
            return "close"
        self.items, self.index, _ = self.stack.pop()
        return None
