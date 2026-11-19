import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import star_engines
import starcli


class StarGrokBotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.fixture = self.root / "fake_grokbot.py"
        patches = (
            patch.object(starcli, "STATE", self.root),
            patch.object(starcli, "LIVE", self.root / "live"),
            patch.object(starcli, "CONVERSATIONS", self.root / "conversations"),
            patch.object(starcli, "LAST", self.root / "last.json"),
            patch.object(starcli, "report_herdr"),
            patch.object(starcli, "refresh_meters"),
            patch.object(starcli.ap, "load_usage"),
            patch.dict(starcli.ap.USAGE, {}, clear=True),
            patch.object(star_engines, "resolve_argv", side_effect=self.fake_argv),
        )
        for fixture in patches:
            fixture.start()
            self.addCleanup(fixture.stop)

    def fake_argv(self, agent):
        return [str(self.fixture), *agent["argv"][1:]]

    def star(self, installed, conversation=None, **options):
        conversation = conversation or starcli.Conversation({"cwd": str(self.root)})
        with patch.object(starcli, "installed", return_value=set(installed)):
            return starcli.Star(conversation, **options)

    def test_email_requests_route_to_the_named_provider(self):
        star = self.star({"Grok Bot", "Codex"})
        cases = {
            "grokbot: check my email": "Grok Bot",
            "Grok Bot, check my email": "Grok Bot",
            "grokbot check my email": "Grok Bot",
            "@grokbot check my email": "Grok Bot",
            "Use Grok-Bot to check email": "Grok Bot",
            "Check my email with grok bot": "Grok Bot",
            'Use "Grok Bot" to check email': "Grok Bot",
            "Ask Grokbot to check email": "Grok Bot",
            "Have Grok Bot scan my inbox": "Grok Bot",
            "Use Codex to explain Grok Bot's routing": "Codex",
        }
        for prompt, provider in cases.items():
            with self.subTest(prompt=prompt):
                _, choices, skipped = star.route(prompt)
                self.assertEqual({choice.provider for choice in choices}, {provider}, skipped)

    def test_unaddressed_tasks_and_cursor_grok_keep_their_routes(self):
        star = self.star({"Grok Bot", "Codex", "Cursor"})
        for prompt in ("Check my email", "Grok Bot has been timing out lately"):
            with self.subTest(prompt=prompt):
                _, choices, _ = star.route(prompt)
                self.assertTrue(choices)
                self.assertNotIn("Grok Bot", {choice.provider for choice in choices})
        _, choices, skipped = star.route("Use Cursor with Grok 4.7 High to explain routing")
        self.assertTrue(choices, skipped)
        self.assertEqual((choices[0].provider, choices[0].model), ("Cursor", "grok-4.7-high"))

    def test_missing_explicit_grok_bot_never_launches_another_provider(self):
        star = self.star({"Codex"})
        with patch.object(star, "turn") as turn, redirect_stdout(io.StringIO()):
            _, status = star.run("grokbot: check email")
        self.assertNotEqual(status, "ok")
        turn.assert_not_called()

    def test_plan_mode_does_not_launch_grok_bot(self):
        star = self.star({"Grok Bot", "Codex"}, plan=True)
        with patch.object(star, "turn") as turn, redirect_stdout(io.StringIO()):
            _, status = star.run("Grok Bot: propose a mailbox review")
        self.assertNotEqual(status, "ok")
        turn.assert_not_called()

    def test_headless_turn_uses_grok_bot_and_resumes_its_session(self):
        self.fixture.write_text(textwrap.dedent('''\
            import json
            from pathlib import Path
            import sys

            with Path("grokbot.argv").open("a", encoding="utf-8") as log:
                print(json.dumps(sys.argv[1:]), file=log)
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "fixture-grok-session"}))
            answer = "Fixture mailbox checked."
            print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": answer}
            ]}}))
            print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                              "session_id": "fixture-grok-session", "result": answer,
                              "usage": {"input": 10, "output": 4}}))
            '''), encoding="utf-8")
        real_turn = star_engines.Turn

        def start_fixture(engine, argv, **kwargs):
            self.assertEqual(engine.name, "Grok Bot")
            self.assertEqual(Path(argv[0]), self.fixture)
            return real_turn(engine, [sys.executable, *argv], **kwargs)

        star = self.star({"Grok Bot", "Codex"})
        with patch.object(star_engines, "Turn", side_effect=start_fixture), redirect_stdout(io.StringIO()) as out:
            reply, status = star.run("Grok Bot: check the fixture mailbox")
            self.assertEqual(status, "ok", out.getvalue())
            self.assertEqual(reply.strip(), "Fixture mailbox checked.")
            saved = starcli.Conversation.load(star.convo.id)
            self.assertEqual(saved.sessions["Grok Bot"], "fixture-grok-session")
            resumed = self.star({"Grok Bot", "Codex"}, saved)
            self.assertEqual(resumed.run("Check the fixture mailbox with Grok Bot")[1], "ok", out.getvalue())

        first, second = [json.loads(line) for line in (self.root / "grokbot.argv").read_text(encoding="utf-8").splitlines()]
        self.assertIn("-p", first)
        self.assertIn("check the fixture mailbox", first[-1])
        self.assertIn("Check the fixture mailbox with Grok Bot", second[-1])
        self.assertNotIn("--resume", first)
        self.assertNotIn("--effort", first)
        if "--model" in first:
            self.assertEqual(first[first.index("--model") + 1], "auto")
        self.assertEqual(second[second.index("--resume") + 1], "fixture-grok-session")


if __name__ == "__main__":
    unittest.main()
