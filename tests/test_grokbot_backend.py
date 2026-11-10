"""Backend delivery recovery and pooled transport must not replay uncertain writes."""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import grokbot_gateway as gateway


class BackendTests(unittest.TestCase):
    def client(self, *answers):
        request = Mock(side_effect=answers)
        return gateway.Gateway(token="account", request=request), request

    def test_receipts_accept_numeric_and_named_delivery(self):
        for delivery in (1, 2, 3, "GROK_BOT_USER_MESSAGE_DELIVERY_ACCEPTED_BOX",
                         "GROK_BOT_USER_MESSAGE_DELIVERY_ACCEPTED_TEMPORAL",
                         "GROK_BOT_USER_MESSAGE_DELIVERY_DUPLICATE"):
            with self.subTest(delivery=delivery):
                client, request = self.client({"delivery": delivery})
                self.assertIsNone(client.send("bot", "task", "nonce"))
                self.assertEqual(request.call_count, 1)

    def test_refused_or_missing_receipt_never_replays(self):
        for receipt, delivery in (({"delivery": 4}, "rejected"), ({}, "unknown"),
                                  (None, "unknown"), ({"delivery": True}, "unknown")):
            with self.subTest(receipt=receipt):
                client, request = self.client(receipt)
                with self.assertRaises(gateway.GrokError) as caught:
                    client.send("bot", "task", "nonce")
                self.assertEqual(caught.exception.delivery, delivery)
                self.assertEqual(request.call_count, 1)

    def test_lost_receipt_checks_status_without_replaying_accepted(self):
        client, request = self.client(gateway.GrokError("Offline", transient=True), {"status": 2})
        self.assertIsNone(client.send("bot", "task", "nonce"))
        self.assertEqual([c.args[0].rsplit("/", 1)[-1] for c in request.call_args_list],
                         ["SendGrokBotUserMessage", "GetGrokBotSendStatus"])
        self.assertEqual(request.call_args_list[1].args[1]["messageId"], "nonce")

    def test_only_not_found_allows_one_resubmission_with_identical_message(self):
        client, request = self.client(gateway.GrokError("Offline"), {"status": 1}, {"delivery": 3})
        self.assertIsNone(client.send("bot", "task", "nonce", machine="local", reply_to="root"))
        self.assertEqual(request.call_count, 3)
        first, last = request.call_args_list[0].args[1], request.call_args_list[2].args[1]
        for key in ("messageId", "text", "sentAtMs", "machineId", "replyToId"):
            self.assertEqual(first[key], last[key])
        self.assertIn("firstSentAgeMs", last)

    def test_second_failure_stays_unknown_even_if_not_found(self):
        client, request = self.client(gateway.GrokError("Offline"), {"status": 1},
                                     gateway.GrokError("Offline"), {"status": 1})
        with self.assertRaises(gateway.GrokError) as caught:
            client.send("bot", "task", "nonce")
        self.assertEqual(caught.exception.delivery, "unknown")
        self.assertEqual(request.call_count, 4)

    def test_pending_unknown_or_failed_lookup_never_replays(self):
        for status in (4, 5, None, "new-status", gateway.GrokError("Offline")):
            with self.subTest(status=status):
                answer = status if isinstance(status, Exception) else {"status": status}
                client, request = self.client(gateway.GrokError("Offline", transient=True), answer)
                with self.assertRaises(gateway.GrokError) as caught:
                    client.send("bot", "task", "nonce")
                self.assertEqual(caught.exception.delivery, "unknown")
                self.assertEqual(request.call_count, 2)

    def test_known_rejection_does_not_query_or_resend(self):
        client, request = self.client(gateway.GrokError("Denied", "rejected"))
        with self.assertRaises(gateway.GrokError) as caught:
            client.send("bot", "task", "nonce")
        self.assertEqual(caught.exception.delivery, "rejected")
        self.assertEqual(request.call_count, 1)

    def test_stop_and_approvals_use_backend_once(self):
        client, request = self.client({"hadActiveRun": True}, {}, {})
        self.assertTrue(client.interrupt("bot")["hadActiveRun"])
        for local, accept in ((True, True), (False, False)):
            entry = {"id": "entry", "message": {"type": "local-tool-permission" if local else "auto-review-approval",
                     "ask" if local else "approval": {"requestId": "request", "status": "pending"}}}
            client.approval("bot", entry, accept)
        calls = request.call_args_list
        self.assertTrue(calls[0].args[0].endswith("/InterruptGrokBotAgentRun"))
        self.assertEqual(calls[0].args[1], {"agentId": "bot", "reason": "user_interrupt"})
        self.assertTrue(calls[1].args[0].endswith("/ResolveGrokBotLocalToolPermission"))
        self.assertEqual(calls[1].args[1], {"agentId": "bot", "requestId": "request", "entryId": "entry", "resolution": 1})
        self.assertTrue(calls[2].args[0].endswith("/ResolveGrokBotAutoReviewApproval"))
        self.assertEqual(calls[2].args[1], {"agentId": "bot", "requestId": "request", "approvalPlatform": "desktop", "resolution": 2})

    def test_refused_approval_is_reported(self):
        client, request = self.client({"refusal": {"message": "not allowed"}})
        with self.assertRaisesRegex(gateway.GrokError, "refused this approval"):
            client.approval("bot", {"id": "entry", "message": {"type": "local-tool-permission",
                            "ask": {"requestId": "request", "status": "pending"}}}, True)
        self.assertEqual(request.call_count, 1)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.pool_patch = patch.object(gateway, "_pool", threading.local())
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)
        self.proxy_patch = patch.object(gateway, "proxied", return_value=False)
        self.proxy_patch.start()
        self.addCleanup(self.proxy_patch.stop)

    def connection(self):
        response = Mock(status=200, will_close=False)
        response.read.return_value = b"{}"
        response.isclosed.return_value = True
        connection = Mock()
        connection.getresponse.return_value = response
        return connection

    def test_reuses_connection_for_consecutive_polls(self):
        connection = self.connection()
        with patch.object(gateway.http.client, "HTTPSConnection", return_value=connection) as constructor:
            for _ in range(2):
                self.assertEqual(gateway.post("https://box.cursorvm.com/api/getAgentTranscriptTail", {}, {}), {})
        self.assertEqual(constructor.call_count, 1)
        self.assertEqual(connection.request.call_count, 2)

    def test_stale_connection_retries_reads_but_never_writes(self):
        for method, retries in (("getAgentTranscriptTail", True), ("SendGrokBotUserMessage", False),
                                ("ResolveGrokBotLocalToolPermission", False), ("InterruptGrokBotAgentRun", False)):
            with self.subTest(method=method):
                gateway._pool.connections = {}
                old, new = self.connection(), self.connection()
                with patch.object(gateway.http.client, "HTTPSConnection", side_effect=[old, new]) as constructor:
                    url = "https://api2.cursor.sh/api/" + method
                    gateway.post(url, {}, {})
                    old.request.side_effect = gateway.http.client.RemoteDisconnected()
                    if retries:
                        self.assertEqual(gateway.post(url, {}, {}), {})
                    else:
                        with self.assertRaises(gateway.GrokError):
                            gateway.post(url, {}, {})
                    self.assertEqual(constructor.call_count, 2 if retries else 1)
                old.close.assert_called()

    def test_transport_rejects_untrusted_hosts_before_opening_connection(self):
        with patch.object(gateway.http.client, "HTTPSConnection") as constructor:
            for url in ("https://evil.test/api/send", "http://api2.cursor.sh", "https://api2.cursor.sh:8443"):
                with self.subTest(url=url), self.assertRaises(gateway.GrokError):
                    gateway.post(url, {}, {"Authorization": "Bearer secret"})
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
