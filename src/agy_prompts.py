"""agy's native prompt layer, independent of herdr-ai's opening instructions."""

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


def prepare(agent, argv):
    """Apply only agy's layer. Preserve models, permissions and Herdr briefs."""
    if agent["name"] != "Antigravity" or provider_defaults.current(agent)["system_prompts"]:
        return list(argv)
    selected = provider_defaults.option(argv, ("--agent",))
    if selected and selected != NAME:
        raise ValueError("agy System Prompts Off cannot be combined with --agent " + selected +
                         "; turn System Prompts On to use a custom agent.")
    name = install()
    return list(argv) if selected else [argv[0], "--agent", name, *argv[1:]]
