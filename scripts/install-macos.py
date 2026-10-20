#!/usr/bin/env python3
"""Link the command to this Git checkout; back up files before changing them."""

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


def install_herdr_ai(bindir):
    """herdr-ai, agent-route, herdr-reset and StarCLI live in this checkout too; herdr-ai replaces the old standalone bash script."""
    bindir.mkdir(parents=True, exist_ok=True)
    for name, script in (("herdr-ai", "herdr_ai.py"), ("agent-route", "agent_route.py"),
                         ("herdr-reset", "herdr_reset.py"), ("starcli", "starcli.py"),
                         ("schedule", "prompt_scheduler.py"), ("grokbot", "grokbot.py"),
                         ("grok", "grokbot.py")):
        wrapper = bindir / name
        content = "#!/bin/sh\n# agent-picker managed launcher\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(ROOT / "src" / script)) + ' "$@"\n'
        if not wrapper.exists() or wrapper.read_text(errors="ignore") != content:
            backup(wrapper)
            if wrapper.is_symlink():
                wrapper.unlink()
            wrapper.write_text(content)
            wrapper.chmod(0o755)
    if not shutil.which("herdr"):
        print("herdr is not installed; herdr-ai needs it: curl -fsSL https://herdr.dev/install.sh | sh")


def main():
    install_herdr_ai(Path.home() / ".local/bin")
    bindir = Path.home() / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "agent-picker"
    content = "#!/bin/sh\n# agent-picker managed launcher\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(ROOT / "src/agent_picker.py")) + ' "$@"\n'
    if not wrapper.exists() or wrapper.read_text() != content:
        backup(wrapper)
        if wrapper.is_symlink():
            wrapper.unlink()
        wrapper.write_text(content)
        wrapper.chmod(0o755)
    # usage used to have its own launcher; it is now the same command as agent.
    old_usage = bindir / "usage-panel"
    if old_usage.is_file() and "# agent-picker managed launcher" in old_usage.read_text(errors="ignore"):
        backup(old_usage)
        old_usage.unlink()
    alias = shlex.quote(str(wrapper))
    block = (START + "\n" + 'export PATH="$HOME/.local/bin:$PATH"\n'
             + "alias agent=" + alias + "\n" + "alias usage=" + shlex.quote(str(wrapper) + " --picker-usage") + "\n"
             + "alias star=" + shlex.quote(str(bindir / "starcli")) + "\n"
             + "alias schedule=" + shlex.quote(str(bindir / "schedule")) + "\n"
             + "alias grok=" + shlex.quote(str(bindir / "grokbot")) + "\n" + END + "\n")
    for rc in (Path.home() / ".zshrc", Path.home() / ".bashrc"):
        original = rc.read_text() if rc.exists() else ""
        text = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", "", original, flags=re.S)
        text = re.sub(r'^alias agent=[\"\']agent-picker[\"\']\s*\n', "", text, flags=re.M)
        # Migrate the earlier gbot management-CLI alias without touching a custom command.
        old_grok = "alias grok=" + shlex.quote(str(bindir / "gbot"))
        text = "\n".join(line for line in text.split("\n")
                         if line not in (old_grok, "alias grok='" + str(bindir / "gbot") + "'",
                                         'alias grok="' + str(bindir / "gbot") + '"'))
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
    print("Installed. Open a new terminal, then run agent --picker-doctor. usage opens the usage panel; star opens StarCLI; schedule opens the scheduler; grok opens Grok Bot.")
    print(f"Updates are read directly from: {ROOT}")


if __name__ == "__main__":
    main()
