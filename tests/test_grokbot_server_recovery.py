"""Server failures must not lose a task or cause a second prompt submission."""

import contextlib
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_grokbot as fixtures
import grokbot as cli
import grokbot_gateway as gateway
import grokbot_sessions as sessions
from test_grokbot_retry import Server


def internal_error():
    error = urllib.error.HTTPError("https://box.cursorvm.com/api/read", 500, "", {}, None)
    with patch("urllib.request.OpenerDirector.open", side_effect=error):
        try:
            gateway.post("https://box.cursorvm.com/api/read", {}, {})
        except gateway.GrokError as exc:
            return exc
    raise AssertionError("Expected HTTP failure")


class GatewayRecoveryTests(unittest.TestCase):
    def test_internal_error_recovers_each_read_method(self):
        for method in gateway.READS:
            with self.subTest(method=method):
                server = Server(**{method: [internal_error()]})
                sleeps = []
                client = gateway.Gateway(token="test", request=server, sleep=sleeps.append)
                client.call(method)
                self.assertEqual(server.calls.count(method), 2)
                self.assertEqual(sleeps, [1])

    def test_sandbox_discovery_recovers_inside_a_read(self):
        calls = []
        server = Server()

        def request(url, body, headers):
            method = url.rsplit("/", 1)[-1]
            calls.append(method)
            if len(calls) < 3:
                raise internal_error()
            return server(url, body, headers)

        client = gateway.Gateway(token="test", request=request, sleep=lambda _: None)
        self.assertEqual(client.agents(), [{"id": "bot"}])
        self.assertEqual(calls, ["EnsureSandBox"] * 3 + ["listAgents"])

    def test_internal_error_never_replays_any_write(self):
        for method in ("sendPrompt", "createAgent", "resolveLocalToolPermission", "interruptAgentRun"):
            with self.subTest(method=method):
                server = Server(**{method: [internal_error()]})
                client = gateway.Gateway(token="test", request=server)
                with self.assertRaises(gateway.GrokError):
                    client.call(method)
                self.assertEqual(server.calls.count(method), 1)

    def test_prompt_failure_keeps_unknown_delivery_and_recoverability(self):
        server = Server(sendPrompt=[internal_error()])
        client = gateway.Gateway(token="test", request=server)
        with self.assertRaises(gateway.GrokError) as caught:
            client.send("bot", "task", "nonce")
        self.assertEqual(caught.exception.delivery, "unknown")
        self.assertTrue(caught.exception.transient)
        self.assertEqual(server.calls.count("sendPrompt"), 1)

    def test_timeout_and_sandbox_417_do_not_claim_a_prompt_was_rejected(self):
        for code in (408, 417):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://box.cursorvm.com/api/sendPrompt", code, "", {}, None)
                with patch("urllib.request.OpenerDirector.open", side_effect=error):
                    with self.assertRaises(gateway.GrokError) as caught:
                        gateway.post("https://box.cursorvm.com/api/sendPrompt", {}, {})
                self.assertEqual(caught.exception.delivery, "unknown")

    def test_auth_and_invalid_requests_are_not_retried(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://box.cursorvm.com/api/read", code, "", {}, None)
                with patch("urllib.request.OpenerDirector.open", side_effect=error):
                    with self.assertRaises(gateway.GrokError) as caught:
                        gateway.post("https://box.cursorvm.com/api/read", {}, {})
                self.assertFalse(caught.exception.transient)


class DeliveryRecoveryTests(unittest.TestCase):
    setUp = fixtures.GrokBotTests.setUp
    conversation = fixtures.GrokBotTests.conversation

    def test_missing_receipt_and_empty_transcript_preserve_original_prompt(self):
        conversation = self.conversation()
        conversation.ensure_session()
        pending = {"nonce": "original", "text": "Original task", "root": None,
                   "delivery": "unknown", "before": [], "shown": []}
        conversation.session["pending"] = pending
        sessions.save(conversation.session)
        with self.assertRaisesRegex(gateway.GrokError, "previous request is still unknown"):
            conversation.turn("New task")
        self.assertEqual(self.gateway.sent, [])
        self.assertEqual(sessions.load(fixtures.BOT)["pending"], pending)
        self.assertEqual(sessions.load(fixtures.BOT)["messages"], [])

    def test_repeated_internal_errors_do_not_end_the_pending_turn(self):
        conversation = self.conversation()
        transcript = self.gateway.transcript
        failures = [internal_error() for _ in range(5)]
        # Preflight succeeds; only the observation connection fails.
        def read(ident):
            if not self.gateway.sent:
                return []
            if failures:
                raise failures.pop()
            return transcript(ident)
        self.gateway.transcript = read
        conversation.wait = lambda _: None
        events = []
        with patch.object(conversation.output, "event", side_effect=events.append), contextlib.redirect_stdout(io.StringIO()):
            conversation.turn("Original task")
        self.assertEqual(conversation.output.answer, "Verified.")
        self.assertIsNone(sessions.load(fixtures.BOT)["pending"])
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertEqual(len([event for event in events if event["type"] == "notice"]), 2)

    def test_read_deadline_preserves_a_request_and_never_sends(self):
        read = Mock(side_effect=internal_error())
        waits, events = [], []
        conversation = self.conversation()
        conversation.ensure_session()
        conversation.wait = waits.append
        with patch.object(cli.time, "monotonic", side_effect=[0, 0, 1]), \
                patch.object(conversation.output, "event", side_effect=events.append):
            with self.assertRaisesRegex(gateway.GrokError, "Observation timed out.*--resume " + fixtures.BOT):
                conversation.watch_read(read, 1)
        read.assert_called_once_with(fixtures.BOT)
        self.assertEqual(waits, [1])

    def test_resuming_a_failed_watch_keeps_the_interactive_panel_open(self):
        conversation = self.conversation()
        conversation.ensure_session()
        conversation.session["pending"] = {"nonce": "original", "text": "Task", "delivery": "unknown"}
        sessions.save(conversation.session)
        with patch.object(cli, "Gateway", return_value=self.gateway), \
                patch.object(cli, "machine_for", return_value=fixtures.MACHINE), \
                patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch("grokbot_ui.CursorInput") as editor, patch("grokbot_ui.Working") as runner, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            editor.return_value.queued = []
            editor.return_value.read.return_value = None
            runner.return_value.error = gateway.GrokError("Observation timed out; request saved")
            runner.return_value.run.return_value = ""
            result = cli.main(["--resume", fixtures.BOT, "--workspace", str(self.root)])
        self.assertEqual(result, 0)
        editor.return_value.read.assert_called_once()
        self.assertIn("Observation timed out", errors.getvalue())
        self.assertEqual(sessions.load(fixtures.BOT)["pending"]["nonce"], "original")


if __name__ == "__main__":
    unittest.main()
