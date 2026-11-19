"""Temporal Grok Bot gateway requests, using only a fake HTTP transport."""

import base64
import json
import os
import sys
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import grokbot_gateway as gateway


GATEWAY_URL = "https://box.cursorvm.com"
AGENT_ID = "c920b117-dcbf-44aa-a266-65d96a773dbc"


class FakeNetwork:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, body, headers):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, body, dict(headers)))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(body)
        return response

    def calls_for(self, method):
        return [body for called, body, _headers in self.calls if called == method]


class TemporalGatewayTests(unittest.TestCase):
    def gateway(self, responses):
        network = FakeNetwork(responses)
        environment = patch.dict(os.environ, {
            "GROK_BOT_GATEWAY_URL": GATEWAY_URL,
            "GROK_BOT_GATEWAY_TOKEN": "test-gateway-token",
            "GROK_BOT_GATEWAY_HEADERS": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        return gateway.Gateway(token="test-account-token", request=network), network

    def test_temporal_create_uses_capabilities_and_returns_normalized_agent(self):
        client, network = self.gateway({
            "GetGrokBotRuntimeCapabilities": {"capabilities": {"temporalCreationEnabled": True}},
            "CreateGrokBotTemporalAgent": lambda body: {"agent": {"agentId": body["agentId"]}},
        })

        created = client.create("Inbox scan", "Classify recruiting messages")

        self.assertEqual(created["id"], network.calls_for("CreateGrokBotTemporalAgent")[0]["agentId"])
        self.assertEqual(created["harness"], "temporal")
        capabilities = network.calls_for("GetGrokBotRuntimeCapabilities")
        payloads = network.calls_for("CreateGrokBotTemporalAgent")
        self.assertEqual(len(capabilities), 1)
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["name"], "Inbox scan")
        self.assertEqual(payload["description"], "Classify recruiting messages")
        self.assertEqual(payload["avatarShape"], "gem")
        self.assertEqual(payload["avatarColor"], "black")
        self.assertEqual(payload["harness"], 2)
        self.assertIs(payload["introductionSuppressed"], True)
        self.assertEqual(str(uuid.UUID(payload["agentId"])), payload["agentId"])
        self.assertNotIn("createAgent", [method for method, _body, _headers in network.calls])

    def test_missing_temporal_capability_uses_legacy_creation(self):
        client, network = self.gateway({
            "GetGrokBotRuntimeCapabilities": {},
            "createAgent": {"agent": {"id": AGENT_ID, "name": "Inbox scan"}},
        })

        created = client.create("Inbox scan", "Classify recruiting messages")

        self.assertEqual(created["id"], AGENT_ID)
        self.assertEqual(len(network.calls_for("GetGrokBotRuntimeCapabilities")), 1)
        self.assertEqual(len(network.calls_for("createAgent")), 1)
        self.assertEqual(network.calls_for("CreateGrokBotTemporalAgent"), [])

    def test_denied_temporal_creation_does_not_fall_back_to_legacy(self):
        denied = gateway.GrokError("Temporal creation denied", "rejected", status=403)
        client, network = self.gateway({
            "GetGrokBotRuntimeCapabilities": {"capabilities": {"temporalCreationEnabled": True}},
            "CreateGrokBotTemporalAgent": denied,
        })

        with self.assertRaises(gateway.GrokError):
            client.create("Inbox scan", "Classify recruiting messages")

        self.assertEqual(len(network.calls_for("CreateGrokBotTemporalAgent")), 1)
        self.assertEqual(network.calls_for("createAgent"), [])

    def test_temporal_agent_status_uses_matching_account_status(self):
        client, network = self.gateway({
            "ListGrokBotAgentStatuses": {"statuses": [
                {"agentId": "another-agent", "isRunning": False},
                {"agentId": AGENT_ID, "isRunning": True},
            ]},
        })
        client.temporal_agents.add(AGENT_ID)

        agent = client.agent(AGENT_ID)

        self.assertTrue(agent["isRunningTurn"])
        self.assertEqual(len(network.calls_for("ListGrokBotAgentStatuses")), 1)

    def test_missing_temporal_status_boolean_defaults_to_not_running(self):
        client, network = self.gateway({
            "ListGrokBotAgentStatuses": {"statuses": [{"agentId": AGENT_ID}]},
        })
        client.temporal_agents.add(AGENT_ID)

        agent = client.agent(AGENT_ID)

        self.assertIs(agent["isRunningTurn"], False)

    def test_temporal_transcript_decodes_sorts_and_attaches_envelope(self):
        first = {"kind": "message", "id": "entry-1", "role": "user"}
        second = {"kind": "send-message", "id": "entry-2", "requestId": "run-1",
                  "message": {"type": "text", "content": "Finished"}}

        def encode(entry):
            return base64.b64encode(json.dumps(entry).encode("utf-8")).decode("ascii")

        client, network = self.gateway({
            "ListGrokBotTranscriptEntries": {"entries": [
                {"seq": 9, "updatedAt": "2026-11-19T09:02:00Z", "updatedSeq": 10,
                 "entryId": "entry-2", "entryKind": "send-message", "body": encode(second)},
                {"seq": 3, "updatedAt": "2026-11-19T09:01:00Z", "updatedSeq": 4,
                 "entryId": "entry-1", "entryKind": "message", "body": encode(first)},
            ]},
        })
        client.temporal_agents.add(AGENT_ID)

        entries = client.transcript(AGENT_ID)

        self.assertEqual([entry["id"] for entry in entries], ["entry-1", "entry-2"])
        self.assertEqual(entries[0]["seq"], 3)
        self.assertEqual(entries[0]["updatedAt"], "2026-11-19T09:01:00Z")
        self.assertEqual(entries[0]["updatedSeq"], 4)
        self.assertEqual(entries[1]["seq"], 9)
        self.assertEqual(entries[1]["updatedSeq"], 10)
        self.assertEqual(len(network.calls_for("ListGrokBotTranscriptEntries")), 1)


if __name__ == "__main__":
    unittest.main()
