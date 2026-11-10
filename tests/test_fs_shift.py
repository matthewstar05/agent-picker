import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import fs_browser as fsb
import platform_runtime as runtime
import preferences
import test_fs_browser as fixtures


class ShiftHighlightTests(fixtures.Base):
    def test_stationary_selection_stays_highlighted_until_shift_is_released(self):
        app = self.app()
        held = Mock(return_value=False)
        app.terminal.shift_pressed = held
        selected = app.selected()
        with patch.object(fsb.time, "monotonic", return_value=10):
            app.last_frame = 9
            self.draw(app)
            self.assertEqual(app.boost, 0)
        held.return_value = True
        for now in (11, 12, 60):
            with patch.object(fsb.time, "monotonic", return_value=now):
                self.draw(app)
                self.assertEqual(app.boost, 1)
                self.assertIs(app.selected(), selected)
        held.return_value = False
        with patch.object(fsb.time, "monotonic", return_value=61):
            self.draw(app)
            self.assertEqual(app.boost, 0)

    def test_moving_selection_while_holding_shift_does_not_expire_the_highlight(self):
        app = self.app()
        app.terminal.shift_pressed = lambda: True
        self.draw(app)
        selected = app.selected()
        app.handle("right")
        with patch.object(fsb.time, "monotonic", return_value=app.last_frame + 10):
            self.draw(app)
            self.assertIsNot(app.selected(), selected)
            self.assertEqual(app.boost, 1)

    def test_modifier_polling_remains_responsive_with_animation_off(self):
        app = self.app()
        preferences.put(fsb.ANIMATE.key, False)
        app.terminal.shift_pressed = lambda: False
        self.draw(app)
        self.assertEqual(app.frame_step(), 0.05)
        app.terminal.shift_pressed = lambda: True
        with patch.object(fsb.time, "monotonic", return_value=app.last_frame + 1):
            self.draw(app)
            self.assertEqual(app.boost, 1)
            self.assertEqual(app.frame_step(), 0.05)
        app.handle("focus-out")
        self.assertFalse(app.shift_held())
        with patch.object(fsb.time, "monotonic", return_value=app.last_frame + 1):
            self.draw(app)
            self.assertEqual(app.boost, 0)
            self.assertEqual(app.frame_step(), 1)

    def test_terminal_shift_events_still_work_without_native_state(self):
        app = self.app()
        app.terminal.shift_pressed = lambda: None
        app.terminal.shift = True
        app.handle("shift")
        with patch.object(fsb.time, "monotonic", return_value=1000):
            self.assertTrue(app.shift_held())
        app.terminal.shift = False
        app.handle("release:shift")
        self.assertFalse(app.shift_held())


class NativeShiftTests(unittest.TestCase):
    def test_polling_binds_once_and_reads_press_hold_release(self):
        reader = Mock(side_effect=[False, True, True, False])
        terminal = runtime.Terminal()
        self.assertIsNone(terminal.shift_pressed())
        terminal.extended = True
        with patch.object(runtime, "_shift_reader", return_value=reader) as bind:
            self.assertEqual([terminal.shift_pressed() for _ in range(4)], [False, True, True, False])
        bind.assert_called_once_with()

    def test_macos_reads_only_the_shift_flag(self):
        with patch.dict(runtime.os.environ, {}, clear=True), patch.object(runtime.sys, "platform", "darwin"), \
                patch.object(ctypes, "CDLL") as library:
            flags = library.return_value.CGEventSourceFlagsState
            flags.side_effect = [0, 1 << 17, 1 << 18]
            read = runtime._shift_reader()
            self.assertEqual([read(), read(), read()], [False, True, False])
            self.assertEqual(flags.call_args.args, (0,))

    def test_windows_reads_current_shift_state_not_the_since_last_call_bit(self):
        with patch.dict(runtime.os.environ, {}, clear=True), patch.object(runtime.sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", create=True) as library:
            state = library.return_value.GetAsyncKeyState
            state.side_effect = [1, 0x8000, 0]
            read = runtime._shift_reader()
            self.assertEqual([read(), read(), read()], [False, True, False])
            self.assertEqual(state.call_args.args, (0x10,))

    def test_remote_sessions_and_unavailable_apis_keep_terminal_event_fallback(self):
        with patch.dict(runtime.os.environ, {"SSH_CONNECTION": "remote"}), patch.object(ctypes, "CDLL") as library:
            self.assertIsNone(runtime._shift_reader())
            library.assert_not_called()
        with patch.dict(runtime.os.environ, {}, clear=True), patch.object(runtime.sys, "platform", "darwin"), \
                patch.object(ctypes, "CDLL", side_effect=OSError):
            self.assertIsNone(runtime._shift_reader())


if __name__ == "__main__":
    unittest.main()
