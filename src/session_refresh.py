"""Resume the exact conversation in a Herdr pane, without replaying its prompt."""

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path


NAMES = {
    "claude": "Claude", "codex": "Codex", "agy": "Antigravity",
    "antigravity": "Antigravity", "antigravity-cli": "Antigravity",
    "cursor": "Cursor", "cursor-agent": "Cursor", "cursor-cli": "Cursor",
    "kiro": "Kiro", "kiro-cli": "Kiro",
    "star": "Star", "starcli": "Star",
}
RESUME = {"Claude": "--resume", "Codex": "resume", "Cursor": "--resume",
          "Kiro": "--resume-id", "Antigravity": "--conversation", "Star": "--resume-id"}
# Unknown flags are deliberately refused before stopping a pane. We must know
# which words are option values so an old opening prompt is never sent again.
VALUES = {
    "Claude": {"--model", "--effort", "--agent", "--settings", "--append-system-prompt",
               "--system-prompt", "--permission-mode", "--add-dir", "--mcp-config"},
    "Codex": {"-m", "--model", "-c", "--config", "-p", "--profile", "-s", "--sandbox",
              "-a", "--ask-for-approval", "-C", "--cd", "--add-dir", "--enable", "--disable"},
    "Cursor": {"--model", "--mode", "--workspace", "--sandbox"},
    "Kiro": {"--model", "--effort", "--agent", "--agent-engine", "--mode", "--wrap"},
    "Antigravity": {"--model", "--effort", "--agent", "--project", "--mode", "--add-dir",
                    "--log-file"},
    "Star": {"--name", "--role", "--use", "--tier", "--timeout"},
}
FLAGS = {
    "Claude": {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
               "--disable-slash-commands", "--verbose", "--strict-mcp-config"},
    "Codex": {"--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
              "--approve-for-me", "--search", "--no-alt-screen", "--no-daemon", "--strict-config"},
    "Cursor": {"--yolo", "--trust", "--force"},
    "Kiro": {"--trust-all-tools", "--tui", "--legacy-ui", "--classic",
             "--v2", "--v3", "--require-mcp-startup"},
    "Antigravity": {"--dangerously-skip-permissions", "--sandbox", "--remote-control"},
    "Star": {"--plan"},
}
OLD_SESSION = {
    "Claude": {"--resume", "-r", "--session-id"}, "Codex": set(),
    "Cursor": {"--resume"}, "Kiro": {"--resume-id"},
    "Antigravity": {"--conversation"}, "Star": {"--resume-id"},
}
# One-shot StarCLI flags. A refresh reopens the interactive conversation, so these
# leave with the old command instead of turning the resumed pane into a print run.
STAR_DROPPED = {"-c", "--continue", "-p", "--print", "--route", "--usage", "--peers", "--wait"}
STAR_DROPPED_VALUES = {"--goal", "--send"}
OLD_FLAGS = {"--continue", "--fork-session", "--last", "--all", "--resume-picker"}


def provider(value):
    return NAMES.get(str(value or "").lower())


def _label(value):
    return str(value or "").replace("\\", "/").split("/")[-1].lower().removesuffix(".exe")


def native_process(info, name):
    """Identify the provider itself, excluding its picker and MCP children.

    StarCLI is a Python script, so its name sits in argv after the interpreter.
    The picker that launched it is not Star, even when its arguments name Star."""
    found = []
    for process in info.get("foreground_processes", []):
        argv = process.get("argv") or []
        if name == "Star" and any(_label(word).startswith("agent_picker") for word in argv[:2]):
            continue
        labels = [process.get("name"), process.get("argv0"), argv[0] if argv else None]
        if name == "Star":
            labels += [word for word in argv[1:3] if not str(word).startswith("-")]
        if argv and any(provider(_label(label).removesuffix(".py").removesuffix(".cmd")) == name
                        for label in labels):
            found.append(process)
    return found[0] if len(found) == 1 else None


def stored(name, session, cwd):
    """Whether a directory-scoped provider saved this conversation for cwd; None if
    the provider is not directory-scoped or its storage is not known.

    Claude and Cursor only resume a conversation from the directory it began in,
    and Claude reports a session ID before its first prompt writes a transcript.
    """
    if name == "Claude":
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        if len(slug) > 200:   # Claude shortens long paths with a hash
            return None
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return (base / "projects" / slug / f"{session}.jsonl").is_file()
    if name == "Cursor":
        digest = hashlib.md5(cwd.encode("utf-8")).hexdigest()
        return (Path.home() / ".cursor" / "chats" / digest / session).exists()
    if name == "Star":
        # StarCLI saves the whole conversation, including its directory. A refresh
        # in another directory starts fresh; the old file stays resumable there.
        from platform_runtime import state_dir
        from star_sessions import valid_id, valid_conversation
        if not valid_id(session):
            return False
        path = state_dir() / "star" / "conversations" / f"{session}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not valid_conversation(data) or data.get("id") != session:
                return False
            saved = data.get("cwd") or ""
        except (OSError, ValueError, TypeError, KeyError):
            return False
        return bool(saved) and os.path.realpath(saved) == os.path.realpath(cwd)
    return None


