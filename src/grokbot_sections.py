"""Weekly sidebar sections for CLI conversations in the Grok Bot desktop app.

The app's own sections are a list of {id, name, agentIds}, kept in the bot
computer's host settings and mirrored to the account, the same two writes the
app makes. Only sections whose id starts with PREFIX belong to the CLI; a
section the user made, and any bot the user filed into one, are left alone.
"""

from contextlib import contextmanager
import datetime
import fcntl
import os

from platform_runtime import state_dir

PREFIX = "cli-week-"
UNASSIGNED = "__agents__"   # the app's own section for bots in no other section
DESCRIPTION = "Coding agent for the Grok Bot terminal CLI. Use the user's selected machine and project."


def week_start(ms):
    day = datetime.datetime.fromtimestamp(ms / 1000).date()
    return day - datetime.timedelta(days=day.weekday())


def week_name(monday, today=None):
    sunday = monday + datetime.timedelta(days=6)
    label = monday.strftime("%b ") + str(monday.day) + " – " + (
        str(sunday.day) if sunday.month == monday.month else sunday.strftime("%b ") + str(sunday.day))
    if monday.year != (today or datetime.date.today()).year:
        label += ", " + str(monday.year)
    return "CLI · " + label


def is_cli(row):
    return row.get("description") == DESCRIPTION or str(row.get("name", "")).startswith("CLI · ")


def arrange(sections, roster, today=None):
    """Return sections with every CLI bot filed under its creation week, or None if nothing changes."""
    before = [dict(s, agentIds=list(s.get("agentIds") or [])) for s in sections if isinstance(s, dict) and s.get("id")]
    sections = [dict(s, agentIds=list(s["agentIds"])) for s in before]
    ours = {s["id"]: s for s in sections if s["id"].startswith(PREFIX)}
    live = {row["id"] for row in roster}
    filed = {ident for s in sections if s["id"] not in ours for ident in s["agentIds"]}
    for section in ours.values():
        section["agentIds"] = [ident for ident in section["agentIds"] if ident in live and ident not in filed]
    for row in sorted(roster, key=lambda r: r.get("createdAt") or 0, reverse=True):
        if not is_cli(row) or row["id"] in filed or not isinstance(row.get("createdAt"), (int, float)):
            continue
        monday = week_start(row["createdAt"])
        key = PREFIX + monday.isoformat()
        for section in ours.values():
            if section["id"] != key and row["id"] in section["agentIds"]:
                section["agentIds"].remove(row["id"])
        section = ours.setdefault(key, {"id": key, "name": week_name(monday, today), "agentIds": []})
        if row["id"] not in section["agentIds"]:
            section["agentIds"].append(row["id"])
    # Other sections keep their order. CLI weeks stay together, newest first, where
    # they already are, else above the app's own Unassigned section.
    weeks = sorted((s for s in ours.values() if s["agentIds"]), key=lambda s: s["id"], reverse=True)
    others = [s for s in sections if s["id"] not in ours]
    first = next((i for i, s in enumerate(sections) if s["id"] in ours), None)
    at = (len([s for s in sections[:first] if s["id"] not in ours]) if first is not None
          else next((i for i, s in enumerate(others) if s["id"] == UNASSIGNED), len(others)))
    result = others[:at] + weeks + others[at:]
    return None if result == before else result


@contextmanager
def locked():
    # CLIs and herdr panes start together; one read-modify-write at a time.
    path = state_dir() / "grokbot"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path / "sections.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def organize(gateway):
    """File CLI bots into week sections. Returns the number of CLI sections written, or None if unchanged."""
    with locked():
        roster = gateway.agents()
        current = gateway.call("getHostSettings").get("sidebarSections") or []
        sections = arrange(current, roster)
        if sections is None:
            return None
        gateway.call("setHostSettings", {"sidebarSections": sections})
        gateway.account("UpdateGrokBotUserRuntimeSettings", {"sidebarSections": {"sections": [
            {"id": s["id"], "name": s["name"], "agentIds": s["agentIds"]} for s in sections]}})
        return sum(1 for s in sections if s["id"].startswith(PREFIX))
