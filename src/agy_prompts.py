"""agy's native prompt layer for system_prompts: a managed custom agent without prompt sections."""

import os
from pathlib import Path
import tempfile

import provider_defaults


NAME = "agent-picker-no-system-prompt-v1"
PROFILE = """---
name: agent-picker-no-system-prompt-v1
description: agent-picker managed profile with agy's conversation system prompts disabled
mainAgent: true
subagent: false
excludeDefaultComponents: true
inheritCustomizations: true
tools:
  - view_file
  - run_command
  - manage_task
  - send_message
  - schedule
  - invoke_subagent
  - define_subagent
  - manage_subagents
  - write_to_file
  - replace_file_content
  - read_url_content
  - search_web
  - ask_question
---
"""
# excludeDefaultComponents opts out of prompts AND default tools. Explicitly
# retain the native tools; inherited MCP servers add their own tool accessors.
# Keeping customizations preserves MCP, hooks and explicit skill invocation,
# while the absence of prompt sections suppresses their automatic prompt text.
# https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md


def install():
    """Register a complete profile atomically, without overwriting user files."""
    path = Path.home() / ".gemini/config/agents" / (NAME + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".picker-prompt-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(PROFILE)
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # another pane registered the same profile
        finally:
            os.unlink(temporary)
    if path.read_text(encoding="utf-8") != PROFILE:
        raise RuntimeError("agy prompt profile conflicts with " + str(path) +
                           "; turn System Prompts On or move that file before launching.")
    return NAME


BRIEF_NAME = "agent-picker-no-system-prompt-brief-v1"


def install_brief(folder):
    """The same profile carrying the Herdr brief of a hidden_briefs folder.

    excludeDefaultComponents drops every rule, including the folder's own, but
    keeps the profile body, so the brief goes there. agy resolves agents in an
    --add-dir folder's .agents/agents by name. Each H1 opens a prompt section,
    so headings inside the brief are demoted."""
    import hidden_briefs
    brief = (folder / hidden_briefs.MARKER).read_text(encoding="utf-8").strip()
    lines = ["#" + line if line.startswith("# ") else line for line in brief.splitlines()]
    text = (PROFILE.replace("name: " + NAME, "name: " + BRIEF_NAME, 1)
            .replace("disabled\n", "disabled, plus the herdr-ai brief\n", 1)
            + "# herdr_brief\n" + "\n".join(lines) + "\n")
    path = folder / ".agents/agents" / (BRIEF_NAME + ".md")
    if not (path.is_file() and path.read_text(encoding="utf-8") == text):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + "." + str(os.getpid()))
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    return BRIEF_NAME


def disable(agent, argv):
    """Apply only agy's layer. Preserve models, permissions and Herdr briefs."""
    if agent.get("native_gemini"):
        return list(argv)   # the scheduler's native gemini fallback has no agy profiles
    import hidden_briefs
    folder = hidden_briefs.folder(argv)
    selected = provider_defaults.option(argv, ("--agent",))
    if selected and selected not in (NAME, BRIEF_NAME):
        raise ValueError("agy System Prompts Off cannot be combined with --agent " + selected +
                         "; turn System Prompts On to use a custom agent.")
    if folder is not None and (folder / hidden_briefs.MARKER).is_file():
        name = install_brief(folder)
    else:
        name = install()
    if selected == name:
        return list(argv)
    if selected:
        raise ValueError("agy System Prompts Off found --agent " + selected + " where " + name +
                         " belongs; relaunch from the picker.")
    return [argv[0], "--agent", name, *argv[1:]]
