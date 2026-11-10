"""Terminal and process handling, kept out of the shared renderer."""

import os
import shutil
import sys
import time
from pathlib import Path


def configure_stdio():
    """Keep Unicode profiles readable when a Windows pipe uses an ANSI locale."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")


def state_dir():
    if os.environ.get("AGENT_PICKER_STATE_HOME"):
        return Path(os.environ["AGENT_PICKER_STATE_HOME"]).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "agent-picker"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "agent-picker"


def _shift_reader():
    """A local modifier-state query for terminals that omit bare modifier events."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None
    import ctypes
    try:
        if sys.platform == "darwin":
            flags = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics").CGEventSourceFlagsState
            flags.argtypes, flags.restype = [ctypes.c_int], ctypes.c_uint64
            return lambda: bool(flags(0) & (1 << 17))  # combined session state, maskShift
        if sys.platform == "win32":
            state = ctypes.WinDLL("user32", use_last_error=True).GetAsyncKeyState
            state.argtypes, state.restype = [ctypes.c_int], ctypes.c_short
            return lambda: bool(state(0x10) & 0x8000)  # VK_SHIFT, currently down
    except (OSError, AttributeError):
        pass
    return None


def _tab_reader():
    """A local Tab key-state query, unavailable in remote terminal sessions."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None
    import ctypes
    try:
        if sys.platform == "darwin":
            state = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics").CGEventSourceKeyState
            state.argtypes, state.restype = [ctypes.c_int, ctypes.c_uint16], ctypes.c_bool
            return lambda: bool(state(0, 48))  # kVK_Tab
        if sys.platform == "win32":
            state = ctypes.WinDLL("user32", use_last_error=True).GetAsyncKeyState
            state.argtypes, state.restype = [ctypes.c_int], ctypes.c_short
            return lambda: bool(state(0x09) & 0x8000)  # VK_TAB, currently down
    except (OSError, AttributeError):
        pass
    return None


class Terminal:
    # Opt-in decoding for screens that need more than menu keys (the file
    # browser): Shift+Enter, the Shift key itself, shifted symbols, focus and
    # SGR mouse reports. Off, every key reads exactly as before.
    extended = False
    # Whether Shift is held as the terminal last reported it, None until a key
    # event says. Only tracked while extended.
    shift = None
    # Whether Tab is held as the terminal last reported it, None until known.
    tab = None

    def __init__(self, raw=False):
        self.raw = raw

    def shift_pressed(self):
        """Current local Shift state, or None when only terminal reports are available."""
        if not self.extended:
            return None
        if not hasattr(self, "_shift_reader"):
            self._shift_reader = _shift_reader()
        if self._shift_reader is None:
            return None
        try:
            return self._shift_reader()
        except OSError:
            self._shift_reader = None
            return None

    def tab_pressed(self):
        """Current local Tab state, or None when only terminal events are available."""
        if not self.extended:
            return None
        if not hasattr(self, "_tab_reader"):
            self._tab_reader = _tab_reader()
        if self._tab_reader is None:
            return None
        try:
            return self._tab_reader()
        except OSError:
            self._tab_reader = None
            return None

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        if sys.platform == "win32":
            return self._enter_windows()
        import termios
        import tty
        self.old = termios.tcgetattr(self.fd)
        (tty.setraw if self.raw else tty.setcbreak)(self.fd)
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

    def __exit__(self, *exc):
        # Mouse and focus reports outlive the alternate screen, so never hand
        # them to the shell, even when the screen that enabled them crashed.
        try:
            self.mouse(False)
            self.focus_events(False)
        except OSError:
            pass
        if sys.platform == "win32":
            for handle, mode in self.modes:
                self.kernel.SetConsoleMode(handle, mode)
            self.kernel.SetConsoleOutputCP(self.output_cp)
            return
        import signal
        import termios
        for number, handler in getattr(self, "signals", {}).items():
            signal.signal(number, handler)
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def wait(self, seconds):
        if getattr(self, "_pending", b""):
            return True
        if sys.platform == "win32":
            until = time.monotonic() + max(0, seconds)
            while True:
                if self._console_pending or self._read_windows_event(False):
                    return True
                remaining = until - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.01, remaining))
        import select
        return bool(select.select([self.fd], [], [], max(0, seconds))[0])

    def keyboard(self, enhanced, flags=11):
        """Turn the kitty keyboard protocol on or off: every key as an escape code,
        with repeat and release events, so a held key can be told from a tap.
        Terminals without it ignore the request and keep sending plain keys.
        Only used on the alternate screen, whose flags the shell never sees.
        flags 15 adds alternate keys, so Shift+/ can arrive as "?". New flags
        while already on replace the pushed entry, so one pop still restores."""
        on = getattr(self, "enhanced", False)
        if enhanced == on and (not enhanced or flags == getattr(self, "keyboard_flags", 11)):
            return
        self.enhanced = enhanced
        self.keyboard_flags = flags
        self.write(("\x1b[<u" if on else "") + f"\x1b[>{flags}u" if enhanced else "\x1b[<u")

    def mouse(self, on):
        """Turn on click and wheel reports in SGR form, read as "mouse:..." keys."""
        if on == getattr(self, "mouse_reports", False):
            return
        self.mouse_reports = on
        self.write("\x1b[?1000h\x1b[?1006h" if on else "\x1b[?1006l\x1b[?1000l")

    def focus_events(self, on):
        """Turn on focus reports, read as "focus-in" and "focus-out"."""
        if on == getattr(self, "focus_reports", False):
            return
        self.focus_reports = on
        self.write("\x1b[?1004h" if on else "\x1b[?1004l")

    def _byte(self):
        pending = getattr(self, "_pending", b"")
        if not pending and sys.platform == "win32":
            pending = self.read_bytes(1)
        ch = pending[:1] if pending else os.read(self.fd, 1)
        self._pending = pending[1:]
        return ch

    def _enter_windows(self):
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        self.kernel.GetStdHandle.restype = wintypes.HANDLE
        self.kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        class Key(ctypes.Structure):
            _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD), ("virtual", wintypes.WORD),
                        ("scan", wintypes.WORD), ("character", wintypes.WCHAR), ("control", wintypes.DWORD)]

        class Event(ctypes.Union):
            _fields_ = [("key", Key), ("padding", ctypes.c_byte * 16)]

        class Record(ctypes.Structure):
            _fields_ = [("kind", wintypes.WORD), ("event", Event)]

        self._console_record = Record
        self.kernel.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Record), wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetNumberOfConsoleInputEvents.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.input_handle = self.kernel.GetStdHandle(-10)
        self._console_pending = b""
        self._surrogate = ""
        self.modes = []
        self.output_cp = self.kernel.GetConsoleOutputCP()
        try:
            for number in (-10, -11):
                handle = self.kernel.GetStdHandle(number)
                mode = wintypes.DWORD()
                if not self.kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    raise OSError("Use Windows Terminal or a Windows console to open agent-picker.")
                self.modes.append((handle, mode.value))
                # VT input preserves escape sequences; turn off line editing,
                # echo and QuickEdit, which otherwise pause animated screens.
                new = ((mode.value & ~(0x2 | 0x4 | 0x40)) | 0x80 | 0x200) if number == -10 else mode.value | 0x4
                if number == -10 and self.raw:
                    new &= ~0x1
                if not self.kernel.SetConsoleMode(handle, new):
                    raise OSError("This console does not support virtual terminal input. Use Windows Terminal.")
            self.kernel.SetConsoleOutputCP(65001)
        except BaseException:
            self.__exit__()
            raise
        return self

    def read_bytes(self, size):
        if sys.platform != "win32":
            return os.read(self.fd, size)
        while not self._console_pending:
            self._read_windows_event(True)
        while len(self._console_pending) < size and self._read_windows_event(False):
            pass
        data, self._console_pending = self._console_pending[:size], self._console_pending[size:]
        return data

    def _read_windows_event(self, blocking):
        """Keep Unicode characters distinct from Windows extended-key sentinels."""
        import ctypes
        from ctypes import wintypes
        while True:
            count = wintypes.DWORD()
            if not blocking:
                if not self.kernel.GetNumberOfConsoleInputEvents(self.input_handle, ctypes.byref(count)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not count.value:
                    return False
            record = self._console_record()
            if not self.kernel.ReadConsoleInputW(self.input_handle, ctypes.byref(record), 1, ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            if record.kind != 1:
                continue
            key = record.event.key
            if not key.down:
                data = self._windows_key(False, "", key.virtual, key.control)
                if data:
                    self._console_pending += data
                    return True
                continue
            text = key.character
            if text and 0xd800 <= ord(text) <= 0xdbff:
                self._surrogate = text
                continue
            if self._surrogate:
                text = (self._surrogate + text).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
                self._surrogate = ""
            data = self._windows_key(True, text, key.virtual, key.control)
            if data:
                self._console_pending += data * max(1, key.repeat)
                return True

    def _windows_key(self, down, text, virtual, control):
        """Bytes for one console key record. Only presses type anything. With
        extended decoding a bare Shift press or release comes through as
        kitty's left Shift code, so read_key reports "shift" and
        "release:shift" as it does under the kitty protocol, and every other
        record, releases included, updates self.shift."""
        if self.extended:
            if virtual == 9:
                self.tab = bool(down)
            if virtual in (0x10, 0xa0, 0xa1):   # VK_SHIFT, VK_LSHIFT, VK_RSHIFT
                return b"\x1b[57441u" if down else b"\x1b[57441;1:3u"
            self.shift = bool(control & 0x10)
        return windows_key_bytes(text, virtual, control) if down else b""

    def read_key(self):
        """A key name: a character, "up"/"down"/"left"/"right", or "esc". With
        the kitty protocol a release comes as "release:" + the key, and
        self.releases turns true once one has. While self.extended, also
        "shift-enter", "shift", "focus-in"/"focus-out" and
        "mouse:<button>:<x>:<y>:press|release" (see decode_key), and
        self.shift follows the Shift key."""
        ch = self._byte()
        if not ch:
            return "esc"
        if ch != b"\x1b":
            # Prompt editors need complete UTF-8 characters, not one byte of
            # a multibyte character per key. Other screens ignore these keys.
            size = 2 if 0xc2 <= ch[0] <= 0xdf else 3 if 0xe0 <= ch[0] <= 0xef else 4 if 0xf0 <= ch[0] <= 0xf4 else 1
            for _ in range(size - 1):
                if not self.wait(0.1):
                    break
                byte = self._byte()
                if not byte or not 0x80 <= byte[0] <= 0xbf:
                    self._pending = byte + self._pending
                    break
                ch += byte
            key = ch.decode(errors="ignore")
            if self.extended and key == "\t":
                self.tab = True
            return key
        # Long enough that an arrow split by a slow link (SSH, a busy machine)
        # is not read as Esc, which quits; short enough that Esc still feels instant.
        if not self.wait(0.1):
            return "esc"
        prefix = self._byte()
        if self.extended and prefix in (b"\r", b"\n"):
            # ESC CR: what terminals set up for Shift+Enter, and Windows consoles, send.
            return "shift-enter"
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
        if seq == b"200~" and prefix == b"[":
            # Keep a multiline paste in one event so its newlines cannot
            # submit a form or trigger menu actions. Bound retained content.
            pasted = bytearray()
            tail = b""
            while self.wait(0.5):
                byte = self._byte()
                if not byte:
                    break
                tail += byte
                if tail.endswith(b"\x1b[201~"):
                    return "paste:" + (pasted + tail[:-6]).decode("utf-8", errors="replace")
                if len(tail) > 6:
                    if len(pasted) < 1048576:
                        pasted += tail[:1]
                    tail = tail[1:]
            return "paste:" + (pasted + tail).decode("utf-8", errors="replace")
        if not seq:
            return "esc"
        text = seq.decode(errors="ignore")
        key, event = decode_key(prefix, text, self.extended)
        if self.extended:
            if key == "\t":
                self.tab = event != 3
            elif prefix == b"[" and text.endswith("u"):
                code = text[:-1].split(";")[0].split(":")[0]
                if code.isdigit() and int(code) == 9:
                    self.tab = event != 3
            if key == "shift":
                self.shift = event != 3
            else:
                bits = key_modifiers(prefix, text)
                if bits is not None:
                    self.shift = bool(bits & 1)
        if event == 3:
            self.releases = True
            return "release:" + key if key else ""
        return key

    def write(self, text):
        data = text.encode("utf-8")
        while data:
            data = data[os.write(sys.stdout.fileno(), data):]


def windows_key_bytes(text, virtual, control):
    """Translate native key records to the editor's existing UTF-8/ANSI events."""
    shift, alt, ctrl = bool(control & 0x10), bool(control & 0x3), bool(control & 0xc)
    modifier = 1 + shift + 2 * alt + 4 * ctrl
    final = {0x26: "A", 0x28: "B", 0x27: "C", 0x25: "D", 0x24: "H", 0x23: "F"}.get(virtual)
    if final:
        return ("\x1b[" + (f"1;{modifier}" if modifier > 1 else "") + final).encode()
    if virtual == 0x2e:
        return b"\x1b[3~"
    if virtual == 9 and shift:
        return b"\x1b[Z"
    if virtual == 13 and (shift or alt):
        return b"\x1b\r"
    if not text or text == "\x00":
        return b""
    # AltGr uses Ctrl+Alt to type normal international characters.
    prefix = b"\x1b" if alt and not ctrl else b""
    return prefix + text.encode("utf-8", "replace")


ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
# Kitty protocol key codes the screens use, besides plain characters.
KITTY_KEYS = {9: "\t", 13: "\r", 27: "esc", 127: "\x7f", 8: "\b", 57414: "\r",
              57417: "left", 57418: "right", 57419: "up", 57420: "down"}
