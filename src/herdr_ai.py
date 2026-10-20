#!/usr/bin/env python3
"""herdr-ai: lay out the current herdr tab as a multi-agent workbench, with
every pane rooted in the directory you ran this from.

It opens a menu like the agent selector's: Standard, or one provider's team.
Standard is the mixed workbench:

  +--------------+--------------+--------------+
  | Antigravity  | Codex        |              |
  |              |              | Claude       |
  +--------------+--------------+              |
  | agent        | Cursor/Kiro  |              |
  | selector     |              +--------------+
  +--------------+--------------+ usage        |
  | terminal                    |              |
  +-----------------------------+--------------+

Star lays the same grid out with StarCLIs: a lead and four crew that know about
each other and hand each other tasks, every one routing its own turns to the
best provider with quota.

A provider's setup follows the "System prompt" setting (m in the menu cycles it,
or herdr-ai --settings): off, individual, or orchestrator-crew. Off and
individual put five independent agents in the grid below, every one on the
model the agent picker launches the provider with, with no lead and no crew. Off gives them no herdr-ai instructions at all. Individual,
the default, has them share context through a notes file for the tab and read
each other's panes (see team.peer_brief). Orchestrator-crew is the lead and crew:

A provider's team is a lead agent on Sol or Opus beside four crew agents on a
cheaper model (the "team" entry in agents.json, which herdr-ai's → edits locally). The lead is the provider's own
strongest model, except that an Antigravity team, whose Gemini Flash should not
orchestrate, is led by Codex, Claude, Cursor or Kiro, whichever has quota first.
Other teams put Gemini Flash in crew4, on Antigravity's separate quota, for
trivial tasks. The lead is told to delegate through herdr (see team.py):

  +--------------+--------------+--------------+
  | crew         | crew         |              |
  |              |              | lead         |
  +--------------+--------------+              |
  | crew         | crew         |              |
  |              |              |              |
  +--------------+--------------+--------------+
  | terminal                    | usage        |
  +-----------------------------+--------------+

The pane you run it in becomes the terminal. Run it again in a tab it already
laid out and it refreshes instead: idle agents resume their exact conversations,
and the selector and usage panel reload, with no new panes or tabs. Working
agents and panes whose session cannot be safely resumed stay running. Outside
herdr it opens a new workspace and attaches.

agent-route reroutes every agent on the current tab, including ones herdr-ai
did not start, to the directory you run it from.

herdr-reset closes every other pane on the current tab, leaving the terminal you
run it from, with its history, so herdr-ai can lay the tab out again.

Agents launch through this checkout's picker (--picker-launch), so models,
effort and permission flags follow your picker defaults. With "Hand off at 90%"
on in --settings (off by default), lead and solo agents are told to check their
usage (herdr-ai --usage) and, at 90% of a limit with the goal unfinished, to
hand the goal off (herdr-ai --handoff): herdr-ai restarts the pane
on the next provider with quota, Codex, then Claude, then Cursor or Kiro
(whichever resets sooner), and starts it with /goal. A team lead on a provider
its crew share hands off the whole team.
The middle-bottom pane chooses Cursor or Kiro by remaining quota and reset date.

Usage: herdr-ai [--setup NAME] [--mode off|individual|orchestrator] [label]
                              (label defaults to the directory name; --setup
                              skips the menu: standard, star, claude, codex, ...;
                              --mode overrides the System prompt setting for this layout)
       herdr-ai --settings    change crew visibility, handoff and usage bars without rerouting
       herdr-ai --open-settings    open those settings in a Herdr popup
       herdr-ai --usage       every provider's usage, and who a handoff would pick
       herdr-ai --handoff GOAL NOTES
                              from an agent's pane: once its turn ends, restart
                              the pane (or its team) on the next provider with
                              quota and start it with /goal GOAL, pointing to NOTES
       agent-route
       herdr-reset
Without a terminal to draw the menu in, herdr-ai lays out Standard.
Env:   HERDR_AI_DRY=1         build the layout with plain shells only
       HERDR_BIN_PATH         herdr executable to use
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from platform_runtime import state_dir
import herdr_sidebar
import session_refresh

ENTRY = Path(__file__).resolve().parent / "agent_picker.py"

# pane -> picker arguments; panes not listed stay plain shells. --picker-return
# reopens the selector when an agent quits, so /quit switches providers in place.
# --picker-solo gives each agent herdr-ai's usage and handoff rules.
PANES = {
    "agy": ["--picker-return", "--picker-launch", "Antigravity", "--picker-solo"],
    "codex": ["--picker-return", "--picker-launch", "Codex", "--picker-solo"],
    "claude": ["--picker-return", "--picker-launch", "Claude", "--picker-solo"],
    "cursor": ["--picker-return", "--picker-launch-priority", "--picker-solo"],  # keep the role for saved-layout compatibility
    "picker": ["--picker-return"],
    "usage": ["--picker-usage"],
}
# A team's panes: the lead, four crew, usage for that provider, and the terminal.
CREW = ("crew1", "crew2", "crew3", "crew4")
PEERS = ("lead", *CREW)   # an individual layout keeps the team's roles, so refresh and handoff work alike
OFF, INDIVIDUAL, ORCHESTRATOR = "off", "individual", "orchestrator"   # the system prompt modes (team.MODE)
SEPARATE = (OFF, INDIVIDUAL)   # modes whose five agents work alone, with no lead
STAR = "Star"   # the Star mesh setup: StarCLIs in the lead and crew panes
# Explicit directory reroutes keep usage running. A context-preserving refresh
# reloads it too, so picker and usage fixes can be checked from the terminal.
KEPT = "usage"


def find_herdr():
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")


def picker_command(args):
    """The line typed into a pane's shell to run the picker with args."""
    return shlex.join([sys.executable, str(ENTRY), *args])


