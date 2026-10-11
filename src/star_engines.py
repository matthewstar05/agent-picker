"""StarCLI engines: run one turn on a provider's own CLI and read its event stream.

Every provider runs headless with the same permissions its agent-picker profile
uses, so the work keeps each CLI's own tools: file edits, shell, MCP, web
search. Each engine builds the argv for a turn (resuming the provider's session
when it has one) and turns the CLI's JSON lines into one event shape:

    {"kind": "session", "id": ...}         the provider's conversation id
    {"kind": "text", "text": ...}          reply text, streamed
    {"kind": "thinking"}                   the model is reasoning
    {"kind": "tool", "label": ...}         a tool call started
    {"kind": "tool_out", "text": ...}      first line of a tool's output
    {"kind": "usage", ...}                 tokens and credits for the turn
    {"kind": "meter", "windows": [...]}    live usage limits from the stream
    {"kind": "error", "text": ...}         the turn failed
"""

import json
import hashlib
import os
import re
import shlex
import subprocess
import threading
import tempfile
import sys
from pathlib import Path

from platform_runtime import resolve_argv

VALID_APPROVAL = {"auto", "prompt", "writes", "approve"}


def first_line(text, width=100):
    line = next((l for l in str(text or "").splitlines() if l.strip()), "").strip()
    return line if len(line) <= width else line[: width - 1] + "…"


def short_path(path):
    path = str(path or "")
    cwd = os.getcwd() + os.sep
    return path[len(cwd):] if path.startswith(cwd) else path


def describe(name, args):
    """'$ npm test', 'edit src/app.py', 'grep TODO': a tool call in a few words."""
    args = args if isinstance(args, dict) else {}
    lower = name.lower()
    command = args.get("command") or args.get("CommandLine") or args.get("cmd")
    if command and any(k in lower for k in ("bash", "shell", "command", "exec", "terminal")):
        return "$ " + first_line(command, 90)
    path = (args.get("file_path") or args.get("path") or args.get("AbsolutePath")
            or args.get("TargetFile") or args.get("notebook_path"))
    verb = {"read": "read", "view": "read", "edit": "edit", "write": "write", "replace": "edit",
            "create": "write", "delete": "delete", "multiedit": "edit", "strreplace": "edit"}
    for key, word in verb.items():
        if key in lower and path:
            return f"{word} {short_path(path)}"
    for key in ("pattern", "query", "Query", "url", "Url", "glob_pattern", "globPattern",
                "description", "prompt", "Pattern", "SearchPath", "DirectoryPath"):
        if isinstance(args.get(key), str) and args[key].strip():
            return f"{name} {first_line(args[key], 80)}"
    if path:
        return f"{name} {short_path(path)}"
    return name


def unwrap_shell(command):
    """codex reports `/bin/zsh -lc 'npm test'`; show `npm test`."""
    match = re.match(r"^\S*/(?:ba|z)?sh\s+-l?c\s+(.+)$", command or "", re.S)
    if not match:
        return command
    try:
        words = shlex.split(match.group(1))
        return words[0] if len(words) == 1 else match.group(1)
    except ValueError:
        return match.group(1)


class Engine:
    name = ""
    resume_flag = None
    asks = False   # its headless mode can put permission prompts to StarCLI

    def __init__(self, agent):
        self.agent = agent
        argv = resolve_argv(agent) or []
        self.exe = argv[0] if argv else None

    def ready(self):
        return bool(self.exe)

    def argv(self, prompt, model, effort, session=None, plan=False, images=(), tier=None, mesh=False, needs_mcp=False):
        """plan: read-only, so the provider can analyze and propose but not edit.
        images: image files the user attached (their paths are in the prompt too).
        tier: light, standard, or heavy; a light turn can skip heavy startup.
        mesh: avoid servers that cannot support concurrent team clients.
        needs_mcp: a short external-tool request still needs its MCP servers."""
        raise NotImplementedError

    def parse(self, data, state):
        """Events for one decoded JSON line. state is a per-turn scratch dict."""
        raise NotImplementedError

    def with_approvals(self, argv, folder):
        """argv whose permission prompts StarCLI can answer, or None when this
        provider's headless mode has no way to ask."""
        return None

    def native(self, session):
        """argv that opens this session in the provider's own interactive UI."""
        base = list(resolve_argv(self.agent) or [])
        if not session:
            return base
        if self.name == "Codex":
            return [base[0], "resume", session] + base[1:]
        return base + [self.resume_flag, session]


