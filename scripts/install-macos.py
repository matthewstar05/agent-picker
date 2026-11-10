#!/usr/bin/env python3
"""Install selected per-user commands, with backups and shell integration."""

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
from install_common import COMMANDS, MANAGED_MARKER, parse_options, selected_commands

START = "# >>> agent-picker managed >>>"
END = "# <<< agent-picker managed <<<"


def backup(path):
    if path.exists() or path.is_symlink():
        target = state_dir() / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / path.name, follow_symlinks=False)
        print(f"Backup: {target / path.name}")


def owned(path):
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return MANAGED_MARKER in text.splitlines() and any(
        shlex.quote(str(ROOT / "src" / script)) in text for script, _ in COMMANDS.values())


def install_commands(bindir, commands):
    bindir.mkdir(parents=True, exist_ok=True)
    for name, (script, extra) in commands.items():
        wrapper = bindir / name
        argv = [sys.executable, str(ROOT / "src" / script), *extra]
        content = "#!/bin/sh\n" + MANAGED_MARKER + "\nexec " + shlex.join(argv) + ' "$@"\n'
        if not wrapper.is_file() or wrapper.read_text(encoding="utf-8", errors="ignore") != content:
            backup(wrapper)
            if wrapper.is_symlink():
                wrapper.unlink()
            wrapper.write_text(content, encoding="utf-8")
        wrapper.chmod(0o755)
    for name in set(COMMANDS) - set(commands):
        wrapper = bindir / name
        if owned(wrapper):
            backup(wrapper)
            wrapper.unlink()
    old_usage = bindir / "usage-panel"
    if owned(old_usage):
        backup(old_usage)
        old_usage.unlink()


def profile_block(bindir, commands):
    lines = [START, "export PATH=" + shlex.quote(str(bindir)) + ':"$PATH"']
    # Retain aliases for shells upgrading from earlier installations. Real
    # launchers also make these names available to subprocesses and other shells.
    aliases = {"agent": [str(bindir / "agent-picker")],
               "usage": [str(bindir / "agent-picker"), "--picker-usage"],
               "star": [str(bindir / "starcli")],
               "schedule": [str(bindir / "schedule")],
               "grok": [str(bindir / "grokbot")],
               "fs": [str(bindir / "herdr-fs")]}
    lines.extend("alias " + name + "=" + shlex.quote(shlex.join(argv))
                 for name, argv in aliases.items() if name in commands)
    return "\n".join(lines + [END, ""])


def install_profiles(bindir, commands):
    block = profile_block(bindir, commands)
    home = Path.home()
    profiles = [home / ".zshrc", home / ".bashrc", home / ".profile"]
    # Bash reads only its first existing login profile. Creating .bash_profile
    # would silently suppress an existing .bash_login or .profile.
    login = next((home / name for name in (".bash_profile", ".bash_login")
                  if (home / name).exists()), None)
    if login is not None:
        profiles.append(login)
    for rc in profiles:
        original = rc.read_text(encoding="utf-8") if rc.exists() else ""
        text = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", "", original, flags=re.S)
        text = re.sub(r'^alias agent=[\"\']agent-picker[\"\']\s*\n', "", text, flags=re.M)
        if "grok" in commands:
            old_grok = "alias grok=" + shlex.quote(str(bindir / "gbot"))
            text = "\n".join(line for line in text.split("\n")
                             if line not in (old_grok, "alias grok='" + str(bindir / "gbot") + "'",
                                             'alias grok="' + str(bindir / "gbot") + '"'))
        marker = "# Kiro CLI post block. Keep at the bottom of this file."
        index = text.find(marker)
        if index >= 0:
            text = text[:index].rstrip() + "\n\n" + block + "\n" + text[index:]
        else:
            text = text.rstrip() + "\n\n" + block
        if text != original:
            backup(rc)
            rc.write_text(text, encoding="utf-8")


def main(argv=None):
    args = parse_options(argv)
    bindir = (args.prefix or Path.home() / ".local/bin").expanduser().absolute()
    commands = selected_commands(args.components)
    install_commands(bindir, commands)
    if not args.no_path:
        install_profiles(bindir, commands)
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
    print("Installed commands: " + ", ".join(commands))
    print(f"Launchers: {bindir}")
    if args.no_path:
        print("PATH unchanged. To use commands in this shell, run:")
        print("  export PATH=" + shlex.quote(str(bindir)) + ':"$PATH"')
    else:
        print("Open a new terminal, or activate commands in this shell now:")
        print("  . " + shlex.quote(str(Path.home() / ".zshrc" if os.environ.get("SHELL", "").endswith("zsh")
                                     else Path.home() / ".bashrc")))
    print("Then run agent --picker-doctor; agent --picker-connect opens provider setup.")
    if "herdr-ai" in commands and not shutil.which("herdr"):
        print("Herdr commands require the separately installed Herdr terminal.")
    print(f"Keep this checkout in place: {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
