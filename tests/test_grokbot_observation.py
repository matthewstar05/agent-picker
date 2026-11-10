"""A disconnected panel must never replay or cancel an accepted remote task."""

import contextlib
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_grokbot as fixtures
import grokbot as cli
import grokbot_gateway as gateway
import grokbot_sessions as sessions
from test_grokbot_retry import Server


BOT = fixtures.BOT


class ObservationTests(unittest.TestCase):
    setUp = fixtures.GrokBotTests.setUp
    conversation = fixtures.GrokBotTests.conversation

    def pending(self):
        c = self.conversation()
        c.ensure_session()
        self.gateway.send(BOT, "Original task", "nonce")
        c.session["pending"] = {"nonce": "nonce", "text": "Original task", "root": None,
                                "before": [], "shown": []}
        sessions.save(c.session)
        return c

    def test_417_is_retried_only_for_reads(self):
        with patch.object(gateway, "exchange", return_value=(417, b"")):
            with self.assertRaises(gateway.GrokError) as caught:
                gateway.post("https://box.cursorvm.com/api/x", {}, {})
        self.assertTrue(caught.exception.transient)
        server = Server(getAgentTranscriptTail=[caught.exception])
        self.assertEqual(gateway.Gateway(token="test", request=server, sleep=lambda _: None).transcript(BOT), [])
        self.assertEqual(server.calls.count("getAgentTranscriptTail"), 2)
        server = Server(SendGrokBotUserMessage=[caught.exception])
        with self.assertRaises(gateway.GrokError):
            gateway.Gateway(token="test", request=server).send(BOT, "Task", "nonce")
        self.assertEqual(server.calls.count("SendGrokBotUserMessage"), 1)

    def test_exhausted_gateway_retries_keep_observing_without_replay(self):
        for method in ("transcript", "agent"):
            with self.subTest(method=method):
                c = self.pending()
                c.wait = lambda _: None
                read = getattr(self.gateway, method)
                events = []
                with patch.object(self.gateway, method, side_effect=[
                        gateway.GrokError("HTTP 417", "rejected", transient=True),
                        gateway.GrokError("HTTP 502", "unknown", transient=True),
                        read(BOT), read(BOT)]), patch.object(c.output, "event", side_effect=events.append), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c.turn()
                self.assertIsNone(sessions.load(BOT)["pending"])
                self.assertEqual(c.output.answer, "Verified.")
                notices = [e["text"] for e in events if e["type"] == "notice"]
                self.assertEqual(len(notices), 2)
                self.assertIn("without resending", notices[0])
                self.assertIn("Reconnected", notices[1])
        self.assertEqual(len(self.gateway.sent), 2)  # fixture sends only
        self.assertFalse(self.gateway.stopped)

    def test_permanent_read_error_preserves_request_and_explains_resume(self):
        c = self.pending()
        with patch.object(c, "report") as report, \
                patch.object(self.gateway, "transcript", side_effect=gateway.GrokError("HTTP 403", "rejected")), \
                self.assertRaisesRegex(gateway.GrokError, "Observation paused.*may still be running.*--resume " + BOT):
            c.turn()
        self.assertEqual(report.call_args.args, ("unknown",))
        self.assertEqual(sessions.load(BOT)["pending"]["nonce"], "nonce")
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertFalse(self.gateway.stopped)

    def test_outage_expires_at_the_watch_deadline_and_preserves_request(self):
        c = self.pending()
        with patch.object(cli.time, "monotonic", side_effect=[0, 0, 0, 0, 1]), \
                patch.object(self.gateway, "transcript", side_effect=gateway.GrokError("Offline", transient=True)), \
                patch.object(c, "wait") as wait, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(gateway.GrokError, "Observation timed out.*--resume " + BOT):
            c.turn()
        wait.assert_called_once_with(1)
        self.assertIsNotNone(sessions.load(BOT)["pending"])
        self.assertEqual(len(self.gateway.sent), 1)
        self.assertFalse(self.gateway.stopped)

    def test_escape_interrupts_reconnection_wait(self):
        c = self.pending()
        with patch.object(self.gateway, "transcript", side_effect=gateway.GrokError("Offline", transient=True)), \
                patch.object(c, "wait", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            c.turn()
        self.assertIsNotNone(sessions.load(BOT)["pending"])


if __name__ == "__main__":
    import unittest
    unittest.main()
