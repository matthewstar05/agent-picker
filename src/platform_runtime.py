"""Terminal and process differences, kept out of the shared renderer."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"


def state_dir():
    if os.environ.get("AGENT_PICKER_STATE_HOME"):
        return Path(os.environ["AGENT_PICKER_STATE_HOME"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        base = (os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))
                if WINDOWS else str(Path.home() / ".local/state"))
    return Path(base) / "agent-picker"


class Terminal:
    def __enter__(self):
        self.fd = sys.stdin.fileno()
        if WINDOWS:
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            self.kernel.GetStdHandle.restype = wintypes.HANDLE
            self.kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            self.kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.modes = []
            for handle_id, output in ((-11, True), (-10, False)):
                handle = self.kernel.GetStdHandle(handle_id & 0xffffffff)
                mode = wintypes.DWORD()
                if not self.kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    self._restore_windows()
                    raise RuntimeError("Use a native PowerShell tab in Windows Terminal.")
                updated = mode.value | 0x0004 if output else (mode.value | 0x0080) & ~0x0047
                if not self.kernel.SetConsoleMode(handle, updated):
                    self._restore_windows()
                    raise RuntimeError("This terminal does not support ANSI output.")
                self.modes.append((handle, mode.value))
        else:
            import termios
            import tty
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            # Ctrl+\ (SIGQUIT), a closed pane (SIGHUP), or kill (SIGTERM) would
            # otherwise abort Python mid-frame: macOS shows "Python quit
            # unexpectedly" and the terminal is left in cbreak mode. Leave the
            # way q does instead, so the caller's cleanup restores everything.
            import signal

            def leave(signum, frame):
                raise KeyboardInterrupt

            self.signals = {}
            for name in ("SIGQUIT", "SIGHUP", "SIGTERM"):
                number = getattr(signal, name)
                self.signals[number] = signal.signal(number, leave)
        return self

    def _restore_windows(self):
        for handle, mode in reversed(self.modes):
            self.kernel.SetConsoleMode(handle, mode)

    def __exit__(self, *exc):
        if WINDOWS:
            self._restore_windows()
        else:
            import signal
            import termios
            for number, handler in getattr(self, "signals", {}).items():
                signal.signal(number, handler)
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def wait(self, seconds):
        if getattr(self, "_pending", b""):
            return True
        if WINDOWS:
            import msvcrt
            deadline = time.monotonic() + max(0, seconds)
            while not msvcrt.kbhit():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(remaining, 0.005))
            return True
        import select
        return bool(select.select([self.fd], [], [], max(0, seconds))[0])

    def read_key(self):
        if WINDOWS:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                return {"H": "up", "P": "down", "M": "right", "K": "left"}.get(msvcrt.getwch(), "")
            return "esc" if ch == "\x1b" else ch
        pending = getattr(self, "_pending", b"")
        ch = pending[:1] if pending else os.read(self.fd, 1)
        self._pending = pending[1:]
        if not ch:
            return "esc"
        if ch != b"\x1b":
            return ch.decode(errors="ignore")
        if not self.wait(0.03):
            return "esc"
        prefix = os.read(self.fd, 1)
        if prefix not in (b"[", b"O"):
            self._pending = prefix + self._pending
            return "esc"
        seq = prefix + (os.read(self.fd, 1) if self.wait(0.03) else b"")
        return {b"[A": "up", b"[B": "down", b"OA": "up", b"OB": "down",
                b"[C": "right", b"[D": "left", b"OC": "right", b"OD": "left"}.get(seq, "esc")

    def write(self, text):
        if WINDOWS:
            sys.stdout.write(text)
            sys.stdout.flush()
        else:
            data = text.encode("utf-8")
            while data:
                data = data[os.write(sys.stdout.fileno(), data):]


# Where agent CLI installers put their binaries when that folder isn't always
# on PATH (a pane started before the install, or a shell without the rc line).
SEARCH_DIRS = ([
    "~/AppData/Roaming/npm", "~/.bun/bin", "~/.cargo/bin", "~/go/bin", "~/.local/bin",
] if WINDOWS else [
    "~/.local/bin", "~/.opencode/bin", "~/.bun/bin", "~/.npm-global/bin", "~/.npm/bin",
    "~/.cargo/bin", "~/go/bin", "~/.amp/bin", "~/.factory/bin", "~/.deno/bin",
    "/opt/homebrew/bin", "/usr/local/bin",
])


def resolve_argv(agent):
    argv = list(agent["argv"])
    # Never search for generic "agent": that would relaunch this picker.
    candidates = [argv[0]] + agent.get("executables", [])
    extra = os.pathsep.join(str(Path(d).expanduser()) for d in SEARCH_DIRS)
    for path in (None, extra):
        for command in candidates:
            found = shutil.which(command, path=path)
            if found:
                return [found] + argv[1:]
    return None


def launch(argv):
    if not WINDOWS:
        os.execvp(argv[0], argv)
    # Windows cannot replace the current process as POSIX exec does.
    # Inherit cwd, environment, and console, and propagate the CLI's exit code.
    return subprocess.call(argv)
