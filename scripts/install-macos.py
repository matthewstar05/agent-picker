#!/usr/bin/env python3
"""Link the command to this Git checkout; back up files before changing them."""

import os
import re
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from platform_runtime import state_dir

START = "# >>> agent-picker managed >>>"
END = "# <<< agent-picker managed <<<"


def backup(path):
    if path.exists() or path.is_symlink():
        target = state_dir() / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / path.name)
        print(f"Backup: {target / path.name}")


def main():
    if os.name == "nt":
        sys.exit("Use scripts/install-windows.ps1 on Windows.")
    bindir = Path.home() / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    wrappers = {}
    for name, entry in (("agent-picker", "agent_picker.py"), ("usage-panel", "usage_panel.py")):
        wrapper = bindir / name
        content = "#!/bin/sh\n# agent-picker managed launcher\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(ROOT / "src" / entry)) + ' "$@"\n'
        if not wrapper.exists() or wrapper.read_text() != content:
            backup(wrapper)
            if wrapper.is_symlink():
                wrapper.unlink()
            wrapper.write_text(content)
            wrapper.chmod(0o755)
        wrappers[name] = wrapper
    block = (START + "\n" + 'export PATH="$HOME/.local/bin:$PATH"\n'
             + "alias agent=" + shlex.quote(str(wrappers["agent-picker"])) + "\n"
             + "alias usage=" + shlex.quote(str(wrappers["usage-panel"])) + "\n" + END + "\n")
    for rc in (Path.home() / ".zshrc", Path.home() / ".bashrc"):
        original = rc.read_text() if rc.exists() else ""
        text = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", "", original, flags=re.S)
        text = re.sub(r'^alias agent=[\"\']agent-picker[\"\']\s*\n', "", text, flags=re.M)
        # Keep Kiro's post integration at the bottom of the profile.
        marker = "# Kiro CLI post block. Keep at the bottom of this file."
        index = text.find(marker)
        if index >= 0:
            text = text[:index].rstrip() + "\n\n" + block + "\n" + text[index:]
        else:
            text = text.rstrip() + "\n\n" + block
        if text != original:
            backup(rc)
            rc.write_text(text)
    print("Installed. Open a new terminal, then run agent --picker-doctor, or usage for the limits panel.")
    print(f"Updates are read directly from: {ROOT}")


if __name__ == "__main__":
    main()
