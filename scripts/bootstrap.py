"""Finish a release install: launchers, the bundled Herdr, and what agent opens.

The one-line installers (scripts/install.sh, scripts/install.ps1) download a
release bundle, unpack it, and run this file from inside it. It can be run
again at any time. Three steps:

1. scripts/install.py writes the command launchers (every component unless
   told otherwise; arguments are passed through).
2. `herdr` is made a command for the bundled binary, only when the machine has
   no Herdr of its own. A Herdr that is already installed is left alone, and
   agent then runs the bundled one in a session of its own.
3. A bare `agent` is set to open the Herdr workspace, unless that setting has
   already been chosen.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKER = "agent-picker managed herdr"


def bundled():
    path = ROOT / "bin" / ("herdr.exe" if sys.platform == "win32" else "herdr")
    return path if path.is_file() else None


def launcher_dir(prefix=None):
    if prefix:
        return Path(prefix).expanduser().absolute()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return local / "agent-picker" / "bin"
    return Path.home() / ".local/bin"


def ours(path):
    """Whether path is a herdr command this installer wrote earlier."""
    try:
        if path.is_symlink():
            # A link into an agent-picker folder: <folder>/bin/herdr beside <folder>/src.
            target = Path(os.readlink(path))
            return target.name == "herdr" and (target.parent.parent / "src" / "agent_picker.py").is_file()
        return path.is_file() and MARKER in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def link_herdr(bindir, binary, which=shutil.which):
    """Make `herdr` run binary. Returns what happened: "linked", "kept" when
    another Herdr is already installed, or "none" when no binary is bundled."""
    if binary is None:
        return "none"
    name = "herdr.cmd" if sys.platform == "win32" else "herdr"
    target = bindir / name
    other = which("herdr")
    if other and not ours(Path(other)) and not ours(target):
        return "kept"
    if (target.exists() or target.is_symlink()) and not ours(target):
        return "kept"
    bindir.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if sys.platform == "win32":
        target.write_text('@echo off\r\nrem {}\r\n"{}" %*\r\n'.format(MARKER, binary), encoding="utf-8")
    else:
        target.symlink_to(binary)
    return "linked"


def choose_workspace():
    """Set a bare agent to open the Herdr workspace on a first install. True
    when the setting was written; a choice already saved is kept."""
    sys.path.insert(0, str(ROOT / "src"))
    import preferences
    try:
        import json
        saved = json.loads(preferences.PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict) and "agent.opens" in saved:
        return False
    preferences.put("agent.opens", "workspace")
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    prefix = argv[argv.index("--prefix") + 1] if "--prefix" in argv and argv.index("--prefix") + 1 < len(argv) else None
    if not any(a in ("--components", "--all", "--without", "--yes") or a.startswith("--components=") for a in argv):
        argv.append("--all")
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "install.py"), *argv])
    if done.returncode:
        return done.returncode
    binary = bundled()
    linked = link_herdr(launcher_dir(prefix), binary)
    if linked == "linked":
        print("herdr: this install's Herdr is now the herdr command.")
    elif linked == "kept":
        print("herdr: your existing Herdr was left as it is. agent opens the bundled one in its own session.")
    else:
        print("herdr: no bundled Herdr in this folder; agent uses the herdr on PATH.")
    if binary and choose_workspace():
        print("agent: opens the Herdr workspace. agent --picker-select opens only the selector;")
        print("       Settings, General, agent runs changes it back.")
    version = ROOT / "VERSION"
    if version.is_file():
        print("agent-picker " + version.read_text(encoding="utf-8").strip() + " is installed in " + str(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