def cd_command(path):
    return "cd " + shlex.quote(path)


def route_directory_command(path):
    # A killed TUI can leave mouse reporting, keyboard protocols, and raw tty
    # flags behind. Reset before the replacement agent starts negotiating them.
    return "stty sane && printf '\\033c' && " + cd_command(path)


def state_file():
    return state_dir() / "herdr-ai-tabs.json"


def load_state():
    try:
        state = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # Drop entries a hand edit or an older version left without the expected shape.
    if not isinstance(state, dict):
        return {}
    return {key: entry for key, entry in state.items()
            if isinstance(entry, dict) and isinstance(entry.get("panes"), dict)}


def save_state(state):
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def note_star_panes(lead, crew, terminals, usage=None, create=True):
    """Remember StarCLIs opened with /crew or /panel so herdr-ai refresh resumes them.

    crew maps role names (crew1, ...) to pane ids. terminals maps pane ids to herdr
    terminal ids. Returns None when the tab's Star layout now includes them, or a
    short reason when this tab belongs to another setup and was left alone.
    create is false for a usage pane, which only attaches to a Star layout that
    already exists."""
    if not inside_herdr():
        return "outside herdr"
    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    entry = state.get(key)
    panes = dict((entry or {}).get("panes") or {})
    if entry and entry.get("team") not in (None, STAR):
        return f"this tab is laid out as {entry['team']}"
    if entry and entry.get("team") is None and any(role in panes for role in ("picker", "claude", "codex")):
        return "this tab is laid out as Standard"
    if entry is None and not create:
        return None
    if panes.get("lead") not in (None, lead):
        return "this tab already has a lead pane"
    panes["lead"] = lead
    for role, pane in crew.items():
        if pane and panes.get(role) in (None, pane):
            panes[role] = pane
    if usage and panes.get("usage") in (None, usage):
        panes["usage"] = usage
    known = dict((entry or {}).get("terminals") or {})
    for pane in panes.values():
        if terminals.get(pane):
            known[pane] = terminals[pane]
    if not known.get(panes.get("lead")):
        # A recorded lead with no terminal id makes the next refresh think the
        # layout's lead is gone and refuse to touch the tab.
        return "herdr did not report the lead pane's terminal, so refresh was left unchanged"
    state[key] = {"panes": panes, "auto_label": True if entry is None else entry.get("auto_label", True),
                  "team": STAR, "lead": STAR, "gemini": False, "terminals": known}
    save_state(state)
    sidebar_update(lambda: herdr_sidebar.mark_roles(
        {role: pane for role, pane in panes.items() if role == "lead" or str(role).startswith("crew")}))
    return None


def tab_key(tab):
    # Pane and tab ids are per server, so key them by the session socket too.
    return (os.environ.get("HERDR_SOCKET_PATH") or "default") + "|" + tab


class Herdr:
    def __init__(self, binary, name):
        self.binary, self.name = binary, name

    def __call__(self, *args):
        out = subprocess.run([self.binary, *args], capture_output=True, text=True, encoding="utf-8")
        if out.returncode:
            sys.exit(f"{self.name}: herdr {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()}")
        return json.loads(out.stdout or "{}").get("result", {})

    def tab_panes(self, workspace, tab):
        panes = self("pane", "list", "--workspace", workspace)["panes"]
        return {p["pane_id"]: p for p in panes if p["tab_id"] == tab}

    def process_info(self, pane):
        return self("pane", "process-info", "--pane", pane).get("process_info", {})


def follow_directory(herdr, entry, label):
    """Rename the tab to label unless someone renamed it in herdr.

    Herdr numbers new tabs, so a number or the label herdr-ai last set
    means the name is still ours to change.
    """
    tab = os.environ["HERDR_TAB_ID"]
    current = str(herdr("tab", "get", tab).get("tab", {}).get("label", ""))
    if current.isdigit() or current == (entry or {}).get("label"):
        herdr("tab", "rename", tab, label)
        if entry is not None:
            entry["label"] = label


def busy(info):
    """Foreground process group of a pane running something besides its shell."""
    pgid, shell = info.get("foreground_process_group_id"), info.get("shell_pid")
    return pgid if pgid and shell and pgid != shell else None


def relaunch_line(info):
    """The command line of the pane's foreground job, to start it again."""
    pgid = busy(info)
    leader = next((p for p in info.get("foreground_processes", []) if p.get("pid") == pgid), None)
    if not leader:
        return None
    if leader.get("argv"):
        return shlex.join(leader["argv"])
    return leader.get("cmdline")


def usage_running(info):
    """Whether the pane's foreground job is the usage panel, or something
    besides the picker that a reroute should not interrupt."""
    pgid = busy(info)
    if not pgid:
        return False
    line = relaunch_line(info) or ""
    return ENTRY.name not in line or "--picker-usage" in line


GEMINI = "Antigravity"


def star_member_args(name, role):
    """Picker arguments for one StarCLI in the mesh. Quitting reopens the selector."""
    return ["--picker-return", "--picker-launch", STAR, "--", "--name", name, "--role", role]


