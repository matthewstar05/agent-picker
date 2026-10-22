"""Single-provider teams for herdr-ai, and the menu herdr-ai opens to pick a setup.

A provider's setup follows the "System prompt" setting (m in the menu cycles it).
Off and individual both open five agents that each work for the user directly,
all on the model and effort the agent picker launches that provider with: the
picker's defaults screen and herdr-ai's (→ on the row) edit the same setting.
Off gives them no herdr-ai instructions at all. Individual, the default, tells
them about each other: they share context through one notes file per tab and can
read each other's panes, but nobody delegates.
Orchestrator-crew is the lead and crew below, with models of its own: → on the
row edits the lead's and the crew's model and effort, saved as the provider's
"team" in the local config, apart from the picker's defaults.

A team is one lead agent beside four crew agents. The lead always runs Sol or
Opus: the provider's strongest model, or for Antigravity, whose Gemini Flash
should not orchestrate, the best other provider with quota (see handoff). The
crew run the team provider's cheaper model, except that crew4 runs Gemini Flash
on Antigravity's separate quota for trivial tasks when Antigravity is installed.
Every member is told to watch its usage; a lead near a limit hands its goal on.
The lead
is told to hand searches, test runs, and well specified edits to the crew
panes through herdr, so its own context and quota stay small. The crew are told
to answer in a short summary the lead can read cheaply. Each provider's pair of
models is the "team" entry of its profile in agents.json, or the local config's.

Instructions reach each CLI the way it supports: Claude appends them to its
system prompt, Codex takes them as developer instructions, and Grok Bot saves
them with its conversation (--append-system-prompt). Kiro, Cursor and
Antigravity have no such flag, so their lead (or Standard pane) gets them as its
opening prompt and their crew get the reply and usage rules inside each task.
"""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import agent_picker as ap
import handoff
import model_catalog
import provider_defaults
import preferences
import herdr_sidebar
from platform_runtime import Terminal

CREW = ("crew1", "crew2", "crew3", "crew4")
REPLY = 15   # lines a crew reply may run, so the lead reads little

SAKURA = [(255, 190, 215), (240, 130, 175), (255, 225, 238)]
STANDARD = {
    "name": "Standard", "color": (240, 130, 175), "pastel": [255, 205, 225], "gradient": SAKURA,
    "model": "every provider", "short": "every provider",
    "aside": ["Claude · Codex · Antigravity", "Cursor/Kiro · selector · usage"],
}
HINTS = [" ↑↓ move · → models · ⏎ lay out · m mode · s settings · esc quit ",
         " → models · ⏎ lay out · m mode · esc quit ", " ⏎ lay out ", ""]
LAST = ap.STATE / "herdr-ai-setup"
SHARED = ap.STATE / "herdr-ai-shared"   # one context notes file per tab for individual peers

OFF, INDIVIDUAL, ORCHESTRATOR = "off", "individual", "orchestrator"
MODE = preferences.Setting(
    "herdr_ai.system_prompt", "Herdr AI", "System prompt", [OFF, INDIVIDUAL, ORCHESTRATOR], INDIVIDUAL,
    hint="Herdr instructions only; provider prompt settings are separate. Off: five plain agents. Individual: sharing context. "
         "Orchestrator-crew: a lead and four crew on herdr-ai's own team models.",
    names={OFF: "off", INDIVIDUAL: "individual", ORCHESTRATOR: "orchestrator-crew"})


def mode():
    preferences.register(MODE)
    value = preferences.get(MODE.key)
    return value if value in MODE.choices else INDIVIDUAL


def next_mode():
    """The mode after the current one, for the menu's m key."""
    choices = MODE.choices
    return choices[(choices.index(mode()) + 1) % len(choices)]


def picker_model(agent):
    """The model an off or individual agent runs: the one the agent picker launches."""
    values = provider_defaults.current(agent) or {}
    return values.get("model") or agent.get("model", "")


def providers():
    """Installed teams, in picker order (Grok Bot sits between Cursor and Kiro)."""
    return [a for a in ap.AGENTS if a.get("team") and ap.installed(a)]


