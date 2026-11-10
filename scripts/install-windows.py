#!/usr/bin/env python3
"""Install selected per-user Windows launchers without changing vendor CLIs."""
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_common

MANAGED_MARKER = "rem agent-picker managed launcher"


def backup(path, backups):
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / path.name
    # Keep each pre-change copy, including repeat installs within one timestamp.
    if target.exists():
        target = backups / (path.stem + "-" + datetime.now().strftime("%H%M%S%f") + path.suffix)
    shutil.copy2(path, target)


def write_launcher(path, content, backups):
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == content:
        return
    if path.exists() or path.is_symlink():
        backup(path, backups)
        if path.is_symlink():
            path.unlink()
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(content)


def launcher_content(python, script, extra):
    # Double quoted constants protect spaces; doubling percent protects literal
    # percent signs from cmd expansion. User arguments remain forwarded via %*.
    constants = [str(python), str(ROOT / "src" / script), *extra]
    command = " ".join('"' + word.replace("%", "%%").replace('"', '\\"') + '"'
                       for word in constants)
    return ("@echo off\n" + MANAGED_MARKER + "\nchcp 65001 >nul\n" +
            command + " %*\nexit /b %errorlevel%\n")


def normalized_path(value):
    return os.path.expandvars(value.strip().strip('"')).rstrip("\\/").casefold()


def prepend_path(current, bindir):
    wanted = normalized_path(str(bindir))
    # Keep all unrelated entries in order and collapse only duplicate copies of
    # our target directory, so the selected launchers win in this process too.
    rest = [part for part in current.split(";")
            if part.strip() and normalized_path(part) != wanted]
    return ";".join([str(bindir), *rest])


def update_user_path(bindir):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        updated = prepend_path(current, bindir)
        if updated != current:
            winreg.SetValueEx(key, "Path", 0, kind, updated)
    os.environ["PATH"] = prepend_path(os.environ.get("PATH", ""), bindir)
    # Notify Explorer so newly opened terminals inherit the updated user PATH.
    import ctypes
    from ctypes import wintypes
    notify = ctypes.WinDLL("user32").SendMessageTimeoutW
    notify.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    result = ctypes.c_size_t()
    notify(0xffff, 0x1a, 0, "Environment", 2, 5000, ctypes.byref(result))


def remove_deselected(bindir, selected, backups):
    if not bindir.exists():
        return
    for path in bindir.glob("*.cmd"):
        name = path.stem.casefold()
        if name in selected or name not in {command.casefold() for command in install_common.COMMANDS}:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        if MANAGED_MARKER not in lines:
            continue
        script, _ = install_common.COMMANDS[name]
        target = '"' + str(ROOT / "src" / script).replace("%", "%%") + '"'
        if target.casefold() in content.casefold():
            backup(path, backups)
            path.unlink()


def main(argv=None):
    args = install_common.parse_options(argv)
    if sys.platform != "win32":
        raise SystemExit("Run this installer on Windows. On macOS use scripts/install-macos.py.")
    if sys.version_info < (3, 9):
        raise SystemExit("Python 3.9 or later is required. Install Python from python.org, then retry.")
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "agent-picker"
    bindir = (args.prefix or local / "bin").resolve()
    environment = ROOT / ".venv-windows"
    python = environment / "Scripts" / "python.exe"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    # Windows has no system IANA zone database. Keep runtime dependencies isolated.
    subprocess.run([str(python), "-m", "pip", "install", "tzdata>=2024.1", "tzlocal>=5,<6"], check=True)
    bindir.mkdir(parents=True, exist_ok=True)
    backups = local / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    commands = install_common.selected_commands(args.components)
    selected = {name.casefold() for name in commands}
    remove_deselected(bindir, selected, backups)
    for name, (script, extra) in commands.items():
        write_launcher(bindir / (name + ".cmd"), launcher_content(python, script, extra), backups)
    if not args.no_path:
        update_user_path(bindir)
        print("Added the launcher directory to the front of PATH for this process and your user account.")
        print("Open a NEW terminal for the user PATH change, then run agent --picker-doctor.")
        print("For this PowerShell window: $env:Path = '" + str(bindir).replace("'", "''") + ";' + $env:Path")
        print("For this CMD window: set \"PATH=" + str(bindir).replace('"', '') + ";%PATH%\"")
    else:
        print("PATH was not changed. Add the launcher directory to PATH before using its commands.")
    print(f"Installed launchers in {bindir}")
    print("Commands: " + ", ".join(commands))
    print(f"Keep this folder in place: {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
