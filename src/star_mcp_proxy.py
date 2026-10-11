"""Relay an MCP stdio server, nesting schemas unsupported at a tool's root.

Kiro's model service rejects top-level oneOf, anyOf, and allOf. Keep the exact
schema under an `input` property and remove that envelope on tools/call. This
preserves validation constraints instead of dropping tools or flattening unions.
"""

import copy
import json
import subprocess
import sys
import threading

COMPOSITIONS = {"oneOf", "anyOf", "allOf"}


def nested_schema(schema):
    def rebase(value, scoped=False):
        if isinstance(value, list):
            return [rebase(item, scoped) for item in value]
        if not isinstance(value, dict):
            return value
        identity = value.get("$id") or value.get("id")
        scoped = scoped or (isinstance(identity, str) and bool(identity))
        literals = {"default", "examples", "const", "enum"}
        out = {key: copy.deepcopy(item) if key in literals else rebase(item, scoped)
               for key, item in value.items()}
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            ref = out.get(key)
            if not scoped and isinstance(ref, str) and (ref == "#" or ref.startswith("#/")):
                out[key] = "#/properties/input" + ref[1:]
        return out

    return {"type": "object", "properties": {"input": rebase(schema)},
            "required": ["input"], "additionalProperties": False}


def tool_list(message, wrapped):
    """Adapt a tools/list response, remembering which calls need unwrapping."""
    result = message.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return message
    message = copy.deepcopy(message)
    for tool in message["result"]["tools"]:
        schema = tool.get("inputSchema")
        name = tool.get("name")
        if name and isinstance(schema, dict) and COMPOSITIONS.intersection(schema):
            tool["inputSchema"] = nested_schema(schema)
            tool["description"] = (tool.get("description", "")
                                   + " Supply this tool's parameters inside the input field.")
            wrapped.add(name)
        elif name:
            wrapped.discard(name)
    return message


def tool_call(message, wrapped):
    if message.get("method") != "tools/call":
        return message
    params = message.get("params") or {}
    if params.get("name") not in wrapped:
        return message
    arguments = params.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != {"input"} or not isinstance(arguments["input"], dict):
        raise ValueError("this tool requires an object in its input field")
    message = copy.deepcopy(message)
    message["params"]["arguments"] = arguments["input"]
    return message


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        sys.exit("star_mcp_proxy needs a server command after --")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    wrapped, output_lock = set(), threading.Lock()

    def emit(message):
        with output_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def incoming():
        try:
            for line in sys.stdin:
                original = None
                try:
                    original = json.loads(line)
                    message = tool_call(original, wrapped)
                except ValueError as exc:
                    if isinstance(original, dict) and "id" in original:
                        emit({"jsonrpc": "2.0", "id": original["id"],
                              "error": {"code": -32602, "message": str(exc)}})
                    continue
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            proc.stdin.close()

    threading.Thread(target=incoming, daemon=True).start()
    try:
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            emit(tool_list(message, wrapped))
        return proc.wait()
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    sys.exit(main())