def summary(agent, kind=None):
    """The exact model name: the picker's model with the system prompt off or
    individual, or the orchestrator's model in orchestrator-crew."""
    kind = kind or mode()
    if kind in (INDIVIDUAL, OFF):
        return picker_model(agent)
    if "lead" not in agent["team"]:   # led by another provider's Sol or Opus
        lead = handoff.orchestrator(usage=handoff.read_usage([], refresh=False))
        agent = next((a for a in ap.AGENTS if a["name"] == lead and "lead" in a.get("team", {})), None)
        if agent is None:
            return "sol/opus"
    return agent["team"]["lead"]["model"]


def option(agent):
    """A provider's team as a row in the setup menu."""
    text = summary(agent)
    return dict(agent, model=text, short=text)


STAR = "Star"


def star_setup():
    """The Star mesh setup: a lead StarCLI and four crew StarCLIs, each routing its
    own turns across every provider. Shown with Star's combined usage meter."""
    star = next((a for a in ap.AGENTS if a["name"] == STAR and ap.installed(a)), None)
    return dict(star, model="lead + 4 crew", short="lead + 4 crew") if star else None


def setups():
    """The setup menu's rows. A CLI hidden on the Visibility page is left out,
    as in the agent selector; --setup NAME still lays it out."""
    star = star_setup()
    return ([STANDARD] + [option(a) for a in providers() if ap.is_shown(a)]
            + ([star] if star and ap.is_shown(star) else []))


def find(name):
    """The setup a --setup name means: None for standard, else a provider name."""
    if name.lower() in ("grok", "grokbot", "grok-bot"):
        name = "Grok Bot"
    if name.lower() == "standard":
        return None
    if name.lower() == "star" and star_setup():
        return STAR
    match = next((a["name"] for a in providers() if a["name"].lower() == name.lower()), None)
    if match is None:
        known = ", ".join(["standard"] + (["star"] if star_setup() else []) + [a["name"].lower() for a in providers()])
        raise ValueError(f"unknown setup {name!r}; choose one of: {known}")
    return match


# ---- launching -------------------------------------------------------------

def herdr_command():
    return "herdr" if shutil.which("herdr") else os.environ.get("HERDR_BIN_PATH") or "herdr"


def reply_rule():
    return (f"Finish with at most {REPLY} lines: what you did or found, files changed, open questions. "
            f"Put anything longer in a file under {tempfile.gettempdir()} and give its path.")


def gemini_work(them="it", they="it"):
    return (f"Send {them} only work that needs no taste or judgment: finding files and symbols, grepping, "
            f"running commands and tests and summarizing their output, renames and edits you specify exactly, "
            f"boilerplate, simple lookups. Keep design, debugging, subtle edits and reviews for yourself or "
            f"stronger workers, and check what {they} return{'s' if they == 'it' else ''}.")


def crew_line(agent, crew, crew_agent, gemini):
    """Who the four workers are, and what each kind is good for."""
    if crew_agent["name"] == handoff.GEMINI:
        return (f"All four run Gemini Flash on Antigravity's own quota, separate from yours. Gemini Flash is "
                f"fast but much weaker than you. {gemini_work('them', 'they')} Split bigger jobs into small, exact steps.")
    model = crew_agent["team"]["crew"]["model"]
    mates = [p for p in crew if p != gemini]
    line = (f"{', '.join(mates)} run {crew_agent['name']} {model}, a cheaper model than yours"
            + (" that shares your usage limit." if crew_agent["name"] == agent["name"] else "."))
    if gemini:
        line += (f" {gemini} runs Gemini Flash on Antigravity's separate quota, so work sent there spares "
                 f"yours. It is much weaker. {gemini_work()}")
    return line