class ClaudeEngine(Engine):
    name, resume_flag = "Claude", "--resume"
    asks = True

    def argv(self, prompt, model, effort, session=None, plan=False, images=(), tier=None, mesh=False, needs_mcp=False):
        argv = [self.exe, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--model", model]
        argv += ["--permission-mode", "plan"] if plan else ["--dangerously-skip-permissions"]
        # A light turn does not need the user's MCP servers. Their tool schemas are
        # what blows a short Claude prompt up to tens of thousands of input tokens.
        if tier == "light" and not needs_mcp:
            argv.append("--strict-mcp-config")
        elif mesh:
            import team
            argv += ["--settings", team.claude_settings()]
        if effort:
            argv += ["--effort", effort]
        if session:
            argv += ["--resume", session]
        return argv

    def with_approvals(self, argv, folder):
        """argv that asks the StarCLI user, through star_approve, wherever the
        picker profile's permission mode would prompt."""
        import star_approve
        profile = list((self.agent or {}).get("argv") or [])
        mode = (profile[profile.index("--permission-mode") + 1]
                if "--permission-mode" in profile[:-1] else "default")
        argv = [a for a in argv if a != "--dangerously-skip-permissions"]
        return argv + ["--permission-mode", mode, "--mcp-config", star_approve.config(folder),
                       "--permission-prompt-tool", star_approve.PROMPT_TOOL]

    def parse(self, d, state):
        kind = d.get("type")
        if kind == "system" and d.get("subtype") == "init":
            return [{"kind": "session", "id": d.get("session_id")}]
        if kind == "stream_event":
            event = d.get("event") or {}
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return [{"kind": "text", "text": delta.get("text", "")}]
            if (event.get("content_block") or {}).get("type") == "thinking":
                return [{"kind": "thinking"}]
            return []
        if kind == "assistant":
            out = []
            for block in (d.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use" and block.get("id") not in state.setdefault("tools", set()):
                    state["tools"].add(block.get("id"))
                    out.append({"kind": "tool", "label": describe(block.get("name", "tool"), block.get("input"))})
            return out
        if kind == "user":
            out = []
            for block in (d.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    out.append({"kind": "tool_out", "text": first_line(content), "error": True})
            return out
        if kind == "rate_limit_event":
            info = (d.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            windows = [{"label": label, "pct": float(info[key]["utilization"]) * 100,
                        "resets": float(info[key]["resetsAt"])}
                       for label, key in (("5h", "five_hour"), ("wk", "seven_day"))
                       if isinstance(info.get(key), dict) and info[key].get("utilization") is not None]
            return [{"kind": "meter", "windows": windows}] if windows else []
        if kind == "result":
            u = d.get("usage") or {}
            out = [{"kind": "session", "id": d.get("session_id")},
                   {"kind": "usage", "input": (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                                               + u.get("cache_read_input_tokens", 0)),
                    "cached": u.get("cache_read_input_tokens", 0), "output": u.get("output_tokens", 0)}]
            if d.get("is_error"):
                out.append({"kind": "error", "text": first_line(d.get("result") or d.get("subtype"), 200)})
            return out
        return []


def codex_compat_overrides(path=None):
    """-c overrides for config.toml values this codex release rejects.

    A config written for another Codex build can carry
    `approval_mode = "never"` on an MCP tool, which codex 0.162 refuses to
    load. Passing the nearest valid value on the command line lets the turn
    run without rewriting the user's config."""
    path = Path(path or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    section, out = None, []
    for line in lines:
        header = re.match(r"^\s*\[([^\[\]]+)\]\s*$", line)
        if header:
            section = header.group(1).strip()
            continue
        value = re.match(r'^\s*approval_mode\s*=\s*"([^"]*)"', line)
        if section and value and value.group(1) not in VALID_APPROVAL:
            out += ["-c", f'{section}.approval_mode="approve"']
    return out


def codex_light_mcp_overrides(path=None):
    """-c flags that turn off user MCP servers for a light Codex turn.

    A light turn is a short chat, search, or mechanical edit. The servers'
    tool schemas would otherwise be loaded into that prompt. The config file
    itself is not changed."""
    path = Path(path or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    servers = []
    for line in lines:
        header = re.match(r"^\s*\[([^\[\]]+)\]\s*$", line)
        if not header:
            continue
        parts = [part.strip() for part in header.group(1).split(".")]
        if len(parts) >= 2 and parts[0] == "mcp_servers" and re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]) \
                and parts[1] not in servers:
            servers.append(parts[1])
    return [item for name in servers for item in ("-c", f"mcp_servers.{name}.enabled=false")]


class CodexEngine(Engine):
    name, resume_flag = "Codex", "resume"
    asks = True

    def with_approvals(self, argv, folder):
        """`codex exec` cannot ask, so the same turn runs through star_codex_app,
        a `codex app-server` client that puts each approval to StarCLI."""
        config = {"codex": argv[0], "approvals": str(folder), "overrides": []}
        words = iter(argv[2:])
        for word in words:
            if word == "resume":
                config["session"] = next(words)
            elif word == "-m":
                config["model"] = next(words)
            elif word == "-i":
                config.setdefault("images", []).append(next(words))
            elif word == "-c":
                value = next(words)
                if value.startswith("model_reasoning_effort="):
                    config["effort"] = value.split("=", 1)[1].strip('"')
                elif not value.startswith("sandbox_mode="):
                    config["overrides"] += ["-c", value]
            elif word == "--":
                config["prompt"] = next(words)
        # The picker profile's own approval policy and sandbox, as in its native pane.
        profile = list((self.agent or {}).get("argv") or [])
        for flags, key in ((("-a", "--ask-for-approval"), "approval_policy"), (("-s", "--sandbox"), "sandbox")):
            for flag in flags:
                if flag in profile[:-1]:
                    config[key] = profile[profile.index(flag) + 1]
        return [sys.executable, str(Path(__file__).with_name("star_codex_app.py")), json.dumps(config)]

    def argv(self, prompt, model, effort, session=None, plan=False, images=(), tier=None, mesh=False, needs_mcp=False):
        argv = [self.exe, "exec"]
        if session:
            argv += ["resume", session]
        argv += ["--json", "-m", model, "--skip-git-repo-check"]
        argv += ["-c", 'sandbox_mode="read-only"'] if plan else ["--dangerously-bypass-approvals-and-sandbox"]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        for image in images:   # the others read image files with their own tools
            argv += ["-i", image]
        if tier == "light" and not needs_mcp:
            argv += codex_light_mcp_overrides()
        if mesh:
            argv += ["-c", "mcp_servers.iMCP.enabled=false"]
        return argv + codex_compat_overrides() + ["--", prompt]

    def parse(self, d, state):
        kind = d.get("type")
        if kind == "thread.started":
            return [{"kind": "session", "id": d.get("thread_id")}]
        if kind in ("item.started", "item.completed"):
            item = d.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message" and kind == "item.completed":
                text = item.get("text", "")
                sep = "\n\n" if state.get("spoke") else ""
                state["spoke"] = True
                return [{"kind": "text", "text": sep + text}]
            if itype == "reasoning":
                return [{"kind": "thinking"}]
            if kind == "item.started":
                if itype == "command_execution":
                    return [{"kind": "tool", "label": "$ " + first_line(unwrap_shell(item.get("command")), 90)}]
                if itype == "mcp_tool_call":
                    return [{"kind": "tool", "label": f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"}]
                if itype == "web_search":
                    return [{"kind": "tool", "label": "web search " + first_line(item.get("query"), 80)}]
                if itype == "todo_list":
                    return [{"kind": "tool", "label": "plan"}]
            if kind == "item.completed":
                if itype == "file_change":
                    paths = [short_path(c.get("path")) for c in item.get("changes") or []]
                    return [{"kind": "tool", "label": "edit " + ", ".join(paths[:3])
                             + (f" +{len(paths) - 3}" if len(paths) > 3 else "")}]
                if itype == "command_execution" and item.get("exit_code") not in (0, None):
                    return [{"kind": "tool_out", "text": f"exit {item.get('exit_code')}: "
                             + first_line(item.get("aggregated_output"), 80), "error": True}]
            return []
        if kind == "turn.completed":
            u = d.get("usage") or {}
            return [{"kind": "usage", "input": u.get("input_tokens", 0),
                     "cached": u.get("cached_input_tokens", 0), "output": u.get("output_tokens", 0)}]
        if kind in ("turn.failed", "error"):
            err = d.get("error") if isinstance(d.get("error"), dict) else d
            return [{"kind": "error", "text": first_line(err.get("message"), 200)}]
        return []


class CursorEngine(Engine):
    name, resume_flag = "Cursor", "--resume"

    def argv(self, prompt, model, effort, session=None, plan=False, images=(), tier=None, mesh=False, needs_mcp=False):
        argv = [self.exe, "-p", "--output-format", "stream-json", "--stream-partial-output",
                "--model", model, "--trust"]
        argv += ["--mode", "plan"] if plan else ["--yolo"]
        if session:
            argv += ["--resume", session]
        return argv + [prompt]

    def parse(self, d, state):
        kind = d.get("type")
        if kind == "system" and d.get("subtype") == "init":
            return [{"kind": "session", "id": d.get("session_id")}]
        if kind == "assistant":
            text = "".join(c.get("text", "") for c in (d.get("message") or {}).get("content") or []
                           if isinstance(c, dict))
            seg = state.get("seg", "")
            if "model_call_id" in d or "timestamp_ms" not in d:
                # the whole message again after its deltas: print only what the deltas missed
                state["seg"] = ""
                if not seg:
                    rest = text                 # nothing streamed: the message is new
                elif text.startswith(seg):
                    rest = text[len(seg):]
                else:
                    rest = ""                   # already shown, worded by its deltas
                return [{"kind": "text", "text": rest}] if rest else []
            state["seg"] = seg + text
            return [{"kind": "text", "text": text}]
        if kind == "thinking":
            return [{"kind": "thinking"}]
        if kind == "tool_call" and d.get("subtype") == "started":
            call = d.get("tool_call") or {}
            key = next(iter(call), "tool")
            args = (call.get(key) or {}).get("args") or {}
            name = re.sub(r"ToolCall$", "", key)
            return [{"kind": "tool", "label": describe(name, args)}]
        if kind == "result":
            u = d.get("usage") or {}
            out = [{"kind": "session", "id": d.get("session_id")},
                   {"kind": "usage", "input": u.get("inputTokens", 0) + u.get("cacheReadTokens", 0),
                    "cached": u.get("cacheReadTokens", 0), "output": u.get("outputTokens", 0)}]
            if d.get("is_error"):
                out.append({"kind": "error", "text": first_line(d.get("result"), 200)})
            return out
        return []


KIRO_AGENT = "star"


def ensure_kiro_agent(home=None, cwd=None, mcp=False):
    """Keep native Kiro resources, adapting MCP schemas on full-tool turns.

    Light turns retain the small built-in-only profile. Standard and heavy
    turns inherit the user's and workspace's servers; stdio servers run through
    a transparent schema bridge. Original profiles and MCP configs stay intact.
    """
    home, cwd = Path(home or Path.home()), Path(cwd or os.getcwd()).resolve()
    folder = home / ".kiro" / "agents"
    name = KIRO_AGENT + ("-mcp-" + hashlib.sha256(str(cwd).encode()).hexdigest()[:12] if mcp else "")
    target = folder / f"{name}.json"
    try:
        base = json.loads((folder / "herdr.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        base = {"tools": ["*"], "allowedTools": [], "resources": ["file://AGENTS.md", "file://README.md"]}
    servers = {}
    if mcp:
        for path in (home / ".kiro/settings/mcp.json", cwd / ".kiro/settings/mcp.json"):
            try:
                entries = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
                if isinstance(entries, dict):
                    servers.update(entries)
            except (OSError, ValueError, AttributeError):
                pass
        servers.update(base.get("mcpServers") or {})
        proxy = str(Path(__file__).with_name("star_mcp_proxy.py"))
        for key, spec in list(servers.items()):
            if isinstance(spec, dict) and spec.get("command") and not spec.get("disabled"):
                servers[key] = dict(spec, command=sys.executable,
                                    args=[proxy, "--", spec["command"], *(spec.get("args") or [])])
    base.update(name=name, description="StarCLI headless turns" + (" (MCP schema bridge)" if mcp else " (built-in tools)"),
                includeMcpJson=False, mcpServers=servers)
    text = json.dumps(base, indent=2) + "\n"
    try:
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            folder.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".star-agent-", suffix=".json", dir=str(folder))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except OSError:
        return None
    return name


class KiroEngine(Engine):
    name, resume_flag = "Kiro", "--resume-id"
    asks = True

    def with_approvals(self, argv, folder):
        """`chat --no-interactive` cannot ask, so the same turn runs through
        star_kiro_acp, a `kiro-cli acp` client that puts each request to StarCLI."""
        config = {"kiro": argv[0], "approvals": str(folder), "flags": [], "prompt": argv[-1]}
        words = iter(argv[2:-1])
        for word in words:
            if word in ("--model", "--agent", "--effort"):
                config["flags"] += [word, next(words)]
            elif word == "--resume-id":
                config["session"] = next(words)
        # Tools the picker profile already trusts stay trusted, as in its native pane.
        profile = list((self.agent or {}).get("argv") or [])
        for i, word in enumerate(profile):
            if word.startswith("--trust-tools="):
                config["flags"].append(word)
            elif word == "--trust-tools" and i + 1 < len(profile):
                config["flags"] += [word, profile[i + 1]]
        return [sys.executable, str(Path(__file__).with_name("star_kiro_acp.py")), json.dumps(config)]

    def argv(self, prompt, model, effort, session=None, plan=False, images=(), tier=None, mesh=False, needs_mcp=False):
        argv = [self.exe, "chat", "--output-format", "stream-json", "--model", model]
        # Kiro has no plan mode: trust only reading, and StarCLI's prompt asks for a plan
        argv += ["--trust-tools=fs_read"] if plan else ["--trust-all-tools"]
        agent = ensure_kiro_agent(mcp=needs_mcp or tier in ("standard", "heavy"))
        if agent:
            argv += ["--agent", agent]
        if effort:
            argv += ["--effort", effort]
        if session:
            argv += ["--resume-id", session]
        return argv + [prompt]

    def parse(self, d, state):
        kind, data = d.get("type"), d.get("data") or {}
        if kind == "metadata":
            out = []
            if data.get("sessionId") and not state.get("session"):
                state["session"] = data["sessionId"]
                out.append({"kind": "session", "id": data["sessionId"]})
            if data.get("meteringUsage"):
                credits = sum(float(m.get("value", 0)) for m in data["meteringUsage"] if isinstance(m, dict))
                out.append({"kind": "usage", "credits": credits})
            return out
        if kind == "sessionUpdate":
            update = data.get("update") or {}
            what = update.get("sessionUpdate")
            if what == "agent_message_chunk":
                return [{"kind": "text", "text": (update.get("content") or {}).get("text", "")}]
            if what == "agent_thought_chunk":
                return [{"kind": "thinking"}]
            if what == "tool_call":
                title = update.get("title") or (update.get("_meta") or {}).get("kiro", {}).get("toolName", "tool")
                return [{"kind": "tool", "label": re.sub(r"^Running: ", "$ ", first_line(title, 90))}]
            if what == "tool_call_update" and update.get("status") == "failed":
                return [{"kind": "tool_out", "text": "failed", "error": True}]
            return []
        if kind == "runError":
            return [{"kind": "error", "text": first_line(data.get("message"), 200)}]
        return []


class AntigravityEngine(Engine):
    name, resume_flag = "Antigravity", "--conversation"

    def argv(self, prompt, model, effort, session=None, plan=False, images=(), tier=None, mesh=False, needs_mcp=False):
        argv = [self.exe, "-p", prompt, "--output-format", "stream-json", "--model", model]
        argv += ["--mode", "plan"] if plan else ["--dangerously-skip-permissions"]
        if session:
            argv += ["--conversation", session]
        return argv

    def parse(self, d, state):
        kind = d.get("event")
        if kind == "init":
            return [{"kind": "session", "id": d.get("conversation_id")}]
        if kind == "step_update":
            step = d.get("step_update") or {}
            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                return [{"kind": "text", "text": step["text_delta"]}]
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                info = step.get("tool_info") or {}
                return [{"kind": "tool", "label": describe(step.get("tool_name") or "tool", info.get("parameters"))}]
            return []
        if kind == "result":
            result = d.get("result") or {}
            u = result.get("usage") or {}
            out = [{"kind": "usage", "input": u.get("input_tokens", 0) + u.get("cache_read_tokens", 0),
                    "cached": u.get("cache_read_tokens", 0), "output": u.get("output_tokens", 0)}]
            if result.get("status") not in (None, "SUCCESS"):
                out.append({"kind": "error", "text": first_line(result.get("error") or result.get("status"), 200)})
            return out
        if kind == "error":
            return [{"kind": "error", "text": first_line(d.get("message") or d.get("error"), 200)}]
        return []


ENGINES = {e.name: e for e in (ClaudeEngine, CodexEngine, CursorEngine, KiroEngine, AntigravityEngine)}

# A provider run headless inside StarCLI must not report itself to herdr: its
# herdr integration would claim StarCLI's pane, mark it working and never let
# go, since StarCLI itself reports the pane's state. The tab and workspace stay,
# so the agent's own `star --send` still finds its peers.
HIDDEN_FROM_CHILDREN = ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_BIN_PATH")


def child_env():
    env = {k: v for k, v in os.environ.items() if k not in HIDDEN_FROM_CHILDREN}
    # Provider integrations must not report as the Star pane, but their shell's
    # star --send command still needs the exact server's mesh namespace.
    if os.environ.get("HERDR_SOCKET_PATH"):
        env["STAR_MESH_SOCKET"] = os.environ["HERDR_SOCKET_PATH"]
    return env


class Turn:
    """One running turn: iterate it for events; interrupt() stops the CLI."""

    def __init__(self, engine, argv, cwd=None, lease=None):
        self.engine, self.argv = engine, argv
        guardian = [sys.executable, str(Path(__file__).with_name("star_process.py"))]
        self.proc = subprocess.Popen(guardian, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.PIPE, cwd=cwd, text=True, encoding="utf-8",
                                     errors="replace", bufsize=1, env=child_env(),
                                     # Its own session, so ctrl-c reaches StarCLI and StarCLI decides how to stop it.
                                     start_new_session=True)
        self.stderr = []
        self.interrupted = False
        self.drain = threading.Thread(target=self._drain, daemon=True)
        self.drain.start()
        config = {"argv": argv}
        if lease and lease.ident:
            config.update(locks=str(lease.folder / "turns"), conversation=lease.ident)
        try:
            self.proc.stdin.write(json.dumps(config) + "\n")
            self.proc.stdin.flush()
            ready = self.proc.stdout.readline()
            if ready.strip() != '{"star_guard":"ready"}':
                self.proc.wait()
                self.drain.join()
                raise OSError(first_line("".join(self.stderr) or "provider guardian did not start"))
            self.proc.stdin.write("go\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.proc.wait(timeout=5)
            self.drain.join(timeout=1)
            raise

    def _drain(self):
        with self.proc.stderr:
            for line in self.proc.stderr:
                self.stderr.append(line)
                del self.stderr[:-40]

    def __iter__(self):
        state, failed = {}, False
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                for event in self.engine.parse(data, state):
                    failed |= event["kind"] == "error"
                    yield event
            code = self.proc.wait()
            self.drain.join()
            if code and not failed and not self.interrupted:
                tail = [l.strip() for l in self.stderr if l.strip() and "no stdin data" not in l
                        and "Reading additional input" not in l]
                yield {"kind": "error", "text": first_line(tail[-1] if tail else f"exit status {code}", 200)}
        finally:
            self.close()

    def close(self):
        if self.proc.poll() is None:
            self.interrupt()
            self.proc.wait(timeout=5)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.drain.join(timeout=1)

    def interrupt(self):
        self.interrupted = True
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
