#!/usr/bin/env python3
"""herdr-ai: lay out the current herdr tab as a multi-agent workbench, with
every pane rooted in the directory you ran this from.

  +--------------+--------------+--------------+
  | Antigravity  | Codex        |              |
  |              |              | Claude       |
  +--------------+--------------+              |
  | agent        | Cursor/Kiro  |              |
  | selector     |              +--------------+
  +--------------+--------------+ usage        |
  | terminal                    |              |
  +-----------------------------+--------------+

The pane you run it in becomes the terminal. Run it again in a tab it already
laid out and it reroutes instead: the agents and the selector restart in the
directory you ran it from, with no new panes or tabs. Outside herdr it opens a
new workspace and attaches.

agent-route reroutes every agent on the current tab, including ones herdr-ai
did not start, to the directory you run it from.

herdr-reset closes every other pane on the current tab, leaving the terminal you
run it from, with its history, so herdr-ai can lay the tab out again.

Agents launch through this checkout's picker (--picker-launch), so models,
effort and permission flags follow your picker defaults.
The middle-bottom pane chooses Cursor or Kiro by remaining quota and reset date.

Usage: herdr-ai [label]       (label defaults to the directory name)
       agent-route
       herdr-reset
Env:   HERDR_AI_DRY=1         build the layout with plain shells only
       HERDR_BIN_PATH         herdr executable to use
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from platform_runtime import state_dir

WINDOWS = os.name == "nt"
ENTRY = Path(__file__).resolve().parent / "agent_picker.py"

# pane -> picker arguments; panes not listed stay plain shells.
PANES = {
    "agy": ["--picker-launch", "Antigravity"],
    "codex": ["--picker-launch", "Codex"],
    "claude": ["--picker-launch", "Claude"],
    "cursor": ["--picker-launch-priority"],  # keep the role for saved-layout compatibility
    "picker": [],
    "usage": ["--picker-usage"],
}
# The usage panel does not depend on the directory, so rerouting leaves it running.
ROUTED = ("agy", "codex", "claude", "cursor", "picker")


def find_herdr():
    found = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not found and WINDOWS:
        # herdr's Windows installer puts its alias here; a terminal opened
        # before the install will not have it on PATH yet.
        alias = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Herdr/bin/herdr.exe"
        found = str(alias) if alias.is_file() else None
    return found


def picker_command(args):
    """The line typed into a pane's shell to run the picker with args."""
    words = [sys.executable, str(ENTRY), *args]
    if not WINDOWS:
        return shlex.join(words)
    # A Windows pane may run PowerShell or cmd, which quote differently. Bare
    # paths work in both; otherwise use the installer's agent-picker.cmd shim.
    if any(c in w for w in words for c in ' "&^%()\'`'):
        return " ".join(["agent-picker.cmd", *args])
    return " ".join(words)


def cd_command(path):
    # Double quotes work in PowerShell and cmd; /d is not needed for cmd within one drive.
    return f'cd "{path}"' if WINDOWS else "cd " + shlex.quote(path)


def state_file():
    return state_dir() / "herdr-ai-tabs.json"


def load_state():
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def tab_key(tab):
    # Pane and tab ids are per server, so key them by the session socket too.
    return (os.environ.get("HERDR_SOCKET_PATH") or "default") + "|" + tab


class Herdr:
    def __init__(self, binary, name):
        self.binary, self.name = binary, name

    def __call__(self, *args):
        out = subprocess.run([self.binary, *args], capture_output=True, text=True, encoding="utf-8")
        if out.returncode:
            sys.exit(f"{self.name}: herdr {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()}")
        return json.loads(out.stdout or "{}").get("result", {})

    def tab_panes(self, workspace, tab):
        panes = self("pane", "list", "--workspace", workspace)["panes"]
        return {p["pane_id"]: p for p in panes if p["tab_id"] == tab}

    def process_info(self, pane):
        return self("pane", "process-info", "--pane", pane).get("process_info", {})


def busy(info):
    """Foreground process group of a pane running something besides its shell."""
    pgid, shell = info.get("foreground_process_group_id"), info.get("shell_pid")
    return pgid if pgid and shell and pgid != shell else None