def lead_brief(agent, crew, crew_agent=None, gemini=None, usage_pane=None):
    herdr = herdr_command()
    crew_agent = crew_agent or agent
    return f"""You lead a herdr team. Four worker agents wait in the herdr panes {', '.join(crew)} on this tab, in your working directory. {crew_line(agent, crew, crew_agent, gemini)} Your context is the expensive one: delegate whatever would fill it, and keep understanding the request, planning, decisions, integration, and the final check for yourself.

Delegate searching and reading code or docs, running builds and tests and digesting their output, mechanical or well specified edits, research, first drafts, and reviews. Do a step yourself only when it is quicker to do than to describe. Use these panes instead of built-in subagents.

How:
- `{herdr} agent list` shows each pane's status. idle or done means ready.
- `{herdr} agent prompt <pane> "<task>"` sends a task. For a single task add `--wait --timeout 900000`. For parallel work, send to several panes without --wait, then run `{herdr} agent wait <pane> --timeout 900000` for each.
- Workers do not see this conversation. Make each task self-contained: the goal, exact paths, constraints, and what to return. Give parallel workers separate files.
- End every task with: "{reply_rule()} Do not prompt other panes. If `{handoff.command()} --usage` shows your provider at {handoff.NEAR}% or more of a limit, stop at a safe point and say so first."
- Read only the result: `{herdr} agent read <pane> --source recent-unwrapped --lines 40`. Open a results file only when you need its detail.
- Check a worker's changes cheaply (git diff, a test run) before you report them as done.
- If a pane is blocked, read it before acting, and ask the user before approving anything risky. Do not close, restart, or rename the worker panes.
- A worker that reports a usage limit is done for now: move its work to another worker or do it yourself.
- Tell the user what you delegated in a line, not the full task.

{handoff.rules(agent['name'], 'lead', usage_pane)}""".rstrip()


def crew_brief(agent, lead, usage_pane=None):
    return (f"You are a worker on a herdr team. The lead agent in herdr pane {lead} sends you tasks and reads "
            f"only the end of your reply. Do the task as given, within the files and scope it names. Do not prompt "
            f"other panes or start subagents. {reply_rule()} {handoff.rules(agent['name'], 'crew', usage_pane)}")


def solo_brief(agent):
    return handoff.rules(agent["name"], "solo")


def shared_notes(peers):
    """This tab's shared context file, created with a header if missing."""
    tab = os.environ.get("HERDR_TAB_ID") or "-".join(sorted(peers))
    path = SHARED / (re.sub(r"[^A-Za-z0-9_.-]", "_", tab) + ".md")
    try:
        SHARED.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# Shared context for the agents on this herdr tab\n\n", encoding="utf-8")
    except OSError:
        pass
    return path


def peer_brief(agent, me, peers, usage_pane=None):
    herdr = herdr_command()
    others = [p for p in peers if p != me]
    notes = shared_notes(peers)
    return f"""You are one of {len(peers)} independent agents on this herdr tab, all in your working directory. You are pane {me}; the others are {', '.join(others)}. Each takes its own requests from the user and nobody leads, so do your own work fully yourself. Share context so you do not collide or redo each other's work:
- Shared notes: {notes}. Read it before you start a task. Append one line when you start (`- {me}: working on <task>; files <paths>`), and one when you finish or learn something the others need (a decision, a finding, a gotcha, where results are). Keep lines short, append only, never edit or delete another pane's lines.
- Before editing a file another pane has claimed there, check what it is doing: `{herdr} agent list` shows each pane's status, and `{herdr} agent read <pane> --source recent-unwrapped --lines 30` shows its recent output. Look at peers only when it matters to your task.
- Do not prompt, interrupt, close, or restart other panes unless the user asks you to.

{handoff.rules(agent['name'], 'solo', usage_pane)}""".rstrip()


# iMCP drops every client when several connect at once, as a team's five panes do at launch.
CLAUDE_SETTINGS = {"deniedMcpServers": [{"serverName": "iMCP"}]}


