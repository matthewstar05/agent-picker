"""Herdr briefs for CLIs without a system prompt flag, kept out of the transcript.

Antigravity and Cursor cannot append to their system prompt from the command
line, so herdr-ai used to send the brief as an opening user prompt. Each now
reads it from a small folder of its own instead:

- Antigravity: `--add-dir FOLDER`, whose `.agents/rules/herdr-brief.md` is an
  always-on rule. agy keeps its full default prompt and adds the rule to its
  `<user_rules>`. agy runs commands in the first workspace in sorted order, so
  the folder lives in the system temporary directory and is used only when it
  sorts after the working directory. Otherwise the brief stays an opening
  prompt. With System Prompts Off, agy_prompts adds a profile to the same
  folder, since its excludeDefaultComponents profile drops every rule.
- Cursor: the hidden `--plugin-dir FOLDER`. The plugin's sessionStart hook
  returns the brief as `additional_context`, which Cursor places in the hidden
  context message before the first turn. Plugin rules are not sent by
  cursor-agent, so a hook carries it.

Folders are named by a hash of their brief, so relaunching reuses one and a
refreshed pane, which keeps its launch flags, still finds it. A copy of every
brief stays in the state directory, so restore() can rebuild a temporary folder
the system cleared. Folders unused for 30 days are removed.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from platform_runtime import state_dir


PROVIDERS = {"Antigravity": ("agy", "--add-dir"), "Cursor": ("cursor", "--plugin-dir")}
MARKER = ".agent-picker-brief"
RULE_LIMIT = 24000   # agy cuts rules over 24 KB
KEEP = 30 * 86400
WORKSPACE_NOTE = ("This instructions folder is not part of your task. Do your work in the other "
                  "workspace, the directory this session started in.")
HOOK = """import json, os, sys
sys.stdin.read()  # Cursor's session payload; not needed or kept
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(root, "brief.txt"), encoding="utf-8") as stream:
    print(json.dumps({"additional_context": stream.read().strip()}))
"""


def _state_root():
    return state_dir() / "briefs"


def _temp_root():
    return Path(tempfile.gettempdir()) / "agent-picker-briefs"


def _root(name):
    # Cursor resolves plugin folders at every start, so its folder must persist.
    return _temp_root() if name == "Antigravity" else _state_root()


def _key(name, brief):
    return PROVIDERS[name][0] + "-" + hashlib.sha256(brief.encode("utf-8")).hexdigest()[:16]


def _sorts_after(folder, cwd):
    """Whether folder comes after cwd in agy's workspace order, raw and resolved."""
    return (str(cwd) < str(folder)
            and os.path.realpath(str(cwd)) < os.path.realpath(str(folder)))


def _hook_command(script):
    if '"' in sys.executable or '"' in str(script):
        return None
    return f'"{sys.executable}" -I "{script}"'


def supported(name, cwd=None, brief=None):
    """Whether name can take a brief out of sight from cwd (default: here)."""
    if name not in PROVIDERS:
        return False
    if brief is not None and not brief.strip():
        return False
    if name == "Antigravity":
        if brief is not None and len(brief.encode("utf-8")) + len(WORKSPACE_NOTE) + 64 > RULE_LIMIT:
            return False
        probe = _temp_root() / (_key(name, brief) if brief is not None else "agy-" + "0" * 16)
        return _sorts_after(probe, cwd or os.getcwd())
    return _hook_command(_state_root() / "cursor" / "hooks" / "session_start.py") is not None


def _files(name, folder, brief):
    if name == "Antigravity":
        rule = "---\ntrigger: always_on\n---\n" + brief.strip() + "\n\n" + WORKSPACE_NOTE + "\n"
        return {".agents/rules/herdr-brief.md": rule}
    script = folder / "hooks" / "session_start.py"
    hooks = {"version": 1, "hooks": {"sessionStart": [{"command": _hook_command(script)}]}}
    manifest = {"name": "agent-picker-brief", "version": "1.0.0",
                "description": "herdr-ai standing instructions for this session"}
    return {".cursor-plugin/plugin.json": json.dumps(manifest, indent=2) + "\n",
            "hooks/hooks.json": json.dumps(hooks, indent=2) + "\n",
            "hooks/session_start.py": HOOK, "brief.txt": brief.strip() + "\n"}


def _prune(root, keep):
    now = time.time()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == keep or entry.name.split(".")[0] == keep:
            continue
        try:
            if now - entry.stat().st_mtime > KEEP:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        except OSError:
            pass


def _touch(path):
    try:
        os.utime(path)
    except OSError:
        pass


def _build(name, key, brief):
    """The brief's folder, written once and atomically; reused while its brief matches."""
    root = _root(name)
    folder = root / key
    if (folder / MARKER).is_file() and (folder / MARKER).read_text(encoding="utf-8") == brief:
        _touch(folder)
        return folder
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="." + key + ".", dir=root))
    try:
        for relative, text in _files(name, folder, brief).items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (staging / MARKER).write_text(brief, encoding="utf-8")
        try:
            os.rename(staging, folder)
        except OSError:
            # Another pane wrote the same brief first, or an older copy is in the way.
            if not ((folder / MARKER).is_file()
                    and (folder / MARKER).read_text(encoding="utf-8") == brief):
                shutil.rmtree(folder, ignore_errors=True)
                os.rename(staging, folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return folder


def attach(name, brief):
    """The launch flags that give name this brief out of sight."""
    key = _key(name, brief)
    folder = _build(name, key, brief)
    saved = _state_root() / (key + ".txt")
    if not (saved.is_file() and saved.read_text(encoding="utf-8") == brief):
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(brief, encoding="utf-8")
    _touch(saved)
    _prune(_root(name), key)
    if _root(name) != _state_root():
        _prune(_state_root(), key)
    return [PROVIDERS[name][1], str(folder)]


def folder(argv):
    """The brief folder an Antigravity argv adds, if any."""
    root = str(_temp_root())
    for i, word in enumerate(argv[:-1]):
        if word == "--add-dir" and argv[i + 1].startswith(root):
            path = Path(argv[i + 1])
            if path.name.startswith("agy-"):
                return path
    for word in argv:
        if word.startswith("--add-dir=" + root) and Path(word.split("=", 1)[1]).name.startswith("agy-"):
            return Path(word.split("=", 1)[1])
    return None


def restore(name, argv):
    """Rebuild a brief folder a saved argv names, if the system cleared it."""
    if name not in PROVIDERS:
        return
    flag = PROVIDERS[name][1]
    for i, word in enumerate(argv[:-1]):
        if word != flag:
            continue
        path = Path(argv[i + 1])
        if path.parent != _root(name) or not path.name.startswith(PROVIDERS[name][0] + "-"):
            continue
        if (path / MARKER).is_file():
            _touch(path)
            continue
        saved = _state_root() / (path.name + ".txt")
        if saved.is_file():
            _build(name, path.name, saved.read_text(encoding="utf-8"))
