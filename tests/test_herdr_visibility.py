"""Hidden tabs must stop render work, including fs's Always on animation mode."""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent_picker as picker
import fs_browser as fsb
import herdr_visibility as visibility
import preferences
import usage_panel
import test_fs_browser as fixtures


ENV = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/fixture", "HERDR_PANE_ID": "w:p1",
       "HERDR_TAB_ID": "w:t1", "HERDR_WORKSPACE_ID": "w"}


def snapshot(tab="w:t1", workspace="w", pane_tab="w:t1", pane="w:p1"):
    return {"focused_tab_id": tab, "focused_workspace_id": workspace,
            "panes": [{"pane_id": pane, "terminal_id": "terminal", "tab_id": pane_tab, "workspace_id": "w"}]}


class VisibilityTests(unittest.TestCase):
    def test_unmanaged_terminals_do_not_start_a_listener(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(visibility.socket, "socket") as connect:
            watch = visibility.Visibility().start()
            self.assertTrue(watch.visible)
            watch.close()
        connect.assert_not_called()

    def test_selected_tab_is_visible_even_when_another_pane_has_focus(self):
        with patch.dict(os.environ, ENV):
            watch = visibility.Visibility()
        data = snapshot()
        data["focused_pane_id"] = "w:p2"
        watch.snapshot(data)
        self.assertTrue(watch.visible)
        watch.snapshot(snapshot(tab="w:t2"))
        self.assertFalse(watch.visible)
        watch.snapshot(snapshot(workspace="another-workspace"))
        self.assertFalse(watch.visible)
        watch.snapshot(snapshot())
        self.assertTrue(watch.visible)

    def test_a_moved_pane_follows_its_terminal_identity(self):
        with patch.dict(os.environ, ENV):
            watch = visibility.Visibility()
        watch.snapshot(snapshot())
        watch.snapshot(snapshot(tab="w:t2", pane_tab="w:t2", pane="w:p99"))
        self.assertEqual(watch.pane, "w:p99")
        self.assertTrue(watch.visible)
        watch.snapshot(snapshot(pane_tab="w:t2", pane="w:p99"))
        self.assertFalse(watch.visible)

    def test_unavailable_api_falls_back_without_spinning_or_leaving_a_thread(self):
        with patch.dict(os.environ, ENV), \
                patch.object(visibility.socket, "AF_UNIX", getattr(socket, "AF_UNIX", 1), create=True), \
                patch.object(visibility.socket, "socket", side_effect=OSError("offline")) as connect:
            watch = visibility.Visibility().start()
            deadline = time.monotonic() + 1
            while not watch.visible and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(watch.visible)
            time.sleep(.05)
            self.assertEqual(connect.call_count, 1)
            watch.close()
        self.assertFalse(watch._thread.is_alive())

    def test_platform_without_unix_sockets_keeps_its_terminal_usable(self):
        transport = SimpleNamespace(socket=Mock())
        with patch.dict(os.environ, ENV), patch.object(visibility, "socket", transport):
            watch = visibility.Visibility().start()
            self.assertFalse(watch.enabled)
            self.assertTrue(watch.visible)
            watch.close()
        transport.socket.assert_not_called()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Herdr uses Unix sockets")
    def test_focus_events_update_visibility_without_idle_polling_and_close_unblocks_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "api.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen()
            stop = threading.Event()
            subscribed = threading.Event()
            clients, requests, failures = [], [], []
            state = [snapshot(tab="w:t2")]

            def serve():
                try:
                    while not stop.is_set():
                        client, _ = server.accept()
                        with client:
                            with client.makefile("rb") as stream:
                                line = stream.readline()
                            if not line:
                                continue
                            req = json.loads(line)
                            requests.append(req["method"])
                            if req["method"] == "events.subscribe":
                                # Retain a duplicate for event writes, just as a subscription stream does.
                                clients.append(client.dup())
                                client.sendall((json.dumps({"id": req["id"], "result": {"type": "subscribed"}}) + "\n").encode())
                                subscribed.set()
                            else:
                                client.sendall((json.dumps({"id": req["id"], "result": {"snapshot": state[0]}}) + "\n").encode())
                except OSError as exc:
                    if not stop.is_set():
                        failures.append(exc)

            worker = threading.Thread(target=serve)
            worker.start()
            with patch.dict(os.environ, dict(ENV, HERDR_SOCKET_PATH=path)):
                watch = visibility.Visibility().start()
            try:
                self.assertTrue(subscribed.wait(2))
                deadline = time.monotonic() + 2
                while not watch.known and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(watch.known)
                self.assertFalse(watch.visible)
                before = len(requests)
                time.sleep(.05)
                self.assertEqual(len(requests), before, "No snapshot polling on an idle event stream")
                for tab, expected in (("w:t1", True), ("w:t2", False), ("w:t1", True)):
                    state[0] = snapshot(tab=tab)
                    clients[0].sendall(b'{"event":"tab_focused","data":{"type":"tab_focused"}}\n')
                    deadline = time.monotonic() + 2
                    while watch.visible != expected and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertIs(watch.visible, expected)
                self.assertEqual(requests.count("events.subscribe"), 1)
                self.assertEqual(requests.count("session.snapshot"), 4)
                clients[0].shutdown(socket.SHUT_RDWR)
                deadline = time.monotonic() + 1
                while watch.visible and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(watch.visible)
                deadline = time.monotonic() + 2
                while not watch.visible and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(watch.visible)
                self.assertEqual(requests.count("events.subscribe"), 2)
            finally:
                watch.close()
                stop.set()
                # Wake the accept without leaving a worker running at interpreter shutdown.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                    wake.connect(path)
                worker.join(2)
                for client in clients:
                    client.close()
                server.close()
            self.assertFalse(watch._thread.is_alive())
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])


