"""Provider-native system prompt layers, independent of herdr-ai's opening instructions.

A provider with an adapter here gets a System Prompts toggle in its launch
defaults. On (the default) launches the provider normally. Off asks the
provider's own CLI to leave out the prompt layers it adds by itself, on every
launch path: the selector, herdr-ai panes, StarCLI turns, native StarCLI
sessions and scheduled prompts. Models, permissions and herdr-ai briefs are
never touched, so the provider layer and the Herdr layer stay independent.

An adapter module exposes disable(agent, argv) -> argv. It returns a new list,
may refuse a conflicting argv with ValueError, and must be idempotent, because
a prepared argv can be prepared again (refresh, StarCLI's native handoff).
"""

import importlib


KEY = "system_prompts"
# provider -> (adapter module, the note shown under the toggle)
ADAPTERS = {
    "Antigravity": ("agy_prompts", "agy context only; herdr-ai adds its own instructions."),
    "Cursor": ("cursor_prompts", "Cursor rules, skills and notes only; herdr-ai adds its own instructions."),
}


def supported(agent):
    return agent.get("name") in ADAPTERS


def enabled(agent):
    """The saved choice, else the shared default in agents.json, else On."""
    return agent.get("defaults", {}).get(KEY, agent.get(KEY, True))


def note(agent):
    return ADAPTERS[agent["name"]][1]


def prepare(agent, argv):
    """argv with the provider's own prompt layer removed when its toggle is Off."""
    if not supported(agent) or enabled(agent):
        return list(argv)
    return importlib.import_module(ADAPTERS[agent["name"]][0]).disable(agent, list(argv))