def layout_commands(team, panes, lead=None, gemini=False, mode=None, providers=None):
    """role -> picker arguments for a layout's panes. team is the crew's provider, or
    None for Standard; lead is the lead's provider (team by default); gemini puts
    Antigravity's Gemini Flash in crew4. mode INDIVIDUAL runs every agent pane as a
    peer of team, or of providers[role] for a pane that handed off; mode OFF does the
    same with no instructions."""
    if not team:
        return PANES
    if mode in SEPARATE and team != STAR:
        providers = providers or {}
        usage = ["--picker-usage-pane", panes["usage"]] if "usage" in panes else []
        peers = ",".join(panes[r] for r in PEERS if r in panes)
        shown = list(dict.fromkeys([team, *(providers.get(r, team) for r in PEERS)]))
        commands = {"usage": ["--picker-usage", "--picker-only", ",".join(shown)]}
        for role in PEERS:
            if role in panes:
                # Quitting a peer reopens the selector, as in a Standard pane.
                launch = ["--picker-return", "--picker-launch", providers.get(role, team)]
                commands[role] = launch + (["--picker-plain"] if mode == OFF else
                                           ["--picker-peer", panes[role], "--picker-peers", peers] + usage)
        return commands
    if team == STAR:
        # Star CLIs that know each other (star_mesh): the lead delegates to the crew,
        # and every one of them routes its own turns across the providers. Quitting
        # one reopens the selector, as in a Standard pane.
        commands = {"lead": star_member_args("lead", "lead"), "usage": ["--picker-usage"]}
        for role in CREW:
            commands[role] = star_member_args(role, "crew")
        return commands
    lead = lead or team
    usage = ["--picker-usage-pane", panes["usage"]] if "usage" in panes else []
    lead_args = ["--picker-launch", lead, "--picker-lead", ",".join(panes[r] for r in CREW)]
    if lead != team:
        lead_args += ["--picker-crew-of", team]
    if gemini:
        lead_args += ["--picker-gemini", panes["crew4"]]
    shown = list(dict.fromkeys([lead, team] + ([GEMINI] if gemini else [])))
    commands = {"lead": lead_args + usage,
                "usage": ["--picker-usage", "--picker-only", ",".join(shown)]}
    for role in CREW:
        crew = GEMINI if gemini and role == "crew4" else team
        commands[role] = ["--picker-launch", crew, "--picker-crew", panes["lead"]] + usage
    return commands


def team_plan(team, mode=None):
    """{"lead": provider, "gemini": bool} for a new team of team's crew: who leads it,
    and whether Gemini Flash takes crew4. Antigravity crews are led by the best
    orchestrator with quota; other providers lead their own crew. With the system
    prompt off or individual (the setting, unless mode says otherwise) there is no
    lead or Gemini crew: {"lead": team, "gemini": False, "mode": "off"|"individual"}."""
    if not team:
        return {}
    if team == STAR:
        return {"lead": STAR, "gemini": False}
    import team as teams
    mode = mode or teams.mode()
    if mode in SEPARATE:
        return {"lead": team, "gemini": False, "mode": mode}
    import agent_picker
    import handoff
    gemini_ok = any(a["name"] == GEMINI and agent_picker.installed(a) for a in agent_picker.AGENTS)
    if team == GEMINI:
        return {"lead": handoff.orchestrator() or team, "gemini": False}
    return {"lead": team, "gemini": gemini_ok}


