import io
import json
import os
import re
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap
import star_engines
import star_meter
import star_router
import starcli
import usage_panel as panel

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
HOUR, DAY = 3600, 86400
NOW = 1_800_000_000.0


def meter(*windows, fetched=NOW):
    return {"windows": [{"label": l, "pct": p, "resets": NOW + r} for l, p, r in windows], "fetched": fetched}


def fresh():
    """Every provider barely used, with its windows half run."""
    return {
        "Claude": meter(("5h", 10, 2.5 * HOUR), ("wk", 10, 3.5 * DAY)),
        "Codex": meter(("5h", 10, 2.5 * HOUR), ("wk", 10, 3.5 * DAY)),
        "Kiro": meter(("mo", 10, 15 * DAY)),
        "Cursor": meter(("cr", 10, 15 * DAY), ("ex", 10, 15 * DAY)),
        "Antigravity": meter(("5h", 10, 2.5 * HOUR), ("wk", 10, 3.5 * DAY)),
    }


ALL = {"Claude", "Codex", "Kiro", "Cursor", "Antigravity"}


class ClassifyTests(unittest.TestCase):
    def test_short_external_tasks_keep_mcp_tools(self):
        for prompt in ("read my calendar", "open this Google Doc", "use MCP to fetch it", "generate an image",
                       "open https://docs.google.com/document/d/fixture"):
            self.assertTrue(star_router.classify(prompt).needs_mcp, prompt)
        self.assertFalse(star_router.classify("run the tests").needs_mcp)

    def test_unknown_services_keep_their_tools_without_requiring_known_keywords(self):
        for prompt in ("read my tasks in AcmeCloud", "quickly rename a field in Airbase",
                       "open https://example.invalid/project", "find my saved reports"):
            self.assertTrue(star_router.classify(prompt).needs_mcp, prompt)
        for prompt in ("hello", "17*23?", "run the tests", "quickly search this repo",
                       "explain this without external tools"):
            self.assertFalse(star_router.classify(prompt).needs_mcp, prompt)
        self.assertTrue(star_router.classify("search this repo and my calendar").needs_mcp)
        self.assertEqual(star_router.classify("read my tasks in AcmeCloud").tier, "light")
    def test_kinds(self):
        cases = {
            "where is the usage cache written?": "search",
            "fix the failing test in tests/test_picker.py, it throws KeyError": "debug",
            "design a plugin architecture for providers": "design",
            "implement a new /usage command for the cli": "build",
            "review my diff for security issues": "review",
            "run the tests": "mechanical",
            "write a README section for star": "write",
            "what does git rebase --onto do?": "chat",
        }
        for prompt, kind in cases.items():
            self.assertEqual(star_router.classify(prompt).kind, kind, prompt)

    def test_tiers(self):
        self.assertEqual(star_router.classify("run the tests").tier, "light")
        self.assertEqual(star_router.classify("refactor the whole renderer carefully").tier, "heavy")
        self.assertEqual(star_router.classify("add a flag to the parser in cli.py").tier, "standard")
        self.assertEqual(star_router.classify("run the tests", tier="heavy").tier, "heavy")

    def test_pasted_traceback_is_debug(self):
        prompt = "this happens:\nTraceback (most recent call last):\n  File \"x.py\", line 3\nValueError: nope"
        self.assertEqual(star_router.classify(prompt).kind, "debug")


class RankTests(unittest.TestCase):
    def rank(self, prompt, usage, **kw):
        return star_router.rank(star_router.classify(prompt), usage, kw.pop("installed", ALL), now=NOW, **kw)

    def test_quality_decides_when_quota_is_even(self):
        choices, _ = self.rank("design a plugin architecture across the whole codebase", fresh())
        self.assertEqual(choices[0].provider, "Claude")
        self.assertEqual(choices[0].model, "claude-opus-5-5")
        choices, _ = self.rank("fix the failing test, it throws KeyError", fresh())
        self.assertEqual(choices[0].provider, "Codex")

    def test_quota_about_to_reset_is_spent_first(self):
        usage = fresh()
        # Codex: 60% of its 5h window left and it resets in 30 minutes; its week is fine.
        before, _ = self.rank("review my diff for security issues", fresh())
        self.assertEqual(before[0].provider, "Claude")
        usage["Codex"] = meter(("5h", 40, 0.5 * HOUR), ("wk", 10, 6 * DAY))
        choices, _ = self.rank("review my diff for security issues", usage)
        self.assertEqual(choices[0].provider, "Codex")
        self.assertIn("expires in", choices[0].why)

    def test_expiring_quota_counts_less_when_the_week_is_tight(self):
        usage = fresh()
        usage["Codex"] = meter(("5h", 40, 0.5 * HOUR), ("wk", 85, 5 * DAY))
        choices, _ = self.rank("review my diff for security issues", usage)
        self.assertNotEqual(choices[0].provider, "Codex")

    def test_near_limit_is_skipped_unless_it_resets_within_the_hour(self):
        usage = fresh()
        usage["Codex"] = meter(("5h", 92, 2 * HOUR), ("wk", 10, 6 * DAY))
        _, skipped = self.rank("fix the bug", usage)
        self.assertIn("92% of its 5h limit", skipped["Codex"])
        usage["Codex"] = meter(("5h", 92, 0.5 * HOUR), ("wk", 10, 6 * DAY))
        choices, skipped = self.rank("fix the bug", usage)
        self.assertNotIn("Codex", skipped)
        self.assertIn("Codex", [c.provider for c in choices])

    def test_gemini_only_takes_trivial_work(self):
        _, skipped = self.rank("fix the failing test, it throws KeyError", fresh())
        self.assertIn("Antigravity", skipped)
        choices, _ = self.rank("run the tests", fresh())
        self.assertIn("Antigravity", [c.provider for c in choices])

    def test_trivial_work_prefers_cheap_pools(self):
        choices, _ = self.rank("run the tests", fresh())
        self.assertIn(choices[0].provider, ("Cursor", "Antigravity"))

    def test_pinned_down_and_sticky(self):
        choices, _ = self.rank("fix the bug", fresh(), pinned="Kiro")
        self.assertEqual([c.provider for c in choices], ["Kiro"])
        _, skipped = self.rank("fix the bug", fresh(), down={"Codex": "boom"})
        self.assertEqual(skipped["Codex"], "failed earlier this session")
        plain, _ = self.rank("fix the bug", fresh())
        sticky, _ = self.rank("fix the bug", fresh(), current="Kiro")
        score = lambda cs: next(c.score for c in cs if c.provider == "Kiro")
        self.assertAlmostEqual(score(sticky) - score(plain), star_router.STICKY, places=3)

    def test_unknown_meters_still_route(self):
        choices, _ = self.rank("fix the bug", {})
        self.assertTrue(choices)
        self.assertIn("usage unknown", choices[0].why)

    def test_a_pin_does_not_bypass_quota_or_image_capabilities(self):
        usage = fresh()
        usage["Codex"] = meter(("5h", 99, HOUR))
        choices, skipped = self.rank("fix the bug", usage, pinned="Codex")
        self.assertEqual(choices, [])
        self.assertIn("limit used", skipped["Codex"])
        choices, skipped = self.rank("describe this picture", fresh(), pinned="Kiro", images=True)
        self.assertEqual(choices, [])
        self.assertIn("attached images", skipped["Kiro"])


class EngineTests(unittest.TestCase):
    def parse(self, name, lines):
        engine = star_engines.ENGINES[name]({"name": name, "argv": ["x"]})
        state, events = {}, []
        for line in lines:
            events += engine.parse(json.loads(line) if isinstance(line, str) else line, state)
        text = "".join(e["text"] for e in events if e["kind"] == "text")
        return text, [e for e in events if e["kind"] not in ("text", "thinking")]

    def test_claude(self):
        text, events = self.parse("Claude", [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                           "input": {"command": "npm test"}}]}},
            {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 0.25, "resetsAt": NOW}, "seven_day": {"utilization": 0.5, "resetsAt": NOW}}}},
            {"type": "result", "session_id": "s1", "usage": {"input_tokens": 4, "cache_read_input_tokens": 96,
                                                              "output_tokens": 7}},
        ])
        self.assertEqual(text, "Hi")
        self.assertIn({"kind": "tool", "label": "$ npm test"}, events)
        meter_event = next(e for e in events if e["kind"] == "meter")
        self.assertEqual([w["pct"] for w in meter_event["windows"]], [25.0, 50.0])
        self.assertIn({"kind": "usage", "input": 100, "cached": 96, "output": 7}, events)

    def test_codex(self):
        text, events = self.parse("Codex", [
            {"type": "thread.started", "thread_id": "th"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'echo hi'"}},
            {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py"}]}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 2}},
        ])
        self.assertEqual(text, "DONE")
        self.assertEqual([e.get("label") for e in events if e["kind"] == "tool"], ["$ echo hi", "edit a.py"])
        self.assertEqual(events[0], {"kind": "session", "id": "th"})

    def test_cursor_does_not_repeat_streamed_text(self):
        text, _ = self.parse("Cursor", [
            {"type": "assistant", "message": {"content": [{"text": "I'll "}]}, "timestamp_ms": 1},
            {"type": "assistant", "message": {"content": [{"text": "run it."}]}, "timestamp_ms": 2},
            {"type": "tool_call", "subtype": "started", "tool_call": {"shellToolCall": {"args": {"command": "ls"}}}},
            {"type": "assistant", "message": {"content": [{"text": "I'll run it."}]}, "model_call_id": "m", "timestamp_ms": 3},
            {"type": "assistant", "message": {"content": [{"text": "DONE"}]}, "timestamp_ms": 4},
            {"type": "assistant", "message": {"content": [{"text": "DONE"}]}},
        ])
        self.assertEqual(text, "I'll run it.DONE")

    def test_kiro_and_antigravity(self):
        text, events = self.parse("Kiro", [
            {"type": "metadata", "data": {"sessionId": "k"}},
            {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call", "title": "Running: ls"}}},
            {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                          "content": {"text": "DONE"}}}},
            {"type": "metadata", "data": {"sessionId": "k", "meteringUsage": [{"value": 0.2}, {"value": 0.05}]}},
            {"type": "runError", "data": {"message": "tools.58 schema"}},
        ])
        self.assertEqual(text, "DONE")
        self.assertIn({"kind": "tool", "label": "$ ls"}, events)
        self.assertIn({"kind": "usage", "credits": 0.25}, events)
        self.assertEqual(events[-1]["kind"], "error")
        text, events = self.parse("Antigravity", [
            {"event": "init", "conversation_id": "c"},
            {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                                                     "tool_info": {"parameters": {"CommandLine": "ls"}}}},
            {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "DONE"}},
            {"event": "result", "result": {"status": "SUCCESS", "usage": {"input_tokens": 3, "output_tokens": 1}}},
        ])
        self.assertEqual(text, "DONE")
        self.assertIn({"kind": "tool", "label": "$ ls"}, events)

    def test_resume_argv(self):
        agent = {"name": "Codex", "argv": ["x"]}
        engine = star_engines.CodexEngine(agent)
        engine.exe = "/bin/codex"
        with patch.object(star_engines, "codex_compat_overrides", return_value=[]):
            argv = engine.argv("hi", "gpt-6-luna", "low", "th")
        self.assertEqual(argv[:4], ["/bin/codex", "exec", "resume", "th"])
        self.assertEqual(argv[-1], "hi")

    def test_codex_overrides_only_rejected_values(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            config.write_text(textwrap.dedent('''
                [mcp_servers.iMCP.tools.events_create]
                approval_mode = "never"
                [mcp_servers.other.tools.ok]
                approval_mode = "approve"
            '''))
            self.assertEqual(star_engines.codex_compat_overrides(config),
                             ["-c", 'mcp_servers.iMCP.tools.events_create.approval_mode="approve"'])

    def test_kiro_agent_leaves_mcp_out(self):
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home) / ".kiro" / "agents"
            folder.mkdir(parents=True)
            (folder / "herdr.json").write_text(json.dumps({"name": "herdr", "includeMcpJson": True, "hooks": {"x": 1}}))
            self.assertEqual(star_engines.ensure_kiro_agent(home), "star")
            made = json.loads((folder / "star.json").read_text())
            self.assertFalse(made["includeMcpJson"])
            self.assertEqual(made["hooks"], {"x": 1})
            self.assertTrue(json.loads((folder / "herdr.json").read_text())["includeMcpJson"])


