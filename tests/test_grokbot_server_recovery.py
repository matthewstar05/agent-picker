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
    with patch.object(gateway, "exchange", return_value=(500, b"")):
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
        server = Server(SendGrokBotUserMessage=[internal_error()])
        client = gateway.Gateway(token="test", request=server)
        with self.assertRaises(gateway.GrokError) as caught:
            client.send("bot", "task", "nonce")
        self.assertEqual(caught.exception.delivery, "unknown")
        self.assertTrue(caught.exception.transient)
        self.assertEqual(server.calls.count("SendGrokBotUserMessage"), 1)

    def test_timeout_and_sandbox_417_do_not_claim_a_prompt_was_rejected(self):
        for code in (408, 417):
            with self.subTest(code=code):
                with patch.object(gateway, "exchange", return_value=(code, b"")):
                    with self.assertRaises(gateway.GrokError) as caught:
                        gateway.post("https://box.cursorvm.com/api/sendPrompt", {}, {})
                self.assertEqual(caught.exception.delivery, "unknown")

    def test_auth_and_invalid_requests_are_not_retried(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                with patch.object(gateway, "exchange", return_value=(code, b"")):
                    with self.assertRaises(gateway.GrokError) as caught:
                        gateway.post("https://box.cursorvm.com/api/read", {}, {})
                self.assertFalse(caught.exception.transient)


def gateway_error(code, body):
    return code, body


REFUSAL = b'{"error":"A Sand box session may not make this request on this endpoint."}'


class RefusalTests(unittest.TestCase):
    def test_explicit_gateway_refusal_is_a_known_unsent_prompt(self):
        with patch.object(gateway, "exchange", return_value=gateway_error(502, REFUSAL)):
            with self.assertRaises(gateway.GrokError) as caught:
                gateway.post("https://box.cursorvm.com/api/sendPrompt", {}, {})
        self.assertEqual(caught.exception.delivery, "rejected")
        self.assertFalse(caught.exception.transient)
        self.assertIn("may not make this request", str(caught.exception))
        self.assertIn("Nothing was sent", str(caught.exception))

    def test_other_error_bodies_stay_unknown_and_unechoed(self):
        for body in (b'{"error":"token abc123 expired upstream"}', b"<html>Bad Gateway</html>", b""):
            with self.subTest(body=body):
                with patch.object(gateway, "exchange", return_value=gateway_error(502, body)):
                    with self.assertRaises(gateway.GrokError) as caught:
                        gateway.post("https://box.cursorvm.com/api/sendPrompt", {}, {})
                self.assertEqual(caught.exception.delivery, "unknown")
                self.assertNotIn("abc123", str(caught.exception))

    def test_refused_send_is_not_wrapped_as_unknown_delivery(self):
        refused = gateway.GrokError("Grok Bot refused the request (HTTP 502): no. Nothing was sent.", "rejected")
        server = Server(SendGrokBotUserMessage=[refused])
        with self.assertRaises(gateway.GrokError) as caught:
            gateway.Gateway(token="test", request=server).send("bot", "task", "nonce")
        self.assertEqual(caught.exception.delivery, "rejected")
        self.assertEqual(server.calls.count("SendGrokBotUserMessage"), 1)


class DeliveryRecoveryTests(unittest.TestCase):
    setUp = fixtures.GrokBotTests.setUp
    conversation = fixtures.GrokBotTests.conversation

    def test_refused_prompt_fails_at_once_and_leaves_nothing_pending(self):
        def refuse(ident, prompt, nonce, machine=None):
            self.gateway.sent.append(nonce)
            raise gateway.GrokError("Grok Bot refused the request (HTTP 502): no. Nothing was sent.", "rejected")
        self.gateway.send = refuse
        conversation = self.conversation()
        with self.assertRaisesRegex(gateway.GrokError, "refused"):
            conversation.turn("Task")
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertIsNone(sessions.load(fixtures.BOT)["pending"])

    def unknown_pending(self, age):
        conversation = self.conversation()
        conversation.ensure_session()
        conversation.session["pending"] = {"nonce": "lost", "text": "Lost task", "root": None, "delivery": "unknown",
                                           "started": cli.time.time() - age, "before": [], "shown": []}
        sessions.save(conversation.session)
        return conversation

    def test_a_lost_prompt_absent_from_the_transcript_is_closed_without_resending(self):
        conversation = self.unknown_pending(cli.ARRIVAL_GRACE + 60)
        with contextlib.redirect_stdout(io.StringIO()):
            conversation.turn("New task")
        saved = sessions.load(fixtures.BOT)
        self.assertEqual([m["status"] for m in saved["messages"] if m.get("status")], ["not delivered"])
        self.assertEqual(saved["messages"][0]["user"], "Lost task")
        self.assertEqual([prompt for _, prompt, _, _ in self.gateway.sent if "Lost task" in prompt], [])
        self.assertEqual(len(self.gateway.sent), 1)    # only the new task

    def test_a_recent_or_visible_lost_prompt_is_still_preserved(self):
        conversation = self.unknown_pending(5)
        with self.assertRaisesRegex(gateway.GrokError, "still unknown"):
            conversation.turn("New task")
        conversation = self.unknown_pending(cli.ARRIVAL_GRACE + 60)
        self.gateway.entries = [{"kind": "message", "id": "u", "role": "user", "clientNonce": "lost"}]
        with self.assertRaisesRegex(gateway.GrokError, "still unknown"):
            conversation.turn("New task")
        self.assertEqual(self.gateway.sent, [])
        self.assertEqual(sessions.load(fixtures.BOT)["pending"]["nonce"], "lost")

    def test_a_partial_transcript_cannot_prove_a_prompt_was_lost(self):
        conversation = self.unknown_pending(cli.ARRIVAL_GRACE + 60)
        recent = int(cli.time.time() * 1000)
        self.gateway.entries = [{"kind": "send-message", "id": f"e{i}", "timestampMs": recent} for i in range(cli.TRANSCRIPT_TAIL)]
        with self.assertRaisesRegex(gateway.GrokError, "still unknown"):
            conversation.turn("New task")
        self.assertEqual(self.gateway.sent, [])

    def test_watching_a_lost_prompt_ends_once_it_provably_never_arrived(self):
        conversation = self.unknown_pending(cli.ARRIVAL_GRACE + 60)
        conversation.wait = lambda _: None
        with self.assertRaisesRegex(gateway.GrokError, "never received"):
            conversation.observe()
        self.assertIsNone(sessions.load(fixtures.BOT)["pending"])
        self.assertEqual(self.gateway.sent, [])

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