def star_identity(info, pane, now=None):
    """Read this exact live Star process's identity on older Herdr servers.

    Native session refs take precedence. This fallback never chooses the newest
    conversation: it requires one fresh record for the process in this pane,
    tab, workspace and server, plus its saved conversation in the same cwd.
    """
    from platform_runtime import state_dir
    process = native_process(info, "Star")
    if not process or not process.get("pid"):
        return None
    now = time.time() if now is None else now
    matches = []
    folder = state_dir() / "star" / "mesh" / "members"
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            age = now - float(data["updated"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if (data.get("pid") != process["pid"] or data.get("pane") != pane
                or data.get("socket") != os.environ.get("HERDR_SOCKET_PATH")
                or data.get("tab") != os.environ.get("HERDR_TAB_ID")
                or data.get("workspace") != os.environ.get("HERDR_WORKSPACE_ID")
                or not 0 <= age <= 15 or data.get("status") not in ("idle", "working", "blocked")):
            continue
        ident = data.get("conversation")
        if (not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", ident)
                or not stored("Star", ident, data.get("cwd") or "")):
            continue
        protected = data.get("draft") or data.get("ready") is False
        matches.append({"agent_session": {"agent": "star", "kind": "id", "value": ident},
                        "agent_status": "working" if protected and data["status"] == "idle" else data["status"]})
    return matches[0] if len(matches) == 1 else None


def resume_argv(name, argv, session):
    """Keep launch settings, replace session selectors, and drop opening prompts."""
    if name not in RESUME or not argv or not isinstance(session, str) or not session.strip():
        raise ValueError("exact session or supported provider unavailable")
    argv = list(argv)
    prefix, words = argv[:1], argv[1:]
    # Cursor exposes Bun's actual argv, including its bundled JavaScript entry.
    if name == "Cursor" and len(words) >= 2 and words[0] == "--use-system-ca" and words[1].endswith(".js"):
        prefix += words[:2]
        words = words[2:]
    if name == "Kiro" and words[:1] == ["chat"]:
        prefix += ["chat"]
        words = words[1:]
    if name == "Cursor" and words[:1] == ["agent"]:
        prefix += ["agent"]
        words = words[1:]
    # python starcli.py: the script path is the program, not an old prompt.
    if name == "Star" and words and not words[0].startswith("-"):
        prefix += words[:1]
        words = words[1:]
    if name == "Codex" and words[:1] in (["resume"], ["fork"]):
        words = words[1:]
        if words and not words[0].startswith("-"):
            words = words[1:]
    kept = []
    i = 0
    while i < len(words):
        word = words[i]
        option = word.split("=", 1)[0]
        if word == "--":
            break  # the remaining words are the old opening prompt
        if option in OLD_SESSION[name] or (name == "Antigravity" and option in {"-i", "--prompt-interactive"}):
            if "=" not in word and i + 1 < len(words) and not words[i + 1].startswith("-"):
                i += 1
        elif name == "Star" and option in STAR_DROPPED_VALUES:
            if "=" not in word and i + 1 < len(words) and not words[i + 1].startswith("-"):
                i += 1
        elif word in OLD_FLAGS or (name == "Claude" and word == "-c") or (name == "Kiro" and word in {"-r", "--resume"}) \
                or (name == "Star" and word in STAR_DROPPED):
            pass
        elif option in VALUES[name]:
            kept.append(word)
            if "=" not in word:
                if i + 1 >= len(words):
                    raise ValueError(f"missing value for {word}")
                i += 1
                kept.append(words[i])
        elif word in FLAGS[name]:
            kept.append(word)
        elif word.startswith("-"):
            raise ValueError(f"unrecognized launch option {option}")
        # Positional words are old prompts, never a new task on refresh.
        i += 1
    selector = ["resume", session] if name == "Codex" else [RESUME[name], session]
    return prefix + kept + selector


def encode(argv):
    # One shell-safe word works in both PowerShell and cmd, even for team briefs.
    return base64.urlsafe_b64encode(json.dumps(argv).encode("utf-8")).decode("ascii")


def decode(value):
    try:
        argv = json.loads(base64.b64decode(value, altchars=b"-_", validate=True).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid saved launch arguments") from exc
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) and "\0" not in word for word in argv):
        raise ValueError("invalid saved launch arguments")
    return argv
