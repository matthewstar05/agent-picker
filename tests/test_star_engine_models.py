import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import claude_ultracode
import star_engines


class ExplicitModelEngineTests(unittest.TestCase):
    def engine(self, name):
        engine = star_engines.ENGINES[name]({"name": name, "argv": ["vendor-cli"]})
        engine.exe = "/vendor/" + name.lower()
        return engine

    def test_antigravity_forwards_supported_effort_flag(self):
        argv = self.engine("Antigravity").argv("task", "gemini-3.8-flash", "xhigh")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_kiro_forwards_supported_effort_flag(self):
        with patch.object(star_engines, "ensure_kiro_agent", return_value=None):
            argv = self.engine("Kiro").argv("task", "claude-opus-5.5", "xhigh")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_cursor_uses_model_variant_for_effort(self):
        argv = self.engine("Cursor").argv("task", "claude-opus-5-thinking-xhigh", "xhigh")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-thinking-xhigh")
        self.assertNotIn("--effort", argv)

    def test_claude_ultracode_is_prompt_scoped(self):
        agent = {"name": "Claude", "argv": ["vendor-cli", "--model", "opus"],
                 "defaults": {"ultracode": False}}
        engine = star_engines.ClaudeEngine(agent)
        engine.exe = "/vendor/claude"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"AGENT_PICKER_STATE_HOME": folder}):
            argv = engine.argv("task", "opus", "high", ultracode=True)
            self.assertIn(claude_ultracode.GUIDANCE, argv)
            settings_path = Path(argv[argv.index("--settings") + 1])
            self.assertEqual(json.loads(settings_path.read_text()),
                             {"ultracode": True, "enableWorkflows": True})
        self.assertFalse(agent["defaults"]["ultracode"])

    def test_claude_ultracode_default_stays_supported(self):
        agent = {"name": "Claude", "argv": ["vendor-cli", "--model", "opus"],
                 "defaults": {"ultracode": True}}
        engine = star_engines.ClaudeEngine(agent)
        engine.exe = "/vendor/claude"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"AGENT_PICKER_STATE_HOME": folder}):
            argv = engine.argv("task", "opus", "high")
            self.assertIn(claude_ultracode.GUIDANCE, argv)

    def test_claude_result_preserves_structured_rate_limit_failure(self):
        engine = self.engine("Claude")
        event = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                 "api_error_status": 429, "errors": ["You've hit your weekly limit"]}
        self.assertEqual(engine.parse(event, {})[-1],
                         {"kind": "error", "text": "HTTP 429: You've hit your weekly limit"})

    def test_grok_bot_recovers_a_result_not_already_streamed(self):
        engine = self.engine("Grok Bot")
        state = {}
        progress = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Working"}]}}
        result = {"type": "result", "session_id": "fixture", "result": "Synthetic task complete"}
        self.assertEqual(engine.parse(progress, state), [{"kind": "text", "text": "Working"}])
        self.assertEqual(engine.parse(result, state)[-1],
                         {"kind": "text", "text": "\n\nSynthetic task complete"})
        state["last_text"] = result["result"]
        self.assertFalse(any(event["kind"] == "text" for event in engine.parse(result, state)))


if __name__ == "__main__":
    unittest.main()
