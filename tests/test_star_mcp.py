import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import star_engines
import star_mcp_proxy as proxy


class McpBridgeTests(unittest.TestCase):
    def test_nesting_preserves_the_union_and_rebases_local_references(self):
        schema = {"oneOf": [{"$ref": "#/$defs/by_id"}, {"type": "object", "required": ["path"]}],
                  "$defs": {"by_id": {"type": "object", "properties": {"id": {"type": "integer"}},
                                      "required": ["id"], "additionalProperties": False}},
                  "examples": [{"$ref": "#/literal-data"}]}
        original = json.loads(json.dumps(schema))
        wrapped = proxy.nested_schema(schema)
        self.assertEqual(wrapped["required"], ["input"])
        self.assertFalse(wrapped["additionalProperties"])
        inner = wrapped["properties"]["input"]
        self.assertEqual(inner["oneOf"][0]["$ref"], "#/properties/input/$defs/by_id")
        self.assertEqual(inner["$defs"], schema["$defs"])
        self.assertEqual(inner["examples"], schema["examples"])
        self.assertEqual(schema, original)
        scoped = {"$id": "https://example.com/tool.json", "anyOf": [{"$ref": "#/$defs/input"}]}
        self.assertEqual(proxy.nested_schema(scoped)["properties"]["input"], scoped)

    def test_only_wrapped_tools_unwrap_and_invalid_envelopes_are_rejected(self):
        wrapped = set()
        listed = {"id": 1, "result": {"tools": [
            {"name": "union", "inputSchema": {"allOf": [{"type": "object"}]}},
            {"name": "normal", "inputSchema": {"type": "object"}},
        ]}}
        adapted = proxy.tool_list(listed, wrapped)
        self.assertEqual(wrapped, {"union"})
        self.assertEqual(adapted["result"]["tools"][1], listed["result"]["tools"][1])
        call = {"id": 2, "method": "tools/call", "params": {"name": "union", "arguments": {"input": {"id": 7}}}}
        self.assertEqual(proxy.tool_call(call, wrapped)["params"]["arguments"], {"id": 7})
        self.assertEqual(call["params"]["arguments"], {"input": {"id": 7}})
        call["params"]["arguments"] = {"id": 7}
        with self.assertRaises(ValueError):
            proxy.tool_call(call, wrapped)
        call["params"]["name"] = "normal"
        self.assertEqual(proxy.tool_call(call, wrapped), call)

    def test_real_stdio_relay_forwards_calls_and_notifications(self):
        with tempfile.TemporaryDirectory() as folder:
            server = Path(folder) / "server.py"
            server.write_text('''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "tools/list":
        result = {"tools": [{"name": "choose", "inputSchema": {"oneOf": [
            {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        ]}}]}
    else:
        result = {"arguments": request["params"]["arguments"]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''')
            proc = subprocess.Popen([sys.executable, str(ROOT / "src/star_mcp_proxy.py"), "--", sys.executable, str(server)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            output = queue.Queue()

            def read():
                for line in proc.stdout:
                    output.put(json.loads(line))

            reader = threading.Thread(target=read, daemon=True)
            reader.start()

            def send(message):
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()

            try:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                listed = output.get(timeout=5)
                self.assertEqual(listed["result"]["tools"][0]["inputSchema"]["type"], "object")
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "choose", "arguments": {"input": {"id": 7}}}})
                self.assertEqual(output.get(timeout=5)["result"]["arguments"], {"id": 7})
                send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "choose", "arguments": {"id": 7}}})
                self.assertEqual(output.get(timeout=5)["error"]["code"], -32602)
                proc.stdin.close()
                self.assertEqual(proc.wait(timeout=5), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                reader.join(timeout=2)
                proc.stdout.close()
                proc.stderr.close()

    def test_full_kiro_profile_inherits_servers_without_rewriting_the_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            home, cwd = Path(folder) / "home", Path(folder) / "project"
            config = home / ".kiro/settings/mcp.json"
            local = cwd / ".kiro/settings/mcp.json"
            base = home / ".kiro/agents/herdr.json"
            for path in (config, local, base):
                path.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({"mcpServers": {
                "docs": {"command": "global-docs", "args": ["--stdio"], "env": {"LOCAL_TOKEN": "fixture"}},
                "disabled": {"command": "do-not-run", "disabled": True},
                "remote": {"url": "https://example.invalid/mcp"},
            }}))
            local.write_text(json.dumps({"mcpServers": {"docs": {"command": "project-docs", "args": ["--local"]}}}))
            base.write_text(json.dumps({"name": "herdr", "tools": ["*"], "hooks": {"test": []},
                                        "mcpServers": {"extra": {"command": "own-tools"}}}))
            sources = {p: p.read_bytes() for p in (config, local, base)}
            name = star_engines.ensure_kiro_agent(home, cwd, mcp=True)
            target = home / ".kiro/agents" / (name + ".json")
            made = json.loads(target.read_text())
            self.assertFalse(made["includeMcpJson"])
            self.assertEqual(set(made["mcpServers"]), {"docs", "disabled", "remote", "extra"})
            docs = made["mcpServers"]["docs"]
            self.assertEqual(docs["command"], sys.executable)
            self.assertEqual(docs["args"][-2:], ["project-docs", "--local"])
            self.assertEqual(made["hooks"], {"test": []})
            self.assertEqual(made["mcpServers"]["disabled"]["command"], "do-not-run")
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual({p: p.read_bytes() for p in sources}, sources)
            other = star_engines.ensure_kiro_agent(home, Path(folder) / "other", mcp=True)
            self.assertNotEqual(name, other)
            light = star_engines.ensure_kiro_agent(home, cwd)
            self.assertEqual(json.loads((home / ".kiro/agents" / (light + ".json")).read_text())["mcpServers"], {})