# Shift+key on a US layout, for terminals that report Shift+/ as / with the
# Shift bit and no alternate key, as Herdr does.
US_SHIFTED = dict(zip("`1234567890-=[]\\;',./", '~!@#$%^&*()_+{}|:"<>?'))


def decode_key(prefix, seq, extended=False):
    """(key name, event) for the bytes after ESC [ or ESC O. event is 1 press,
    2 repeat, 3 release. Kitty sends CSI code;mods:event u, and arrows as
    CSI 1;mods:event A. Other sequences (Home, F-keys, focus, paste and mouse
    reports) have no binding and come back as "", so they are ignored rather
    than read as Esc, which quits most screens.

    extended adds "shift-enter", "shift" (either Shift key), shifted symbols
    ("?" for Shift+/), "focus-in"/"focus-out", and SGR mouse reports as
    "mouse:<button>:<x>:<y>:press" or ":release", numbers as sent (1-based
    cells; the wheel is button 64/65, motion adds 32)."""
    final, fields = seq[-1:], seq[:-1].split(";")
    if extended and prefix == b"[":
        if seq in ("I", "O"):
            return ("focus-in" if seq == "I" else "focus-out"), 1
        if seq[:1] == "<" and final in ("M", "m"):
            numbers = seq[1:-1].split(";")
            if len(numbers) != 3 or not all(n.isdigit() for n in numbers):
                return "", 1
            button, x, y = (int(n) for n in numbers)
            state = "press" if final == "M" else "release"
            return f"mouse:{button}:{x}:{y}:{state}", 1
    mods, event = 1, 1
    if len(fields) > 1:
        parts = fields[1].split(":")
        mods = int(parts[0]) if parts[0].isdigit() else 1
        event = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    if final in ARROWS and (prefix == b"O" or not fields[0].strip("1")):
        return ARROWS[final], event
    if prefix != b"[" or final != "u":
        return "", event
    codes = fields[0].split(":")
    code = int(codes[0]) if codes[0].isdigit() else 0
    bits = mods - 1
    if extended and code in (13, 57414) and bits & 1:
        return "shift-enter", event
    if extended and code in (57441, 57447):   # left and right Shift
        return "shift", event
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
    if extended and bits & 1:
        # Report-alternate-keys sends the shifted key as CSI base:shifted;mods u.
        shifted = int(codes[1]) if len(codes) > 1 and codes[1].isdigit() else 0
        return (chr(shifted) if 32 <= shifted <= 126 else US_SHIFTED.get(key, key.upper())), event
    if bits & (1 | 64):   # shift or caps lock
        key = key.upper()
    return key, event