def codex_problem(team, lead=None):
    """Codex's own error when its config.toml won't load, else None. A bad value
    there (say an MCP approval_mode this Codex build doesn't know) makes every
    Codex pane exit at startup, so check once before laying out a team of them.
    `codex mcp list` reads the config without starting a session or MCP servers."""
    if team and "Codex" not in (team, lead):
        return None
    import agent_picker
    from platform_runtime import resolve_argv
    codex = next((a for a in agent_picker.AGENTS if a["name"] == "Codex"), None)
    argv = resolve_argv(codex) if codex else None
    if not argv:
        return None
    try:
        out = subprocess.run([argv[0], "mcp", "list"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None   # can't tell; let the panes show whatever happens
    if out.returncode == 0:
        return None
    lines = [l.rstrip() for l in (out.stderr or out.stdout).splitlines() if l.strip()]
    return "\n  ".join(lines[:6]) or f"codex exited with {out.returncode}"


def require_codex(team, lead=None):
    if os.environ.get("HERDR_AI_DRY", "0") == "1":
        return
    problem = codex_problem(team, lead)
    if problem:
        sys.exit(f"herdr-ai: Codex can't load its config, so its panes would exit at startup:\n  {problem}\n"
                 f"Fix ~/.codex/config.toml (or run codex once to see the error), then run herdr-ai again.")


def stop_panes(herdr, panes):
    """Stop whatever runs in each pane and wait for its shell prompt."""
    pending = {pane: busy(herdr.process_info(pane)) for pane in panes}
    pending = {pane: pgid for pane, pgid in pending.items() if pgid}
    if os.getpgrp() in pending.values():
        sys.exit("herdr-ai: refusing to restart the caller's process group")

    def group_alive(pgid):
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS answers EPERM, not ESRCH, while every member is a zombie the
            # pane's shell has yet to reap. Nothing in the group can run again.
            return False

    for sig, wait in ((signal.SIGTERM, 4.0), (signal.SIGKILL, 2.0)):
        for pgid in pending.values():
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + wait
        while pending and time.monotonic() < deadline:
            time.sleep(0.15)
            # The wrapper can exit before its agent and MCP children. They must
            # finish cleanup before a new agent touches the same terminal.
            pending = {p: g for p, g in pending.items() if group_alive(g)}
        if not pending:
            break
    # Reaping can trail the last exit; give each shell a moment to take its pane back.
    deadline = time.monotonic() + 2.0
    while not pending and time.monotonic() < deadline and any(busy(herdr.process_info(p)) for p in panes):
        time.sleep(0.15)
    if pending or any(busy(herdr.process_info(pane)) for pane in panes):
        sys.exit("herdr-ai: panes did not stop; no restart commands were sent")


def refresh_details(pane, details, info):
    """Fill missing Star session refs from its exact live process's receipt."""
    identity = session_refresh.star_identity(info, pane)
    if identity:
        state, native = details.get("agent_status"), details.get("agent_session")
        if native:
            if (not isinstance(native, dict) or native.get("kind") != "id"
                    or session_refresh.provider(native.get("agent")) != "Star"
                    or native.get("value") != identity["agent_session"]["value"]):
                return {**details, "agent_status": "working"}
            details = {**details, "agent_status": identity["agent_status"]}
        else:
            details = {**details, **identity}
        if state in {"working", "blocked"}:
            details["agent_status"] = state
    return details


def refresh_line(pane, details, info, role, commands, cwd):
    """A safe resume command, or None to retain the live pane and its context.

    A pane moving to another directory that cannot take its conversation along
    starts fresh there; its conversation stays resumable from the old directory."""
    name = session_refresh.provider(details.get("agent"))
    if name is None:
        matches = [name for name in session_refresh.RESUME if session_refresh.native_process(info, name)]
        name = matches[0] if len(matches) == 1 else None
    process = session_refresh.native_process(info, name) if name else None
    line = relaunch_line(info) or ""
    leader = next((p for p in info.get("foreground_processes", []) if p.get("pid") == busy(info)), {})
    args = leader.get("argv") or []
    selector = str(ENTRY) in args and not any(a.startswith(("--picker-launch", "--picker-relaunch")) for a in args)
    if selector and not process:
        # /quit leaves the selector in the former agent slot. Reload that
        # selector, rather than launching the slot's original provider blank.
        return line
    if details.get("agent_status") in {"working", "blocked"}:
        print(f"herdr-ai: kept {role} ({pane}) running: {details['agent_status']}")
        return None
    session = details.get("agent_session") or {}
    if not process:
        if not details.get("agent") and not session and not busy(info):
            return picker_command(commands[role])
        reason = "live agent command unavailable"
    elif (session.get("kind") != "id" or session_refresh.provider(session.get("agent")) != name
          or not session.get("value")):
        reason = "exact conversation ID unavailable"
    elif session_refresh.stored(name, session["value"], cwd) is False:
        reason = "no saved conversation for this directory"
    else:
        try:
            argv = session_refresh.resume_argv(name, process["argv"], session["value"])
            returning = "--picker-return" in commands[role] or "--picker-return" in line
            args = (["--picker-return"] if returning else []) + ["--picker-relaunch", session_refresh.encode(argv)]
            return picker_command(args)
        except ValueError as exc:
            reason = str(exc)
    if details.get("cwd") and os.path.realpath(details["cwd"]) != os.path.realpath(cwd):
        print(f"herdr-ai: started {role} ({pane}) fresh: {reason}")
        return picker_command(commands[role])
    print(f"herdr-ai: kept {role} ({pane}) unchanged: {reason}")
    return None


def route(herdr, cwd, recorded, everything, commands=PANES, preserve_context=False):
    """Restart agents on the caller's tab in cwd. recorded maps herdr-ai roles to
    pane ids, and commands maps those roles to picker arguments; everything also
    takes any other pane herdr sees an agent in."""
    if preserve_context and os.environ.get("HERDR_AI_DRY", "0") == "1":
        print("herdr-ai: dry refresh keeps existing panes running")
        return []
    me = os.environ.get("HERDR_PANE_ID")
    live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
    by_pane = {pane: role for role, pane in recorded.items()}
    plan = {}
    checks = {}

    def remember(pane, details, process):
        checks[pane] = (details.get("terminal_id"), details.get("agent_session"),
                        process.get("foreground_process_group_id"),
                        sorted(p.get("pid", 0) for p in process.get("foreground_processes", [])))

    for pane, info in live.items():
        if pane == me:
            continue
        role = by_pane.get(pane)
        if role == KEPT:
            # Left running, unless it was quit or swapped for the selector.
            process = herdr.process_info(pane)
            if role not in commands or (usage_running(process) and
                    (not preserve_context or "--picker-usage" not in (relaunch_line(process) or ""))):
                continue
            plan[pane] = (role, picker_command(commands[role]))
            remember(pane, info, process)
            continue
        agent = info.get("agent")
        if role in commands:
            if preserve_context:
                process = herdr.process_info(pane)
                info = refresh_details(pane, info, process)
                line = refresh_line(pane, info, process, role, commands, cwd)
                if line:
                    plan[pane] = (role, line)
                    remember(pane, info, process)
                continue
            # A selector that already launched an agent keeps that agent, as does
            # a pane switched to another provider through --picker-return.
            # herdr does not recognize every provider, so a switched pane is
            # known by its command line too.
            line = relaunch_line(herdr.process_info(pane))
            switched = line and "--picker-return" in line and "--picker-launch" in line
            if not (switched or (role == "picker" and agent)):
                line = None
            plan[pane] = (role, line or picker_command(commands[role]))
        elif everything and agent:
            line = relaunch_line(herdr.process_info(pane))
            if line:
                plan[pane] = (agent, line)
    if preserve_context and checks:
        # A prompt, /new, provider switch, or pane replacement after planning
        # must not result in stopping a different job or resuming its old ID.
        current = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
        for pane, expected in checks.copy().items():
            details = current.get(pane, {})
            process = herdr.process_info(pane) if details else {}
            details = refresh_details(pane, details, process)
            remember(pane, details, process)
            if checks[pane] != expected or details.get("agent_status") in {"working", "blocked"}:
                role, _ = plan.pop(pane)
                print(f"herdr-ai: kept {role} ({pane}) unchanged: pane changed during refresh")
    if not plan:
        return []
    stop_panes(herdr, list(plan))
    if os.environ.get("HERDR_AI_DRY", "0") == "1":
        plan = {pane: (name, None) for pane, (name, _) in plan.items()}
    for pane, (_, line) in plan.items():
        clear_prompt(herdr, pane)
        type_line(herdr, pane, route_directory_command(cwd))
        if line:
            type_line(herdr, pane, line)
    return [name for name, _ in plan.values()]


def clear_prompt(herdr, pane):
    """Drop whatever a stopped agent left on the shell's input line: late replies
    to its terminal queries, and mouse reports until the reset turns them off.
    Ctrl+U erases the line both in the line editor and in a tty left in cooked mode."""
    herdr("pane", "send-text", pane, "\x03\x15")


def type_line(herdr, pane, line):
    """Type line at the pane's shell and submit it. The control bytes go out
    as raw text, not as send-keys: a killed agent can leave the kitty keyboard
    protocol on in herdr's copy of the pane, and herdr then encodes Ctrl+C,
    Ctrl+U and Enter as CSI u sequences the shell prints instead of acting on.
    The reset in route_directory_command turns the protocol off again. A
    newline submits even when the tty no longer translates CR."""
    herdr("pane", "send-text", pane, "\x15" + line + "\n")


def build(herdr, root, cwd, team=None, plan=None):
    """Split root into the workbench, or into team's layout (plan from team_plan).
    root ends up as the terminal along the bottom."""
    def split(pane, direction, ratio):
        return herdr("pane", "split", pane, "--direction", direction, "--ratio", str(ratio),
                     "--cwd", cwd, "--no-focus")["pane"]["pane_id"]

    if team:
        return build_team(herdr, root, split, team, plan or {})
    panes = {"terminal": root}
    panes["claude"] = split(root, "right", 0.72)            # right column: Claude over usage
    panes["usage"] = split(panes["claude"], "down", 0.54)
    panes["agy"] = split(root, "down", 0.80)                # terminal across the bottom of the left 2/3
    # Swap so the caller's shell, which keeps focus, becomes the terminal.
    herdr("pane", "swap", "--source-pane", root, "--target-pane", panes["agy"])
    panes["codex"] = split(panes["agy"], "right", 0.49)
    panes["picker"] = split(panes["agy"], "down", 0.54)
    panes["cursor"] = split(panes["codex"], "down", 0.54)

    launch_panes(herdr, panes, PANES)
    return panes


# Teams whose usage pane shows Cursor's card, which has a third bar. Grok Bot's
# quota is Cursor's, so its team shows the same card.
TALL_USAGE = ("Cursor", "Grok Bot")


def one_row(herdr, pane):
    """One terminal row of pane's height as a split ratio; 0.04 when herdr does
    not report the height."""
    try:
        rows = herdr("pane", "get", pane)["pane"]["scroll"]["viewport_rows"]
    except (KeyError, TypeError):
        rows = 0
    return 1 / rows if isinstance(rows, int) and rows >= 10 else 0.04


def build_team(herdr, root, split, team, plan):
    """The lead on the right over a short usage pane (one provider fits in a few
    rows), the crew in a 2x2 grid on the left, and the terminal under them."""
    panes = {"terminal": root}
    panes["lead"] = split(root, "right", 0.72)
    ratio = 0.82
    if team in TALL_USAGE:
        # Cursor's card has three bars, so its usage pane and the terminal beside
        # it start one row higher to keep the bars and the terminal lined up.
        ratio -= one_row(herdr, root)
    panes["usage"] = split(panes["lead"], "down", round(ratio, 4))
    panes["crew1"] = split(root, "down", round(ratio, 4))
    # Swap so the caller's shell, which keeps focus, becomes the terminal.
    herdr("pane", "swap", "--source-pane", root, "--target-pane", panes["crew1"])
    panes["crew2"] = split(panes["crew1"], "right", 0.49)
    panes["crew3"] = split(panes["crew1"], "down", 0.54)
    panes["crew4"] = split(panes["crew2"], "down", 0.54)
    launch_panes(herdr, panes, layout_commands(team, panes, plan.get("lead"), plan.get("gemini", False),
                                               plan.get("mode")))
    return panes


def launch_panes(herdr, panes, commands):
    if os.environ.get("HERDR_AI_DRY", "0") != "1":
        sidebar_update(lambda: herdr_sidebar.mark_roles(panes))
        for role, args in commands.items():
            herdr("pane", "run", panes[role], picker_command(args))


def pick_setup(name=None):
    """The provider whose team to lay out, None for Standard, or False to quit.
    name is a --setup value; otherwise the menu decides when there is a terminal for it."""
    if name is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    import team   # loads the agent profiles; only needed when choosing
    if name is not None:
        return team.find(name)
    chosen = team.choose()
    return False if chosen is None else None if chosen is team.STANDARD else chosen["name"]


def require_herdr(name):
    found = find_herdr()
    if not found:
        sys.exit(f"{name}: herdr not found. Install it with:\n  curl -fsSL https://herdr.dev/install.sh | sh")
    return found


def inside_herdr():
    return (os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_WORKSPACE_ID")
            and os.environ.get("HERDR_TAB_ID") and os.environ.get("HERDR_PANE_ID"))


def recorded_panes(herdr, state, key):
    """The tab's herdr-ai panes that still exist, or {} if its layout is gone."""
    entry = state.get(key)
    if not entry:
        return {}
    live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
    panes = {role: pane for role, pane in entry["panes"].items()
             if pane in live and live[pane].get("terminal_id") == (entry.get("terminals") or {}).get(pane)}
    return panes if any(role not in ("terminal", KEPT) for role in panes) else {}


def close_leftovers(herdr, entry):
    """Close a previous layout's usage pane before laying the tab out again;
    it outlives the agents, and would otherwise sit beside the new one."""
    if not entry:
        return
    live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
    pane = entry["panes"].get(KEPT)
    if (pane and pane != os.environ.get("HERDR_PANE_ID") and pane in live
            and live[pane].get("terminal_id") == (entry.get("terminals") or {}).get(pane)):
        herdr("pane", "close", pane)


def layout_problem(entry, recorded, setup, mode=None):
    """Why a laid-out tab cannot simply be rerouted, or None."""
    team = entry.get("team")
    if mode and team and team != STAR and (entry.get("mode") or ORCHESTRATOR) != mode:
        return f"this tab's {team} setup is {entry.get('mode') or ORCHESTRATOR}, not {mode}"
    if setup is not None:
        import team as teams
        try:
            wanted = teams.find(setup)
        except ValueError as exc:
            sys.exit(f"herdr-ai: {exc}")
        if wanted != team:
            return f"this tab is laid out as {team or 'Standard'}, not {wanted or 'Standard'}"
    if team and "lead" not in recorded:
        return f"this tab's {team} lead pane is gone"
    return None


def entry_commands(entry, recorded=None):
    """The picker arguments for each role of a recorded layout. A team whose lead
    is gone keeps no commands, so its crew restart as they were."""
    if not entry:
        return PANES
    if entry.get("team") and recorded is not None and "lead" not in recorded:
        return {}
    return layout_commands(entry.get("team"), entry["panes"], entry.get("lead"), entry.get("gemini", False),
                           entry.get("mode"), entry.get("providers"))


def report(name, routed, cwd):
    if routed:
        print(f"{name}: restarted {', '.join(routed)} in {cwd}")
    else:
        print(f"{name}: no agents to route on this tab")


# ---- usage and handoff -------------------------------------------------------

def pane_provider(herdr, pane, info=None):
    """The provider running in pane: herdr's agent, else a single native CLI in its foreground."""
    info = info if info is not None else {}
    name = session_refresh.provider(info.get("agent"))
    if name:
        return name
    process = herdr.process_info(pane)
    found = [n for n in session_refresh.RESUME if session_refresh.native_process(process, n)]
    return found[0] if len(found) == 1 else None


def caller_provider():
    """The provider in the pane this command runs from, or None outside herdr."""
    if not inside_herdr():
        return None
    binary = find_herdr()
    if not binary:
        return None
    herdr = Herdr(binary, "herdr-ai")
    try:
        live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
        pane = os.environ["HERDR_PANE_ID"]
        return pane_provider(herdr, pane, live.get(pane, {}))
    except SystemExit:
        return None


def usage_main(argv):
    if argv:
        sys.exit("herdr-ai: --usage takes no arguments")
    import handoff
    print(handoff.report(caller_provider()))
    return 0


def handoff_goal(goal, caller, notes):
    goal = " ".join(goal.split())
    return (f"/goal {goal} (Handed off from {caller}, which reached its usage limit. "
            f"Read {notes} first, then continue from there.)")


def handoff_main(argv, herdr=None, spawn=None):
    """herdr-ai --handoff GOAL NOTES [--from NAME]: plan the takeover, then leave the
    switch to a detached runner that waits for the calling agent's turn to end."""
    extra, argv = {}, list(argv)
    if "--from" in argv:
        i = argv.index("--from")
        if i + 1 >= len(argv):
            sys.exit("herdr-ai: --from needs a provider name")
        extra["from"] = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) != 2 or not argv[0].strip():
        sys.exit('herdr-ai: usage: herdr-ai --handoff "<goal in one line>" <notes file> [--from NAME]')
    goal, notes = argv[0], os.path.abspath(argv[1])
    if not os.path.isfile(notes):
        sys.exit(f"herdr-ai: handoff notes not found: {notes}. Write them first.")
    if not inside_herdr():
        sys.exit("herdr-ai: --handoff runs from an agent's pane inside herdr")
    import handoff
    if not handoff.enabled():
        sys.exit("herdr-ai: handoff is off. Turn on \"Hand off at 90%\" in herdr-ai --settings to use it.")
    herdr = Herdr(herdr or require_herdr("herdr-ai"), "herdr-ai")
    me = os.environ["HERDR_PANE_ID"]
    live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
    info = live.get(me, {})
    caller = session_refresh.provider(extra.get("from")) or pane_provider(herdr, me, info)
    if not caller:
        sys.exit("herdr-ai: cannot tell which provider runs in this pane; add --from NAME")
    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    entry = state.get(key) or {}
    role = next((r for r, p in entry.get("panes", {}).items() if p == me), None)
    individual = entry.get("mode") in SEPARATE and role in PEERS
    if role and role.startswith("crew") and not individual:
        sys.exit("herdr-ai: crew do not hand off; tell your lead you are at the usage limit")
    if caller == STAR or entry.get("team") == STAR:
        # A Star mesh is not one provider's quota. The next turn is routed to
        # whoever has room, in the same panes, so replacing the mesh would throw
        # away the layout herdr-ai --setup star just built.
        sys.exit("herdr-ai: a Star mesh stays in these panes. StarCLI routes the next turn "
                 "to a provider with quota, so there is no pane to hand off.")
    target, why = handoff.target(caller)
    if not target:
        print(f"herdr-ai: no handoff: {why}. Tell the user, and stop.")
        return 1
    cwd = info.get("cwd") or os.getcwd()
    update = {}
    if individual:
        # Peers each use their own provider's quota: only this pane moves.
        providers = dict(entry.get("providers") or {}, **{role: target})
        panes = entry["panes"]
        commands = layout_commands(entry["team"], panes, mode=entry["mode"], providers=providers)
        lines = {panes[r]: picker_command(commands[r]) for r in (role, "usage") if r in panes}
        update = {"providers": providers}
        moved = "this pane"
    elif entry.get("team") and role == "lead":
        panes, gemini = entry["panes"], entry.get("gemini", False)
        lead = entry.get("lead") or entry["team"]
        if lead == entry["team"]:
            # The crew share the lead's exhausted quota: the whole team moves.
            team, roles = target, ["lead", "usage"] + [r for r in CREW if not (gemini and r == "crew4")]
        else:
            team, roles = entry["team"], ["lead", "usage"]
        commands = layout_commands(team, panes, target, gemini)
        lines = {panes[r]: picker_command(commands[r]) for r in roles if r in panes and r in commands}
        update = {"team": team, "lead": target}
        moved = "the team" if team == target else "the lead"
    else:
        returning = ["--picker-return"] if role or not entry else []
        lines = {me: picker_command(returning + ["--picker-launch", target, "--picker-solo"])}
        moved = "this pane"
    job = {"herdr": herdr.binary, "caller": me, "lead": me, "lines": lines, "cwd": cwd, "key": key,
           "update": update, "goal": handoff_goal(goal, caller, notes),
           "opening": target not in ("Claude", "Codex")}
    log = state_dir() / "herdr-ai-handoff.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    (spawn or spawn_runner)(job, log)
    print(f"herdr-ai: {target} takes over {moved} once your turn ends ({why}); it starts with /goal. "
          f"End your turn now with one line saying {target} takes over. Log: {log}")
    return 0


def spawn_runner(job, log):
    with open(log, "a", encoding="utf-8") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--handoff-run", json.dumps(job)],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True, start_new_session=True)


def handoff_run(job, sleep=time.sleep):
    """The detached half of --handoff: wait out the caller's turn, switch, prompt /goal."""
    herdr = Herdr(job["herdr"], "herdr-ai handoff")
    stamp = lambda: time.strftime("%Y-%m-%d %H:%M:%S")

    def quiet(*args):
        out = subprocess.run([herdr.binary, *args], capture_output=True, text=True, encoding="utf-8")
        return out.returncode == 0

    print(f"{stamp()} handoff from {job['caller']}: waiting for its turn to end", flush=True)
    sleep(2)
    if not quiet("agent", "wait", job["caller"], "--until", "idle", "--until", "done", "--timeout", "1800000"):
        print(f"{stamp()} the caller did not finish its turn within 30 minutes; handoff cancelled", flush=True)
        return 1
    sleep(1.5)
    stop_panes(herdr, list(job["lines"]))
    for pane, line in job["lines"].items():
        clear_prompt(herdr, pane)
        type_line(herdr, pane, route_directory_command(job["cwd"]))
        type_line(herdr, pane, line)
    if job["update"]:
        state = load_state()
        if job["key"] in state:
            state[job["key"]].update(job["update"])
            save_state(state)
            sidebar_update(lambda: herdr_sidebar.sync(state))
    print(f"{stamp()} restarted {', '.join(job['lines'])}", flush=True)
    lead = job["lead"]
    sleep(5)
    if job["opening"]:   # a flagless CLI reads its brief as an opening prompt first
        quiet("agent", "wait", lead, "--until", "working", "--timeout", "30000")
    quiet("agent", "wait", lead, "--until", "idle", "--until", "done", "--timeout", "180000")
    sleep(1)
    if not quiet("agent", "prompt", lead, job["goal"]):
        herdr("pane", "send-text", lead, job["goal"] + "\r")
    print(f"{stamp()} sent: {job['goal']}", flush=True)
    return 0


def main(argv=None, herdr=None, cwd=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip())
        return 0
    if argv[:1] == ["--usage"]:
        return usage_main(argv[1:])
    if argv[:1] == ["--handoff"]:
        return handoff_main(argv[1:], herdr)
    if argv[:1] == ["--handoff-run"]:
        try:
            return handoff_run(json.loads(argv[1]))
        except SystemExit as exc:
            print(f"handoff failed: {exc}", flush=True)
            return 1
    if argv[:1] == ["--sync-sidebar"]:
        herdr_sidebar.sync(load_state(), restore=True)
        import herdr_team_status
        herdr_team_status.ensure_running()
        return 0
    if argv[:1] == ["--watch-sidebar"]:
        import herdr_team_status
        herdr_team_status.run()
        return 0
    if argv[:1] == ["--open-settings"]:
        try:
            herdr_sidebar.request("plugin.pane.open", {
                "plugin_id": "local.herdr-ai", "entrypoint": "settings"})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            sys.exit(f"herdr-ai: {exc}")
        return 0
    if argv[:1] == ["--settings"]:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            sys.exit("herdr-ai: --settings needs an interactive terminal")
        binary = herdr or find_herdr()
        if binary:
            sidebar_update(lambda: herdr_sidebar.install_startup(binary))
            sidebar_update(lambda: herdr_sidebar.sync(load_state()))
        import team
        team.choose(settings_only=True)
        return 0
    setup, mode = None, None
    if "--mode" in argv or any(a.startswith("--mode=") for a in argv):
        i = next(i for i, a in enumerate(argv) if a == "--mode" or a.startswith("--mode="))
        mode = argv[i].split("=", 1)[1] if "=" in argv[i] else (argv[i + 1] if i + 1 < len(argv) else "")
        if mode not in (OFF, INDIVIDUAL, ORCHESTRATOR):
            sys.exit("herdr-ai: --mode takes off, individual or orchestrator")
        del argv[i:i + (1 if "=" in argv[i] else 2)]
    if argv[:1] == ["--setup"] or (argv and argv[0].startswith("--setup=")):
        if argv[0] == "--setup" and len(argv) < 2:
            sys.exit("herdr-ai: --setup needs a name: standard, claude, codex, ...")
        setup, argv = (argv[1], argv[2:]) if argv[0] == "--setup" else (argv[0].split("=", 1)[1], argv[1:])
    if argv and argv[0].startswith("-"):
        sys.exit(f"herdr-ai: unknown option {argv[0]}; see herdr-ai --help")
    if len(argv) > 1:
        sys.exit("herdr-ai: takes one label; quote it if it has spaces")
    herdr = Herdr(herdr or require_herdr("herdr-ai"), "herdr-ai")
    cwd = cwd or os.getcwd()
    auto = not argv
    label = argv[0] if argv else (Path(cwd).name or "home")
    if os.environ.get("HERDR_AI_DRY", "0") != "1":
        sidebar_update(lambda: herdr_sidebar.install_startup(herdr.binary))

    def choose():
        try:
            return pick_setup(setup)
        except ValueError as exc:
            sys.exit(f"herdr-ai: {exc}")

    if not inside_herdr():
        team = choose()
        if team is False:
            return 0
        plan = team_plan(team, mode)
        require_codex(team, plan.get("lead"))
        out = herdr("workspace", "create", "--cwd", cwd, "--label", label, "--focus")
        panes = build(herdr, out["root_pane"]["pane_id"], cwd, team, plan)
        # Outside launches need the same saved identity as tabs built inside Herdr.
        root = out["root_pane"]
        workspace = root.get("workspace_id") or out.get("workspace", {}).get("workspace_id")
        tab = root.get("tab_id") or out.get("tab", {}).get("tab_id")
        if workspace and tab:
            live = herdr.tab_panes(workspace, tab)
            state = load_state()
            key = herdr_sidebar.endpoint() + "|" + tab
            state[key] = {"panes": panes, "auto_label": auto, "team": team, **plan,
                                   "terminals": {p: live.get(p, {}).get("terminal_id") for p in panes.values()}}
            save_state(state)
        sidebar_update(lambda: herdr_sidebar.sync(load_state()))
        return os.execvp(herdr.binary, [herdr.binary])

    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    recorded = recorded_panes(herdr, state, key)
    if recorded:
        entry = state[key]
        problem = layout_problem(entry, recorded, setup, mode)
        if problem:
            sys.exit(f"herdr-ai: {problem}; run herdr-reset, then herdr-ai")
        require_codex(entry.get("team"), entry.get("lead"))
        refreshed = route(herdr, cwd, recorded, everything=False, commands=entry_commands(entry),
                          preserve_context=True)
        print(f"herdr-ai: refreshed {', '.join(refreshed)} in {cwd}; conversations preserved" if refreshed
              else "herdr-ai: existing agents retained; no panes restarted")
        entry["auto_label"] = entry.get("auto_label", True) and auto
    else:
        team = choose()
        if team is False:
            return 0
        plan = team_plan(team, mode)
        require_codex(team, plan.get("lead"))
        close_leftovers(herdr, state.get(key))
        panes = build(herdr, os.environ["HERDR_PANE_ID"], cwd, team, plan)
        live = herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"])
        entry = state[key] = {"panes": panes, "auto_label": auto, "team": team, **plan,
                              "terminals": {p: live.get(p, {}).get("terminal_id") for p in panes.values()}}
    # An explicit label sticks; otherwise the tab follows the directory
    # unless it was renamed in herdr.
    if not auto:
        herdr("tab", "rename", os.environ["HERDR_TAB_ID"], label)
    elif entry["auto_label"]:
        follow_directory(herdr, entry, label)
    save_state(state)
    sidebar_update(lambda: herdr_sidebar.sync(state))
    return 0


def sidebar_update(action):
    """Visibility errors should explain the limitation without interrupting launches."""
    if os.environ.get("HERDR_AI_DRY", "0") == "1":
        return
    try:
        return action()
    except Exception as exc:   # herdr's replies vary by version; never block a layout on them
        print(f"herdr-ai: sidebar update unavailable: {exc}", file=sys.stderr)


def route_main(argv, herdr=None, cwd=None):
    """agent-route: restart every agent on this tab in the current directory."""
    if argv[:1] in (["-h"], ["--help"]):
        print("Usage: agent-route\n\nRestart every agent on the current herdr tab in the current directory.\n"
              "Run it from a shell pane on that tab; the pane you run it from is left alone.")
        return 0
    if argv:
        sys.exit("agent-route: takes no arguments; cd to the target directory first")
    if not inside_herdr():
        sys.exit("agent-route: run this from a pane inside herdr")
    herdr = Herdr(herdr or require_herdr("agent-route"), "agent-route")
    cwd = cwd or os.getcwd()
    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    entry = state.get(key)
    recorded = recorded_panes(herdr, state, key)
    report("agent-route", route(herdr, cwd, recorded, everything=True,
                                commands=entry_commands(entry, recorded)), cwd)
    if entry and entry.get("auto_label", True):
        follow_directory(herdr, entry, Path(cwd).name or "home")
        save_state(state)
    return 0


def reset_main(argv, herdr=None, cwd=None):
    """herdr-reset: close every other pane on this tab, leaving the caller's terminal."""
    if argv[:1] in (["-h"], ["--help"]):
        print("Usage: herdr-reset\n\nClose every other pane on the current herdr tab, leaving only the pane\n"
              "you run it from, with its history and directory, as a fresh herdr-ai starting point.")
        return 0
    if argv:
        sys.exit("herdr-reset: takes no arguments")
    if not inside_herdr():
        sys.exit("herdr-reset: run this from a pane inside herdr")
    herdr = Herdr(herdr or require_herdr("herdr-reset"), "herdr-reset")
    cwd = cwd or os.getcwd()
    me = os.environ["HERDR_PANE_ID"]
    others = [p for p in herdr.tab_panes(os.environ["HERDR_WORKSPACE_ID"], os.environ["HERDR_TAB_ID"]) if p != me]
    for pane in others:
        herdr("pane", "close", pane)
    state, key = load_state(), tab_key(os.environ["HERDR_TAB_ID"])
    entry = state.pop(key, None)
    if entry is not None:
        save_state(state)
    if entry is None or entry.get("auto_label", True):
        follow_directory(herdr, entry, Path(cwd).name or "home")
    print(f"herdr-reset: closed {len(others)} pane{'s' * (len(others) != 1)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