FAKE = '''#!{python}
import json, sys
argv = sys.argv[1:]
open({log!r}, "a").write(json.dumps(argv) + "\\n")
if {fail}:
    print("auth expired", file=sys.stderr)
    sys.exit(1)
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "sess-1"}}))
print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
      "delta": {{"type": "text_delta", "text": "# Done\\n- fixed **it**\\n"}}}}}}))
print(json.dumps({{"type": "result", "session_id": "sess-1", "usage": {{"input_tokens": 10, "output_tokens": 3}}}}))
'''


# A Claude stand-in for permission prompts: it starts the MCP server named in
# --mcp-config, as Claude would, and asks its prompt tool about two commands.
APPROVING_CLAUDE = '''
import json, os, subprocess, sys
argv = sys.argv[1:]
open(os.path.join(os.path.dirname(__file__), "approvals.argv"), "w").write(json.dumps(argv))
server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["star"]
tool = argv[argv.index("--permission-prompt-tool") + 1].split("__")[-1]
proc = subprocess.Popen([server["command"]] + server["args"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        text=True, env={**os.environ, **server["env"]})
def call(n, method, params):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}) + "\\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())
call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake"}})
assert call(2, "tools/list", {})["result"]["tools"][0]["name"] == tool
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-approve"}), flush=True)
for n, target in ((3, "build"), (4, "dist")):
    answer = call(n, "tools/call", {"name": tool, "arguments": {"tool_name": "Bash",
                  "input": {"command": "rm -rf " + target}, "tool_use_id": str(n)}})
    decision = json.loads(answer["result"]["content"][0]["text"])
    text = decision["behavior"] + ":" + decision.get("updatedInput", {}).get("command", "") + " "
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": text}}}), flush=True)
proc.stdin.close()
proc.wait()
print(json.dumps({"type": "result", "session_id": "sess-approve", "usage": {}}), flush=True)
'''


# A `codex app-server` stand-in with the schema's field names. Its turn runs a
# command that needs approval and reports what the client decided.
FAKE_APP_SERVER = '''#!{python}
import json, sys
log = open({log!r}, "a")
def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()
def receive():
    message = json.loads(sys.stdin.readline())
    log.write(json.dumps(message) + "\\n")
    log.flush()
    return message
assert sys.argv[1] == "app-server"
init = receive()
send({{"id": init["id"], "result": {{"userAgent": "fake"}}}})
assert receive()["method"] == "initialized"
start = receive()
thread = start["params"].get("threadId", "thread-new")
send({{"method": "mcpServer/startupStatus/updated", "params": {{}}}})
send({{"id": start["id"], "result": {{"thread": {{"id": thread}}}}}})
turn = receive()
send({{"id": turn["id"], "result": {{"turn": {{"id": "turn-1"}}}}}})
for n, command in enumerate(("rm -rf build", "rm -rf dist")):
    send({{"method": "item/started", "params": {{"item": {{"type": "commandExecution", "id": "cmd%d" % n,
          "command": command, "commandActions": [], "cwd": "/w", "status": "inProgress"}}}}}})
    send({{"id": 100 + n, "method": "item/commandExecution/requestApproval",
          "params": {{"itemId": "cmd%d" % n, "threadId": thread, "turnId": "turn-1", "startedAtMs": 1,
                     "command": command}}}})
    decision = receive()["result"]["decision"]
    send({{"method": "item/completed", "params": {{"item": {{"type": "agentMessage", "id": "m%d" % n,
          "text": decision + ":" + command}}}}}})
send({{"id": 200, "method": "item/tool/requestUserInput", "params": {{}}}})
assert receive()["error"]["code"] == -32601
send({{"method": "thread/tokenUsage/updated", "params": {{"threadId": thread, "turnId": "turn-1", "tokenUsage": {{
      "total": {{"inputTokens": 150, "cachedInputTokens": 20, "outputTokens": 9, "reasoningOutputTokens": 0, "totalTokens": 159}},
      "last": {{"inputTokens": 50, "cachedInputTokens": 20, "outputTokens": 9, "reasoningOutputTokens": 0, "totalTokens": 59}}}}}}}})
send({{"method": "turn/completed", "params": {{"threadId": thread, "turn": {{"id": "turn-1", "status": "completed", "items": []}}}}}})
sys.stdin.readline()
'''


# A `kiro-cli acp` stand-in speaking the Agent Client Protocol.
FAKE_KIRO_ACP = '''#!{python}
import json, sys
open({log!r}, "w").write(json.dumps(sys.argv[1:]))
def send(message):
    sys.stdout.write(json.dumps(dict(jsonrpc="2.0", **message)) + "\\n")
    sys.stdout.flush()
def receive():
    return json.loads(sys.stdin.readline())
init = receive()
assert init["params"]["clientCapabilities"]["terminal"] is False
send({{"id": init["id"], "result": {{"protocolVersion": 1, "agentCapabilities": {{"loadSession": True}}}}}})
load = receive()
assert load["method"] == "session/load", load
sid = load["params"]["sessionId"]
update = lambda text: send({{"method": "session/update", "params": {{"sessionId": sid, "update": {{
    "sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": text}}}}}}}})
update("OLD HISTORY ")
send({{"id": load["id"], "result": {{}}}})
prompt = receive()
assert prompt["method"] == "session/prompt"
options = [{{"optionId": "yes", "name": "Yes", "kind": "allow_once"}},
           {{"optionId": "always", "name": "Always", "kind": "allow_always"}},
           {{"optionId": "no", "name": "No", "kind": "reject_once"}}]
for n, command in enumerate(("rm -rf build", "rm -rf dist")):
    send({{"method": "session/update", "params": {{"sessionId": sid, "update": {{"sessionUpdate": "tool_call",
          "toolCallId": str(n), "title": "Running: " + command, "status": "pending"}}}}}})
    send({{"id": 50 + n, "method": "session/request_permission", "params": {{"sessionId": sid, "options": options,
          "toolCall": {{"toolCallId": str(n), "title": "Running: " + command, "kind": "execute",
                       "rawInput": {{"command": command}}, "_meta": {{"kiro": {{"toolName": "execute_bash"}}}}}}}}}})
    answer = receive()
    update(answer["result"]["outcome"]["optionId"] + ":" + command + " ")
send({{"id": 90, "method": "fs/read_text_file", "params": {{"sessionId": sid, "path": "/etc/hosts"}}}})
assert receive()["error"]["code"] == -32601
send({{"id": prompt["id"], "result": {{"stopReason": "end_turn"}}}})
sys.stdin.readline()
'''


class StarSessionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.patches = [patch.object(starcli, "STATE", self.dir), patch.object(starcli, "LIVE", self.dir / "live"),
                        patch.object(starcli, "CONVERSATIONS", self.dir / "conversations"),
                        patch.object(starcli, "LAST", self.dir / "last.json"),
                        patch.object(starcli, "refresh_meters", lambda *a, **k: None),
                        patch.object(starcli, "report_herdr", lambda *a, **k: None),
                        patch.object(ap, "load_usage", lambda: None),
                        patch.dict(ap.USAGE, fresh(), clear=True),
                        patch.object(star_router.time, "time", return_value=NOW)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def fake(self, name, fail=False):
        path = self.dir / f"fake-{name}"
        path.write_text(FAKE.format(python=sys.executable, log=str(self.dir / f"{name}.log"), fail=fail))
        path.chmod(0o755)
        return str(path)

    def star(self, fakes):
        class ScriptEngine(star_engines.ClaudeEngine):
            def argv(self, *args, **kwargs):
                # Invoke the fake through Python on both platforms. Windows
                # does not execute Unix shebang files as native programs.
                return [sys.executable] + super().argv(*args, **kwargs)

        star = starcli.Star(starcli.Conversation({"cwd": str(self.dir)}))
        star.engines = {}
        for name, fail in fakes.items():
            engine = ScriptEngine({"name": "Claude", "argv": ["x"]})   # every fake speaks Claude's stream
            engine.name, engine.exe = name, self.fake(name, fail)
            star.engines[name] = engine
        star.names = list(fakes)
        return star

    def test_headless_turn_never_overrides_a_profile_requiring_native_approvals(self):
        star = self.star({"Claude": False, "Codex": False})
        star.pinned = "Claude"
        star.engines["Claude"].asks = False   # a provider whose headless mode cannot ask
        with patch.object(starcli, "bypass_enabled", return_value=False), \
                patch.object(star, "turn") as turn, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(star.run("edit the source"), ("", "blocked"))
            turn.assert_not_called()
            self.assertNotIn("Claude", star.down)
            self.assertIn("/open Claude", out.getvalue())
            saved = starcli.Conversation.load(star.convo.id)
            self.assertIn("edit the source", saved.notes[-1])
            self.assertIn("provider permission settings", star.hint()[0])
        # Plan mode still runs using the provider's read-only controls.
        star.plan = True
        with patch.object(starcli, "bypass_enabled", return_value=False), \
                patch.object(star, "turn", return_value=("analysis", None, None, False, "")) as turn, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(star.run("analyze the source"), ("analysis", "ok"))
            turn.assert_called_once()

    def test_claude_asks_the_star_user_through_the_approval_server(self):
        """A fake Claude reads --mcp-config, starts the real approval server and
        asks it twice; the user allows the first with "a", so the second needs no key."""
        client = self.dir / "fake-approving-claude"
        client.write_text(APPROVING_CLAUDE)
        star = self.star({"Claude": False})
        star.engines["Claude"].exe = str(client)
        star.member = MagicMock(role="", brief=MagicMock(return_value=""), peers=MagicMock(return_value=[]))
        keys_pressed, questions = ["a"], []

        class Keys:
            active, typed, question = True, "", None

            def __init__(self, on_escape):
                self.stop = threading.Event()

            def __enter__(self):
                def press():
                    while not self.stop.is_set():
                        if self.question and keys_pressed:
                            questions.append(True)
                            self.question.key(keys_pressed.pop(0))
                        time.sleep(.02)
                threading.Thread(target=press, daemon=True).start()
                return self

            def __exit__(self, *exc):
                self.stop.set()

        with patch.object(starcli, "bypass_enabled", return_value=False), \
                patch.object(starcli, "KeyWatch", Keys), redirect_stdout(io.StringIO()) as out:
            reply, status = star.run("clean the build")
        self.assertEqual(status, "ok", out.getvalue())
        self.assertEqual(reply.strip(), "allow:rm -rf build allow:rm -rf dist")
        self.assertEqual(len(questions), 1)
        self.assertEqual(star.approved, {"Bash"})
        self.assertIn("wants to $ rm -rf build", out.getvalue())
        argv = json.loads((self.dir / "approvals.argv").read_text())
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "mcp__star__approve")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        blocked = [c for c in star.member.beat.call_args_list if c.kwargs.get("status") == "blocked"]
        self.assertEqual(blocked[0].kwargs["task"], "approve: $ rm -rf build")
        self.assertFalse(any((self.dir / "approvals").iterdir()))

    def test_routing_skips_providers_that_cannot_ask_for_approval(self):
        star = self.star({"Codex": False, "Claude": False})
        star.engines["Codex"].asks = False
        star.pinned = None
        with patch.object(starcli, "bypass_enabled", return_value=False), \
                patch.object(star, "turn", return_value=("done", None, None, False, "")) as turn, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(star.run("edit the source"), ("done", "ok"))
        self.assertEqual(turn.call_args[0][1].provider, "Claude")
        self.assertIn("Codex needs native approvals", turn.call_args[0][3])

    @unittest.skipIf(os.name == "nt", "the fake app-server is a Unix script")
    def test_codex_approvals_run_through_the_app_server_client(self):
        server = self.dir / "fake-codex-app-server"
        log = self.dir / "app-server.log"
        server.write_text(FAKE_APP_SERVER.format(python=sys.executable, log=str(log)))
        server.chmod(0o755)
        star = self.star({"Codex": False})
        engine = star_engines.CodexEngine({"name": "Codex", "argv": ["codex", "--ask-for-approval", "on-request",
                                                                    "--sandbox", "workspace-write"]})
        engine.exe = str(server)
        star.engines["Codex"] = engine
        star.convo.sessions["Codex"] = "thread-saved"
        keys_pressed = ["y", "n"]

        class Keys:
            active, typed, question = True, "", None

            def __init__(self, on_escape):
                self.stop = threading.Event()

            def __enter__(self):
                def press():
                    while not self.stop.is_set():
                        if self.question and keys_pressed:
                            self.question.key(keys_pressed.pop(0))
                        time.sleep(.02)
                threading.Thread(target=press, daemon=True).start()
                return self

            def __exit__(self, *exc):
                self.stop.set()

        with patch.object(starcli, "bypass_enabled", return_value=False), \
                patch.object(starcli, "KeyWatch", Keys), redirect_stdout(io.StringIO()) as out:
            reply, status = star.run("clean the build")
        self.assertEqual(status, "ok", out.getvalue())
        self.assertEqual(reply.split("\n\n"), ["accept:rm -rf build", "decline:rm -rf dist"])
        self.assertIn("$ rm -rf build", out.getvalue())
        self.assertEqual(star.convo.sessions["Codex"], "thread-saved")
        sent = [json.loads(line) for line in log.read_text().splitlines()]
        resume = next(m for m in sent if m.get("method") == "thread/resume")["params"]
        self.assertEqual(resume["threadId"], "thread-saved")
        self.assertEqual((resume["approvalPolicy"], resume["sandbox"]), ("on-request", "workspace-write"))
        self.assertEqual(Path(resume["cwd"]).resolve(), self.dir.resolve())
        turn = next(m for m in sent if m.get("method") == "turn/start")["params"]
        self.assertIn("clean the build", turn["input"][0]["text"])
        self.assertEqual(star.convo.turns[-1]["provider"], "Codex")

    @unittest.skipIf(os.name == "nt", "the fake ACP agent is a Unix script")
    def test_kiro_permissions_run_through_the_acp_client(self):
        server = self.dir / "acp-kiro-agent"   # not fake-Kiro: macOS paths ignore case
        log = self.dir / "kiro-acp.argv"
        server.write_text(FAKE_KIRO_ACP.format(python=sys.executable, log=str(log)))
        server.chmod(0o755)
        star = self.star({"Kiro": False})
        engine = star_engines.KiroEngine({"name": "Kiro", "argv": ["kiro-cli", "chat", "--trust-tools=fs_read"]})
        engine.exe = str(server)
        star.engines["Kiro"] = engine
        star.convo.sessions["Kiro"] = "kiro-saved"
        keys_pressed = ["y", "n"]

        class Keys:
            active, typed, question = True, "", None

            def __init__(self, on_escape):
                self.stop = threading.Event()

            def __enter__(self):
                def press():
                    while not self.stop.is_set():
                        if self.question and keys_pressed:
                            self.question.key(keys_pressed.pop(0))
                        time.sleep(.02)
                threading.Thread(target=press, daemon=True).start()
                return self

            def __exit__(self, *exc):
                self.stop.set()

        with patch.object(starcli, "bypass_enabled", return_value=False), \
                patch.object(star_engines, "ensure_kiro_agent", return_value="star"), \
                patch.object(starcli, "KeyWatch", Keys), redirect_stdout(io.StringIO()) as out:
            reply, status = star.run("clean the build")
        self.assertEqual(status, "ok", out.getvalue())
        self.assertEqual(reply.strip(), "yes:rm -rf build no:rm -rf dist")
        self.assertNotIn("OLD HISTORY", out.getvalue())
        self.assertIn("wants to $ rm -rf build", out.getvalue())
        self.assertEqual(star.convo.sessions["Kiro"], "kiro-saved")
        flags = json.loads(log.read_text())
        self.assertEqual(flags[0], "acp")
        self.assertIn("--trust-tools=fs_read", flags)
        self.assertEqual(flags[flags.index("--agent") + 1], "star")
        self.assertNotIn("--trust-all-tools", flags)

    def test_a_denied_or_unaskable_request_is_refused(self):
        folder = self.dir / "approvals-unit"
        folder.mkdir()
        (folder / "1.req").write_text(json.dumps({"tool_name": "Write", "input": {"file_path": "a.txt"}}))
        keys = MagicMock(active=False)
        with redirect_stdout(io.StringIO()) as out, starcli.Approvals(folder, "Claude", keys, set()):
            for _ in range(100):
                if (folder / "1.res").exists():
                    break
                time.sleep(.02)
        decision = json.loads((folder / "1.res").read_text())
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("no terminal", decision["message"])
        self.assertIn("write a.txt", out.getvalue())
        # A key that is not an answer stays typed text; esc denies.
        keys = MagicMock(active=True)
        (folder / "2.req").write_text(json.dumps({"tool_name": "Bash", "input": {"command": "ls"}}))
        with redirect_stdout(io.StringIO()), starcli.Approvals(folder, "Claude", keys, set()) as asking:
            for _ in range(100):
                if keys.question is asking:
                    break
                time.sleep(.02)
            self.assertFalse(asking.key("x"))
            self.assertTrue(asking.key("esc"))
            for _ in range(100):
                if (folder / "2.res").exists():
                    break
                time.sleep(.02)
        self.assertEqual(json.loads((folder / "2.res").read_text())["behavior"], "deny")

    def test_native_interface_protects_the_star_pane_and_does_not_claim_it(self):
        star = self.star({"Claude": False})
        star.member = MagicMock()
        star.convo.sessions["Claude"] = "saved-session"
        star.engines["Claude"].native = MagicMock(return_value=["native-claude", "--resume", "saved-session"])
        def launch(argv, **kwargs):
            self.assertEqual(argv, ["native-claude", "--resume", "saved-session"])
            self.assertEqual(kwargs["cwd"], str(self.dir))
            self.assertNotIn("HERDR_ENV", kwargs["env"])
            self.assertNotIn("HERDR_PANE_ID", kwargs["env"])
            self.assertEqual(kwargs["env"]["STAR_MESH_SOCKET"], "/test/server.sock")
            star.member.beat.assert_called_with(status="working", provider="Claude",
                                                task="native interface", ready=False, draft=False)
            raise OSError("fixture launch failure")
        with patch.dict(os.environ, HERDR_ENV="1", HERDR_PANE_ID="w1:p2", HERDR_SOCKET_PATH="/test/server.sock"), \
                patch.object(starcli.subprocess, "call", side_effect=launch), \
                patch.object(star, "show_goal_state") as restore, redirect_stdout(io.StringIO()):
            starcli.Commands(star).do_open("Claude")
            restore.assert_called_once()

    def test_a_provider_command_runs_as_a_routed_turn_and_builtins_win(self):
        star = self.star({"Claude": False})
        folder = self.dir / ".claude/commands"
        folder.mkdir(parents=True)
        (folder / "ship-it.md").write_text("---\nallowed-tools: Bash\n---\nShip $ARGUMENTS carefully.")
        (folder / "status.md").write_text("not this")
        with patch.object(star, "run", return_value=("", "ok")) as run, redirect_stdout(io.StringIO()) as out:
            self.assertTrue(starcli.Commands(star)("/Ship-It the release"))
            run.assert_called_once_with("Ship the release carefully.")
            starcli.Commands(star)("/status")
            run.assert_called_once()
            starcli.Commands(star)("/no-such-thing")
        self.assertIn("unknown command /no-such-thing", out.getvalue())

    def test_a_lead_can_hand_crew_a_provider_command_by_name(self):
        star = self.star({"Claude": False})
        folder = self.dir / ".claude/commands"
        folder.mkdir(parents=True)
        (folder / "ship-it.md").write_text("Ship $ARGUMENTS carefully.")
        with patch.object(star, "run", return_value=("shipped", "ok")) as run, \
                patch.object(starcli.star_mesh, "reply") as reply, redirect_stdout(io.StringIO()):
            star.handle({"kind": "task", "id": "t1", "from": "lead", "text": "/ship-it the release"})
        framed = run.call_args[0][0]
        self.assertIn("Ship the release carefully.", framed)
        self.assertNotIn("/ship-it", framed)
        self.assertEqual(reply.call_args[0][1], "shipped")

    def test_plan_mode_does_not_open_a_native_profile_with_write_permissions(self):
        star = self.star({"Claude": False})
        star.plan = True
        with patch.object(starcli.subprocess, "call") as launch, redirect_stdout(io.StringIO()) as out:
            starcli.Commands(star).do_open("Claude")
        launch.assert_not_called()
        self.assertIn("leave Star plan mode", out.getvalue())

    def test_failed_provider_reroutes_and_bridges_context(self):
        star = self.star({"Codex": True, "Claude": False})
        out = io.StringIO()
        with redirect_stdout(out):
            star.run("fix the failing test, it throws KeyError")
        text = ANSI.sub("", out.getvalue())
        self.assertIn("✗ Codex: auth expired", text)
        self.assertIn("after Codex failed", text)
        self.assertIn("fixed it", text)
        self.assertEqual(star.convo.turns[-1]["provider"], "Claude")
        self.assertEqual(star.convo.sessions["Claude"], "sess-1")
        self.assertIn("Codex", star.down)
        self.assertFalse(list((self.dir / "live").glob("*.json")), "the live-turn file is removed after the turn")

        # the next turn resumes Claude's own session
        with redirect_stdout(io.StringIO()):
            star.run("now add a test for it")
        last = json.loads((self.dir / "Claude.log").read_text().splitlines()[-1])
        self.assertEqual(last[last.index("--resume") + 1], "sess-1")

    def test_quota_failure_rejoins_after_reset_but_auth_failure_does_not(self):
        star = self.star({"Claude": False, "Codex": False})
        star.pinned = "Claude"
        choice = star.route("fix the bug")[1][0]
        ap.USAGE["Claude"] = meter(("5h", 80, 60), ("wk", 40, 86400))
        star.mark_failure(choice, "usage limit reached")
        self.assertFalse(star.route("fix the bug")[1])
        with patch.object(star_router.time, "time", return_value=NOW + 61):
            self.assertEqual(star.route("fix the bug")[1][0].provider, "Claude")
        star.mark_failure(choice, "auth expired")
        ap.USAGE["Claude"] = meter(("5h", 0, 18000), fetched=NOW + 100)
        with patch.object(star_router.time, "time", return_value=NOW + 200):
            self.assertFalse(star.route("fix the bug")[1])
            self.assertIn("Claude", star.down)

    def test_newer_healthy_meter_can_recover_a_quota_failure_early(self):
        star = self.star({"Claude": False})
        star.pinned = "Claude"
        choice = star.route("fix the bug")[1][0]
        ap.USAGE["Claude"] = meter(("5h", 85, 3600))
        star.mark_failure(choice, "rate limit exceeded")
        self.assertFalse(star.route("fix the bug")[1])
        ap.USAGE["Claude"] = meter(("5h", 20, 3600), fetched=NOW + 10)
        with patch.object(star_router.time, "time", return_value=NOW + 11):
            self.assertTrue(star.route("fix the bug")[1])

    def test_a_provider_joining_late_gets_a_recap(self):
        convo = starcli.Conversation({"cwd": str(self.dir)})
        convo.record("Codex", "first question", "first answer", "th")
        convo.record("Codex", "second question", "x" * 2000, "th")
        prompt = convo.prompt_for("Claude", "third question")
        self.assertIn("first answer", prompt)
        self.assertIn("[…]", prompt)
        self.assertTrue(prompt.endswith("third question"))
        self.assertEqual(convo.prompt_for("Codex", "third question"), "third question")

    def test_partial_failure_keeps_work_and_session_for_the_next_provider(self):
        star = self.star({"Codex": False, "Claude": False})
        path = Path(star.engines["Codex"].exe)
        script = path.read_text()
        script = script.replace('"# Done\\n- fixed **it**\\n"', '"Changed a.py. GOAL COMPLETE"')
        script += '\nprint("quota exhausted", file=sys.stderr)\nsys.exit(1)\n'
        path.write_text(script)
        star.convo.notes.append("crew1 found an edge case")
        with redirect_stdout(io.StringIO()):
            reply, status = star.run("fix the failing test")
        self.assertEqual(status, "ok")
        self.assertIn("fixed **it**", reply)
        self.assertIn("Codex", star.down)
        failed = star.convo.turns[-2]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "quota exhausted")
        self.assertEqual(star.convo.sessions["Codex"], "sess-1")
        self.assertIn("Changed a.py", failed["reply"])
        launched = json.loads((self.dir / "Claude.log").read_text().splitlines()[0])
        bridge = launched[launched.index("-p") + 1]
        self.assertIn("quota exhausted", bridge)
        self.assertIn("Inspect the current state", bridge)
        self.assertIn("Changed a.py", bridge)
        self.assertIn("crew1 found an edge case", bridge)
        loaded = starcli.Conversation.load(star.convo.id)
        self.assertEqual(loaded.turns[-2]["status"], "failed")

    def test_partial_failure_without_a_fallback_pauses_the_goal(self):
        star = self.star({"Codex": False})
        path = Path(star.engines["Codex"].exe)
        script = path.read_text().replace('"# Done\\n- fixed **it**\\n"', '"GOAL COMPLETE"')
        path.write_text(script + '\nprint("quota exhausted", file=sys.stderr)\nsys.exit(1)\n')
        with redirect_stdout(io.StringIO()):
            star.pursue("fix the failing test")
        self.assertEqual(star.convo.goal, "fix the failing test")
        self.assertEqual(star.convo.turns[-1]["status"], "failed")

    def test_a_goal_continues_past_twenty_turns_until_it_is_complete(self):
        star = self.star({"Claude": False})
        calls = []
        def run(prompt, **kwargs):
            calls.append(prompt)
            return ("GOAL COMPLETE" if len(calls) == 23 else "GOAL CONTINUES: next requirement"), "ok"
        star.run = run
        with redirect_stdout(io.StringIO()):
            self.assertEqual(star.pursue("finish every requirement"), "complete")
        self.assertEqual(len(calls), 23)
        self.assertEqual(star.convo.goal, "")

    def test_an_explicit_goal_turn_budget_remains_resumable(self):
        star = self.star({"Claude": False})
        star.run = lambda *a, **k: ("GOAL CONTINUES: remaining work", "ok")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(star.pursue("finish the migration", steps=2), "paused")
        self.assertEqual(star.convo.goal, "finish the migration")

    def test_a_failed_request_save_blocks_before_any_provider_launch(self):
        star = self.star({"Claude": False})
        with patch.object(star.convo, "save", return_value=False), \
                patch.object(star, "turn") as turn, redirect_stdout(io.StringIO()):
            self.assertEqual(star.run("edit important.txt"), ("", "blocked"))
        turn.assert_not_called()

    def test_recovery_warning_reaches_the_resumed_native_provider(self):
        star = self.star({"Claude": False})
        star.convo.sessions["Claude"] = "interrupted-session"
        star.convo.inflight = {"provider": "Claude", "prompt": "preserve all original data"}
        with redirect_stdout(io.StringIO()):
            star.run("continue safely")
        argv = json.loads((self.dir / "Claude.log").read_text().splitlines()[0])
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("--resume", argv)
        self.assertIn("interrupted-session", argv)
        self.assertIn("previous Star process stopped", prompt)
        self.assertIn("preserve all original data", prompt)
        saved = starcli.Conversation.load(star.convo.id)
        self.assertIsNone(saved.inflight)
        self.assertEqual(saved.turns[0]["status"], "interrupted")
        self.assertIn("preserve all original data", saved.requests)

    def test_missing_native_session_resends_context(self):
        convo = starcli.Conversation({"summary": "earlier decisions", "cwd": str(self.dir)})
        convo.record("Codex", "first request", "first answer", None, "crew brief")
        prompt = convo.prompt_for("Codex", "next request", "crew brief")
        for text in ("earlier decisions", "crew brief", "first request", "first answer"):
            self.assertIn(text, prompt)

    def test_switching_provider_keeps_early_user_constraints_after_history_is_trimmed(self):
        convo = starcli.Conversation({"cwd": str(self.dir)})
        constraint = "Work only in src/parser.py. Do not commit or send external messages."
        convo.record("Codex", constraint, "understood", "codex-session")
        for n in range(70):
            convo.record("Codex", f"follow-up request {n}", "x" * 1000, "codex-session")
        convo.save()
        loaded = starcli.Conversation.load(convo.id)
        self.assertEqual(len(loaded.turns), 60)
        self.assertNotIn(constraint, [turn["user"] for turn in loaded.turns])
        prompt = loaded.prompt_for("Claude", "continue the task")
        self.assertIn(constraint, prompt)
        self.assertIn("follow-up request 0", prompt)
        self.assertIn("follow-up request 69", prompt)
        self.assertIn("unless a later user message supersedes", prompt)
        self.assertEqual(loaded.prompt_for("Codex", "continue the task"), "continue the task")

    def test_compacting_preserves_user_constraints_beyond_the_model_summary(self):
        star = self.star({"Codex": False})
        constraint = "Do not modify shipping files or contact anyone."
        star.convo.record("Codex", constraint, "understood", "codex-session")
        star.run = lambda *a, **k: ("summary of completed work", "ok")
        with redirect_stdout(io.StringIO()):
            starcli.Commands(star).do_compact("")
        self.assertEqual(star.convo.turns, [])
        self.assertIn(constraint, star.convo.prompt_for("Claude", "next task"))

    def test_lead_uses_strong_models_and_crew_keeps_task_routing(self):
        star = self.star({name: False for name in ALL})
        star.member = SimpleNamespace(role="lead")
        task, choices, skipped = star.route("ship it")
        self.assertEqual(task.tier, "heavy")
        self.assertTrue(all(c.tier == "heavy" for c in choices))
        self.assertIn("Antigravity", skipped)
        self.assertIn("orchestration", task.reasons[-1])
        star.member.role = "crew"
        self.assertEqual(star.route("run the tests")[0].tier, "light")
        star.member.role = "lead"
        star.tier = "standard"
        self.assertEqual(star.route("ship it")[0].tier, "standard")

    def test_print_mode_reports_provider_failure_to_the_shell(self):
        star = self.star({"Codex": True})
        with patch.object(starcli, "Star", return_value=star), patch.object(ap, "load_usage"), \
                redirect_stdout(io.StringIO()):
            code = starcli.main(["-p", "fix the bug"])
        self.assertEqual(code, 1)
        self.assertEqual(starcli.Conversation.load(star.convo.id).turns[-1]["status"], "failed")

    def test_print_goal_reports_blocked_and_keeps_the_objective(self):
        star = self.star({"Codex": False})
        path = Path(star.engines["Codex"].exe)
        path.write_text(path.read_text().replace('"# Done\\n- fixed **it**\\n"', '"GOAL BLOCKED: need a login"'))
        # A provider's shell tool can hand Star a pipe that never closes.
        pipe = MagicMock(isatty=MagicMock(return_value=False), read=MagicMock(side_effect=AssertionError("stdin read")))
        with patch.object(starcli, "Star", return_value=star), patch.object(ap, "load_usage"), \
                patch.object(starcli.sys, "stdin", pipe), redirect_stdout(io.StringIO()):
            code = starcli.main(["-p", "--goal", "fix the bug"])
        self.assertEqual(code, 2)
        self.assertEqual(starcli.Conversation.load(star.convo.id).goal, "fix the bug")

    def test_print_mode_honors_an_explicit_mesh_role(self):
        import star_mesh
        star = self.star({"Codex": False})
        patches = [patch.object(star_mesh, key, self.dir / key.lower())
                   for key in ("ROOT", "MEMBERS", "INBOX", "REPLIES")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with patch.object(starcli, "Star", return_value=star), patch.dict(os.environ), redirect_stdout(io.StringIO()):
            code = starcli.main(["-p", "--name", "lead", "--role", "lead", "ship it"])
        self.assertEqual(code, 0)
        self.assertEqual(star.last[0].tier, "heavy")
        args = json.loads((self.dir / "Codex.log").read_text().splitlines()[0])
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertFalse(list(star_mesh.MEMBERS.glob("*.json")))


class ProviderCommandTests(unittest.TestCase):
    """Commands and skills a native pane accepts by name keep working in StarCLI."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home, self.project = Path(folder.name) / "home", Path(folder.name) / "project"
        self.project.mkdir(parents=True)

    def put(self, path, text="x"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_discovery_covers_skills_plugins_commands_and_project_overrides(self):
        import star_commands
        h = self.home
        synced = self.put(h / ".claude/skills/synced/acct/deep-research/SKILL.md")
        for plugin, skill in (("design", "ux-copy"), ("design", "review"), ("sales", "review")):
            self.put(h / f".claude/plugins/synced/acct/{plugin}/.claude-plugin/plugin.json",
                     json.dumps({"name": plugin}))
            self.put(h / f".claude/plugins/synced/acct/{plugin}/skills/{skill}/SKILL.md")
        nested = self.put(h / ".claude/commands/git/sync.md")
        prompt = self.put(h / ".codex/prompts/triage.md")
        kiro = self.put(h / ".kiro/skills/herdr/SKILL.md")
        local = self.put(self.project / ".claude/skills/deep-research/SKILL.md")
        found = star_commands.find(self.project, home=h)
        self.assertEqual(found["design:ux-copy"][0], "skill")
        self.assertEqual(found["ux-copy"], found["design:ux-copy"])
        # Two plugins share "review", so only their qualified names exist.
        self.assertNotIn("review", found)
        self.assertIn("sales:review", found)
        self.assertEqual(found["git:sync"], ("command", nested))
        self.assertEqual(found["prompts:triage"], ("command", prompt))
        self.assertEqual(found["herdr"], ("skill", kiro))
        self.assertEqual(found["deep-research"], ("skill", local))
        self.assertNotEqual(synced, local)

    def test_expansion_matches_the_native_template_rules(self):
        import star_commands
        template = self.put(self.project / "fix.md", "---\ndescription: fix it\n---\nFix issue $1 ($ARGUMENTS) now.\n")
        self.assertEqual(star_commands.expand("command", template, '42 "in auth"'),
                         'Fix issue 42 (42 "in auth") now.')
        plain = self.put(self.project / "plain.md", "Review the diff.")
        self.assertEqual(star_commands.expand("command", plain, "only tests"),
                         "Review the diff.\n\nARGUMENTS: only tests")
        self.assertEqual(star_commands.expand("command", plain, ""), "Review the diff.")
        self.assertEqual(star_commands.expand("command", template, "costs $1"), "Fix issue costs (costs $1) now.")
        skill = self.put(self.project / "s/SKILL.md")
        text = star_commands.expand("skill", skill, "renewables")
        self.assertIn(str(skill), text)
        self.assertIn("Request: renewables", text)


class StarMeterTests(unittest.TestCase):
    AGENTS = {n: {"name": n, "color": (200, 200, 200)} for n in ALL}

    def test_summary_weights_and_reset_order(self):
        usage = fresh()
        usage["Codex"] = meter(("5h", 100, 0.5 * HOUR), ("wk", 10, 6 * DAY))   # empty for now
        info = star_meter.summary(usage, ALL, self.AGENTS, NOW)
        names = [p["name"] for p in info["parts"]]
        self.assertEqual(names[0], "Codex")   # resets first
        self.assertEqual(next(p for p in info["parts"] if p["name"] == "Codex")["now"], 0.0)
        self.assertLess(info["now"], info["long"])
        self.assertIsNone(star_meter.summary({}, ALL, self.AGENTS, NOW))

    def test_bar_is_one_gradient_lit_for_the_total(self):
        info = {"now": 57.0}
        cells, _ = star_meter.bar(info, 20)
        self.assertEqual("".join(g for g, _ in cells), "▰" * 11 + "▱" * 9)
        filling, _ = star_meter.bar(info, 20, depleting=False)
        self.assertEqual("".join(g for g, _ in filling), "▰" * 9 + "▱" * 11)

    def test_resets_row_shrinks_to_fit(self):
        info = star_meter.summary(fresh(), ALL, self.AGENTS, NOW)
        wide = "".join(ch for ch, _ in star_meter.resets_row(info, 120, None, NOW))
        self.assertTrue(wide.startswith("● Claude 2h 30m"), wide)
        self.assertIn("Gemini", wide)
        narrow = "".join(ch for ch, _ in star_meter.resets_row(info, 16, None, NOW))
        self.assertTrue(narrow.startswith("●2h"), narrow)
        self.assertLessEqual(len(narrow), 16)


    def test_provider_cooldowns_are_an_opt_in_star_setting(self):
        info = star_meter.summary(fresh(), ALL, self.AGENTS, NOW)
        self.assertEqual(ap.STAR_RESETS.default, False)
        with patch.object(panel, "star_info", return_value=(star_meter, info, set())):
            with patch.object(ap, "star_resets", return_value=False):
                hidden = panel.star_lines(60, 0.0, NOW, False, False)
            with patch.object(ap, "star_resets", return_value=True):
                shown = panel.star_lines(60, 0.0, NOW, False, False)
        self.assertEqual(len(hidden), 1)
        self.assertEqual(len(shown), 2)
        self.assertIn("●", ANSI.sub("", shown[1].paint()))

class UsagePanelStarTests(unittest.TestCase):
    def test_a_star_turn_lights_star_and_its_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            live = Path(folder)
            (live / "1.json").write_text(json.dumps({"pid": os.getpid(), "provider": "Codex", "pane": None}))
            (live / "2.json").write_text(json.dumps({"pid": 999999, "provider": "Claude", "pane": None}))   # dead
            found = {s: {} for s in panel.SCOPE_ORDER}
            working = {}
            with patch.object(panel, "STAR_LIVE", live):
                panel.add_star_turns(found, working)
            for scope in panel.SCOPE_ORDER:
                self.assertEqual(found[scope]["Codex"], {"panes": 1, "working": 1})
                self.assertEqual(found[scope]["Star"], {"panes": 1, "working": 1})
                self.assertNotIn("Claude", found[scope])
            self.assertEqual(panel.STAR_DRIVING, {"Codex"})
            self.assertFalse((live / "2.json").exists())

    def test_starcli_process_is_detected(self):
        self.assertEqual(panel.from_argv(["/usr/bin/python3", "/x/src/starcli.py", "-c"]), "Star")


if __name__ == "__main__":
    unittest.main()


class MeshTests(unittest.TestCase):
    def test_windows_liveness_checks_a_handle_without_sending_a_signal(self):
        import ctypes
        import star_mesh
        kernel = MagicMock()
        kernel.OpenProcess.return_value = 42
        with patch.object(ap, "WINDOWS", True), patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                patch.object(ctypes, "get_last_error", return_value=87, create=True) as error, \
                patch.object(os, "kill", side_effect=AssertionError("must not signal a Windows process")):
            kernel.WaitForSingleObject.return_value = 258  # still running
            self.assertTrue(star_mesh.alive(123))
            kernel.WaitForSingleObject.return_value = 0  # exited
            self.assertFalse(star_mesh.alive(123))
            kernel.CloseHandle.assert_called_with(42)
            kernel.OpenProcess.return_value = None
            self.assertFalse(star_mesh.alive(123))
            error.return_value = 5  # access denied is not proof it exited
            self.assertTrue(star_mesh.alive(123))
        for pid in (None, "bad-pid", 0, -1):
            self.assertFalse(star_mesh.alive(pid))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        import star_mesh
        self.mesh = star_mesh
        reports = patch.object(starcli, "report_herdr")
        reports.start()
        self.addCleanup(reports.stop)
        for name, value in (("ROOT", root), ("MEMBERS", root / "members"), ("INBOX", root / "inbox"),
                            ("REPLIES", root / "replies")):
            p = patch.object(star_mesh, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"HERDR_TAB_ID": "w1:t1", "HERDR_WORKSPACE_ID": "w1", "HERDR_PANE_ID": "w1:p2"})
        env.start()
        self.addCleanup(env.stop)
        # Release every member's name lock before the folder goes: Windows
        # cannot delete a file that is still open.
        created, original = [], star_mesh.Member.__init__

        def track(member, *args, **kwargs):
            original(member, *args, **kwargs)
            created.append(member)
        p = patch.object(star_mesh.Member, "__init__", track)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: [m.release_name() for m in created])

    def test_members_see_each_other_and_exchange_a_task(self):
        lead = self.mesh.Member("lead", "lead")
        crew = self.mesh.Member("crew1", "crew")
        self.assertEqual([m["name"] for m in lead.peers()], ["crew1"])
        self.assertIn("crew1 (crew)", lead.brief())
        self.assertIn("--send <name>", lead.brief())
        self.assertIn("this pane stays", lead.brief())
        self.assertIn("Do not run", lead.brief())
        self.assertIn("--handoff", lead.brief())
        self.assertNotIn("write handoff notes", lead.brief())
        self.assertIn("say so at the top", crew.brief())
        self.assertIn("Do not run", crew.brief())
        mid = self.mesh.send("crew1", "make a.txt", sender="lead", sender_is_star=True)
        [task] = crew.inbox()
        self.assertEqual((task["id"], task["text"], task["from"]), (mid, "make a.txt", "lead"))
        crew.take(task)
        self.mesh.reply(task, "done: a.txt", "crew1")
        self.assertEqual(crew.inbox(), [])
        [answer] = lead.inbox()
        self.assertEqual((answer["kind"], answer["text"], answer["from"]), ("reply", "done: a.txt", "crew1"))

    def test_send_and_wait_returns_the_reply(self):
        crew = self.mesh.Member("crew1", "crew")
        import threading

        def answer():
            for _ in range(100):
                box = crew.inbox()
                if box:
                    crew.take(box[0])
                    self.mesh.reply(box[0], "391", "crew1")
                    return
                time.sleep(0.02)

        threading.Thread(target=answer).start()
        self.assertEqual(self.mesh.send("crew1", "17*23?", wait=True, timeout=5, poll=0.02), "391")

    def test_names_are_per_tab_and_unknown_names_fail(self):
        here = self.mesh.Member(None, "solo")
        self.assertEqual(here.name, "star1")
        with patch.dict(os.environ, {"HERDR_TAB_ID": "w9:t1", "HERDR_WORKSPACE_ID": "w9"}):
            elsewhere = self.mesh.Member(None, "solo")
            self.assertEqual(elsewhere.name, "star1")   # another tab has its own names
            self.assertEqual(elsewhere.peers(), [])
        self.assertEqual(here.brief(), "")              # a solo StarCLI with no peers has nothing to add
        with patch.dict(os.environ, {"HERDR_TAB_ID": "w9:t9", "HERDR_WORKSPACE_ID": "w9"}):
            waiting = self.mesh.Member("lead", "lead")
            self.assertNotIn("Peers right now", waiting.brief())
            self.assertIn("this pane stays", waiting.brief())
        with self.assertRaises(LookupError):
            self.mesh.send("nobody", "hi")

    def test_identical_tab_ids_on_different_servers_have_separate_names_and_inboxes(self):
        with patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/server/one"}):
            first = self.mesh.Member("crew1", "crew")
            self.mesh.send("crew1", "only server one", sender="lead", sender_is_star=True)
        with patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/server/two"}):
            second = self.mesh.Member("crew1", "crew")
            self.assertEqual(second.peers(), [])
            self.assertEqual(second.inbox(), [])
            self.mesh.send("crew1", "only server two", sender="lead", sender_is_star=True)
        self.assertNotEqual(first.box, second.box)
        self.assertEqual([m["text"] for m in first.inbox()], ["only server one"])
        self.assertEqual([m["text"] for m in second.inbox()], ["only server two"])

    def test_a_name_has_one_inbox_consumer_until_its_owner_leaves(self):
        first = self.mesh.Member("crew1", "crew")
        with self.assertRaisesRegex(starcli.star_sessions.ConversationBusy, "already running"):
            self.mesh.Member("crew1", "crew")
        self.mesh.send("crew1", "one task", sender="lead")
        self.assertEqual(len(first.inbox()), 1)
        first.leave()
        replacement = self.mesh.Member("crew1", "crew")
        self.assertEqual([m["text"] for m in replacement.inbox()], ["one task"])

    def test_windows_mesh_commands_quote_executables_for_powershell(self):
        with patch.object(ap, "WINDOWS", True), \
                patch.object(self.mesh.sys, "executable", "C:\\Program Files\\Python\\python.exe"), \
                patch.object(self.mesh, "STARCLI", "C:\\Users\\Sam's Home\\starcli.py"):
            self.assertEqual(self.mesh.command(),
                             "& 'C:\\Program Files\\Python\\python.exe' 'C:\\Users\\Sam''s Home\\starcli.py'")
            self.assertIn("Wait-Job and Receive-Job", self.mesh.parallel_note())
            self.assertNotIn("`wait`", self.mesh.parallel_note())

    def test_a_task_survives_restart_and_an_answer_is_not_executed_twice(self):
        crew = self.mesh.Member("crew1", "crew")
        lead = self.mesh.Member("lead", "lead")
        root = self.mesh.ROOT
        with patch.object(starcli, "CONVERSATIONS", root / "conversations"), \
                patch.object(starcli, "LAST", root / "last"), redirect_stdout(io.StringIO()):
            worker = starcli.Star(starcli.Conversation({"cwd": str(root)}), member=crew)
            self.mesh.send("crew1", "edit x.py", sender="lead", sender_is_star=True)
            [task] = crew.inbox()

            def crash(*a, **k):
                self.assertEqual(worker.convo.mesh_tasks[task["id"]]["status"], "running")
                self.assertEqual(len(crew.inbox()), 1)
                raise RuntimeError("simulated crash after changing a file")

            worker.run = crash
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                worker.handle(task)
            loaded = starcli.Conversation.load(worker.convo.id)
            self.assertEqual(loaded.mesh_tasks[task["id"]]["status"], "running")
            restarted = starcli.Star(loaded, member=crew)
            calls = []

            def finish(prompt, **_k):
                calls.append(prompt)
                self.assertIn("An earlier attempt", prompt)
                self.assertIn("verify their effects before repeating", prompt)
                return "verified x.py", "ok"

            restarted.run = finish
            # Crash after saving the result, before delivering it or removing the task.
            with patch.object(self.mesh, "reply", side_effect=OSError("delivery failed")):
                with self.assertRaisesRegex(OSError, "delivery failed"):
                    restarted.handle(task)
            loaded = starcli.Conversation.load(worker.convo.id)
            self.assertEqual(loaded.mesh_tasks[task["id"]]["status"], "answered")
            recovered = starcli.Star(loaded, member=crew)
            recovered.run = lambda *a, **k: self.fail("executed an already answered task")
            recovered.handle(crew.inbox()[0])
            self.assertEqual(crew.inbox(), [])
            [answer] = lead.inbox()
            self.assertEqual(answer["text"], "verified x.py")
            self.assertEqual(len(calls), 1)

    def test_peer_replies_are_saved_and_duplicate_delivery_is_ignored(self):
        lead = self.mesh.Member("lead", "lead")
        root = self.mesh.ROOT
        with patch.object(starcli, "CONVERSATIONS", root / "conversations"), \
                patch.object(starcli, "LAST", root / "last"), redirect_stdout(io.StringIO()):
            star = starcli.Star(starcli.Conversation({"cwd": str(root)}), member=lead)
            answer = {"kind": "reply", "id": "task-id", "from": "crew1", "task": "find x", "text": "x.py"}
            star.handle(answer)
            loaded = starcli.Conversation.load(star.convo.id)
            self.assertEqual(len(loaded.notes), 1)
            self.assertIn("x.py", loaded.notes[0])
            resumed = starcli.Star(loaded, member=lead)
            resumed.handle(answer)
            self.assertEqual(len(resumed.convo.notes), 1)

    def test_a_goal_waits_for_async_work_and_integrates_a_late_reply(self):
        import threading
        lead = self.mesh.Member("lead", "lead")
        crew = self.mesh.Member("crew1", "crew")
        root = self.mesh.ROOT
        errors, prompts = [], []

        def work():
            try:
                deadline = time.monotonic() + 3
                while not crew.inbox() and time.monotonic() < deadline:
                    time.sleep(0.01)
                [task] = crew.inbox()
                time.sleep(0.1)
                self.mesh.reply(task, "tests passed", "crew1")
                crew.take(task)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(starcli, "CONVERSATIONS", root / "conversations"), \
                patch.object(starcli, "LAST", root / "last"), \
                patch.object(starcli, "report_herdr", lambda *a, **k: None), redirect_stdout(io.StringIO()):
            star = starcli.Star(starcli.Conversation({"cwd": str(root)}), member=lead)

            def run(prompt, **_k):
                prompts.append(prompt)
                if len(prompts) == 1:
                    self.mesh.send("crew1", "run tests", sender="lead", sender_is_star=True)
                    self.assertEqual(len(lead.outstanding()), 1)
                else:
                    self.assertEqual(star.convo.goal, "ship it")
                    self.assertTrue(any("tests passed" in note for note in star.convo.notes))
                    self.assertEqual(lead.outstanding(), [])
                star.convo.record("Codex", prompt, "GOAL COMPLETE", "session")
                return "GOAL COMPLETE", "ok"

            star.run = run
            thread = threading.Thread(target=work)
            thread.start()
            try:
                star.pursue("ship it")
            finally:
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(prompts), 2)
            self.assertEqual(star.convo.goal, "")


class EditorTests(unittest.TestCase):
    def editor(self, text):
        import star_input
        ed = star_input.Editor([("quit", "", "leave"), ("goal", "<objective>", "keep going"), ("use", "<provider>", "pin")],
                               values=lambda c: [("Codex", ""), ("Claude", "")] if c == "use" else [],
                               history=["fix the parser bug", "/goal ship it"], usage={"goal": 3})
        ed.text, ed.pos, ed.sel = text, len(text), 0
        return ed

    def test_slash_menu_ghost_and_values(self):
        ed = self.editor("/qu")
        items = ed.menu_items()
        self.assertEqual(items[0][0], "/quit")
        self.assertEqual(ed.ghost(items), "it")
        ed = self.editor("/g")
        self.assertEqual(ed.menu_items()[0][0], "/goal")
        ed = self.editor("/use cl")
        self.assertEqual(ed.menu_items()[0][0], "/use Claude")
        ed.accept(ed.menu_items())
        self.assertEqual(ed.text, "/use Claude")

    def test_history_ghost_and_fuzzy(self):
        ed = self.editor("fix the")
        self.assertEqual(ed.ghost([]), " parser bug")
        import star_input
        self.assertGreater(star_input.fuzzy("qu", "quit"), star_input.fuzzy("qt", "quit"))
        self.assertIsNone(star_input.fuzzy("zz", "quit"))

    def test_wrapping_puts_the_cursor_on_the_right_row(self):
        ed = self.editor("a" * 30 + "\nsecond")
        rows, (row, col) = ed.wrap(2, 22)
        self.assertEqual(len(rows), 3)       # 30 chars wrap at 19, then the second line
        self.assertEqual((row, col), (2, 2 + len("second")))

    def test_clipboard_image_writes_the_png_osascript_returns(self):
        import star_input
        from unittest.mock import patch

        def run(argv, **kwargs):
            text = " ".join(argv)
            dest = text.split('POSIX file "', 1)[1].split('"', 1)[0]
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(star_input.sys, "platform", "darwin"), \
                patch.object(star_input.subprocess, "run", side_effect=run):
            dest = str(Path(folder) / "paste.png")
            self.assertTrue(star_input.clipboard_image(dest))
            self.assertGreater(Path(dest).stat().st_size, 0)
        with patch.object(star_input.sys, "platform", "linux"):
            self.assertFalse(star_input.clipboard_image("/nope.png"))

    def test_windows_console_keys_use_the_same_names(self):
        import star_input
        self.assertEqual(star_input.windows_event("\x00", "H"), "up")
        self.assertEqual(star_input.windows_event("\xe0", "\x0f"), "shift-tab")
        self.assertEqual(star_input.windows_event("\x16"), "ctrl-v")
        self.assertEqual(star_input.windows_event("\r"), "enter")
        self.assertEqual(star_input.windows_event("a"), ("text", "a"))

    def test_windows_surrogate_keys_form_one_character_and_backspace_removes_it(self):
        import star_input
        events = star_input.WindowsEvents()
        self.assertIsNone(events.feed("\ud83d"))
        self.assertEqual(events.feed("\ude80"), ("text", "🚀"))
        self.assertEqual(events.feed("\xe0", "H"), "up")
        self.assertIsNone(events.feed("\ud83d"))
        self.assertEqual(events.feed("\x1b"), "esc")
        self.assertEqual(events.feed("a"), ("text", "a"))

        watch = starcli.KeyWatch(lambda: None)
        units = iter(["\ud83d", "\ude80", "\x08", "é"])
        fake = SimpleNamespace(kbhit=lambda: True, getwch=lambda: next(units))
        with patch.dict(sys.modules, msvcrt=fake):
            with self.assertRaises(StopIteration):
                watch._watch_windows()
        self.assertEqual(watch.typed, "é")

    def test_windows_control_c_interrupts_a_turn_in_raw_console_mode(self):
        interrupted = []
        watch = starcli.KeyWatch(lambda: interrupted.append(True))
        units = iter(["\x03"])
        fake = SimpleNamespace(kbhit=lambda: True, getwch=lambda: next(units))
        with patch.dict(sys.modules, msvcrt=fake):
            with self.assertRaises(StopIteration):
                watch._watch_windows()
        self.assertEqual(interrupted, [True])

    def test_windows_clipboard_image_asks_powershell(self):
        import star_input

        def run(argv, **kwargs):
            self.assertEqual(argv[0], "powershell")
            # pathlib follows os.name, so keep this open() while the name is patched.
            found = argv[-1].split("Save('", 1)[1].split("'", 1)[0]
            with open(found, "wb") as handle:
                handle.write(b"\x89PNG\r\n")
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        folder = tempfile.mkdtemp()
        try:
            dest = os.path.join(folder, "paste.png")
            with patch.object(star_input.sys, "platform", "win32"), \
                    patch.object(star_input.os, "name", "nt"), \
                    patch.object(star_input.subprocess, "run", side_effect=run):
                self.assertTrue(star_input.clipboard_image(dest))
        finally:
            os.remove(dest)
            os.rmdir(folder)


class PlanAndEnvTests(unittest.TestCase):
    def test_light_external_tasks_do_not_disable_their_required_tools(self):
        for name in ("Claude", "Codex", "Kiro"):
            engine = star_engines.ENGINES[name]({"name": name, "argv": ["x"]})
            engine.exe = "vendor-cli"
            with patch.object(star_engines, "codex_light_mcp_overrides", return_value=["-c", "all-mcp-off"]) as light, \
                    patch.object(star_engines, "ensure_kiro_agent", return_value="star-mcp") as profile:
                argv = engine.argv("read my calendar", "light-model", "low", tier="light", needs_mcp=True)
                self.assertNotIn("--strict-mcp-config", argv)
                self.assertNotIn("all-mcp-off", argv)
                light.assert_not_called()
                if name == "Kiro":
                    profile.assert_called_once_with(mcp=True)

    def test_mesh_turns_keep_the_native_team_mcp_restrictions(self):
        import team
        for name in ("Claude", "Codex"):
            engine = star_engines.ENGINES[name]({"name": name, "argv": ["x"]})
            engine.exe = "vendor-cli"
            with tempfile.TemporaryDirectory() as folder, patch.object(team.tempfile, "gettempdir", return_value=folder):
                mesh = engine.argv("task", "strong-model", "high", tier="heavy", mesh=True)
                solo = engine.argv("task", "strong-model", "high", tier="heavy")
                if name == "Claude":
                    path = Path(mesh[mesh.index("--settings") + 1])
                    self.assertEqual(json.loads(path.read_text()), team.CLAUDE_SETTINGS)
                    self.assertNotIn("--settings", solo)
                else:
                    self.assertIn("mcp_servers.iMCP.enabled=false", mesh)
                    self.assertNotIn("mcp_servers.iMCP.enabled=false", solo)

    def test_plan_mode_drops_bypass_flags(self):
        for name, bypass, plan in (("Claude", "--dangerously-skip-permissions", "plan"),
                                   ("Codex", "--dangerously-bypass-approvals-and-sandbox", 'sandbox_mode="read-only"'),
                                   ("Cursor", "--yolo", "plan"), ("Kiro", "--trust-all-tools", "--trust-tools=fs_read"),
                                   ("Antigravity", "--dangerously-skip-permissions", "plan")):
            engine = star_engines.ENGINES[name]({"name": name, "argv": ["x"]})
            engine.exe = "/bin/x"
            with patch.object(star_engines, "codex_compat_overrides", return_value=[]), \
                    patch.object(star_engines, "ensure_kiro_agent", return_value=None):
                normal = engine.argv("hi", "m", "low")
                planned = engine.argv("hi", "m", "low", plan=True)
            self.assertIn(bypass, normal, name)
            self.assertNotIn(bypass, planned, name)
            self.assertIn(plan, planned, name)
        claude = star_engines.ENGINES["Claude"]({"name": "Claude", "argv": ["x"]})
        claude.exe = "/bin/claude"
        light = claude.argv("hi", "haiku", "low", tier="light")
        heavy = claude.argv("hi", "opus", "high", tier="heavy")
        self.assertIn("--strict-mcp-config", light)
        self.assertNotIn("--strict-mcp-config", heavy)

    def test_a_light_codex_turn_turns_user_mcp_servers_off(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "config.toml"
        path.write_text('[mcp_servers.docs]\ncommand = "docs"\n\n'
                        '[mcp_servers.docs.tools.fetch]\napproval_mode = "never"\n\n'
                        '[mcp_servers.bad name]\ncommand = "nope"\n', encoding="utf-8")
        flags = star_engines.codex_light_mcp_overrides(path)
        self.assertEqual(flags, ["-c", 'mcp_servers.docs.enabled=false'])
        engine = star_engines.ENGINES["Codex"]({"name": "Codex", "argv": ["x"]})
        engine.exe = "/bin/codex"
        with patch.object(star_engines, "codex_light_mcp_overrides", return_value=flags), \
                patch.object(star_engines, "codex_compat_overrides", return_value=[]):
            light = engine.argv("hi", "luna", "medium", tier="light")
            heavy = engine.argv("hi", "sol", "high", tier="heavy")
        self.assertIn('mcp_servers.docs.enabled=false', light)
        self.assertNotIn('mcp_servers.docs.enabled=false', heavy)

    def test_children_cannot_claim_the_herdr_pane(self):
        with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "p", "HERDR_TAB_ID": "t", "HERDR_SOCKET_PATH": "s"}):
            env = star_engines.child_env()
        self.assertNotIn("HERDR_PANE_ID", env)
        self.assertNotIn("HERDR_ENV", env)
        self.assertNotIn("HERDR_SOCKET_PATH", env)
        self.assertEqual(env["HERDR_TAB_ID"], "t")
        self.assertEqual(env["STAR_MESH_SOCKET"], "s")


class HerdrStarSetupTests(unittest.TestCase):
    def test_star_layout_runs_starclis(self):
        import herdr_ai
        panes = {"lead": "p1", "usage": "p2", "crew1": "a", "crew2": "b", "crew3": "c", "crew4": "d"}
        commands = herdr_ai.layout_commands("Star", panes)
        self.assertEqual(commands["lead"][-7:], ["--picker-launch", "Star", "--", "--name", "lead", "--role", "lead"])
        self.assertEqual(commands["crew3"][-4:], ["--name", "crew3", "--role", "crew"])
        self.assertEqual(herdr_ai.team_plan("Star"), {"lead": "Star", "gemini": False})


class GoalTests(unittest.TestCase):
    def setUp(self):
        reports = patch.object(starcli, "report_herdr")
        self.reports = reports.start()
        self.addCleanup(reports.stop)

    def test_blocked_goal_lifecycle_survives_refresh_and_clears_with_the_goal(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(starcli, "CONVERSATIONS", Path(folder) / "conversations"), \
                patch.object(starcli, "LAST", Path(folder) / "last"), \
                redirect_stdout(io.StringIO()):
            star = starcli.Star(starcli.Conversation({"cwd": folder}))
            star.member = Mock()
            star.member.role = "solo"
            star.member.outstanding.return_value = []
            star.member.inbox.return_value = []
            star.run = lambda *a, **k: ("GOAL BLOCKED: need the user's account access", "ok")
            self.assertEqual(star.pursue("read the shared account"), "blocked")
            self.assertEqual(star.convo.goal_state, "blocked")
            self.reports.assert_called_with("blocked", "read the shared account")
            loaded = starcli.Conversation.load(star.convo.id)
            self.assertEqual(loaded.goal_state, "blocked")
            resumed = starcli.Star(loaded, member=star.member)
            self.reports.assert_called_with("blocked", "read the shared account")
            starcli.Commands(resumed).do_goal("clear")
            self.reports.assert_called_with("idle", "")
            self.assertEqual(resumed.convo.goal_state, "")

    def test_only_a_final_plain_verdict_controls_a_goal(self):
        for text in ("Example: GOAL COMPLETE", "> GOAL COMPLETE", "`GOAL COMPLETE`",
                     "GOAL COMPLETE\nStill working", "```text\nGOAL COMPLETE",
                     "```\nGOAL COMPLETE\n```", "GOAL COMPLETE-ish", "GOAL BLOCKEDNESS", "    GOAL COMPLETE"):
            self.assertEqual(starcli.goal_verdict(text), ("CONTINUES", ""), text)
        self.assertEqual(starcli.goal_verdict("Verified the files.\nGOAL COMPLETE\n"), ("COMPLETE", ""))
        self.assertEqual(starcli.goal_verdict("GOAL BLOCKED: need login"), ("BLOCKED", "need login"))
        self.assertEqual(starcli.goal_verdict("```\nexample\n```\nGOAL COMPLETE"), ("COMPLETE", ""))

    def test_goal_prompt_asks_for_a_verdict(self):
        first, later = starcli.goal_prompt("ship it", 1), starcli.goal_prompt("ship it", 3)
        for text in (first, later):
            self.assertIn("GOAL COMPLETE", text)
            self.assertIn("ship it", text)
        self.assertIn("step 3", later)
        self.assertNotIn("--send", first)
        delegated = starcli.goal_prompt("ship it", 2, ["crew1", "crew3"])
        self.assertIn("crew1, crew3", delegated)
        self.assertIn("star --send", delegated)

    def test_lead_hint_shows_crew_status(self):
        star = starcli.Star(starcli.Conversation())
        star.member = type("M", (), {"name": "lead", "role": "lead", "peers": lambda self: [
            {"name": "crew1", "role": "crew", "status": "idle"},
            {"name": "crew2", "role": "crew", "status": "working"},
        ]})()
        self.assertIn("crew 1 idle · 1 working", star.hint()[1])
        star.member.peers = lambda: [{"name": "crew1", "role": "crew", "status": "idle"}]
        self.assertIn("crew 1 idle", star.hint()[1])
        star.member.peers = lambda: [{"name": "crew1", "role": "crew", "status": "blocked"}]
        self.assertIn("crew 1 blocked", star.hint()[1])
        star.member.role = "crew"
        self.assertEqual(star.crew_hint(), "")

    def test_a_goal_keeps_crew_replies_and_a_crew_yields_to_a_task(self):
        class Box:
            def __init__(self, role):
                self.mail, self.name, self.role = [], "lead" if role == "lead" else "crew1", role

            def peers(self):
                return [{"name": "crew1", "role": "crew", "status": "idle"}]

            def outstanding(self):
                return []

            def beat(self, **_k):
                pass

            def inbox(self):
                return list(self.mail)

            def take(self, message):
                self.mail.remove(message)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(starcli, "CONVERSATIONS", root / "conversations"), \
                    patch.object(starcli, "LAST", root / "last"), \
                    redirect_stdout(io.StringIO()):
                lead = starcli.Star(starcli.Conversation({"cwd": folder}))
                lead.member = Box("lead")
                seen = []

                def run(prompt, route_text=None, images=()):
                    seen.append(prompt)
                    if len(seen) == 1:
                        lead.member.mail.append({"kind": "reply", "from": "crew1", "text": "found the parser",
                                                 "task": "search"})
                        return "GOAL CONTINUES: look again", "ok"
                    self.assertTrue(any("found the parser" in note for note in lead.convo.notes))
                    return "GOAL COMPLETE", "ok"

                lead.run = run
                lead.pursue("ship it")
                self.assertEqual(lead.convo.goal, "")
                self.assertEqual(len(seen), 2)

                crew = starcli.Star(starcli.Conversation({"cwd": folder}))
                crew.member = Box("crew")
                crew.member.mail.append({"kind": "task", "from": "lead", "text": "edit x"})
                crew.run = lambda *a, **k: self.fail("crew ran a goal step while a task was waiting")
                crew.pursue("ship it")
                self.assertEqual(crew.convo.goal, "ship it")
                self.assertEqual(crew.member.mail[0]["kind"], "task")

    def test_a_goal_stops_when_every_provider_is_at_the_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(starcli, "CONVERSATIONS", root / "conversations"), \
                    patch.object(starcli, "LAST", root / "last"), \
                    redirect_stdout(io.StringIO()) as out:
                star = starcli.Star(starcli.Conversation({"cwd": folder}))
                star.run = lambda *a, **k: ("", "none")
                star.pursue("ship it")
                self.assertEqual(star.convo.goal, "ship it")
                self.assertIn("no provider can take this task", out.getvalue())

                sent = []
                star.member = type("M", (), {"name": "crew1", "take": lambda *a: None})()
                with patch.object(starcli.star_mesh, "reply", lambda message, text, sender: sent.append(text)):
                    star.handle({"kind": "task", "from": "lead", "text": "edit x"})
                self.assertIn("no provider can take this task", sent[0])
                self.assertIn("crew1 stopped", sent[0])


class RoutingMemoryTests(unittest.TestCase):
    def test_resume_keeps_the_pin_tier_and_plan_mode(self):
        origin = os.getcwd()
        self.addCleanup(lambda: os.chdir(origin))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(starcli, "CONVERSATIONS", root), patch.object(starcli, "LAST", root / "last"), \
                    redirect_stdout(io.StringIO()):
                star = starcli.Star(starcli.Conversation({"cwd": folder, "goal": "ship it"}))
                star.pinned, star.tier, star.plan = "Claude", "heavy", True
                star.convo.save()
                loaded = starcli.Conversation.load(star.convo.id)
                self.assertEqual(loaded.routing, {"pinned": "Claude", "tier": "heavy", "plan": True})

                again = starcli.Star(loaded, tier="light", plan=False)
                again.pinned = "Codex"
                self.assertTrue(starcli.apply_saved_routing(again))
                self.assertEqual(again.pinned, "Claude")
                self.assertEqual(again.tier, "heavy")
                self.assertTrue(again.plan)

                again.toggle_plan()
                self.assertFalse(starcli.Conversation.load(again.convo.id).routing["plan"])

                bare = starcli.Conversation({"id": "older-one", "cwd": folder, "goal": "older"})
                bare.save()
                fresh = starcli.Star(starcli.Conversation.load(bare.id), plan=True)
                fresh.pinned = "Cursor"
                self.assertFalse(starcli.apply_saved_routing(fresh))
                self.assertEqual(fresh.pinned, "Cursor")
                self.assertTrue(fresh.plan)

                try:
                    starcli.Commands(fresh).do_resume(loaded.id)
                finally:
                    os.chdir(origin)   # /resume moved here; Windows cannot delete a current directory
                self.assertEqual(fresh.pinned, "Claude")
                self.assertEqual(fresh.tier, "heavy")
                self.assertFalse(fresh.plan)
                self.assertEqual(fresh.convo.id, loaded.id)

    def test_two_conversations_in_the_same_second_keep_separate_ids(self):
        first = starcli.Conversation({"cwd": "/tmp"})
        second = starcli.Conversation({"cwd": "/tmp"})
        self.assertNotEqual(first.id, second.id)
