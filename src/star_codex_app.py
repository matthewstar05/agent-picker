"""Run one Codex turn through `codex app-server`, so its approvals reach StarCLI.

`codex exec` cannot ask anyone, so a Codex profile with Bypass Permissions Off
needs the app-server protocol: it sends approval requests to its client. This
adapter is that client. It starts or resumes the thread, starts the turn, puts
each approval request to the StarCLI user through the same folder protocol as
star_approve, and prints the `codex exec --json` events that CodexEngine.parse
reads, so the rest of StarCLI is unchanged. Field names follow the schema from
`codex app-server generate-json-schema`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import star_approve
from platform_runtime import direct_argv

CLIENT = {"name": "starcli", "title": "StarCLI", "version": "1"}
UNSUPPORTED = -32601


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


class Session:
    def __init__(self, proc, folder):
        self.proc, self.folder = proc, folder
        self.next_id = 0
        self.items = {}       # item id -> started item, for file paths in approvals
        self.asked = 0
        self.before = None    # thread token total before this turn
        self.usage = {}
        self.finished = None  # the completed turn, even if it ends inside a request

    def send(self, message):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        """Send a request and handle everything that arrives until its answer."""
        self.next_id += 1
        ident = self.next_id
        self.send({"id": ident, "method": method, "params": params})
        while True:
            message = self.read()
            if message.get("id") == ident and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message', 'failed')}")
                return message.get("result") or {}
            self.dispatch(message)

    def read(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise EOFError("codex app-server stopped")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def dispatch(self, message):
        """Handle a server request or notification."""
        method, params = message.get("method"), message.get("params") or {}
        if "id" in message and method:
            self.answer(message["id"], method, params)
            return
        if method == "item/started":
            item = params.get("item") or {}
            self.items[item.get("id")] = item
            event = started(item)
            if event:
                emit({"type": "item.started", "item": event})
        elif method == "item/completed":
            event = completed(params.get("item") or {})
            if event:
                emit({"type": "item.completed", "item": event})
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            total, last = usage.get("total") or {}, usage.get("last") or {}
            if self.before is None:
                self.before = {k: total.get(k, 0) - last.get(k, 0) for k in total}
            self.usage = {k: total.get(k, 0) - self.before.get(k, 0) for k in total}
        elif method == "error" and not params.get("willRetry"):
            emit({"type": "error", "message": (params.get("error") or {}).get("message", "Codex error")})
        elif method == "turn/completed":
            self.finished = params.get("turn") or {}

    def answer(self, ident, method, params):
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            command = params.get("command")
            if isinstance(command, list):
                command = " ".join(command)
            allowed = self.ask("Bash", {"command": command or "a command", "description": params.get("reason") or ""})
            if method == "execCommandApproval":
                result = {"decision": "approved" if allowed else "denied"}
            else:
                result = {"decision": "accept" if allowed else "decline"}
        elif method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            if method == "applyPatchApproval":
                paths = list((params.get("fileChanges") or {}).keys())
            else:
                paths = [c.get("path") for c in (self.items.get(params.get("itemId")) or {}).get("changes") or []]
            allowed = self.ask("Edit", {"file_path": ", ".join(p for p in paths if p) or "files"})
            if method == "applyPatchApproval":
                result = {"decision": "approved" if allowed else "denied"}
            else:
                result = {"decision": "accept" if allowed else "decline"}
        elif method == "item/permissions/requestApproval":
            requested = params.get("permissions") or {}
            allowed = self.ask("Permissions", {"description": params.get("reason") or json.dumps(requested)})
            result = {"permissions": requested if allowed else {}, "scope": "turn"}
        else:
            # Questions StarCLI cannot put to the user decline, rather than hang the turn.
            self.send({"id": ident, "error": {"code": UNSUPPORTED, "message": f"StarCLI does not support {method}"}})
            return
        self.send({"id": ident, "result": result})

    def ask(self, tool, details):
        self.asked += 1
        decision = star_approve.ask(self.folder, f"codex-{self.asked}", {"tool_name": tool, "input": details})
        return decision.get("behavior") == "allow"


def started(item):
    kind = item.get("type")
    if kind == "commandExecution":
        return {"type": "command_execution", "command": item.get("command", "")}
    if kind == "mcpToolCall":
        return {"type": "mcp_tool_call", "server": item.get("server"), "tool": item.get("tool")}
    if kind == "webSearch":
        return {"type": "web_search", "query": item.get("query", "")}
    if kind == "plan":
        return {"type": "todo_list"}
    if kind == "reasoning":
        return {"type": "reasoning"}
    return None


def completed(item):
    kind = item.get("type")
    if kind == "agentMessage":
        return {"type": "agent_message", "text": item.get("text", "")}
    if kind == "fileChange":
        return {"type": "file_change", "changes": [{"path": c.get("path")} for c in item.get("changes") or []]}
    if kind == "commandExecution":
        return {"type": "command_execution", "exit_code": item.get("exitCode"),
                "aggregated_output": item.get("aggregatedOutput") or ""}
    return None


def run(config):
    # A Windows npm shim runs its script directly, as for every provider turn.
    try:
        argv = direct_argv([config["codex"], "app-server"] + list(config.get("overrides") or []))
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        emit({"type": "turn.failed", "error": {"message": str(exc)}})
        return 1
    session = Session(proc, Path(config["approvals"]))
    try:
        session.request("initialize", {"clientInfo": CLIENT, "capabilities": {}})
        session.send({"method": "initialized"})
        # The guardian starts this adapter in the conversation's directory.
        thread = {"cwd": config.get("cwd") or os.getcwd(), "model": config.get("model"),
                  "approvalPolicy": config.get("approval_policy", "on-request"),
                  "sandbox": config.get("sandbox", "workspace-write")}
        if config.get("session"):
            result = session.request("thread/resume", {"threadId": config["session"], **thread})
        else:
            result = session.request("thread/start", thread)
        thread_id = (result.get("thread") or {}).get("id") or config.get("session")
        emit({"type": "thread.started", "thread_id": thread_id})
        inputs = [{"type": "text", "text": config["prompt"]}]
        inputs += [{"type": "localImage", "path": path} for path in config.get("images") or ()]
        turn = {"threadId": thread_id, "input": inputs}
        if config.get("effort"):
            turn["effort"] = config["effort"]
        session.request("turn/start", turn)
        while session.finished is None:
            session.dispatch(session.read())
        done = session.finished
        status = done.get("status")
        if status == "completed":
            u = session.usage
            emit({"type": "turn.completed", "usage": {"input_tokens": u.get("inputTokens", 0),
                                                      "cached_input_tokens": u.get("cachedInputTokens", 0),
                                                      "output_tokens": u.get("outputTokens", 0)}})
            return 0
        message = (done.get("error") or {}).get("message") or f"Codex turn {status or 'ended'}"
        emit({"type": "turn.failed", "error": {"message": message}})
        return 1
    except (EOFError, RuntimeError) as exc:
        emit({"type": "turn.failed", "error": {"message": str(exc)}})
        return 1
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    sys.exit(run(json.loads(sys.argv[1])))
