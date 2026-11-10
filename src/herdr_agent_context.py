"""Position context for agents running in a Herdr tab."""

import json
import os
import copy

from platform_runtime import state_dir


_STANDARD = (("agy", "agent 1"), ("codex", "agent 2"), ("claude", "agent 3"),
            ("picker", "agent 4"), ("cursor", "agent 5"))
_TEAM = (("crew1", "agent 1"), ("crew2", "agent 2"), ("lead", "agent 3"),
         ("crew3", "agent 4"), ("crew4", "agent 5"))


def _layout():
    """Return a complete known role mapping for this tab, or an empty mapping."""
    if (os.environ.get("HERDR_ENV") != "1" or not os.environ.get("HERDR_TAB_ID")
            or not os.environ.get("HERDR_PANE_ID")):
        return {}
    key = (os.environ.get("HERDR_SOCKET_PATH") or "default") + "|" + os.environ["HERDR_TAB_ID"]
    try:
        data = json.loads((state_dir() / "herdr-ai-tabs.json").read_text(encoding="utf-8"))
        entry = data.get(key)
        if not isinstance(entry, dict):
            return {}
        roles = _TEAM if entry.get("team") else _STANDARD
        panes = entry.get("panes")
        if not isinstance(panes, dict):
            return {}
        mapped = {role: panes.get(role) for role, _ in roles}
        if any(not isinstance(pane, str) or not pane for pane in mapped.values()):
            return {}
        if len(set(mapped.values())) != len(mapped):
            return {}
        return {role: (number, mapped[role]) for role, number in roles}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def brief():
    """Describe pane positions and, when available, their saved native pane IDs."""
    if (os.environ.get("HERDR_ENV") != "1" or not os.environ.get("HERDR_TAB_ID")
            or not os.environ.get("HERDR_PANE_ID")):
        return ""
    text = [
        "Herdr agent positions are visual: the top row from left to right is agent 1, agent 2, agent 3; "
        "the bottom row from left to right is agent 4, agent 5. The tall pane on the right is agent 3. "
        "The terminal, file browser (fs), and usage pane are unnumbered. Agent numbers mean screen position, "
        "not provider, crew ordinal, or raw pane ID."
    ]
    layout = _layout()
    if not layout:
        text.append("No recognized saved Herdr layout is available; inspect the live layout before guessing an agent number or pane ID.")
        return "\n".join(text)

    ordered = sorted(layout.values(), key=lambda item: int(item[0].split()[-1]))
    text.append("Saved agent pane map: " + "; ".join(f"{number} = {pane}" for number, pane in ordered) + ".")
    text.append("The saved map can be stale if panes moved or were rearranged; check the live layout before relying on a saved pane ID.")
    own = os.environ.get("HERDR_PANE_ID")
    current = next(((number, pane) for number, pane in layout.values() if pane == own), None)
    if current:
        text.append(f"You are {current[0]} in pane {current[1]}.")
    second = next(((number, pane) for number, pane in layout.values() if number == "agent 2"), None)
    if second:
        text.append(f"To inspect agent 2, run `herdr agent list`, then `herdr agent read {second[1]} --source recent-unwrapped --lines 30`. This only reads status and recent output; do not interrupt the pane.")
    return "\n".join(text)


def annotate(agent):
    """Copy a non-Star agent profile and attach context when this is a Herdr tab."""
    if agent.get("name") == "Star":
        return agent
    context = brief()
    if not context or os.environ.get("HERDR_ENV") != "1":
        return agent
    annotated = copy.deepcopy(agent)
    annotated["_herdr_context"] = context
    return annotated
