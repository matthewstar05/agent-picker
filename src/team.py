"""Single-provider teams for herdr-ai, and the menu herdr-ai opens to pick a setup.

A team is one lead agent beside four crew agents, all from one provider. The
lead runs the provider's strongest model; the crew run a cheaper one. The lead
is told to hand searches, test runs, and well specified edits to the crew
panes through herdr, so its own context and quota stay small. The crew are told
to answer in a short summary the lead can read cheaply. Each provider's pair of
models is the "team" entry of its profile in agents.json.

Instructions reach each CLI the way it supports: Claude appends them to its
system prompt, Codex takes them as developer instructions. Kiro, Cursor and
Antigravity have no such flag, so their lead gets them as its opening prompt
and their crew get the reply rules inside each task the lead sends.
"""

import copy
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

import agent_picker as ap
import provider_defaults
from platform_runtime import WINDOWS, Terminal, resolve_argv

CREW = ("crew1", "crew2", "crew3", "crew4")
REPLY = 15   # lines a crew reply may run, so the lead reads little

SAKURA = [(255, 190, 215), (240, 130, 175), (255, 225, 238)]
STANDARD = {
    "name": "Standard", "color": (240, 130, 175), "pastel": [255, 205, 225], "gradient": SAKURA,
    "model": "every provider", "short": "every provider",
    "aside": ["Claude · Codex · Antigravity", "Cursor/Kiro · selector · usage"],
}
HINTS = [" ↑↓ move · ⏎ lay out · esc quit ", " ⏎ lay out · esc quit ", " ⏎ lay out ", ""]
LAST = ap.STATE / "herdr-ai-setup"


def providers():
    """Installed profiles that define a team, in picker order."""
    return [a for a in ap.AGENTS if a.get("team") and ap.installed(a)]


def nick(agent, model):
    """A model's family in a word or two: claude-opus-5-5 -> opus, gpt-6.1-sol -> sol."""
    name = model.lower()
    if name.startswith("gemini-"):
        return " ".join(name.split("-")[2:]) or name   # gemini-3.8-flash-high -> flash high
    name = re.sub(r"^(claude|cursor|gpt)-", "", name)
    words = [w for w in re.split(r"[-_ ]", name) if w and not re.match(r"^[\d.]+$", w)]
    return words[-1] if model.lower().startswith("gpt-") and words else (words[0] if words else model)


def summary(agent):
    """'opus → haiku': the lead's model, then the crew's."""
    lead, crew = (nick(agent, agent["team"][r]["model"]) for r in ("lead", "crew"))
    shared = lead.split(" ")[0]
    if " " in lead and crew.startswith(shared + " "):
        crew = crew[len(shared) + 1:]   # flash high → low
    return f"{lead} → {crew}"


def option(agent):
    """A provider's team as a row in the setup menu."""
    text = summary(agent)
    return dict(agent, model=text, short=text)


def setups():
    return [STANDARD] + [option(a) for a in providers()]


def find(name):
    """The setup a --setup name means: None for standard, else a provider name."""
    if name.lower() == "standard":
        return None
    match = next((a["name"] for a in providers() if a["name"].lower() == name.lower()), None)
    if match is None:
        known = ", ".join(["standard"] + [a["name"].lower() for a in providers()])
        raise ValueError(f"unknown setup {name!r}; choose one of: {known}")
    return match


# ---- launching -------------------------------------------------------------

def herdr_command():
    return "herdr" if shutil.which("herdr") else os.environ.get("HERDR_BIN_PATH") or "herdr"


def reply_rule():
    return (f"Finish with at most {REPLY} lines: what you did or found, files changed, open questions. "
            f"Put anything longer in a file under {tempfile.gettempdir()} and give its path.")


def lead_brief(agent, crew):
    herdr = herdr_command()
    worker = agent["team"]["crew"]["model"]
    return f"""You lead a herdr team. Four {agent['name']} worker agents running {worker}, a cheaper model than yours, wait in the herdr panes {', '.join(crew)} on this tab, in your working directory. Your context is the expensive one: delegate whatever would fill it, and keep understanding the request, planning, decisions, integration, and the final check for yourself.

Delegate searching and reading code or docs, running builds and tests and digesting their output, mechanical or well specified edits, research, first drafts, and reviews. Do a step yourself only when it is quicker to do than to describe. Use these panes instead of built-in subagents.

How:
- `{herdr} agent list` shows each pane's status. idle or done means ready.
- `{herdr} agent prompt <pane> "<task>"` sends a task. For a single task add `--wait --timeout 900000`. For parallel work, send to several panes without --wait, then run `{herdr} agent wait <pane> --timeout 900000` for each.
- Workers do not see this conversation. Make each task self-contained: the goal, exact paths, constraints, and what to return. Give parallel workers separate files.
- End every task with: "{reply_rule()} Do not prompt other panes."
- Read only the result: `{herdr} agent read <pane> --source recent-unwrapped --lines 40`. Open a results file only when you need its detail.
- Check a worker's changes cheaply (git diff, a test run) before you report them as done.
- If a pane is blocked, read it before acting, and ask the user before approving anything risky. Do not close, restart, or rename the worker panes.
- Tell the user what you delegated in a line, not the full task."""


def crew_brief(agent, lead):
    return (f"You are a worker on a herdr team. The lead agent in herdr pane {lead} sends you tasks and reads "
            f"only the end of your reply. Do the task as given, within the files and scope it names. Do not prompt "
            f"other panes or start subagents. {reply_rule()}")