def relaunch_line(info):
    """The command line of the pane's foreground job, to start it again."""
    pgid = busy(info)
    leader = next((p for p in info.get("foreground_processes", []) if p.get("pid") == pgid), None)
    if not leader:
        return None
    if leader.get("argv"):
        return shlex.join(leader["argv"]) if not WINDOWS else " ".join(leader["argv"])
    return leader.get("cmdline")


def stop_panes(herdr, panes):
    """Stop whatever runs in each pane and wait for its shell prompt."""
    if WINDOWS:   # no process groups; ask politely the way a user would
        for pane in panes:
            herdr("pane", "send-keys", pane, "ctrl+c", "ctrl+c")
        time.sleep(1.5)
        return
    for sig, wait in ((signal.SIGTERM, 4.0), (signal.SIGKILL, 2.0)):
        pending = {}
        for pane in panes:
            pgid = busy(herdr.process_info(pane))
            if pgid and pgid != os.getpgrp():
                pending[pane] = pgid
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + wait
        while pending and time.monotonic() < deadline:
            time.sleep(0.15)
            pending = {p: g for p, g in pending.items() if busy(herdr.process_info(p))}
        if not pending:
            return


def route(herdr, cwd, recorded, everything):
    """Restart agents on the caller's tab in cwd. recorded maps herdr-ai roles to
    pane ids; everything also takes any other pane herdr sees an agent in."""
    me = os.environ.get("HERDR_PANE_ID")
    live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
    by_pane = {pane: role for role, pane in recorded.items()}
    plan = {}
    for pane, info in live.items():
        if pane == me:
            continue
        role = by_pane.get(pane)
        if role in PANES and role not in ROUTED:
            continue
        agent = info.get("agent")
        if role in ROUTED:
            # A selector that already launched an agent keeps that agent.
            line = (relaunch_line(herdr.process_info(pane)) if role == "picker" and agent else None)
            plan[pane] = (role, line or picker_command(PANES[role]))
        elif everything and agent:
            line = relaunch_line(herdr.process_info(pane))
            if line:
                plan[pane] = (agent, line)
    if not plan:
        return []
    stop_panes(herdr, list(plan))
    if os.environ.get("HERDR_AI_DRY", "0") == "1":
        plan = {pane: (name, None) for pane, (name, _) in plan.items()}
    for pane, (_, line) in plan.items():
        herdr("pane", "run", pane, cd_command(cwd))
        if line:
            herdr("pane", "run", pane, line)
    return [name for name, _ in plan.values()]


def build(herdr, root, cwd):
    """Split root into the workbench. root ends up as the terminal along the bottom."""
    def split(pane, direction, ratio):
        return herdr("pane", "split", pane, "--direction", direction, "--ratio", str(ratio),
                     "--cwd", cwd, "--no-focus")["pane"]["pane_id"]

    panes = {"terminal": root}
    panes["claude"] = split(root, "right", 0.72)            # right column: Claude over usage
    panes["usage"] = split(panes["claude"], "down", 0.54)
    panes["agy"] = split(root, "down", 0.80)                # terminal across the bottom of the left 2/3
    # Swap so the caller's shell, which keeps focus, becomes the terminal.
    herdr("pane", "swap", "--source-pane", root, "--target-pane", panes["agy"])
    panes["codex"] = split(panes["agy"], "right", 0.49)
    panes["picker"] = split(panes["agy"], "down", 0.54)
    panes["cursor"] = split(panes["codex"], "down", 0.54)

    if os.environ.get("HERDR_AI_DRY", "0") != "1":
        for name, args in PANES.items():
            herdr("pane", "run", panes[name], picker_command(args))
    return panes


def require_herdr(name):
    found = find_herdr()
    if not found:
        install = ('powershell -ExecutionPolicy Bypass -c "irm https://herdr.dev/install.ps1 | iex"'
                   if WINDOWS else "curl -fsSL https://herdr.dev/install.sh | sh")
        sys.exit(f"{name}: herdr not found. Install it with:\n  " + install)
    return found


def inside_herdr():
    return (os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_WORKSPACE_ID")
            and os.environ.get("HERDR_TAB_ID") and os.environ.get("HERDR_PANE_ID"))


