"""Rex's sidebar for agents that do not report to it themselves.

Rex lists a pane's agent on the left once the program in it sends Rex's
Program Status Protocol (OSC 7501, https://www.superlogical.com/rex/docs/build/program-status).
Claude, Codex and Antigravity send it on their own. Cursor and Grok Bot do not,
so their panes stayed off the sidebar:

- Grok Bot sends it from its own state reports (grokbot.Conversation.report).
- Cursor gets it from agent-picker. The picker reports the pane idle before it
  starts cursor-agent and clears the report once it quits. A small plugin, passed
  with cursor-agent's repeatable `--plugin-dir`, turns Cursor's prompt and stop
  hooks into working and idle. Hooks write to the pane's terminal named in
  AGENT_PICKER_TTY, since their own output goes back to cursor-agent.

Outside Rex nothing is sent or installed.
"""

import base64
import json
import os
from pathlib import Path
import re
import sys

from platform_runtime import state_dir


TTY_ENV = "AGENT_PICKER_TTY"
STATES = ("idle", "working", "done", "blocked", "error", "clear")
KINDS = ("permission", "question", "auth")
APP = re.compile(r"[A-Za-z0-9_.+-]{1,32}")
# Agents that never send OSC 7501 and the app id each is reported under.
PICKER_REPORTED = {"Cursor": "cursor-agent"}
# Rex picks a pane's icon from its app id, and has none for Grok Bot, which runs
# on Cursor's backend. Reporting as Cursor gives the pane Cursor's logo.
GROKBOT_APP = "cursor-agent"
CURSOR_HOOK = """import base64, os, sys
# Cursor hook: report this pane's state to Rex. Cursor's payload is not needed.
sys.stdin.read()
state = sys.argv[1] if len(sys.argv) > 1 else "idle"
path = os.environ.get("%(env)s") or ("CONOUT$" if os.name == "nt" else "/dev/tty")
try:
    with open(path, "w", encoding="utf-8") as tty:
        tty.write("\\x1b]7501;state=" + state + ":app=%(app)s\\x1b\\\\")
except OSError:
    pass
print('{"continue": true}' if state == "working" else "{}")
""" % {"env": TTY_ENV, "app": PICKER_REPORTED["Cursor"]}


def inside_rex(env=None):
    env = os.environ if env is None else env
    return bool(env.get("REX_BLOCK") or env.get("REX_SESSION")) or env.get("TERM_PROGRAM") == "rex"


def sequence(state, app=None, msg=None, kind=None):
    """One OSC 7501 report, or None for a state or app the protocol would drop."""
    if state not in STATES or (app is not None and not APP.fullmatch(app)):
        return None
    pairs = ["state=" + state]
    if kind in KINDS and state == "blocked":
        pairs.append("kind=" + kind)
    if app and state != "clear":
        pairs.append("app=" + app)
    if msg and state != "clear":
        text = "".join(ch for ch in msg if ch >= " " and not "\x7f" <= ch <= "\x9f")[:2048]
        pairs.append("msg=" + base64.b64encode(text.encode("utf-8")).decode("ascii"))
    return "\x1b]7501;" + ":".join(pairs) + "\x1b\\"


def report(state, app=None, msg=None, kind=None, stream=None):
    """Tell Rex this pane's state. Only for a terminal inside Rex; errors are ignored."""
    stream = sys.stdout if stream is None else stream
    text = sequence(state, app, msg, kind)
    if text is None or not inside_rex():
        return False
    try:
        if not stream.isatty():
            return False
        stream.flush()
        # One write, so a report from a worker thread cannot split the UI's own output.
        os.write(stream.fileno(), text.encode("utf-8"))
        return True
    except (OSError, ValueError, AttributeError):
        return False


def terminal_path(stream=None):
    stream = sys.stdout if stream is None else stream
    if sys.platform == "win32":
        return "CONOUT$"
    try:
        return os.ttyname(stream.fileno())
    except (OSError, ValueError, AttributeError):
        return None


def _hook_command(script):
    if '"' in sys.executable or '"' in str(script):
        return None
    return f'"{sys.executable}" -I "{script}"'


def cursor_plugin(root=None):
    """The plugin folder whose hooks report Cursor's turns, written when it changed."""
    folder = (state_dir() if root is None else Path(root)) / "rex-status" / "cursor"
    script = folder / "hooks" / "rex_status.py"
    command = _hook_command(script)
    if command is None:
        return None
    hooks = {"version": 1, "hooks": {
        "sessionStart": [{"command": command + " idle"}],
        "beforeSubmitPrompt": [{"command": command + " working"}],
        "stop": [{"command": command + " idle"}],
    }}
    manifest = {"name": "agent-picker-rex-status", "version": "1.0.0",
                "description": "Reports Cursor's turns to Rex's sidebar"}
    files = {".cursor-plugin/plugin.json": json.dumps(manifest, indent=2) + "\n",
             "hooks/hooks.json": json.dumps(hooks, indent=2) + "\n",
             "hooks/rex_status.py": CURSOR_HOOK}
    try:
        for relative, text in files.items():
            path = folder / relative
            if path.is_file() and path.read_text(encoding="utf-8") == text:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_name(path.name + f".{os.getpid()}.tmp")
            staging.write_text(text, encoding="utf-8")
            os.replace(staging, path)
    except OSError:
        return None
    return folder


def prepare(agent, argv, env=None):
    """argv for agent, with what lets Rex list it. Sets AGENT_PICKER_TTY in env.
    Returns (argv, app): app is what the picker reports for it, or None."""
    env = os.environ if env is None else env
    app = PICKER_REPORTED.get(agent.get("name"))
    if app is None or not inside_rex(env):
        return argv, None
    path = terminal_path()
    if path:
        env[TTY_ENV] = path
    if agent.get("name") == "Cursor" and path:
        folder = cursor_plugin()
        if folder is not None:
            argv = argv + ["--plugin-dir", str(folder)]
    return argv, app
