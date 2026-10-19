"""Terminal and process handling, kept out of the shared renderer."""

import os
import shutil
import sys
from pathlib import Path


def state_dir():
    if os.environ.get("AGENT_PICKER_STATE_HOME"):
        return Path(os.environ["AGENT_PICKER_STATE_HOME"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "agent-picker"


class Terminal:
    def __enter__(self):
        self.fd = sys.stdin.fileno()
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

    def __exit__(self, *exc):
        import signal
        import termios
        for number, handler in getattr(self, "signals", {}).items():
            signal.signal(number, handler)
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def wait(self, seconds):
        if getattr(self, "_pending", b""):
            return True
        import select
        return bool(select.select([self.fd], [], [], max(0, seconds))[0])

    def keyboard(self, enhanced):
        """Turn the kitty keyboard protocol on or off: every key as an escape code,
        with repeat and release events, so a held key can be told from a tap.
        Terminals without it ignore the request and keep sending plain keys.
        Only used on the alternate screen, whose flags the shell never sees."""
        if enhanced == getattr(self, "enhanced", False):
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
        key, event = decode_key(prefix, seq.decode(errors="ignore"))
        if event == 3:
            self.releases = True
            return "release:" + key if key else ""
        return key

    def write(self, text):
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
SEARCH_DIRS = [
    "~/.local/bin", "~/.opencode/bin", "~/.bun/bin", "~/.npm-global/bin", "~/.npm/bin",
    "~/.cargo/bin", "~/go/bin", "~/.amp/bin", "~/.factory/bin", "~/.deno/bin",
    "/opt/homebrew/bin", "/usr/local/bin",
]


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
    os.execvp(argv[0], argv)
