import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from platform_runtime import Terminal, decode_key, windows_key_bytes


def feed(terminal, data):
    """Read every key in data through read_key, with Shift state after each.
    The bytes sit in the terminal's own pending buffer, so read_key's
    push-back (ESC then a plain key) works as it does on a real terminal."""
    terminal._pending = bytes(data)
    terminal.wait = lambda _timeout: bool(terminal._pending)
    keys = []
    while terminal._pending:
        keys.append((terminal.read_key(), terminal.shift))
    return keys


def keys(data, extended=True):
    terminal = Terminal()
    terminal.extended = extended
    return [key for key, _ in feed(terminal, data)]


class Writes(Terminal):
    """A terminal that records what it writes instead of touching stdout."""

    def __init__(self):
        super().__init__()
        self.written = []

    def write(self, text):
        self.written.append(text)


class NonExtendedTests(unittest.TestCase):
    """Screens that never opt in read every key exactly as before."""

    def test_off_by_default(self):
        terminal = Terminal()
        self.assertFalse(terminal.extended)
        self.assertIsNone(terminal.shift)

    def test_esc_then_enter_stays_two_keys(self):
        self.assertEqual(keys(b"\x1b\r\x1b\n", extended=False), ["esc", "\r", "esc", "\n"])

    def test_kitty_shift_enter_stays_enter(self):
        self.assertEqual(keys(b"\x1b[13;2u\x1b[57414;2u\x1b[13;2:3u", extended=False), ["\r", "\r", "release:\r"])

    def test_shifted_symbols_stay_unshifted(self):
        self.assertEqual(keys(b"\x1b[47;2u\x1b[49;2u\x1b[97:65;2u\x1b[47:63;2u", extended=False),
                         ["/", "1", "A", "/"])

    def test_shift_focus_and_mouse_stay_unbound(self):
        data = (b"\x1b[57441;2u\x1b[57441;1:3u\x1b[57447;2u\x1b[I\x1b[O"
                b"\x1b[<0;10;5M\x1b[<0;10;5m\x1b[<64;1;1M")
        self.assertEqual(keys(data, extended=False), [""] * 8)

    def test_shift_is_not_tracked(self):
        terminal = Terminal()
        feed(terminal, b"\x1b[57441;2u\x1b[97:65;2u\x1b[1;2A")
        self.assertIsNone(terminal.shift)

    def test_decode_key_defaults_to_the_old_decoding(self):
        for seq in ("13;2u", "57441;2u", "47;2u", "97:65;2u", "I", "O", "<0;1;1M"):
            self.assertEqual(decode_key(b"[", seq), decode_key(b"[", seq, False))
        self.assertEqual(decode_key(b"[", "47;2u"), ("/", 1))
        self.assertEqual(decode_key(b"[", "I"), ("", 1))

    def test_keyboard_default_writes_the_same_sequences(self):
        terminal = Writes()
        terminal.keyboard(True)
        terminal.keyboard(True)
        terminal.keyboard(False)
        terminal.keyboard(False)
        self.assertEqual(terminal.written, ["\x1b[>11u", "\x1b[<u"])

    def test_windows_records_type_as_before(self):
        terminal = Terminal()
        self.assertEqual(terminal._windows_key(True, "\x00", 0x10, 0x10), b"")
        self.assertEqual(terminal._windows_key(False, "", 0x10, 0), b"")
        self.assertEqual(terminal._windows_key(True, "A", 65, 0x10), b"A")
        self.assertEqual(terminal._windows_key(False, "A", 65, 0x10), b"")
        self.assertEqual(terminal._windows_key(True, "\r", 13, 0x10), b"\x1b\r")
        self.assertEqual(terminal._windows_key(True, "", 0x25, 0x10), windows_key_bytes("", 0x25, 0x10))
        self.assertIsNone(terminal.shift)


