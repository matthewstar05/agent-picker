"""Terminal and process differences, kept out of the shared renderer."""

import os
import re
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

    def keyboard(self, enhanced):
        """Turn the kitty keyboard protocol on or off: every key as an escape code,
        with repeat and release events, so a held key can be told from a tap.
        Terminals without it ignore the request and keep sending plain keys.
        Only used on the alternate screen, whose flags the shell never sees."""
        if WINDOWS or enhanced == getattr(self, "enhanced", False):
            return
        self.enhanced = enhanced
        self.write("\x1b[>11u" if enhanced else "\x1b[<u")

    def _byte(self):
        pending = getattr(self, "_pending", b"")
        ch = pending[:1] if pending else os.read(self.fd, 1)
        self._pending = pending[1:]
        return ch

    def read_key(self):
        """A key name: a character, "up"/"down"/"left"/"right", or "esc". With
        the kitty protocol a release comes as "release:" + the key, and
        self.releases turns true once one has."""
        if WINDOWS:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                return {"H": "up", "P": "down", "M": "right", "K": "left"}.get(msvcrt.getwch(), "")
            return "esc" if ch == "\x1b" else ch
        ch = self._byte()
        if not ch:
            return "esc"
        if ch != b"\x1b":
            return ch.decode(errors="ignore")
        # Long enough that an arrow split by a slow link (SSH, a busy machine)
        # is not read as Esc, which quits; short enough that Esc still feels instant.
        if not self.wait(0.1):
            return "esc"
        prefix = self._byte()
        if prefix not in (b"[", b"O"):
            self._pending = prefix + self._pending
            return "esc"
        # Read through the final byte even past the cap, so a long or unknown
        # sequence is dropped whole instead of leaking its tail as keystrokes.
        seq = b""
        while self.wait(0.03):
            byte = self._byte()
            if not byte:
                break
            seq = (seq + byte)[-32:]
            if 0x40 <= byte[0] <= 0x7e:
                break
        if seq == b"M" and prefix == b"[":   # X10 mouse report: three raw bytes follow
            for _ in range(3):
                if self.wait(0.03):
                    self._byte()
            return ""
        if not seq:
            return "esc"
        key, event = decode_key(prefix, seq.decode(errors="ignore"))
        if event == 3:
            self.releases = True
            return "release:" + key if key else ""
        return key

    def write(self, text):
        if WINDOWS:
            sys.stdout.write(text)
            sys.stdout.flush()
        else:
            data = text.encode("utf-8")
            while data:
                data = data[os.write(sys.stdout.fileno(), data):]


ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
# Kitty protocol key codes the screens use, besides plain characters.
KITTY_KEYS = {9: "\t", 13: "\r", 27: "esc", 127: "\x7f", 8: "\b", 57414: "\r",
              57417: "left", 57418: "right", 57419: "up", 57420: "down"}


def decode_key(prefix, seq):
    """(key name, event) for the bytes after ESC [ or ESC O. event is 1 press,
    2 repeat, 3 release. Kitty sends CSI code;mods:event u, and arrows as
    CSI 1;mods:event A. Other sequences (Home, F-keys, focus, paste and mouse
    reports) have no binding and come back as "", so they are ignored rather
    than read as Esc, which quits most screens."""
    final, fields = seq[-1:], seq[:-1].split(";")
    mods, event = 1, 1
    if len(fields) > 1:
        parts = fields[1].split(":")
        mods = int(parts[0]) if parts[0].isdigit() else 1
        event = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    if final in ARROWS and (prefix == b"O" or not fields[0].strip("1")):
        return ARROWS[final], event
    if prefix != b"[" or final != "u":
        return "", event
    code = fields[0].split(":")[0]
    code = int(code) if code.isdigit() else 0
    bits = mods - 1
    if code == 9 and bits & 1:   # Shift+Tab, unbound like the legacy ESC [ Z
        return "", event
    if code in KITTY_KEYS:
        return KITTY_KEYS[code], event
    if 57399 <= code <= 57408:   # keypad digits
        return str(code - 57399), event
    # Modifiers, other function keys, and characters no screen binds; Alt and
    # Super combinations are not bindings either.
    if code < 32 or code > 126 or bits & (2 | 8):
        return "", event
    key = chr(code)
    if bits & 4 and key.isalpha():   # ctrl: ctrl+c is "\x03", as without the protocol
        return chr(ord(key.lower()) & 0x1f), event
    if bits & (1 | 64):   # shift or caps lock
        key = key.upper()
    return key, event


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


# Windows runs a .cmd or .bat file through cmd.exe, which re-parses its command
# line: it expands %VAR%, runs & | < > and ends the command at a newline. Python
# quotes arguments for normal programs only, so a prompt cannot go to cmd.exe.
BATCH_SUFFIXES = (".cmd", ".bat")
CMD_UNSAFE = re.compile(r'[\r\n%^&|<>"]')
# npm, pnpm and yarn shims end with: "<node>" "%~dp0\path\to\cli.js" %*
SHIM_SCRIPT = re.compile(r'"%(?:~dp0|dp0%)\\?([^"%]+)"\s+%\*')


class UnsafeLauncher(OSError):
    pass


def shim_target(path):
    """[node, script] that an npm-style batch shim would run, or None."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    folder = Path(path).parent
    for match in reversed(SHIM_SCRIPT.findall(text)):
        script = folder / match.replace("\\", os.sep)
        if script.name.lower() == "node.exe" or not script.is_file():
            continue
        node = folder / "node.exe"
        node = str(node) if node.is_file() else shutil.which("node")
        return [node, str(script)] if node else None
    return None


def direct_argv(argv, windows=WINDOWS):
    """argv that passes every argument unchanged. A batch launcher is replaced
    by the script it runs; one that cannot be unwrapped is refused when an
    argument holds characters cmd.exe would interpret."""
    if not windows or not argv or not str(argv[0]).lower().endswith(BATCH_SUFFIXES):
        return list(argv)
    target = shim_target(argv[0])
    if target:
        return target + list(argv[1:])
    if any(CMD_UNSAFE.search(str(a)) for a in argv[1:]):
        raise UnsafeLauncher(f"{Path(argv[0]).name} is a batch launcher, which cannot receive this prompt "
                             "safely; point the local config at the provider's executable")
    return list(argv)


def launch(argv):
    if not WINDOWS:
        os.execvp(argv[0], argv)
    # Windows cannot replace the current process as POSIX exec does.
    # Inherit cwd, environment, and console, and propagate the CLI's exit code.
    return subprocess.call(argv)
