"""Directory assignment keeps the browser and the other panel slots in place."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_fs_browser as fixtures
import fs_browser as fsb
import preferences


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class PanelAssignmentTests(fixtures.Base):
    def menu(self, app):
        with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}):
            app.handle("\t")
            app.handle("\r")
        self.assertEqual(app.screen, "assign")

    def test_tab_enter_opens_numbered_menu_without_opening_folder(self):
        app = self.app()
        selected = app.selected()
        self.menu(app)
        self.assertEqual(app.cwd, self.tree)
        self.assertIs(app.selected(), selected)
        text = "\n".join(self.draw(app, 100, 16))
        self.assertIn("1 left", text)
        self.assertIn("2 middle", text)
        self.assertIn("3 right", text)
        self.assertIn("4 left", text)
        self.assertIn("5 right", text)
        self.assertIn("alpha", text)

    def test_every_number_routes_only_requested_panel_without_navigating(self):
        for panel in range(1, 6):
            with self.subTest(panel=panel):
                app = self.app()
                selected = app.selected()
                self.menu(app)
                with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}), \
                        patch.object(fsb.threading, "Thread", InlineThread), \
                        patch.object(fsb.subprocess, "run", return_value=SimpleNamespace(
                            returncode=0, stdout="assigned\n", stderr="")) as run:
                    app.handle(str(panel))
                    app.routed()
                self.assertEqual(run.call_args.args[0][1:], [str(fsb.HERDR_AI), "--assign-panel", str(panel)])
                self.assertEqual(run.call_args.kwargs["cwd"], str(self.tree / "alpha"))
                self.assertEqual(app.cwd, self.tree)
                self.assertIs(app.selected(), selected)
                self.assertEqual((app.back, app.forward), ([], []))
                self.assertEqual(app.screen, "browse")
                self.assertIn(f"panel {panel} assigned", app.toast[0])

    def test_cancel_and_invalid_number_never_assign_or_navigate(self):
        app = self.app()
        self.menu(app)
        with patch.object(app, "route") as route:
            for key in ("0", "6", "up", "\r", "\t"):
                app.handle(key)
                self.assertEqual(app.screen, "assign")
            app.handle("esc")
            route.assert_not_called()
        self.assertEqual(app.cwd, self.tree)
        self.assertEqual(app.screen, "browse")
        self.assertIsNone(app.assignment_target)

    def test_release_and_focus_loss_do_not_leave_tab_chord_stuck(self):
        for key in ("release:\t", "focus-out"):
            app = self.app()
            app.handle("\t")
            app.handle(key)
            app.handle("focus-in")
            app.handle("\r")
            self.assertEqual(app.cwd, self.tree / "alpha")
            self.assertEqual(app.screen, "browse")

    def test_file_assignment_uses_current_folder(self):
        app = self.app()
        app.index = self.names(app).index("notes.md")
        self.menu(app)
        self.assertEqual(app.assignment_target, self.tree)

    def test_assignment_failure_reports_error_without_layout_confirmation(self):
        app = self.app()
        app.routing = {"target": self.tree / "alpha", "panel": 1,
                       "result": (3, "herdr-ai: no saved layout")}
        app.routed()
        self.assertEqual(app.cwd, self.tree)
        self.assertEqual(app.screen, "browse")
        self.assertIsNone(app.confirm)
        self.assertIn("no saved layout", app.toast[0])

    def test_outside_herdr_assignment_never_creates_workspace(self):
        app = self.app()
        app.handle("\t")
        app.handle("\r")
        self.assertEqual(app.cwd, self.tree)
        self.assertEqual(app.screen, "browse")
        self.assertIsNone(app.confirm)
        self.assertIn("existing herdr-ai tab", app.toast[0])

    def test_menu_fits_small_and_large_panes(self):
        app = self.app()
        self.menu(app)
        for cols, rows in ((18, 4), (30, 8), (60, 12), (120, 24)):
            self.draw(app, cols, rows)

    def test_native_tab_held_opens_menu_without_tab_event(self):
        app = self.app()
        app.terminal.tab_pressed = lambda: True
        with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}):
            app.handle("\r")
        self.assertEqual(app.screen, "assign")
        self.assertEqual(app.cwd, self.tree)

    def test_holding_tab_has_shift_highlight_until_release(self):
        app = self.app()
        held = Mock(return_value=True)
        app.terminal.tab_pressed = held
        preferences.put(fsb.ANIMATE.key, False)
        app.last_frame = 9   # the patched clock starts here, not at the real monotonic time
        for now in (10, 50):
            with patch.object(fsb.time, "monotonic", return_value=now):
                self.draw(app)
                self.assertEqual(app.boost, 1)
                self.assertEqual(app.frame_step(), 0.05)
        held.return_value = False
        with patch.object(fsb.time, "monotonic", return_value=51):
            self.draw(app)
            self.assertEqual(app.boost, 0)


if __name__ == "__main__":
    unittest.main()