class ExtendedKeyTests(unittest.TestCase):
    def test_esc_then_enter_is_shift_enter(self):
        self.assertEqual(keys(b"\x1b\r\x1b\n"), ["shift-enter", "shift-enter"])

    def test_esc_and_alt_keys_are_unchanged(self):
        self.assertEqual(keys(b"\x1bq\x1b"), ["esc", "q", "esc"])
        self.assertEqual(keys(b"\r\x1b[A\x1bOB\x1b[27u"), ["\r", "up", "down", "esc"])

    def test_kitty_shift_enter(self):
        data = b"\x1b[13;2u\x1b[13;2:2u\x1b[13;2:3u\x1b[57414;2u\x1b[57414;2:3u\x1b[13;6u"
        self.assertEqual(keys(data), ["shift-enter", "shift-enter", "release:shift-enter",
                                      "shift-enter", "release:shift-enter", "shift-enter"])

    def test_plain_enter_is_still_enter(self):
        self.assertEqual(keys(b"\x1b[13u\x1b[57414u\x1b[13;1:3u\x1b[13;3u"), ["\r", "\r", "release:\r", "\r"])

    def test_shift_keys(self):
        data = (b"\x1b[57441;2u\x1b[57441;2:2u\x1b[57441;1:3u"
                b"\x1b[57447;2u\x1b[57447;2:2u\x1b[57447;1:3u\x1b[57441u")
        self.assertEqual(keys(data), ["shift", "shift", "release:shift", "shift", "shift", "release:shift", "shift"])

    def test_other_modifier_keys_stay_unbound(self):
        codes = list(range(57442, 57447)) + list(range(57448, 57453))
        data = b"".join(b"\x1b[%du\x1b[%d;1:3u" % (code, code) for code in codes)
        self.assertEqual(keys(data), [""] * (2 * len(codes)))

    def test_alternate_key_gives_the_shifted_character(self):
        self.assertEqual(keys(b"\x1b[97:65;2u\x1b[49:33;2u\x1b[47:63;2u\x1b[59:58;2:2u\x1b[47:63;2:3u"),
                         ["A", "!", "?", ":", "release:?"])

    def test_alternate_with_a_base_layout_key(self):
        # CSI code:shifted:base;mods u, and an empty shifted field.
        self.assertEqual(keys(b"\x1b[97:65:97;2u\x1b[47::47;2u"), ["A", "?"])

    def test_us_layout_fallback_without_an_alternate(self):
        pairs = dict(zip("`1234567890-=[]\\;',./", '~!@#$%^&*()_+{}|:"<>?'))
        data = b"".join(b"\x1b[%d;2u" % ord(base) for base in pairs)
        self.assertEqual(keys(data), list(pairs.values()))
        self.assertEqual(keys(b"\x1b[97;2u\x1b[122;2u\x1b[32;2u"), ["A", "Z", " "])

    def test_non_ascii_alternate_falls_back_to_the_us_map(self):
        self.assertEqual(keys(b"\x1b[51:163;2u"), ["#"])

    def test_literal_and_unshifted_symbols(self):
        self.assertEqual(keys(b"\x1b[63u\x1b[47u\x1b[97u\x1b[65u"), ["?", "/", "a", "A"])

    def test_ctrl_alt_and_caps_lock_with_shift(self):
        # Ctrl+Shift+a stays ctrl+a; Alt+Shift+/ stays unbound; caps lock alone upper-cases.
        self.assertEqual(keys(b"\x1b[97:65;6u\x1b[47:63;4u\x1b[97;65u"), ["\x01", "", "A"])

    def test_shift_tab_stays_unbound(self):
        self.assertEqual(keys(b"\x1b[9;2u\x1b[Z"), ["", ""])

    def test_focus(self):
        self.assertEqual(keys(b"\x1b[I\x1b[O\x1bOA"), ["focus-in", "focus-out", "up"])
        self.assertEqual(decode_key(b"[", "I", True), ("focus-in", 1))
        self.assertEqual(decode_key(b"O", "I", True), ("", 1))

    def test_sgr_mouse(self):
        data = (b"\x1b[<0;10;5M\x1b[<0;10;5m\x1b[<2;1;1M\x1b[<64;3;4M\x1b[<65;3;4M"
                b"\x1b[<32;7;8M\x1b[<4;120;40M\x1b[<0;10M\x1b[<1;;1M")
        self.assertEqual(keys(data), ["mouse:0:10:5:press", "mouse:0:10:5:release", "mouse:2:1:1:press",
                                      "mouse:64:3:4:press", "mouse:65:3:4:press", "mouse:32:7:8:press",
                                      "mouse:4:120:40:press", "", ""])

    def test_mouse_release_is_not_a_key_release(self):
        terminal = Terminal()
        terminal.extended = True
        feed(terminal, b"\x1b[<0;1;1m")
        self.assertFalse(getattr(terminal, "releases", False))

    def test_paste_and_unknown_sequences_are_unchanged(self):
        self.assertEqual(keys(b"\x1b[200~a\rb\x1b[201~\x1b[H\x1b[5~\x1b[99999999uj"),
                         ["paste:a\rb", "", "", "", "j"])


