"""Cursor's native prompt layer for system_prompts.

cursor-agent's base system prompt is assembled by Cursor's servers. Its only
replacement flag, the hidden --system-prompt <file>, is limited to Cursor's
own team and rejects an empty file, so the base prompt cannot be removed here.
What the CLI adds from the machine and workspace can be: the hidden
--exclude-workspace-context flag strips workspace-sourced context (rules,
skills, transcripts, notes) from the session.
"""

FLAG = "--exclude-workspace-context"


def disable(agent, argv):
    """Apply only Cursor's layer. Preserve models, permissions and Herdr briefs."""
    return list(argv) if FLAG in argv[1:] else [argv[0], FLAG, *argv[1:]]