def claude_settings():
    """A settings file that keeps iMCP out of a Claude team pane. A file, not inline JSON,
    since a .cmd shim would strip the quotes."""
    path = os.path.join(tempfile.gettempdir(), "herdr-team-claude-settings.json")
    text = json.dumps(CLAUDE_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return path
    except OSError:
        pass
    # Several mesh panes can start Claude at once. Readers must never see a
    # settings file truncated by a concurrent writer.
    fd, temporary = tempfile.mkstemp(prefix="herdr-team-claude-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def options(args, names):
    """Pull --picker-NAME VALUE pairs for names out of args."""
    found, rest, i = {}, [], 0
    while i < len(args):
        if args[i] in names and i + 1 < len(args):
            found[args[i]] = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    return found, rest


def prepare(agent, args):
    """The profile and arguments for a herdr-ai agent, from the first of args:
    --picker-lead PANES [--picker-crew-of NAME] [--picker-gemini PANE] [--picker-usage-pane PANE],
    --picker-crew PANE [--picker-usage-pane PANE], --picker-peer PANE --picker-peers PANES
    [--picker-usage-pane PANE] (an individual peer), --picker-plain (a peer with the
    system prompt off), or --picker-solo (a Standard pane)."""
    if args[:1] == ["--picker-peer"]:
        return prepare_peer(agent, args)
    if args[:1] == ["--picker-plain"]:
        return prepare_plain(agent, args)
    solo = args[:1] == ["--picker-solo"]
    if solo and (agent["name"] == "Star" or not handoff.enabled()):
        # StarCLI routes its own turns, and with handoff off a solo pane has no brief.
        return agent, list(args[1:])
    if not solo and len(args) < 2:
        raise ValueError(f"{args[0]} needs pane ids")
    lead = args[0] == "--picker-lead"
    target, args = (None, list(args[1:])) if solo else (args[1], list(args[2:]))
    extra, args = options(args, ("--picker-crew-of", "--picker-gemini", "--picker-usage-pane"))
    agent = copy.deepcopy(agent)
    if solo:
        brief = solo_brief(agent)
    else:
        config = (agent.get("team") or {}).get("lead" if lead else "crew")
        if not config:
            raise ValueError(f"{agent['name']} has no team {'lead' if lead else 'crew'} in agents.json")
        if provider_defaults.current(agent) is not None:
            provider_defaults.apply(agent, {"model": config["model"],
                                            "effort": config.get("effort", "provider default")})
        agent["detail"] = ("lead · " if lead else "crew · ") + agent.get("detail", "")
        usage_pane = extra.get("--picker-usage-pane")
        if lead:
            crew_of = extra.get("--picker-crew-of")
            crew_agent = next((a for a in ap.AGENTS if a["name"] == crew_of), None) if crew_of else None
            if crew_of and crew_agent is None:
                raise ValueError(f"Unknown provider: {crew_of}")
            brief = lead_brief(agent, target.split(","), crew_agent, extra.get("--picker-gemini"), usage_pane)
        else:
            brief = crew_brief(agent, target, usage_pane)
    return instruct(agent, args, brief, opening=lead or solo, shared=not solo)


def as_picked(agent, label):
    """A copy of agent as the picker launches it, its model and effort included."""
    agent = copy.deepcopy(agent)
    agent["detail"] = label + " · " + agent.get("detail", "")
    return agent


def prepare_peer(agent, args):
    """An individual peer: the picker's model, told who its peers are."""
    if len(args) < 2:
        raise ValueError("--picker-peer needs its pane id")
    me, (extra, args) = args[1], options(list(args[2:]), ("--picker-peers", "--picker-usage-pane"))
    peers = [p for p in (extra.get("--picker-peers") or me).split(",") if p]
    agent = as_picked(agent, "peer")
    brief = peer_brief(agent, me, peers, extra.get("--picker-usage-pane"))
    return instruct(agent, args, brief, opening=True, shared=True)


def prepare_plain(agent, args):
    """A peer with the system prompt off: the picker's model and no instructions.
    Only iMCP stays out, since five panes start at once."""
    agent = as_picked(agent, "plain")
    if agent["name"] == "Claude":
        agent["argv"] += ["--settings", claude_settings()]
    elif agent["name"] == "Codex":
        agent["argv"] += ["-c", "mcp_servers.iMCP.enabled=false"]
    return agent, list(args[1:])


def instruct(agent, args, brief, opening, shared):
    """Give the agent its brief the way its CLI supports. shared: several panes of
    this tab start at once, so keep iMCP out."""
    name = agent["name"]
    if name == "Claude":
        agent["argv"] += ["--append-system-prompt", brief]
        if shared:
            agent["argv"] += ["--settings", claude_settings()]
    elif name == "Codex":
        agent["argv"] += ["-c", "developer_instructions=" + json.dumps(brief)]   # a TOML string
        if shared:
            agent["argv"] += ["-c", "mcp_servers.iMCP.enabled=false"]  # see CLAUDE_SETTINGS
    elif name == "Grok Bot":
        agent["argv"] += ["--append-system-prompt", brief]
    elif opening:
        # No system prompt flag: the agent reads its brief as the opening prompt.
        opening = brief + "\n\nThese are your standing instructions for this session. Reply only: ready."
        args += ["-i", opening] if name == "Antigravity" else [opening]
    return agent, args


# ---- team models ---------------------------------------------------------------

ROLES = ("lead", "crew")


class TeamMenu(provider_defaults.Menu):
    """The orchestrator-crew models of a provider: the picker's defaults screen with a
    Role row that switches between editing the lead and the crew."""
    LABELS = dict(provider_defaults.Menu.LABELS, role="Role")
    TITLE = "{} team models"

    def __init__(self, agent):
        self.agent = agent
        team = agent.get("team") or {}
        self.roles = [r for r in ROLES if r in team]
        self.role = self.roles[0]
        self.originals = {r: {"role": r, "model": team[r]["model"],
                              "effort": team[r].get("effort", "provider default")} for r in self.roles}
        self.edits = copy.deepcopy(self.originals)
        self.model_rows = model_catalog.options(agent, "")
        for r in reversed(self.roles):   # both roles' current models stay choosable
            model = self.edits[r]["model"]
            if not any(row["id"] == model for row in self.model_rows):
                self.model_rows.insert(0, {"id": model, "name": model + " (current)",
                                           "efforts": [] if model.startswith("gemini") else None})
        self.index = 0
        self.error = ""
        self.browsing = False
        self.model_index = 0
        model_catalog.request(agent)

    @property
    def values(self):
        return self.edits[self.role]

    @property
    def original(self):
        return self.originals[self.role]

    @property
    def changed(self):
        return self.edits != self.originals

    def fields(self):
        effort = () if str(self.values["model"]).startswith("gemini") else ("effort",)
        return (("role",) if len(self.roles) > 1 else ()) + ("model",) + effort

    def handle(self, key):
        if not self.browsing and key in ("left", "right", "\r", "\n", " "):
            self.index = min(self.index, len(self.fields()) - 1)
            if self.fields()[self.index] == "role":
                step = -1 if key == "left" else 1
                self.role = self.roles[(self.roles.index(self.role) + step) % len(self.roles)]
                self.error = ""
                return None
        return super().handle(key)

    def review(self):
        selection, delta = [], []
        for r in self.roles:
            new, old = self.edits[r], self.originals[r]
            selection.append((f"{r.title()} model", f"{new['model']} · {new['effort']}"))
            delta += [(f"{r.title()} {self.LABELS[k].lower()}", old[k], new[k])
                      for k in ("model", "effort") if old[k] != new[k]]
        return selection, delta

    def save(self, path=None):
        save_team(self.agent, {r: {k: self.edits[r][k] for k in ("model", "effort")} for r in self.roles}, path)
        self.originals = copy.deepcopy(self.edits)


def save_team(agent, roles, path=None):
    """Store a provider's orchestrator-crew models in the local config, and in agent."""
    import settings
    path = path or settings.config_path()
    for config in roles.values():
        model = config.get("model")
        if not isinstance(model, str) or not model.strip() or any(ord(c) < 32 for c in model):
            raise ValueError(f"{agent['name']}: model must be a nonempty model identifier")
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    saved = data.setdefault("agents", {}).setdefault(agent["name"], {}).setdefault("team", {})
    for role, config in roles.items():
        saved[role] = dict(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".team-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    agent["team"] = dict(agent.get("team") or {}, **{r: dict(c) for r, c in roles.items()})


def models_menu(name):
    """→ on a setup row: the picker's defaults for off and individual, which their
    agents launch with, or the team's own models for orchestrator-crew. None for a
    row with neither (Standard, Star)."""
    agent = next((a for a in providers() if a["name"] == name), None)
    if agent is None or name == "Grok Bot":
        return None
    return TeamMenu(agent) if mode() == ORCHESTRATOR else provider_defaults.Menu(agent)


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
        name = o.get("usage_provider", o["name"])
        if name not in ap.FETCHERS:
            continue
        with ap.USAGE_LOCK:
            fresh = time.time() - ap.USAGE.get(name, {}).get("fetched", 0) < 300
            if fresh or name in ap.PENDING:
                continue
            ap.PENDING.add(name)
        threading.Thread(target=ap._fetch, args=(name,), daemon=True).start()


def choose(settings_only=False):
    """The agent selector's panel listing the setups. Returns the chosen row, or None to quit."""
    options = setups()
    if not settings_only:
        refresh_usage(options)
    available = [True] * len(options)
    selected = load_last(options)
    terminal = Terminal()
    writer = ap.Screen(terminal)
    out = sys.stdout
    start = time.monotonic()
    ap.visibility_rows([a for a in ap.AGENTS if ap.installed(a)])
    menu = preferences.Menu([preferences.register(s) for s in
                             (ap.VISIBILITY, *herdr_sidebar.SETTINGS, ap.BARS, ap.CURSOR_GROK_TRANSIENT, handoff.AUTO, MODE)])
    settings_open = settings_only
    models = None          # a provider's models screen (models_menu), while open
    confirming, choice = False, 0
    notices = []

    def draw(sweep=None):
        cols, rows = shutil.get_terminal_size((80, 24))
        t = time.monotonic() - start
        lines = (ap.render_settings(menu, cols, rows, t, STANDARD) if settings_open else
                 ap.render_back_confirmation(models, choice, cols, rows, t) if confirming else
                 ap.render_defaults(models, cols, rows, t) if models else
                 ap.render(selected, available, t, cols, rows, sweep, options, title="herdr-ai", hints=HINTS))
        writer.show(ap.centered(lines, cols, rows), cols, rows)

    def close_models():
        nonlocal models, confirming
        models, confirming = None, False
        options[:] = setups()   # the summaries name the models
        writer.reset()

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
            if settings_open:
                if key in ("\x03", "\x04"):
                    return None
                before = [preferences.get(s.key) for s in herdr_sidebar.SETTINGS]
                closed = menu.handle(key) == "close"
                if [preferences.get(s.key) for s in herdr_sidebar.SETTINGS] != before:
                    try:
                        import herdr_ai
                        herdr_sidebar.sync(herdr_ai.load_state())
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        notices.append(f"herdr-ai: visibility saved; {exc}")
                if closed:
                    if settings_only:
                        return None
                    settings_open = False
                    options[:] = setups()   # the team mode may have changed the summaries
                    writer.reset()
                draw()
                continue
            if confirming:
                if key in ("left", "right", "up", "down", "\t"):
                    choice = 1 - choice
                elif key in ("s", "S", "y", "Y") or (key in ("\r", "\n", " ") and choice == 0):
                    try:
                        if isinstance(models, TeamMenu):
                            models.save()
                        else:
                            import settings
                            provider_defaults.save(models.agent, models.values, settings.config_path())
                        close_models()
                    except (OSError, ValueError) as exc:
                        models.error, confirming = f"Could not save: {exc}", False
                        writer.reset()
                elif key in ("\r", "\n", " ", "d", "D", "n", "N"):
                    close_models()
                elif key in ("esc", "q"):
                    confirming = False
                    writer.reset()
                elif key in ("\x03", "\x04"):
                    return None
                draw()
                continue
            if models:
                if key in ("\x03", "\x04"):
                    return None
                if models.handle(key) == "save":
                    if models.changed:
                        confirming, choice = True, 0
                        writer.reset()
                    else:
                        close_models()
                draw()
                continue
            if key == "s":
                settings_open = True
                writer.reset()
            elif key == "right":
                models = models_menu(options[selected]["name"])
                if models:
                    writer.reset()
            elif key == "m":
                preferences.put(MODE.key, next_mode())
                options[:] = setups()
            elif key in ("up", "k"):
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
        for notice in dict.fromkeys(notices):
            print(notice, file=sys.stderr)