class ShiftStateTests(unittest.TestCase):
    def states(self, data):
        terminal = Terminal()
        terminal.extended = True
        return feed(terminal, data)

    def test_shift_key_press_repeat_and_release(self):
        self.assertEqual(self.states(b"\x1b[57441;2u\x1b[57441;2:2u\x1b[57441;1:3u"),
                         [("shift", True), ("shift", True), ("release:shift", False)])

    def test_release_sets_false_even_with_the_shift_bit(self):
        self.assertEqual(self.states(b"\x1b[57447;2u\x1b[57447;2:3u"),
                         [("shift", True), ("release:shift", False)])

    def test_other_keys_report_their_shift_bit(self):
        data = b"\x1b[97u\x1b[97:65;2u\x1b[97;1:3u\x1b[1;2A\x1b[A\x1b[1;2:3B\x1b[3;2~\x1b[3~\x1b[13;2u\x1b[57442;5u"
        self.assertEqual(self.states(data),
                         [("a", False), ("A", True), ("release:a", False), ("up", True), ("up", False),
                          ("release:down", True), ("", True), ("", False), ("shift-enter", True), ("", False)])

    def test_unknown_until_a_key_event_says(self):
        # Plain bytes, ESC CR, SS3 keys, focus and mouse reports carry no Shift state.
        self.assertEqual(self.states(b"a\x1b\r\x1bOA\x1b[I\x1b[<0;1;1M"),
                         [("a", None), ("shift-enter", None), ("up", None), ("focus-in", None),
                          ("mouse:0:1:1:press", None)])

    def test_focus_and_mouse_keep_the_last_state(self):
        self.assertEqual(self.states(b"\x1b[57441;2u\x1b[O\x1b[<0;1;1M\x1b[57441;1:3u\x1b[I"),
                         [("shift", True), ("focus-out", True), ("mouse:0:1:1:press", True),
                          ("release:shift", False), ("focus-in", False)])

    def test_key_modifiers(self):
        from platform_runtime import key_modifiers
        self.assertEqual(key_modifiers(b"[", "97;6u"), 5)
        self.assertEqual(key_modifiers(b"[", "97u"), 0)
        self.assertEqual(key_modifiers(b"[", "1;2:3A"), 1)
        self.assertEqual(key_modifiers(b"[", "A"), 0)
        self.assertEqual(key_modifiers(b"[", "15;2~"), 1)
        for prefix, seq in ((b"O", "A"), (b"[", "I"), (b"[", "<0;1;1M"), (b"[", "?15u"), (b"[", "5;2A"), (b"[", "Z")):
            self.assertIsNone(key_modifiers(prefix, seq), seq)


class ModeWriteTests(unittest.TestCase):
    def test_keyboard_flags(self):
        terminal = Writes()
        terminal.keyboard(True, flags=15)
        terminal.keyboard(True, flags=15)
        terminal.keyboard(False)
        self.assertEqual(terminal.written, ["\x1b[>15u", "\x1b[<u"])

    def test_new_flags_while_on_replace_the_push(self):
        terminal = Writes()
        terminal.keyboard(True)
        terminal.keyboard(True, flags=15)
        terminal.keyboard(True, 15)
        terminal.keyboard(True)
        terminal.keyboard(False, flags=15)
        terminal.keyboard(False)
        self.assertEqual(terminal.written, ["\x1b[>11u", "\x1b[<u\x1b[>15u", "\x1b[<u\x1b[>11u", "\x1b[<u"])

    def test_mouse(self):
        terminal = Writes()
        terminal.mouse(False)
        terminal.mouse(True)
        terminal.mouse(True)
        terminal.mouse(False)
        terminal.mouse(False)
        self.assertEqual(terminal.written, ["\x1b[?1000h\x1b[?1006h", "\x1b[?1006l\x1b[?1000l"])

    def test_focus_events(self):
        terminal = Writes()
        terminal.focus_events(False)
        terminal.focus_events(True)
        terminal.focus_events(True)
        terminal.focus_events(False)
        self.assertEqual(terminal.written, ["\x1b[?1004h", "\x1b[?1004l"])

    def leave(self, terminal):
        # The Windows branch of __exit__ restores console modes through kernel32,
        # which a stub stands in for on every platform.
        terminal.modes = []
        terminal.output_cp = 65001
        terminal.kernel = type("Kernel", (), {"SetConsoleOutputCP": staticmethod(lambda cp: 1)})()
        with patch.object(sys, "platform", "win32"):
            terminal.__exit__(None, None, None)

    def test_leaving_turns_reports_off(self):
        terminal = Writes()
        terminal.mouse(True)
        terminal.focus_events(True)
        self.leave(terminal)
        self.assertEqual(terminal.written[2:], ["\x1b[?1006l\x1b[?1000l", "\x1b[?1004l"])

    def test_leaving_without_reports_writes_nothing(self):
        terminal = Writes()
        self.leave(terminal)
        self.assertEqual(terminal.written, [])


