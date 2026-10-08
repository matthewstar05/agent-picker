"""Preferences behind the settings screen, shared by every view of the command.

Each setting is one Setting in SETTINGS. A feature registers its own with
register() and reads the current value with get(); the settings screen (press s)
lists every registered setting, grouped by section, with no other wiring.
Values are saved to preferences.json in the local state directory.
"""

import json
import os

from platform_runtime import state_dir

PATH = state_dir() / "preferences.json"
SETTINGS = []
_values = None


class Setting:
    def __init__(self, key, section, label, choices, default, hint="", names=None):
        self.key = key
        self.section = section      # heading it is listed under
        self.label = label
        self.choices = list(choices)
        self.default = default
        self.hint = hint            # one line shown while the setting is selected
        self.names = names or {}    # display text for a value, when it differs from the value

    def show(self, value):
        return self.names.get(value, str(value))


def register(setting):
    """Add a setting (once); returns it so a module can keep a handle."""
    if not any(s.key == setting.key for s in SETTINGS):
        SETTINGS.append(setting)
    return setting


def find(key):
    return next((s for s in SETTINGS if s.key == key), None)


def _load():
    global _values
    if _values is None:
        try:
            _values = json.loads(PATH.read_text(encoding="utf-8"))
            if not isinstance(_values, dict):
                _values = {}
        except Exception:
            _values = {}
    return _values


def get(key):
    setting = find(key)
    value = _load().get(key, setting.default if setting else None)
    if setting and value not in setting.choices:
        return setting.default
    return value


def put(key, value):
    _load()[key] = value
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_name(f".preferences.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(_values, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, PATH)
    except Exception:
        pass


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

    def handle(self, key):
        """Apply a key. Returns "close" when the screen should be left."""
        if not self.items:
            return "close"
        self.index = min(self.index, len(self.items) - 1)
        if key in ("up", "k"):
            self.index = (self.index - 1) % len(self.items)
        elif key in ("down", "j", "\t"):
            self.index = (self.index + 1) % len(self.items)
        elif key in ("left", "h"):
            cycle(self.items[self.index].key, -1)
        elif key in ("right", "l", "\r", "\n", " "):
            cycle(self.items[self.index].key, 1)
        elif key in ("esc", "s", "q", "\x7f", "\b"):
            return "close"
        return None
