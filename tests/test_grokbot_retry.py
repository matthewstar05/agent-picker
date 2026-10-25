import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import grokbot_gateway as gateway

BOX = {"gatewayUrl": "https://box.cursorvm.com", "gatewayToken": "private"}


class Server:
    """Answers EnsureSandBox, then fails each gateway method with `fails[method]` in turn."""
    def __init__(self, **fails):
        self.fails = {k: list(v) for k, v in fails.items()}
        self.calls = []

    def __call__(self, url, body, headers):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method == "EnsureSandBox":
            return BOX
        if self.fails.get(method):
            raise self.fails[method].pop(0)
        return {"agents": [{"id": "bot"}]} if method == "listAgents" else {"entries": [], "accepted": True}


def bad_gateway():
    return gateway.GrokError("Grok Bot request failed (HTTP 502).", "unknown", transient=True)


class RetryTests(unittest.TestCase):
    def gw(self, server):
        self.slept = []
        return gateway.Gateway(token="account", request=server, sleep=self.slept.append)

    def test_a_502_while_polling_the_transcript_is_retried(self):
        server = Server(getAgentTranscriptTail=[bad_gateway(), bad_gateway()])
        self.assertEqual(self.gw(server).transcript("bot"), [])
        self.assertEqual(server.calls.count("getAgentTranscriptTail"), 3)
        self.assertEqual(self.slept, [1, 2])

    def test_a_repeated_502_reconnects_to_the_sandbox(self):
        server = Server(listAgents=[bad_gateway(), bad_gateway()])
        self.assertEqual(self.gw(server).agents(), [{"id": "bot"}])
        self.assertEqual(server.calls.count("EnsureSandBox"), 2)

    def test_reads_give_up_after_the_last_retry(self):
        server = Server(listAgents=[bad_gateway() for _ in range(4)])
        with self.assertRaisesRegex(gateway.GrokError, "HTTP 502"):
            self.gw(server).agents()
        self.assertEqual(server.calls.count("listAgents"), 4)

    def test_sending_a_prompt_is_never_repeated(self):
        server = Server(sendPrompt=[bad_gateway()])
        with self.assertRaisesRegex(gateway.GrokError, "Delivery is unknown"):
            self.gw(server).send("bot", "task", "nonce")
        self.assertEqual(server.calls.count("sendPrompt"), 1)
        self.assertEqual(self.slept, [])

    def test_client_errors_are_not_retried(self):
        server = Server(listAgents=[gateway.GrokError("Grok Bot request failed (HTTP 404).", "rejected")])
        with self.assertRaises(gateway.GrokError):
            self.gw(server).agents()
        self.assertEqual(server.calls.count("listAgents"), 1)

    def test_post_marks_gateway_errors_transient(self):
        for code, transient in ((502, True), (503, True), (504, True), (500, False), (404, False)):
            error = urllib.error.HTTPError("https://box.cursorvm.com/api/x", code, "", {}, None)
            with self.subTest(code=code), patch("urllib.request.OpenerDirector.open", side_effect=error):
                with self.assertRaises(gateway.GrokError) as caught:
                    gateway.post("https://box.cursorvm.com/api/x", {}, {})
                self.assertIs(caught.exception.transient, transient)


if __name__ == "__main__":
    unittest.main()