class WindowsShiftTests(unittest.TestCase):
    """Console key records to bytes. Unverified on a real Windows console."""

    def terminal(self):
        terminal = Terminal()
        terminal.extended = True
        return terminal

    def test_bare_shift_press_and_release(self):
        terminal = self.terminal()
        for virtual in (0x10, 0xa0, 0xa1):
            self.assertEqual(terminal._windows_key(True, "\x00", virtual, 0x10), b"\x1b[57441u")
            self.assertEqual(terminal._windows_key(False, "\x00", virtual, 0), b"\x1b[57441;1:3u")

    def test_other_records_update_shift(self):
        terminal = self.terminal()
        self.assertEqual(terminal._windows_key(True, "A", 65, 0x10), b"A")
        self.assertTrue(terminal.shift)
        self.assertEqual(terminal._windows_key(False, "A", 65, 0), b"")
        self.assertFalse(terminal.shift)
        self.assertEqual(terminal._windows_key(True, "\r", 13, 0x10), b"\x1b\r")
        self.assertTrue(terminal.shift)
        self.assertEqual(terminal._windows_key(True, "", 0x26, 0), b"\x1b[A")
        self.assertFalse(terminal.shift)

    def test_bytes_read_back_as_extended_keys(self):
        terminal = self.terminal()
        data = (terminal._windows_key(True, "\x00", 0x10, 0x10) + terminal._windows_key(True, "\r", 13, 0x10)
                + terminal._windows_key(False, "\x00", 0x10, 0) + terminal._windows_key(True, "", 0x25, 0x10))
        self.assertEqual(feed(terminal, data),
                         [("shift", True), ("shift-enter", True), ("release:shift", False), ("left", True)])

    def records(self, extended, events):
        """Run _read_windows_event over fake console records; the bytes it queues."""
        try:
            import ctypes
            from ctypes import wintypes
        except (ImportError, ValueError):
            self.skipTest("ctypes.wintypes is unavailable")

        # The same layout _enter_windows declares for ReadConsoleInputW.
        class Key(ctypes.Structure):
            _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD), ("virtual", wintypes.WORD),
                        ("scan", wintypes.WORD), ("character", wintypes.WCHAR), ("control", wintypes.DWORD)]

        class Event(ctypes.Union):
            _fields_ = [("key", Key), ("padding", ctypes.c_byte * 16)]

        class Record(ctypes.Structure):
            _fields_ = [("kind", wintypes.WORD), ("event", Event)]

        queue = list(events)

        class Kernel:
            def GetNumberOfConsoleInputEvents(self, handle, count):
                count._obj.value = len(queue)
                return 1

            def ReadConsoleInputW(self, handle, record, size, count):
                kind, down, character, virtual, control = queue.pop(0)
                record._obj.kind = kind
                record._obj.event.key = Key(down, 1, virtual, 0, character, control)
                count._obj.value = 1
                return 1

        terminal = Terminal()
        terminal.extended = extended
        terminal.kernel = Kernel()
        terminal.input_handle = None
        terminal._console_record = Record
        terminal._console_pending = b""
        terminal._surrogate = ""
        while terminal._read_windows_event(False):
            pass
        return terminal, terminal._console_pending

    # kind, down, character, virtual key, control state (0x10 Shift)
    EVENTS = [(1, True, "\x00", 0x10, 0x10), (1, True, "A", 65, 0x10), (1, False, "A", 65, 0x10),
              (2, True, "\x00", 0, 0), (1, False, "\x00", 0x10, 0), (1, True, "\x00", 0xa1, 0x10),
              (1, True, "\r", 13, 0x10), (1, False, "\r", 13, 0x10), (1, False, "\x00", 0xa1, 0),
              (1, True, "b", 66, 0), (1, False, "b", 66, 0)]

    def test_event_loop_without_extended_skips_shift_and_releases(self):
        terminal, data = self.records(False, self.EVENTS)
        self.assertEqual(data, b"A\x1b\rb")
        self.assertIsNone(terminal.shift)

    def test_event_loop_with_extended_reports_shift(self):
        terminal, data = self.records(True, self.EVENTS)
        self.assertEqual(data, b"\x1b[57441uA\x1b[57441;1:3u\x1b[57441u\x1b\r\x1b[57441;1:3ub")
        self.assertFalse(terminal.shift)
        self.assertEqual([key for key, _ in feed(terminal, data)],
                         ["shift", "A", "release:shift", "shift", "shift-enter", "release:shift", "b"])


if __name__ == "__main__":
    unittest.main()