def recorded_panes(herdr, state, key):
    """The tab's herdr-ai panes that still exist, or {} if its layout is gone."""
    entry = state.get(key)
    if not entry:
        return {}
    live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
    panes = {role: pane for role, pane in entry["panes"].items()
             if pane in live and live[pane].get("terminal_id") == entry["terminals"].get(pane)}
    return panes if any(role in ROUTED for role in panes) else {}


def report(name, routed, cwd):
    if routed:
        print(f"{name}: restarted {', '.join(routed)} in {cwd}")
    else:
        print(f"{name}: no agents to route on this tab")


def main(argv=None, herdr=None, cwd=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip())
        return 0
    herdr = Herdr(herdr or require_herdr("herdr-ai"), "herdr-ai")
    cwd = cwd or os.getcwd()
    auto = not argv
    label = argv[0] if argv else (Path(cwd).name or "home")

    if not inside_herdr():
        out = herdr("workspace", "create", "--cwd", cwd, "--label", label, "--focus")
        build(herdr, out["root_pane"]["pane_id"], cwd)
        if WINDOWS:   # exec on Windows detaches from the console
            return subprocess.call([herdr.binary])
        return os.execvp(herdr.binary, [herdr.binary])

    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    recorded = recorded_panes(herdr, state, key)
    if recorded:
        report("herdr-ai", route(herdr, cwd, recorded, everything=False), cwd)
        entry = state[key]
        entry["auto_label"] = entry.get("auto_label", True) and auto
    else:
        panes = build(herdr, os.environ["HERDR_PANE_ID"], cwd)
        live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
        entry = state[key] = {"panes": panes, "auto_label": auto,
                              "terminals": {p: live.get(p, {}).get("terminal_id") for p in panes.values()}}
    # An explicit label sticks; otherwise the tab follows the directory.
    if not auto or entry["auto_label"]:
        herdr("tab", "rename", os.environ["HERDR_TAB_ID"], label)
    save_state(state)
    return 0


def route_main(argv, herdr=None, cwd=None):
    """agent-route: restart every agent on this tab in the current directory."""
    if argv[:1] in (["-h"], ["--help"]):
        print("Usage: agent-route\n\nRestart every agent on the current herdr tab in the current directory.\n"
              "Run it from a shell pane on that tab; the pane you run it from is left alone.")
        return 0
    if argv:
        sys.exit("agent-route: takes no arguments; cd to the target directory first")
    if not inside_herdr():
        sys.exit("agent-route: run this from a pane inside herdr")
    herdr = Herdr(herdr or require_herdr("agent-route"), "agent-route")
    cwd = cwd or os.getcwd()
    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    report("agent-route", route(herdr, cwd, recorded_panes(herdr, state, key), everything=True), cwd)
    entry = state.get(key)
    if entry and entry.get("auto_label", True):
        herdr("tab", "rename", os.environ["HERDR_TAB_ID"], Path(cwd).name or "home")
    return 0


def reset_main(argv, herdr=None, cwd=None):
    """herdr-reset: close every other pane on this tab, leaving the caller's terminal."""
    if argv[:1] in (["-h"], ["--help"]):
        print("Usage: herdr-reset\n\nClose every other pane on the current herdr tab, leaving only the pane\n"
              "you run it from, with its history and directory, as a fresh herdr-ai starting point.")
        return 0
    if argv:
        sys.exit("herdr-reset: takes no arguments")
    if not inside_herdr():
        sys.exit("herdr-reset: run this from a pane inside herdr")
    herdr = Herdr(herdr or require_herdr("herdr-reset"), "herdr-reset")
    cwd = cwd or os.getcwd()
    me = os.environ["HERDR_PANE_ID"]
    others = [p for p in herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"]) if p != me]
    for pane in others:
        herdr("pane", "close", pane)
    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    entry = state.pop(key, None)
    if entry is not None:
        save_state(state)
    if entry is None or entry.get("auto_label", True):
        herdr("tab", "rename", os.environ["HERDR_TAB_ID"], Path(cwd).name or "home")
    print(f"herdr-reset: closed {len(others)} pane{'s' * (len(others) != 1)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
