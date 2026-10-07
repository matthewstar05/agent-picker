import copy
from contextlib import ExitStack
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as picker
import provider_defaults as defaults
import settings
import model_catalog


class DefaultsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.refresh = patch.dict(os.environ, AGENT_PICKER_NO_MODEL_REFRESH="1", AGENT_PICKER_CONFIG=str(Path(folder.name) / "config.json"))
        self.refresh.start()
        self.addCleanup(self.refresh.stop)

    def agents(self):
        return {a["name"]: a for a in settings.load_agents()}

    def test_local_persistence_preserves_paths_and_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"keep": True, "agents": {
                "Cursor": {"command": "vendor-cursor", "keep": "value"},
                "Claude": {"command": "vendor-claude"}}}))
            with patch.dict(os.environ, AGENT_PICKER_CONFIG=str(path)):
                cursor = self.agents()["Cursor"]
                values = dict(defaults.current(cursor), effort="medium", bypass_permissions=False)
                defaults.save(cursor, values, path)
                reloaded = self.agents()["Cursor"]
            data = json.loads(path.read_text())
            self.assertTrue(data["keep"])
            self.assertEqual(data["agents"]["Cursor"]["command"], "vendor-cursor")
            self.assertEqual(data["agents"]["Claude"]["command"], "vendor-claude")
            self.assertNotIn("argv", data["agents"]["Cursor"])
            self.assertEqual(reloaded["argv"][0], "vendor-cursor")
            self.assertIn("grok-4.7-medium", reloaded["argv"])
            self.assertNotIn("--yolo", reloaded["argv"])
            self.assertNotIn("--trust", reloaded["argv"])

    def test_known_providers_update_launch_flags(self):
        agents = self.agents()
        for name in defaults.PROVIDERS:
            agent = agents[name]
            original = copy.deepcopy(agent)
            values = dict(defaults.current(agent), effort="low", bypass_permissions=False)
            defaults.apply(agent, values)
            if name == "Antigravity":
                self.assertEqual(defaults.current(agent)["model"], "gemini-3.8-flash-high")
                self.assertEqual(defaults.option(agent["argv"], ("--effort",)), "")
            else:
                self.assertEqual(defaults.current(agent)["effort"], "low", name)
            self.assertFalse(defaults.current(agent)["bypass_permissions"], name)
            values["bypass_permissions"] = True
            defaults.apply(agent, values)
            self.assertTrue(defaults.current(agent)["bypass_permissions"], name)
            defaults.apply(original, defaults.current(original))
            self.assertEqual(original["argv"], self.agents()[name]["argv"])

    def test_codex_preserves_unrelated_configuration(self):
        agent = self.agents()["Codex"]
        agent["argv"].extend(["-c", "keep=value", "--other"])
        values = dict(defaults.current(agent), model="new-model", effort="xhigh")
        defaults.apply(agent, values)
        self.assertIn("keep=value", agent["argv"])
        self.assertIn("--other", agent["argv"])
        self.assertIn("model_reasoning_effort=xhigh", agent["argv"])
        self.assertNotIn("model_reasoning_effort=high", agent["argv"])
        self.assertIn("new-model", agent["argv"])

    def test_cursor_fast_suffix_and_bracket_parameters(self):
        for model, expected in (("grok-4.7-high-fast", "grok-4.7-low-fast"),
                                ("claude-opus-4-8[context=1m,effort=high]", "claude-opus-4-8[context=1m,effort=low]")):
            agent = self.agents()["Cursor"]
            index = agent["argv"].index("--model") + 1
            agent["argv"][index] = model
            defaults.apply(agent, dict(defaults.current(agent), effort="low"))
            self.assertIn(expected, agent["argv"])

    def test_menu_model_selection_toggle_and_save_key(self):
        menu = defaults.Menu(self.agents()["Claude"])
        original = menu.values["model"]
        menu.handle("right")
        menu.handle("left")
        self.assertEqual(menu.values["model"], original)
        menu.handle("\r")
        self.assertTrue(menu.browsing)
        menu.handle("down")
        self.assertEqual(menu.values["model"], original)
        chosen = menu.models()[menu.model_index]["id"]
        menu.handle("\r")
        self.assertFalse(menu.browsing)
        self.assertEqual(menu.values["model"], chosen)
        self.assertNotEqual(chosen, original)
        menu.handle("down")
        menu.handle("right")
        menu.handle("down")
        menu.handle("left")
        self.assertFalse(menu.values["bypass_permissions"])
        self.assertTrue(menu.changed)
        self.assertEqual(menu.handle("esc"), "save")

    def test_save_failure_does_not_change_launch_profile(self):
        agent = self.agents()["Claude"]
        before = copy.deepcopy(agent)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text("invalid json")
            with self.assertRaises(ValueError):
                defaults.save(agent, dict(defaults.current(agent), model="sonnet"), path)
        self.assertEqual(agent, before)

    def test_keyboard_flow_saves_then_confirms_selector_exit(self):
        import usage_panel
        keys = iter(["right", "down", "left", "down", "left", "esc", "right", "\r",
                     "esc", "esc", "esc", "right", "\r"])
        frames = []
        class Keyboard:
            def __enter__(self):
                self.old = True
                return self
            def __exit__(self, *args):
                pass
            def wait(self, seconds):
                return True
            def read_key(self):
                return next(keys)
            def write(self, text):
                frames.append(text)
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / "config.json"
            agents = list(self.agents().values())[:5]
            stack.enter_context(patch.dict(os.environ, AGENT_PICKER_CONFIG=str(path)))
            stack.enter_context(patch.object(picker, "AGENTS", agents))
            stack.enter_context(patch.object(picker, "installed", return_value=True))
            stack.enter_context(patch.object(picker, "load_last", return_value=0))
            stack.enter_context(patch.object(picker, "load_usage"))
            stack.enter_context(patch.object(usage_panel, "start"))
            stack.enter_context(patch.object(picker.preferences, "get", return_value="agents"))
            stack.enter_context(patch.object(picker, "Terminal", Keyboard))
            stack.enter_context(patch.object(sys, "stdout", io.StringIO()))
            self.assertIsNone(picker.choose())
            saved = json.loads(path.read_text())["agents"]["Claude"]["defaults"]
            self.assertEqual(saved, {"model": "opus", "effort": "low", "bypass_permissions": False})
        visible = picker._ANSI.sub("", "".join(frames))
        self.assertGreaterEqual(visible.count("Are you sure"), 2)
        self.assertRegex(visible, r"Model +opus")
        self.assertRegex(visible, r"Effort +low")
        self.assertIn("medium → low", visible)
        self.assertIn("On → Off", visible)
        self.assertIn("Your changes are saved locally", visible)

    def test_unchanged_defaults_return_without_prompt(self):
        import usage_panel
        keys = iter(["right", "esc", "esc"])
        frames = []
        class Keyboard:
            def __enter__(self):
                self.old = True
                return self
            def __exit__(self, *args):
                pass
            def wait(self, seconds):
                return True
            def read_key(self):
                return next(keys)
            def write(self, text):
                frames.append(text)
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / "config.json"
            stack.enter_context(patch.dict(os.environ, AGENT_PICKER_CONFIG=str(path)))
            stack.enter_context(patch.object(picker, "AGENTS", list(self.agents().values())[:5]))
            stack.enter_context(patch.object(picker, "installed", return_value=True))
            stack.enter_context(patch.object(picker, "load_last", return_value=0))
            stack.enter_context(patch.object(picker, "load_usage"))
            stack.enter_context(patch.object(usage_panel, "start"))
            stack.enter_context(patch.object(picker.preferences, "get", return_value="agents"))
            stack.enter_context(patch.object(picker, "Terminal", Keyboard))
            stack.enter_context(patch.object(sys, "stdout", io.StringIO()))
            self.assertIsNone(picker.choose())
            self.assertFalse(path.exists())
        visible = picker._ANSI.sub("", "".join(frames))
        self.assertNotIn("Are you sure", visible)

    def test_saved_defaults_reach_fake_provider_on_next_invocation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script, output, config = (root / name for name in ("fake.py", "output.json", "config.json"))
            script.write_text("import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n")
            config.write_text(json.dumps({"agents": {"Codex": {
                "argv": [sys.executable, str(script), str(output), "-m", "before", "-c", "model_reasoning_effort=high", "--dangerously-bypass-approvals-and-sandbox"]}}}))
            with patch.dict(os.environ, AGENT_PICKER_CONFIG=str(config)):
                agent = self.agents()["Codex"]
                defaults.save(agent, {"model": "after", "effort": "low", "bypass_permissions": False}, config)
            env = dict(os.environ, AGENT_PICKER_CONFIG=str(config), AGENT_PICKER_STATE_HOME=str(root / "state"), AGENT_PICKER_NO_PRETRUST="1")
            result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"), "--picker-launch", "Codex"], env=env, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(output.read_text())
            self.assertIn("after", args)
            self.assertIn("model_reasoning_effort=low", args)
            self.assertIn("on-request", args)
            self.assertIn("workspace-write", args)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

    @unittest.skipIf(os.name == "nt", "POSIX pseudo-terminal")
    def test_real_arrow_keys_save_and_restore_terminal(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            master, slave = pty.openpty()
            original = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            code = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import agent_picker as a
import usage_panel as u
class Keyboard(a.Terminal):
    def __enter__(self):
        super().__enter__()
        Path(sys.argv[2]).write_text("ready")
        return self
    def read_key(self):
        key = super().read_key()
        with Path(sys.argv[3]).open("a") as stream:
            stream.write(repr(key) + "\\n")
        return key
a.Terminal=Keyboard
a.installed=lambda _:True
a.load_usage=lambda:None
u.start=lambda _:None
a.main()
'''
            env = dict(os.environ, AGENT_PICKER_CONFIG=str(root / "config.json"), AGENT_PICKER_STATE_HOME=str(root / "state"))
            ready, acknowledged = root / "ready", root / "keys"
            process = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "src"), str(ready), str(acknowledged)], stdin=slave, stdout=slave, stderr=slave, env=env)
            output = b""
            keys = [b"\x1b[C", b"\x1b[B", b"\x1b[D", b"\x1b[B", b"\x1b[D", b"\x1b", b"\x1b[C", b"\r", b"\x1b", b"\x1b[C", b"\r"]
            start, sent = time.monotonic(), 0
            try:
                while time.monotonic() - start < 15:
                    received = acknowledged.read_text().count("\n") if acknowledged.exists() else 0
                    if ready.exists() and received == sent and sent < len(keys):
                        os.write(master, keys[sent])
                        sent += 1
                    if select.select([master], [], [], 0.01)[0]:
                        output += os.read(master, 65536)
                    if process.poll() is not None:
                        break
                self.assertEqual(process.poll(), 0, output[-1000:])
                values = json.loads((root / "config.json").read_text())["agents"]["Claude"]["defaults"]
                self.assertEqual(values["effort"], "low")
                self.assertFalse(values["bypass_permissions"])
                self.assertIn(b"Are you sure", output)
                restored = termios.tcgetattr(slave)
                for modes in (original, restored):
                    modes[3] &= ~getattr(termios, "PENDIN", 0)
                self.assertEqual(restored, original)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                os.close(slave)

    def test_defaults_render_and_selector_title_fit(self):
        menu = defaults.Menu(self.agents()["Codex"])
        for cols in (1, 8, 24, 40, 80, 120):
            for rows in (1, 4, 7, 12, 24):
                lines = picker.render_defaults(menu, cols, rows, 0)
                self.assertLessEqual(len(lines), rows)
                self.assertTrue(all(picker.visible_len(line) <= cols for line in lines), (cols, rows))
        text = "\n".join(picker._ANSI.sub("", line) for line in picker.render(0, [True] * 5, 0, 80, 24, agents=picker.AGENTS[:5]))
        self.assertIn("agent selector", text)
        self.assertNotIn("new session", text)
        self.assertNotIn("permissions skipped", text)


if __name__ == "__main__":
    unittest.main()