class FilesystemVisibilityTests(fixtures.Base):
    def test_hidden_tab_overrides_always_on_and_busy_animations(self):
        app = self.app()
        app.visibility = SimpleNamespace(visible=False)
        app.terminal.shift_pressed = Mock(return_value=True)
        preferences.put(fsb.ANIMATE.key, "always")
        app.routing = object()
        self.assertFalse(app.animating())
        self.assertFalse(app.shift_held())
        app.terminal.shift_pressed.assert_not_called()
        self.assertEqual(app.frame_step(), 1)
        app.visibility.visible = True
        self.assertTrue(app.animating())
        self.assertEqual(app.frame_step(), 1 / 30)

    def test_hidden_loop_never_renders_or_scans_and_resumes_on_selection(self):
        app = self.app()
        watched = SimpleNamespace(visible=False)
        app.visibility = watched
        counts = {"wait": 0}
        clock = [0.0]
        terminal = Mock()
        terminal.extended = False
        terminal.read_key.return_value = None

        def wait(seconds):
            counts["wait"] += 1
            if counts["wait"] <= 2:
                self.assertAlmostEqual(seconds, 1)
                frame.assert_not_called()
                scan.assert_not_called()
            if counts["wait"] == 2:
                watched.visible = True
            if counts["wait"] == 3:
                raise KeyboardInterrupt
            clock[0] += seconds
            return False

        terminal.wait.side_effect = wait
        with patch.object(fsb, "Terminal", return_value=terminal), patch.object(app, "enter"), \
                patch.object(app, "leave"), patch.object(app, "frame", return_value=[]) as frame, \
                patch.object(app, "watch") as scan, patch.object(app, "routed"), \
                patch.object(fsb.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(fsb, "SegmentScreen") as writer:
            app.loop()
        frame.assert_called_once()
        writer.return_value.show.assert_called_once()


class PickerVisibilityTests(unittest.TestCase):
    def test_hidden_usage_panel_does_not_render_and_resumes_after_switch(self):
        watched = SimpleNamespace(visible=False, start=Mock(), close=Mock())
        waits = []
        terminal = MagicMock()
        terminal.fd = 0
        terminal.read_key.return_value = "q"

        def wait(seconds):
            waits.append(seconds)
            if len(waits) <= 2:
                self.assertGreater(seconds, .9)
                render.assert_not_called()
            if len(waits) == 2:
                watched.visible = True
            return len(waits) == 3

        terminal.wait.side_effect = wait
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(preferences, "PATH", Path(folder) / "preferences.json"), \
                patch.object(preferences, "_values", {}), \
                patch.object(picker.VISIBILITY, "children", list(picker.VISIBILITY.children)), \
                patch.object(picker, "STATE", Path(folder)), \
                patch.object(picker, "installed", return_value=True), \
                patch.object(picker, "Visibility", return_value=watched), \
                patch.object(picker, "Terminal", return_value=terminal), \
                patch.object(picker, "Screen"), patch.object(picker, "load_usage"), \
                patch.object(picker.model_catalog, "request_all"), patch.object(usage_panel, "start"), \
                patch.object(usage_panel, "render", return_value=[]) as render, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(picker.choose("usage"))
        render.assert_called_once()
        watched.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
