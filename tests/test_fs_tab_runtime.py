import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import platform_runtime as runtime


class NativeTabTests(unittest.TestCase):
    def test_polling_is_extended_only_and_memoized(self):
        reader = Mock(side_effect=[False, True, True, False])
        terminal = runtime.Terminal()
        self.assertIsNone(terminal.tab_pressed())
        terminal.extended = True
        with patch.object(runtime, "_tab_reader", return_value=reader) as bind:
            self.assertEqual([terminal.tab_pressed() for _ in range(4)], [False, True, True, False])
        bind.assert_called_once_with()

    def test_macos_reads_tab_virtual_key_state(self):
        with patch.dict(runtime.os.environ, {}, clear=True), patch.object(runtime.sys, "platform", "darwin"), \
                patch.object(ctypes, "CDLL") as library:
            state = library.return_value.CGEventSourceKeyState
            state.side_effect = [False, True, False]
            read = runtime._tab_reader()
            self.assertEqual([read(), read(), read()], [False, True, False])
            self.assertEqual(state.call_args.args, (0, 48))

    def test_windows_reads_current_tab_state_not_since_last_call_bit(self):
        with patch.dict(runtime.os.environ, {}, clear=True), patch.object(runtime.sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", create=True) as library:
            state = library.return_value.GetAsyncKeyState
            state.side_effect = [1, 0x8000, 0]
            read = runtime._tab_reader()
            self.assertEqual([read(), read(), read()], [False, True, False])
            self.assertEqual(state.call_args.args, (0x09,))

    def test_ssh_and_unavailable_apis_fall_back_to_terminal_events(self):
        with patch.dict(runtime.os.environ, {"SSH_TTY": "/dev/pts/1"}), patch.object(ctypes, "CDLL") as library:
            self.assertIsNone(runtime._tab_reader())
            library.assert_not_called()
        with patch.dict(runtime.os.environ, {}, clear=True), patch.object(runtime.sys, "platform", "darwin"), \
                patch.object(ctypes, "CDLL", side_effect=OSError):
            self.assertIsNone(runtime._tab_reader())

    def test_reader_failure_is_memoized_as_unavailable(self):
        terminal = runtime.Terminal()
        terminal.extended = True
        reader = Mock(side_effect=OSError("unavailable"))
        terminal._tab_reader = reader
        self.assertIsNone(terminal.tab_pressed())
        self.assertIsNone(terminal.tab_pressed())
        reader.assert_called_once_with()


class TabEventTests(unittest.TestCase):
    def test_windows_records_track_tab_press_and_release(self):
        terminal = runtime.Terminal()
        terminal.extended = True
        terminal.tab = None
        terminal._windows_key(True, "\t", 9, 0)
        self.assertTrue(terminal.tab)
        terminal._windows_key(False, "", 9, 0)
        self.assertFalse(terminal.tab)

    def test_legacy_raw_tab_press_updates_state_when_extended(self):
        terminal = runtime.Terminal()
        terminal.extended = True
        terminal.tab = None
        terminal._byte = Mock(return_value=b"\t")
        self.assertEqual(terminal.read_key(), "\t")
        self.assertTrue(terminal.tab)

    def test_kitty_tab_press_and_release_track_state(self):
        terminal = runtime.Terminal()
        terminal.extended = True
        terminal.tab = None
        terminal._byte = Mock(side_effect=[b"\x1b", b"[", b"9", b"u"])
        terminal.wait = Mock(return_value=True)
        self.assertEqual(terminal.read_key(), "\t")
        self.assertTrue(terminal.tab)
        terminal._byte = Mock(side_effect=[b"\x1b", b"[", b"9", b";", b"1", b":", b"3", b"u"])
        terminal.wait = Mock(return_value=True)
        self.assertEqual(terminal.read_key(), "release:\t")
        self.assertFalse(terminal.tab)


if __name__ == "__main__":
    unittest.main()
