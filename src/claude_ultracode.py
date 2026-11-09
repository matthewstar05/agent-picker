"""Claude Code's ultracode for picker launches: settings plus model guidance.

The Ultracode default lives in the Claude agent's local defaults. At launch it
adds {"ultracode": true, "enableWorkflows": true} to Claude's settings and a
short note on when Workflow agents should use haiku to its system prompt.
Claude reads one --settings and one --append-system-prompt, so both are merged
into any already present (team panes add their own) instead of repeated.
"""

import hashlib
import json
import os
import tempfile

import provider_defaults
from platform_runtime import state_dir


SETTINGS = {"ultracode": True, "enableWorkflows": True}
GUIDANCE = ("Ultracode workflows: pass model: 'haiku' to Workflow agent() calls whose job is "
            "mechanical or narrow, such as searching, listing or reading files, extracting or "
            "reformatting data, or simple checks, so they run fast and cheap. Leave model unset "
            "(the session model) for stages that design, write or change important code, judge, "
            "synthesize, or adversarially verify, and whenever the user asks for a specific model.")
SETTINGS_FLAG = "--settings"
PROMPT_FLAG = "--append-system-prompt"
WINDOWS = os.name == "nt"


def enabled(agent):
    """On for Claude when its default is on, except herdr-ai crew panes: their brief
    says to do the task as given without starting subagents."""
    if agent.get("name") != "Claude" or agent.get("team_role") == "crew":
        return False
    return (provider_defaults.current(agent) or {}).get("ultracode") is True


def occurrences(argv, flag):
    """(start, end, value) for each `flag VALUE` or `flag=VALUE` before a bare --."""
    found, i = [], 1
    while i < len(argv) and argv[i] != "--":
        arg = argv[i]
        if arg == flag and i + 1 < len(argv):
            found.append((i, i + 2, argv[i + 1]))
            i += 2
        elif arg.startswith(flag + "="):
            found.append((i, i + 1, arg[len(flag) + 1:]))
            i += 1
        else:
            i += 1
    return found


def rewrite(argv, flag, found, value):
    """argv with every occurrence of flag dropped and one `flag value` in the first's place,
    or before a bare -- (else at the end) when there was none."""
    drop = {i for start, end, _ in found for i in range(start, end)}
    if found:
        at = found[0][0]
    else:
        at = argv.index("--") if "--" in argv[1:] else len(argv)
    out = [arg for i, arg in enumerate(argv[:at]) if i not in drop]
    out += [flag, value]
    out += [arg for i, arg in enumerate(argv[at:], at) if i not in drop]
    return out


def read_settings(value):
    """A --settings value: inline JSON or a JSON file path."""
    try:
        if value.lstrip().startswith("{"):
            data = json.loads(value)
        else:
            with open(value, encoding="utf-8-sig") as f:
                data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Ultracode could not read Claude --settings {value}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Ultracode: Claude --settings {value} is not a JSON object")
    return data


def settings_file(data):
    """A settings file named by its content, so concurrent panes share it and
    never read one another's partial writes. A file, not inline JSON, since a
    .cmd shim would strip the quotes."""
    text = json.dumps(data, sort_keys=True)
    folder = state_dir() / "claude-settings"
    path = folder / ("ultracode-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] + ".json")
    try:
        if path.read_text(encoding="utf-8") == text:
            return str(path)
    except OSError:
        pass
    folder.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ultracode-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path)


def prepare(agent, argv):
    """The final Claude argv with ultracode on, when its default is on. Idempotent."""
    argv = list(argv)
    if not enabled(agent) or not argv:
        return argv
    found = occurrences(argv, SETTINGS_FLAG)
    merged = {}
    for _, _, value in found:   # later --settings win, as in Claude Code
        merged.update(read_settings(value))
    merged.update(SETTINGS)
    argv = rewrite(argv, SETTINGS_FLAG, found, settings_file(merged))
    found = occurrences(argv, PROMPT_FLAG)
    texts = [value for _, _, value in found if value]
    if not any(GUIDANCE in text for text in texts):
        texts.append(GUIDANCE)
    # cmd.exe ends a .cmd shim's command line at a line break, so join on one line there.
    shim = WINDOWS and argv[0].lower().endswith((".cmd", ".bat"))
    return rewrite(argv, PROMPT_FLAG, found, (" " if shim else "\n\n").join(texts))
