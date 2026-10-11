"""Answer Claude's permission prompts from the StarCLI pane that runs the turn.

A headless Claude turn whose picker profile requires approvals runs with
`--permission-prompt-tool mcp__star__approve`. This stdio MCP server writes each
request to the turn's approval folder and waits for StarCLI, which asks the
user, to write the answer beside it. Claude starts it as its own child, so it
stops with the turn.
"""

import json
import os
import sys
import time
from pathlib import Path

TOOL = "approve"
SERVER = "star"
PROMPT_TOOL = f"mcp__{SERVER}__{TOOL}"
SCHEMA = {"type": "object", "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                                           "tool_use_id": {"type": "string"}},
          "required": ["tool_name", "input"]}


def config(folder):
    """The --mcp-config JSON that starts this server for one turn's folder."""
    return json.dumps({"mcpServers": {SERVER: {
        "type": "stdio", "command": sys.executable, "args": [str(Path(__file__).resolve())],
        "env": {"STAR_APPROVALS": str(folder)}}}})


def _write(path, data):
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def ask(folder, number, request, poll=.1):
    """Leave the request for StarCLI and wait for its decision."""
    _write(folder / f"{number}.req", request)
    answer = folder / f"{number}.res"
    while not answer.exists():
        time.sleep(poll)
    decision = json.loads(answer.read_text(encoding="utf-8"))
    if decision.get("behavior") == "allow":
        return {"behavior": "allow", "updatedInput": request.get("input") or {}}
    return {"behavior": "deny", "message": decision.get("message") or "The user denied this action."}


def respond(message, folder, counter):
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None   # a notification
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "starcli-approvals", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": TOOL, "description": "Ask the StarCLI user to approve a tool call.",
                             "inputSchema": SCHEMA}]}
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "unknown tool"}}
        args = params.get("arguments") or {}
        counter[0] += 1
        # The PID keeps names unique if Claude restarts this server mid-turn.
        decision = ask(folder, f"{os.getpid()}-{counter[0]}", {"tool_name": args.get("tool_name", ""),
                                            "input": args.get("input") or {},
                                            "tool_use_id": args.get("tool_use_id", "")})
        result = {"content": [{"type": "text", "text": json.dumps(decision)}]}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def main():
    folder = Path(os.environ["STAR_APPROVALS"])
    counter = [0]
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = respond(message, folder, counter) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
