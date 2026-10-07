#!/usr/bin/env python3
"""herdr-ai: open a new herdr tab laid out as a multi-agent workbench, with
every pane rooted in the directory you ran this from.

  +--------------+--------------+--------------+
  | Antigravity  | Codex        |              |
  |              |              | Claude       |
  +--------------+--------------+              |
  | agent        | Cursor       |              |
  | selector     |              +--------------+
  +--------------+--------------+ usage        |
  | terminal                    |              |
  +-----------------------------+--------------+

Agents launch through this checkout's picker (--picker-launch), so models,
effort and permission flags follow your picker defaults.

Usage: herdr-ai [label]       (label defaults to the directory name)
Env:   HERDR_AI_DRY=1         build the layout with plain shells only
       HERDR_BIN_PATH         herdr executable to use
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = os.name == "nt"
ENTRY = Path(__file__).resolve().parent / "agent_picker.py"

# pane -> picker arguments; panes not listed stay plain shells.
PANES = {
    "agy": ["--picker-launch", "Antigravity"],
    "codex": ["--picker-launch", "Codex"],
    "claude": ["--picker-launch", "Claude"],
    "cursor": ["--picker-launch", "Cursor"],
    "picker": [],
    "usage": ["--picker-usage"],
}


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


def main(argv=None, herdr=None, cwd=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip())
        return 0
    herdr = herdr or find_herdr()
    if not herdr:
        install = ('powershell -ExecutionPolicy Bypass -c "irm https://herdr.dev/install.ps1 | iex"'
                   if WINDOWS else "curl -fsSL https://herdr.dev/install.sh | sh")
        sys.exit("herdr-ai: herdr not found. Install it with:\n  " + install)
    cwd = cwd or os.getcwd()
    label = argv[0] if argv else (Path(cwd).name or "root")

    def call(*args):
        out = subprocess.run([herdr, *args], capture_output=True, text=True, encoding="utf-8")
        if out.returncode:
            sys.exit(f"herdr-ai: herdr {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()}")
        return out.stdout

    def split(pane, direction, ratio, focus=False):
        out = call("pane", "split", pane, "--direction", direction, "--ratio", str(ratio),
                   "--cwd", cwd, "--focus" if focus else "--no-focus")
        return json.loads(out)["result"]["pane"]["pane_id"]

    # Inside herdr: new tab in the current workspace. Outside: new workspace, then attach.
    inside = os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_WORKSPACE_ID")
    if inside:
        out = call("tab", "create", "--workspace", os.environ["HERDR_WORKSPACE_ID"],
                   "--cwd", cwd, "--label", label, "--focus")
    else:
        out = call("workspace", "create", "--cwd", cwd, "--label", label, "--focus")
    panes = {"agy": json.loads(out)["result"]["root_pane"]["pane_id"]}

    panes["claude"] = split(panes["agy"], "right", 0.72)    # right column: Claude over usage
    panes["usage"] = split(panes["claude"], "down", 0.59)
    split(panes["agy"], "down", 0.80, focus=True)           # terminal across the bottom of the left 2/3
    panes["codex"] = split(panes["agy"], "right", 0.49)
    panes["picker"] = split(panes["agy"], "down", 0.54)
    panes["cursor"] = split(panes["codex"], "down", 0.54)

    if os.environ.get("HERDR_AI_DRY", "0") != "1":
        for name, args in PANES.items():
            call("pane", "run", panes[name], picker_command(args))

    if inside:
        return 0
    if WINDOWS:   # exec on Windows detaches from the console
        return subprocess.call([herdr])
    os.execvp(herdr, [herdr])


if __name__ == "__main__":
    sys.exit(main())
