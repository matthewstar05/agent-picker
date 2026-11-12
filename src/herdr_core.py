"""The Herdr this checkout ships: where its binary is, and how `agent` runs it.

The Herdr source lives in herdr/ and is built into bin/herdr (bin\\herdr.exe on
Windows) by a release, by scripts/herdr_bin.py build, or fetched from a release
by scripts/herdr_bin.py fetch. A machine that already has another Herdr keeps
it: this one then runs in a named session of its own, with its own server, so
the two never share a socket.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = "agent-picker"     # the named session used beside another Herdr
PANE_VARIABLES = ("HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_CLIENT_SOCKET_PATH", "HERDR_PANE_ID",
                  "HERDR_TAB_ID", "HERDR_WORKSPACE_ID")


def bundled():
    """This checkout's own Herdr binary, or None when it has not been built or fetched."""
    path = ROOT / "bin" / ("herdr.exe" if sys.platform == "win32" else "herdr")
    return str(path) if path.is_file() else None


def find():
    """The Herdr the helpers drive: the one that owns this pane, else the one on
    PATH, else the bundled one."""
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or bundled()


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def session_for(binary, environ=None, which=shutil.which):
    """The session `agent` opens binary in: the one already chosen in the
    environment, the default when binary is the machine's only Herdr, and
    SESSION when another Herdr is installed beside it."""
    environ = os.environ if environ is None else environ
    if environ.get("HERDR_SESSION"):
        return environ["HERDR_SESSION"]
    other = which("herdr")
    return SESSION if other and not _same(other, binary) else None


def adopt(binary, environ=None):
    """Point this process and its children at binary's own session. Pane
    variables are dropped: they name the Herdr this command was typed in, and
    would send every call to that server instead."""
    environ = os.environ if environ is None else environ
    session = session_for(binary, environ)
    for name in PANE_VARIABLES:
        environ.pop(name, None)
    environ["HERDR_BIN_PATH"] = binary
    if session:
        environ["HERDR_SESSION"] = session
    return session


def server_running(binary, run=None):
    try:
        out = (run or subprocess.run)([binary, "status", "server"], capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip() == "status: running" for line in (out.stdout or "").splitlines())


def ensure_server(binary, wait=15.0, run=None, spawn=None, sleep=time.sleep):
    """Start binary's server for the current session when none is running, so a
    workspace can be laid out before the screen attaches. True once it answers."""
    if server_running(binary, run):
        return True
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        (spawn or subprocess.Popen)([binary, "server"], **options)
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if server_running(binary, run):
            return True
        sleep(0.2)
    return False