def key_modifiers(prefix, seq):
    """Modifier bits (1 Shift, 2 Alt, 4 Ctrl, ...) of a key event after ESC [:
    kitty's CSI code;mods u, CSI 1;mods A and CSI number;mods ~. None for bytes
    that report no modifiers, such as ESC O keys, focus and mouse reports."""
    final, fields = seq[-1:], seq[:-1].split(";")
    if prefix != b"[":
        return None
    if final in ("u", "~"):
        if not fields[0].split(":")[0].isdigit():
            return None
    elif not final or final not in "ABCDEFHPQS" or fields[0] not in ("", "1"):
        return None
    mods = fields[1].split(":")[0] if len(fields) > 1 else "1"
    return max(int(mods) - 1, 0) if mods.isdigit() else 0


# Where agent CLI installers put their binaries when that folder isn't always
# on PATH (a pane started before the install, or a shell without the rc line).
SEARCH_DIRS = [
    "~/.local/bin", "~/.opencode/bin", "~/.bun/bin", "~/.npm-global/bin", "~/.npm/bin",
    "~/.cargo/bin", "~/go/bin", "~/.amp/bin", "~/.factory/bin", "~/.deno/bin",
    "/opt/homebrew/bin", "/usr/local/bin",
]


def resolve_argv(agent):
    argv = list(agent["argv"])
    # Never search for generic "agent": that would relaunch this picker.
    candidates = [argv[0]] + agent.get("executables", [])
    directories = [str(Path(d).expanduser()) for d in SEARCH_DIRS]
    if sys.platform == "win32":
        directories += [str(Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "npm"),
                        str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Microsoft/WinGet/Links")]
    extra = os.pathsep.join(directories)
    for path in (None, extra):
        for command in candidates:
            found = shutil.which(command, path=path)
            if found:
                return [found] + argv[1:]
    return None


# cmd.exe ends a batch file's command line at a line break, expands %NAME%
# inside quotes, and has no escape for an embedded quote, so these characters
# cannot reach a program through a .cmd shim intact.
BATCH_UNSAFE = ("\r", "\n", "%", '"', "\x00")


def _shim_target(shim):
    """The real program a simple .cmd shim runs: npm's, or one fixed command line."""
    import re
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    folder = str(shim.parent)
    lines = [line for line in text.splitlines() if "%*" in line]
    if len(lines) != 1:
        return None
    # npm prefixes the call with `endLocal & goto ... || title %COMSPEC% &`.
    line, quoted, start = lines[0], False, 0
    for index, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == "&" and not quoted:
            start = index + 1
    command = line[start:].strip().lstrip("@")
    match = re.fullmatch(r'((?:"[^"]*"\s*)+)%\*\s*', command)
    if not match:
        return None
    argv = []
    for token in re.findall(r'"([^"]*)"', match[1]):
        if token == "%_prog%":   # npm: node.exe beside the shim, else node on PATH
            local = Path(folder) / "node.exe"
            token = str(local) if local.is_file() else (shutil.which("node") or "")
        else:
            token = re.sub(r"^%~?dp0%?\\?", lambda _: folder + "\\", token)
        if not token or "%" in token:
            return None
        argv.append(token)
    return argv or None


def direct_argv(argv):
    """Arguments for subprocess that never pass free text through cmd.exe.

    Windows npm installs (claude, codex, gemini...) are .cmd shims. A prompt
    passed through one is cut at its first line break and its %VAR% text is
    expanded, so run the shim's real program instead. A shim of unknown shape
    still runs, but only with arguments cmd.exe cannot change.
    """
    argv = list(argv)
    if sys.platform != "win32" or not argv or Path(argv[0]).suffix.lower() not in (".cmd", ".bat"):
        return argv
    target = _shim_target(Path(argv[0]))
    if target:
        return target + argv[1:]
    if any(ch in arg for arg in argv[1:] for ch in BATCH_UNSAFE):
        raise ValueError(f"{Path(argv[0]).name} is a batch file, which cannot pass line breaks, quotes or % "
                         "signs intact. Install the provider's native executable, or point its profile at the "
                         "program the batch file runs.")
    return argv


def launch(argv):
    if sys.platform == "win32":
        import subprocess
        return subprocess.call(argv)
    os.execvp(argv[0], argv)


def pipe_ready(fd, timeout):
    """Wait for a pipe, including EOF. Windows select accepts only sockets."""
    if sys.platform != "win32":
        import select
        return bool(select.select([fd], [], [], timeout)[0])
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                     wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    deadline = time.monotonic() + timeout
    while True:
        available = wintypes.DWORD()
        if not peek(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in (109, 232, 233):
                return True
            raise ctypes.WinError(ctypes.get_last_error())
        if available.value:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def stop_process_tree(process):
    """Terminate this owned child and its descendants, then reap it."""
    import signal
    import subprocess
    if sys.platform == "win32":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if process.poll() is None:
                process.kill()
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def windows_processes():
    """Read process arguments without requesting elevated access."""
    import ctypes
    from ctypes import wintypes
    import json
    import subprocess
    command = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; "
               "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        return []
    entries = json.loads(result.stdout or "[]")
    if isinstance(entries, dict):
        entries = [entries]
    shell = ctypes.WinDLL("shell32")
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    rows = []
    for entry in entries:
        if not entry.get("CommandLine"):
            continue
        count = ctypes.c_int()
        pointer = shell.CommandLineToArgvW(entry["CommandLine"], ctypes.byref(count))
        if pointer:
            try:
                rows.append((entry["ProcessId"], entry["ParentProcessId"], [pointer[i] for i in range(count.value)]))
            finally:
                kernel.LocalFree(pointer)
    return rows


def process_alive(pid):
    """Probe a PID without sending a signal (os.kill(pid, 0) kills on Windows)."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)
