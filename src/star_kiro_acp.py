"""Run one Kiro turn through `kiro-cli acp`, so its permission requests reach StarCLI.

`kiro-cli chat --no-interactive` denies what it would have asked about, so a
Kiro profile without --trust-all-tools needs the Agent Client Protocol: the
agent sends `session/request_permission` to its client. This adapter is that
client. It opens or loads the session, sends the prompt, puts each permission
request to the StarCLI user through the star_approve folder protocol, and
prints the `chat --output-format stream-json` events KiroEngine.parse reads.
"""

import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
from pathlib import Path

import star_approve
from platform_runtime import direct_argv

CLIENT = {"name": "starcli", "title": "StarCLI", "version": "1"}
ALLOW = ("allow_once", "allow_always")
REJECT = ("reject_once", "reject_always")


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


class Session:
    def __init__(self, proc, folder):
        self.proc, self.folder = proc, folder
        self.next_id = 0
        self.asked = 0
        self.quiet = False   # session/load replays history; it is not this turn's output

    def send(self, message):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        ident = self.next_id
        self.send({"id": ident, "method": method, "params": params})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise EOFError("kiro-cli acp stopped")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == ident and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message', 'failed')}")
                return message.get("result") or {}
            self.dispatch(message)

    def dispatch(self, message):
        method, params = message.get("method"), message.get("params") or {}
        if "id" in message and method:
            if method == "session/request_permission":
                self.send({"id": message["id"], "result": {"outcome": self.permission(params)}})
            else:
                # No file system or terminal capability was offered; refuse anything else.
                self.send({"id": message["id"], "error": {"code": -32601,
                                                         "message": f"StarCLI does not support {method}"}})
            return
        if self.quiet:
            return
        if method == "session/update":
            emit({"type": "sessionUpdate", "data": {"update": params.get("update") or {}}})
        elif isinstance(params, dict) and params.get("meteringUsage"):
            emit({"type": "metadata", "data": {"meteringUsage": params["meteringUsage"]}})

    def permission(self, params):
        call = params.get("toolCall") or {}
        options = params.get("options") or []
        raw = call.get("title") or "a tool"
        title = re.sub(r"^Running: ", "", raw)
        details = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
        if raw.startswith("Running: ") and "command" not in details:
            details = {**details, "command": title}   # Kiro's title for a shell command
        # Kiro may name neither the tool nor its kind; a command is its shell tool,
        # so "always allow" never covers every unnamed tool at once.
        tool = (((call.get("_meta") or {}).get("kiro") or {}).get("toolName") or call.get("kind")
                or ("shell" if "command" in details else "tool"))
        details = {**details, "description": title}
        self.asked += 1
        decision = star_approve.ask(self.folder, f"kiro-{self.asked}", {"tool_name": tool, "input": details})
        wanted = ALLOW if decision.get("behavior") == "allow" else REJECT
        for kind in wanted:
            for option in options:
                if option.get("kind") == kind:
                    return {"outcome": "selected", "optionId": option.get("optionId")}
        return {"outcome": "cancelled"}


def prompt_blocks(text, images):
    blocks = [{"type": "text", "text": text}]
    for path in images or ():
        try:
            data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            continue
        blocks.append({"type": "image", "data": data,
                       "mimeType": mimetypes.guess_type(path)[0] or "image/png"})
    return blocks


def run(config):
    argv = [config["kiro"], "acp"] + list(config.get("flags") or [])
    try:
        proc = subprocess.Popen(direct_argv(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        emit({"type": "runError", "data": {"message": str(exc)}})
        return 1
    session = Session(proc, Path(config["approvals"]))
    cwd = config.get("cwd") or os.getcwd()
    try:
        session.request("initialize", {"protocolVersion": 1, "clientInfo": CLIENT, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        ident = config.get("session")
        if ident:
            session.quiet = True
            session.request("session/load", {"sessionId": ident, "cwd": cwd, "mcpServers": []})
            session.quiet = False
        else:
            ident = session.request("session/new", {"cwd": cwd, "mcpServers": []}).get("sessionId")
        emit({"type": "metadata", "data": {"sessionId": ident}})
        result = session.request("session/prompt", {"sessionId": ident,
                                                    "prompt": prompt_blocks(config["prompt"], config.get("images"))})
        reason = result.get("stopReason")
        if reason not in (None, "end_turn"):
            emit({"type": "runError", "data": {"message": f"Kiro stopped: {reason}"}})
            return 1
        return 0
    except (EOFError, RuntimeError) as exc:
        emit({"type": "runError", "data": {"message": str(exc)}})
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
