import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap
import herdr_ai
import herdr_sidebar as sidebar
import preferences
import team


class SidebarTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        for p in (patch.object(preferences, "PATH", self.path / "preferences.json"),
                  patch.object(preferences, "_values", None),
                  patch.object(preferences, "SETTINGS", list(preferences.SETTINGS)),
                  patch("herdr_team_status.ensure_running"),
                  patch.object(sidebar, "state_dir", return_value=self.path)):
            p.start()
            self.addCleanup(p.stop)
        for setting in sidebar.SETTINGS:
            preferences.register(setting)

    def test_grouped_filter_preserves_agents_and_does_not_change_lifecycle(self):
        preferences.put(sidebar.ORDER.key, "default")
        call = Mock(return_value={})
        sidebar.sync(socket_path="fake.sock", call=call, migrate=False)
        method, params = call.call_args.args
        self.assertEqual(method, "agent.view.set")
        self.assertEqual(params["filter"], {"op": "not", "filter": {
            "op": "eq", "field": {"token": sidebar.ROLE}, "value": "crew"}})
        self.assertEqual(params["sort"], [{"field": field, "order": "asc"}
                                         for field in ("workspace_order", "tab_order", "pane_order")])
        preferences.put(sidebar.HIDE_CREW.key, False)
        sidebar.sync(socket_path="fake.sock", call=call, migrate=False)
        self.assertNotIn("filter", call.call_args.args[1])
        self.assertNotIn("label", call.call_args.args[1])
        self.assertFalse(any(c.args[0] in ("pane.release_agent", "pane.close", "pane.report_agent")
                             for c in call.call_args_list))

    def test_priority_and_crew_hiding_can_be_enabled_independently(self):
        call = Mock(return_value={})
        for hide, priority in ((True, True), (False, True), (True, False), (False, False)):
            with self.subTest(hide=hide, priority=priority):
                preferences.put(sidebar.HIDE_CREW.key, hide)
                preferences.put(sidebar.ORDER.key, "priority" if priority else "default")
                sidebar.sync(socket_path="fake.sock", call=call, migrate=False)
                method, params = call.call_args.args
                self.assertEqual(method, "agent.view.set")
                self.assertEqual("filter" in params, hide)
                self.assertIn("sort", params)
                if priority:
                    self.assertEqual(params["sort"], [
                        {"field": {"token": "herdr_ai_priority"}, "order": "desc"},
                        {"field": "attention", "order": "desc"},
                        {"field": "state_change_seq", "order": "desc"}])
                self.assertNotIn("label", params)

    def test_priority_defaults_on_and_order_setting_applies_live_from_menu(self):
        self.assertEqual(preferences.get(sidebar.ORDER.key), "priority")
        terminal = MagicMock()
        terminal.wait.return_value = True
        terminal.read_key.side_effect = ["down", "left", "esc"]
        with patch.object(team, "Terminal", return_value=terminal), \
             patch.object(team.ap, "Screen", return_value=Mock()), \
             patch.object(team.sys, "stdout", io.StringIO()), \
             patch.object(sidebar, "sync") as sync:
            self.assertIsNone(team.choose(settings_only=True))
        self.assertTrue(preferences.get(sidebar.HIDE_CREW.key))
        self.assertEqual(preferences.get(sidebar.ORDER.key), "default")
        sync.assert_called_once()

    def test_setting_persists_and_scoped_menu_does_not_change_other_settings(self):
        preferences.put("unrelated", "keep")
        menu = preferences.Menu([sidebar.HIDE_CREW])
        menu.handle("left")
        self.assertFalse(preferences.get(sidebar.HIDE_CREW.key))
        self.assertEqual(menu.handle("esc"), "close")
        preferences._values = None
        self.assertFalse(preferences.get(sidebar.HIDE_CREW.key))
        self.assertEqual(preferences.get("unrelated"), "keep")

    def test_saved_layout_migration_checks_server_and_terminal_identity(self):
        call = Mock()
        state = {"fake.sock|tab": {"team": "Codex", "panes": {
            "lead": "lead", "crew1": "crew", "crew2": "reused"},
            "terminals": {"lead": "a", "crew": "b", "reused": "old"}},
            "other.sock|tab": {"team": "Codex", "panes": {"crew1": "other"}, "terminals": {"other": "d"}}}
        live = {"lead": {"terminal_id": "a"}, "crew": {"terminal_id": "b"},
                "reused": {"terminal_id": "new"}, "other": {"terminal_id": "d"}}
        sidebar.restore_roles(state, "fake.sock", live, call)
        self.assertEqual({c.args[1]["pane_id"] for c in call.call_args_list}, {"lead", "crew"})

    def test_existing_outside_team_is_migrated_by_exact_brief(self):
        live = {p: {"agent": "agy", "tab_id": "tab"} for p in ("lead", "a", "b", "c", "d")}
        brief = "You lead a herdr team. Four agents wait in the herdr panes a, b, c, d on this tab, in your working directory."
        call = Mock(side_effect=lambda method, params: {"process_info": {
            "foreground_processes": [{"argv": ["agy", brief]}]}} if params.get("pane_id") == "lead" else {})
        sidebar.legacy_roles(live, call)
        tags = [c.args[1] for c in call.call_args_list if c.args[0] == "pane.report_metadata"]
        self.assertEqual(len(tags), 5)
        self.assertEqual([t["pane_id"] for t in tags if t["tokens"][sidebar.ROLE] == "crew"], ["a", "b", "c", "d"])
        live["d"]["tab_id"] = "elsewhere"
        call.reset_mock()
        sidebar.legacy_roles(live, call)
        self.assertFalse(any(c.args[0] == "pane.report_metadata" for c in call.call_args_list))

    def test_role_registry_restores_moved_panes_and_excludes_reused_ids(self):
        registry = self.path / "herdr-ai-roles.json"
        registry.write_text(json.dumps({"fake.sock": {"stable-terminal": "crew", "gone-terminal": "crew"}}))
        live = [{"pane_id": "moved", "terminal_id": "stable-terminal", "tokens": {}},
                {"pane_id": "reused", "terminal_id": "new-terminal", "tokens": {}}]
        def call(method, params):
            if method == "pane.list":
                return {"panes": live}
            if method == "pane.report_metadata":
                next(p for p in live if p["pane_id"] == params["pane_id"])["tokens"].update(params["tokens"])
            return {}
        sidebar.sync(socket_path="fake.sock", call=call)
        self.assertEqual(live[0]["tokens"][sidebar.ROLE], "crew")
        self.assertEqual(live[1]["tokens"], {})
        saved = json.loads(registry.read_text())["fake.sock"]
        self.assertEqual(list(saved), ["stable-terminal"])
        self.assertEqual(saved["stable-terminal"]["role"], "crew")

    def test_startup_restores_roles_when_herdr_allocates_new_terminal_ids(self):
        registry = self.path / "herdr-ai-roles.json"
        original = {"fake.sock": {"old-terminal": {"role": "crew", "pane_id": "crew", "tab_id": "tab"}}}
        live = [{"pane_id": "crew", "tab_id": "tab", "terminal_id": "new-terminal", "tokens": {}}]
        def call(method, params):
            if method == "pane.list":
                return {"panes": live}
            if method == "pane.report_metadata":
                live[0]["tokens"].update(params["tokens"])
            return {}
        registry.write_text(json.dumps(original))
        sidebar.sync(socket_path="fake.sock", call=call)
        self.assertEqual(live[0]["tokens"], {})
        registry.write_text(json.dumps(original))
        sidebar.sync(socket_path="fake.sock", call=call, restore=True)
        self.assertEqual(live[0]["tokens"][sidebar.ROLE], "crew")

    def test_startup_manifest_uses_local_python_and_only_syncs(self):
        with patch.object(sidebar.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            sidebar.install_startup("fake-herdr")
            sidebar.install_startup("fake-herdr")
        self.assertEqual(run.call_count, 1)
        text = (self.path / "herdr-ai-plugin/herdr-plugin.toml").read_text()
        self.assertIn(json.dumps([sys.executable, str(ROOT / "src/herdr_ai.py"), "--sync-sidebar"]), text)
        self.assertNotIn("--setup", text)
        self.assertIn('placement = "popup"', text)
        self.assertIn(json.dumps([sys.executable, str(ROOT / "src/herdr_ai.py"), "--settings"]), text)
        self.assertIn('title = "Herdr AI settings"', text)

    def test_settings_action_opens_popup_without_routing_or_launching_agents(self):
        with patch.object(sidebar, "request") as request, \
             patch.object(herdr_ai, "build") as build, patch.object(herdr_ai, "route") as route:
            self.assertEqual(herdr_ai.main(["--open-settings"]), 0)
        request.assert_called_once_with("plugin.pane.open", {
            "plugin_id": "local.herdr-ai", "entrypoint": "settings"})
        build.assert_not_called()
        route.assert_not_called()

    def test_windows_pipe_payload_is_structured_and_bounded(self):
        def run(args, **kwargs):
            data = json.loads(kwargs["input"])
            req = json.loads(data["payload"])
            self.assertEqual(data["socket"], "fake-pipe")
            self.assertEqual(req["params"], {"source": 'text with "quotes"'})
            self.assertEqual(kwargs["timeout"], 5)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"id": req["id"], "result": {"active": False}}))
        with patch.object(sidebar, "WINDOWS", True), patch.object(sidebar.subprocess, "run", side_effect=run):
            self.assertEqual(sidebar.request("agent.view.clear", {"source": 'text with "quotes"'},
                                            r"\\.\pipe\fake-pipe"), {"active": False})

    @unittest.skipIf(os.name == "nt", "Unix transport")
    def test_socket_transport_ignores_events_and_reports_api_errors(self):
        # Short macOS Unix socket path, below sockaddr_un's 104-byte limit.
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            path = str(Path(folder) / "api.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen()
                def answer():
                    with server.accept()[0] as client:
                        req = json.loads(client.makefile("rb").readline())
                        client.sendall(b'{"event":"irrelevant"}\n')
                        client.sendall((json.dumps({"id": req["id"], "error": {"message": "unsupported view"}}) + "\n").encode())
                worker = threading.Thread(target=answer)
                worker.start()
                try:
                    with self.assertRaisesRegex(RuntimeError, "unsupported view"):
                        sidebar.request("agent.view.set", {}, path)
                finally:
                    worker.join(timeout=3)

    @unittest.skipUnless(os.name == "nt", "Windows named pipe transport")
    def test_native_windows_named_pipe_round_trip(self):
        name = "herdr-ai-test-" + uuid.uuid4().hex
        script = r'''
$server = [IO.Pipes.NamedPipeServerStream]::new($env:HERDR_AI_TEST_PIPE, [IO.Pipes.PipeDirection]::InOut)
try {
    $server.WaitForConnection()
    $reader = [IO.StreamReader]::new($server)
    $writer = [IO.StreamWriter]::new($server, [Text.UTF8Encoding]::new($false), 4096, $true)
    $writer.AutoFlush = $true
    $request = $reader.ReadLine() | ConvertFrom-Json
    $writer.WriteLine((@{id=$request.id; result=@{active=$true}} | ConvertTo-Json -Compress))
} finally { $server.Dispose() }
'''
        server = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                  env={**os.environ, "HERDR_AI_TEST_PIPE": name},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(sidebar.request("agent.view.set", {}, name), {"active": True})
            _, error = server.communicate(timeout=5)
            self.assertEqual(server.returncode, 0, error)
        finally:
            if server.poll() is None:
                server.kill()
                server.communicate()

    def test_settings_screen_uses_shared_renderer_at_various_sizes(self):
        menu = preferences.Menu([sidebar.HIDE_CREW])
        for cols, rows in ((90, 24), (50, 14), (25, 7)):
            lines = ap.render_settings(menu, cols, rows, 0, team.STANDARD)
            text = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))
            self.assertIn("Hide crew agents", text)
            self.assertNotIn("Usage panel", text)
            self.assertLessEqual(len(lines), rows)

    def test_settings_only_exits_without_launch_or_usage_fetch(self):
        terminal = MagicMock()
        terminal.wait.return_value = True
        terminal.read_key.side_effect = ["left", "esc"]
        with patch.object(team, "Terminal", return_value=terminal), \
             patch.object(team.ap, "Screen", return_value=Mock()), \
             patch.object(team, "refresh_usage") as refresh, \
             patch.object(team.sys, "stdout", io.StringIO()), \
             patch.object(sidebar, "sync") as sync:
            self.assertIsNone(team.choose(settings_only=True))
        self.assertFalse(preferences.get(sidebar.HIDE_CREW.key))
        sync.assert_called_once()
        refresh.assert_not_called()

    def test_bars_setting_is_in_herdr_ai_settings_without_resyncing_sidebar(self):
        terminal = MagicMock()
        terminal.wait.return_value = True
        terminal.read_key.side_effect = ["down", "down", "right", "esc"]
        with patch.object(team, "Terminal", return_value=terminal), \
             patch.object(team.ap, "Screen", return_value=Mock()), \
             patch.object(team.sys, "stdout", io.StringIO()), \
             patch.object(sidebar, "sync") as sync:
            self.assertIsNone(team.choose(settings_only=True))
        self.assertEqual(preferences.get(ap.BARS.key), "used")
        self.assertFalse(ap.depleting())
        sync.assert_not_called()

    def test_change_saved_by_another_command_is_seen_and_kept(self):
        preferences.put("usage.bars", "left")
        self.assertTrue(ap.depleting())
        # Another command (the usage panel, herdr-ai) saves while this one runs.
        saved = json.loads(preferences.PATH.read_text())
        saved.update({"usage.bars": "used", "usage.theme": "mono"})
        preferences.PATH.write_text(json.dumps(saved))
        os.utime(preferences.PATH, ns=(1, 1))
        with patch.object(preferences, "_checked", 0.0):
            self.assertFalse(ap.depleting())
        preferences.put(sidebar.HIDE_CREW.key, False)   # must not drop the other command's values
        on_disk = json.loads(preferences.PATH.read_text())
        self.assertEqual((on_disk["usage.bars"], on_disk["usage.theme"]), ("used", "mono"))

    def test_settings_cli_never_builds_or_routes(self):
        with patch.object(herdr_ai.sys.stdin, "isatty", return_value=True), \
             patch.object(herdr_ai.sys.stdout, "isatty", return_value=True), \
             patch.object(herdr_ai, "sidebar_update"), patch.object(team, "choose") as choose, \
             patch.object(herdr_ai, "build") as build, patch.object(herdr_ai, "route") as route:
            self.assertEqual(herdr_ai.main(["--settings"], herdr="fake-herdr"), 0)
        choose.assert_called_once_with(settings_only=True)
        build.assert_not_called()
        route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