def cmd_safe(text):
    """One line without the characters cmd.exe treats specially, for a CLI behind a .cmd shim:
    cmd ends a command at a newline and misreads quotes, <, >, &, | and ^ inside arguments."""
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r'["<>&|^%]', "", text)


# iMCP drops every client when several connect at once, as a team's five panes do at launch.
CLAUDE_SETTINGS = {"deniedMcpServers": [{"serverName": "iMCP"}]}


def claude_settings():
    """A settings file that keeps iMCP out of a Claude team pane. A file, not inline JSON,
    since a .cmd shim would strip the quotes."""
    path = os.path.join(tempfile.gettempdir(), "herdr-team-claude-settings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(CLAUDE_SETTINGS, f)
    return path


def prepare(agent, args):
    """The profile and arguments for a team member: --picker-lead PANES or --picker-crew PANE first in args."""
    if len(args) < 2:
        raise ValueError(f"{args[0]} needs pane ids")
    lead = args[0] == "--picker-lead"
    target, args = args[1], list(args[2:])
    config = (agent.get("team") or {}).get("lead" if lead else "crew")
    if not config:
        raise ValueError(f"{agent['name']} has no team in agents.json")
    agent = copy.deepcopy(agent)
    if provider_defaults.current(agent) is not None:
        provider_defaults.apply(agent, {"model": config["model"],
                                        "effort": config.get("effort", "provider default")})
    agent["detail"] = ("lead · " if lead else "crew · ") + agent.get("detail", "")
    brief = lead_brief(agent, target.split(",")) if lead else crew_brief(agent, target)
    shim = WINDOWS and str((resolve_argv(agent) or [""])[0]).lower().endswith((".cmd", ".bat"))
    if shim:
        brief = cmd_safe(brief)
    name = agent["name"]
    if name == "Claude":
        agent["argv"] += ["--append-system-prompt", brief, "--settings", claude_settings()]
    elif name == "Codex":
        # A TOML string; through a cmd shim, unquoted text Codex reads as a literal string instead.
        agent["argv"] += ["-c", "developer_instructions=" + (brief if shim else json.dumps(brief))]
        agent["argv"] += ["-c", "mcp_servers.iMCP.enabled=false"]  # see CLAUDE_SETTINGS
    elif lead:
        # No system prompt flag: the lead reads its brief as the opening prompt.
        opening = brief + "\n\nThese are your standing instructions for this session. Reply only: ready."
        args += ["-i", opening] if name == "Antigravity" else [opening]
    return agent, args


# ---- the setup menu ----------------------------------------------------------

def load_last(options):
    try:
        name = LAST.read_text(encoding="utf-8").strip()
        return next(i for i, o in enumerate(options) if o["name"] == name)
    except Exception:
        return 0


def save_last(option):
    try:
        LAST.parent.mkdir(parents=True, exist_ok=True)
        LAST.write_text(option["name"], encoding="utf-8")
    except OSError:
        pass


def refresh_usage(options):
    """Cached limits at once, and a background read of any that are old."""
    ap.load_usage()
    for o in options:
        name = o["name"]
        if name not in ap.FETCHERS:
            continue
        with ap.USAGE_LOCK:
            fresh = time.time() - ap.USAGE.get(name, {}).get("fetched", 0) < 300
            if fresh or name in ap.PENDING:
                continue
            ap.PENDING.add(name)
        threading.Thread(target=ap._fetch, args=(name,), daemon=True).start()


def choose():
    """The agent selector's panel listing the setups. Returns the chosen row, or None to quit."""
    options = setups()
    refresh_usage(options)
    available = [True] * len(options)
    selected = load_last(options)
    terminal = Terminal()
    writer = ap.Screen(terminal)
    out = sys.stdout
    start = time.monotonic()

    def draw(sweep=None):
        cols, rows = shutil.get_terminal_size((80, 24))
        t = time.monotonic() - start
        lines = ap.render(selected, available, t, cols, rows, sweep, options, title="herdr-ai", hints=HINTS)
        writer.show(ap.centered(lines, cols, rows), cols, rows)

    out.write(f"{ap.ESC}[?1049h{ap.ESC}[?25l{ap.ESC}[?7l")  # alt screen, hide cursor, no autowrap
    out.flush()
    try:
        terminal.__enter__()
        draw()
        frame_at = time.monotonic()
        while True:
            frame_at += ap.FRAME_TIME
            wait = frame_at - time.monotonic()
            if wait < 0:
                frame_at, wait = time.monotonic(), 0
            if not terminal.wait(wait):
                draw()
                continue
            key = terminal.read_key()
            if key in ("up", "k"):
                selected = (selected - 1) % len(options)
            elif key in ("down", "j", "\t"):
                selected = (selected + 1) % len(options)
            elif key.isdigit() and 1 <= int(key) <= len(options):
                selected = int(key) - 1
            elif key in ("\r", "\n"):
                launch = time.monotonic()
                while (elapsed := time.monotonic() - launch) < 0.21:
                    draw(sweep=min(1.0, (elapsed / 0.18) ** 1.6))
                    time.sleep(1 / 120)
                draw(sweep=1.0)
                save_last(options[selected])
                return options[selected]
            elif key in ("q", "esc", "\x03", "\x04"):
                return None
            draw()
    finally:
        if hasattr(terminal, "old") or hasattr(terminal, "modes"):
            terminal.__exit__()
        out.write(f"{ap.ESC}[?7h{ap.ESC}[?25h{ap.ESC}[?1049l")
        out.flush()
