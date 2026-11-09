#!/usr/bin/env python3
"""Install per-user Windows launchers without changing vendor CLIs or accounts."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def write_launcher(path, content, backups):
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    if path.exists():
        import shutil
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups / path.name)
    path.write_text(content, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, help="launcher directory (default: local app data/agent-picker/bin)")
    parser.add_argument("--no-path", action="store_true", help="do not add the directory to your user PATH")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Run this installer on Windows. On macOS use scripts/install-macos.py.")
    if sys.version_info < (3, 9):
        parser.error("Python 3.9 or later is required. Install Python from python.org, then retry.")
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "agent-picker"
    bindir = (args.prefix or local / "bin").resolve()
    environment = ROOT / ".venv-windows"
    python = environment / "Scripts/python.exe"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    # Windows has no system IANA zone database. Isolate these small runtime
    # dependencies from the user's Python installation.
    subprocess.run([str(python), "-m", "pip", "install", "tzdata>=2024.1", "tzlocal>=5,<6"], check=True)
    bindir.mkdir(parents=True, exist_ok=True)
    backups = local / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    commands = {
        "agent-picker": ("agent_picker.py", []), "agent": ("agent_picker.py", []),
        "usage": ("agent_picker.py", ["--picker-usage"]), "schedule": ("prompt_scheduler.py", []),
        "herdr-ai": ("herdr_ai.py", []), "agent-route": ("agent_route.py", []),
        "herdr-reset": ("herdr_reset.py", []), "starcli": ("starcli.py", []),
        "star": ("starcli.py", []), "grokbot": ("grokbot.py", []), "grok": ("grokbot.py", []),
        "fs": ("fs_browser.py", []), "herdr-fs": ("fs_browser.py", []),
    }
    for name, (script, extra) in commands.items():
        # Quote every path, including paths without spaces. Batch constants may
        # contain percent signs, while user arguments must pass through intact.
        constants = [str(python), str(ROOT / "src" / script), *extra]
        command = " ".join('"' + word.replace('%', '%%') + '"' for word in constants)
        content = '@echo off\nchcp 65001 >nul\n' + command + ' %*\nexit /b %errorlevel%\n'
        write_launcher(bindir / (name + ".cmd"), content, backups)
    if not args.no_path:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            parts = current.split(";")
            if not any(os.path.expandvars(part).rstrip("\\/").casefold() == str(bindir).casefold() for part in parts):
                winreg.SetValueEx(key, "Path", 0, kind, str(bindir) + (";" + current if current else ""))
        # Notify Explorer so a newly opened terminal inherits the user PATH.
        import ctypes
        from ctypes import wintypes
        notify = ctypes.WinDLL("user32").SendMessageTimeoutW
        notify.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        result = ctypes.c_size_t()
        notify(0xffff, 0x1a, 0, "Environment", 2, 5000, ctypes.byref(result))
    print(f"Installed launchers in {bindir}")
    print("Open a NEW terminal, then run agent --picker-connect. Press c in the picker to return to Connections.")
    print(f"Keep this folder in place: {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
