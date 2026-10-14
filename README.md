# agent-picker

The source for my `agent` command on macOS. Edit, commit, and push, then run `agent --picker-update` to pull the change. Installed launchers run the checkout directly, so copying or reinstalling the application is unnecessary after ordinary code and profile updates.

The picker preserves the original animated gradients, usage meters, remembered selection, adaptive layout, launch animation, arrow keys, j/k navigation, number keys, Enter, and q/Escape. Extra arguments go to the selected CLI.

## Requirements

- Git and access to this private GitHub repository.
- Python 3.9 or later. No third-party Python packages are required.
- At least one provider CLI installed and signed in on that computer.
- macOS and an interactive terminal. Windows is no longer supported.

The installer installs this picker, not the provider CLIs or their account credentials. Providers missing from PATH are disabled in the menu. Credentials and usage caches remain local.

## Setup

```sh
git clone https://github.com/matthewstar05/agent-picker.git ~/Desktop/Projects/agent-picker
cd ~/Desktop/Projects/agent-picker
python3 scripts/install-macos.py
```

Open a new terminal, then run `agent --picker-doctor` and `agent`. The installer backs up the existing `~/.local/bin/agent-picker` and changed shell profiles, replaces the launcher with a checkout-linked wrapper, does the same for `~/.local/bin/herdr-ai`, and adds a managed `agent` alias to `.zshrc` and `.bashrc`. On this Mac, it replaces the original alias and preserves Kiro's post integration at the bottom of `.zshrc`. Do not move or delete the checkout after installation; rerun the installer if you relocate it or your Python executable.

## Commands

```text
agent                                open the picker
agent "help me understand this repo"  forward this prompt to the chosen CLI
agent --picker-launch Codex           directly launch Codex
agent --picker-priority               explain the Cursor/Kiro workbench choice
agent --picker-doctor                 inspect detected providers and local paths
agent --picker-update                 pull shared updates from GitHub
agent --picker-catalog                every known CLI, its colors, and whether it is installed
agent --picker-help                   show picker help
agent -- --picker-help                pass a management-looking argument to the CLI
usage                                opens straight on the usage panel
```

Inside the command: `u` switches between the agent selector and the usage panel, and `s` opens general settings from either one.

In the agent selector, highlight a provider and press `→` to open its defaults. `↑↓` moves between **Model**, **Effort**, and **Bypass Permissions**. Arrow keys do not change the model. Enter opens a scrollable model list; `↑↓` or `←→` move through it and Enter chooses the model. On Effort, `←→` cycles levels; on Bypass Permissions the same keys toggle On/Off. Space or Enter also changes Effort or Bypass Permissions. Esc returns to the selector. If nothing changed, it returns immediately. If something changed, Esc shows the new model and effort, each changed value, and offers **Save settings** or **Discard changes**, with Save settings selected. Enter confirms the selected action and returns to the selector; Esc returns to editing. After saving or discarding, Esc or q exits the selector directly. The saved defaults remain when you reopen `agent`.

The five original providers support these controls. Cursor effort is part of its model identifier; changing it preserves any fast suffix or existing bracket parameters. Gemini effort is also part of the model id (`gemini-3.8-flash-high`), so those models have no separate Effort control and do not pass `--effort`. Other catalog providers keep their own defaults until their launch controls are supported. Model choices come from bundled provider catalogs, refreshed locally from installed CLIs or their caches in the background. Your current model stays selectable. Cursor effort choices follow the available model variants; Codex and Claude use the effort levels reported in their catalogs. Model access still depends on your account.

Updates are explicit, use `git pull --ff-only`, and refuse a dirty checkout. If the two machines have diverging commits, resolve them with normal Git operations before updating. The command does not commit, stash, reset, push, or update provider CLIs automatically.

## StarCLI

`star` opens StarCLI, one terminal for every installed provider. You type a request; StarCLI picks the provider and model for that turn and runs it on the provider's own CLI headless, with the same bypass-permissions flags as its picker profile, so each turn keeps that CLI's tools (edits, shell, MCP, web search). Shift+Tab switches to plan mode, where providers may read but not change anything. `star -p "prompt"` runs one prompt and exits; `star -c` continues the last conversation; `star --route "prompt"` shows how a prompt would be routed without spending anything.

Routing never calls a model. A prompt is classified by keywords and shape into a kind (debug, design, edit, build, review, write, search, chat...) and a tier (light, standard, heavy), and every provider is scored on quality for that kind against how freely its quota can be spent: what is left in its tightest window divided by the share of the window still to run, plus a bonus for quota that will reset unused within a few hours (Codex with 40% of its 5-hour window left and an hour to go is run close to empty, unless its weekly window is the tight one). Quick questions go to cheap or separate pools (Cursor Auto, Gemini Flash); hard problems go to the strongest model with room. A provider at 90% of a window is skipped, or at 96% when that window resets within the hour. Each provider's models per tier, its quality per kind, how precious its quota is and how much work it buys live in `star.json`.

A conversation stays with one provider while that is sensible, since its session keeps the context cached. When a turn moves elsewhere, the new provider gets a short recap of recent replies and the earlier user requests and constraints. Those requests survive transcript trimming and `/compact`. A failed provider's partial work and error are saved before the next provider continues. Quota failures can rejoin after their recorded reset or a newer meter showing recovered capacity. Authentication and configuration failures stay set aside until an explicit retry through `/auto`, `/use`, or `/goal resume`.

`/goal <objective>` keeps taking turns until the final plain status line reports `GOAL COMPLETE` or `GOAL BLOCKED`. It waits for outstanding crew tasks without spending model turns on polling, then integrates their reports before completing. A mesh lead defaults to the heavy tier; `/tier` can override that choice. Provider pins respect quota and image capabilities. Before each turn, Star adopts newer shared usage readings and refreshes stale meters. Concurrent panes, the picker, and the usage panel preserve each other's newest readings.

`star -p` returns 0 for success, 1 for an unavailable or failed provider, and 130 for interruption. With `--goal`, a blocked goal returns 2 and a paused goal returns 3. The saved objective remains resumable. Explicit `--name` and `--role` flags also work in print mode.

The input box works like Claude Code's: `/` opens a filtered command menu with descriptions and ghost-text completion (Tab or → accepts, the commands you use most rank first), `@` completes file paths, history completes the rest, `!` runs a shell command, `#` adds a line to AGENTS.md, Esc interrupts a turn, and text typed during a turn is kept for the next prompt. `/help` lists every command: `/goal`, `/plan`, `/status`, `/usage`, `/cost`, `/route`, `/why`, `/use`, `/model`, `/tier`, `/auto`, `/models`, `/crew`, `/panel`, `/peers`, `/send`, `/open`, `/new`, `/clear`, `/compact`, `/resume`, `/export`, `/init`, `/review`, `/memory`, `/doctor`, `/cd`. Any other `/name` that a provider's own interface would accept also works: Claude skills, plugin skills (`/design:ux-copy`), and command files in `.claude/commands`, `.cursor/commands` or `~/.codex/prompts` (`/prompts:name`), plus Codex, Cursor, Kiro and `.agents` skills. StarCLI expands the command, or points at the skill's SKILL.md, so whichever provider takes the turn can follow it; a crew task that starts with such a name is expanded the same way.exe.

StarCLIs in the same herdr tab form a mesh. Each registers what it is doing; the agents inside it are told who their peers are and how to hand them work (`star --send NAME "task" --wait` prints the peer's reply), so a lead fans tasks out to its crew in parallel and every crew member routes its task across the providers again. The agents inside are told to check `herdr-ai --usage` and to leave the pane in place; StarCLI routes the next turn. `herdr-ai --setup star` lays out a lead and four crew StarCLIs with a usage panel; inside herdr, `/crew [n]` opens crew panes beside the current StarCLI and `/panel` opens the usage panel. StarCLI reports its idle and working state to herdr, so its panes appear in herdr's Agents sidebar, and the usage panel shows a Star card (an estimated combined limit) that lights up together with the provider StarCLI is driving.

Mesh tasks stay in their recipient's inbox until an answer is saved and delivered. Refreshing an unfinished task asks the provider to inspect earlier effects before continuing. If an answer was already saved, delivery can retry without executing the task again. Peer replies are saved before removal from the inbox. Mesh names have OS locks, so concurrent startup assigns distinct automatic names and refuses a duplicate explicit name within the same mesh.

A saved conversation has one Star writer. A second resume is refused while it is open, and missing or invalid explicit resume IDs fail instead of opening a blank session. A small process guardian stops a headless provider and its child tools when Star crashes or the turn is interrupted; its lock protects the conversation during cleanup. The request is saved before launch, and the native provider session ID is saved as soon as it arrives. Recovery retains that request and asks the provider to inspect earlier effects before repeating actions. Reopening a goal that was running at a crash continues it automatically; deliberately paused and blocked goals stay paused.

Mesh names and inboxes are scoped to their Herdr server and tab. On Herdr versions that omit Star session references, refresh uses a fresh local record tied to the exact Star process, pane, server and saved conversation, then rechecks it before stopping the process. Missing or changed identities leave the pane running. Herdr 0.9.3 shows Star lifecycle state but its native `agent prompt` does not accept custom Star agents; use the mesh send command to delegate to them.

Refresh also keeps panes with typed drafts or an editor that is still starting. `/open` protects the pane while a native provider interface is active. Leave Star plan mode before opening a native interface, which uses its own picker permission profile. Claude, Codex and Kiro with Bypass Permissions Off ask in the Star pane instead: each permission prompt from the profile's permission mode, Codex's approval policy and sandbox, or Kiro's trusted tools shows the action, and `y` allows it, `a` allows that tool for the rest of the session, `n` denies it, and Esc denies it and stops the turn. The pane shows as blocked while it waits, as a native pane does. For Claude, a small MCP server started for that turn carries the question; Codex turns run through `codex app-server` and Kiro turns through `kiro-cli acp`, whose approval requests Star answers. Star never adds a bypass flag. Cursor and Antigravity headless modes cannot ask, so Star routes around a profile of theirs with Bypass Permissions Off. When no provider can take the request, Star blocks it, saves the unexecuted request, and suggests `/open <provider>`. Plan mode remains available for read-only analysis. Star does not turn bypass back on.

Light Claude, Codex, and Kiro turns omit MCP startup only for clearly local work, arithmetic, or greetings. Other requests keep MCP available, including unfamiliar service names. Standard and heavy Kiro turns inherit global, workspace, and agent MCP servers. Stdio servers use a local schema bridge: unsupported root unions are nested under an `input` field, preserving their constraints, and calls are unwrapped before reaching the original server. Source profiles and MCP configs are unchanged. Claude and Codex mesh turns use the native teams' iMCP exclusions. Codex also passes a nearest valid value when `config.toml` carries an `approval_mode` that the installed Codex rejects.

The [StarCLI parity checklist](docs/STARCLI_PARITY.md) tracks remaining capability gaps and distinguishes automated coverage from native runtime verification.

## herdr-ai, agent-route and herdr-reset

Run `herdr-ai` in a herdr pane to lay out that tab as the workbench: Antigravity, Codex, Cursor or Kiro, the agent selector, Claude, the usage panel, and a terminal along the bottom. The pane you ran it from becomes that terminal. Nothing else on the tab is touched, and no new tab is created. Outside herdr it opens a new workspace and attaches. An optional label renames the tab; otherwise the tab is named after the directory, unless you already renamed it in herdr (herdr names new tabs with a number).

In the five Standard agent panes, quitting an agent (`/quit`, `/exit`, or Ctrl+C or Ctrl+D) reopens the agent selector in that pane, so you can switch the pane to another provider. Esc or q in that selector leaves you at the pane's shell. Rerouting keeps the provider you switched to. Teams and plain `agent` runs still exit to the shell. The same goes for an agent started any other way in a tab herdr-ai laid out, such as one typed at a pane's shell or a pane split off: herdr-ai's background watcher notices it quit back to the shell prompt and opens the selector there within a second or two. The tab's bottom terminal stays a shell, as does a pane you left with Esc from the selector.

`herdr-ai` first opens a menu drawn like the agent selector, with each provider's usage beside it. **Standard** (sakura pink) is the workbench above, unchanged. Choosing a provider lays out five agents of that provider instead: one on the right, four in a 2x2 grid on the left, the terminal under them, and a short usage panel for that provider alone under the right-hand agent. The launch animation and key bindings match the selector; Esc quits without touching the tab, and the menu remembers your last choice. `herdr-ai --setup NAME [label]` skips the menu (`standard`, `claude`, `codex`, `antigravity`, `cursor`, `kiro`), and without a terminal to draw in it lays out Standard.

How those five agents work is the **System prompt** setting: **off**, **individual** (the default), or **orchestrator-crew**. Press `m` in the setup menu to cycle it (each row's summary changes to match), change it under `s` or `herdr-ai --settings`, or pass `--mode off|individual|orchestrator` for one layout.

**Off** and **individual** both run five independent agents. Each works for you directly, nobody delegates, and every one runs the model and effort the agent picker launches that provider with. The two stay in sync: change a provider's model in `agent` (→ on its row) or in herdr-ai's setup menu (→ on its row, with off or individual selected) and both use it. Quitting one reopens the selector, as in Standard, and all five appear in the Agents sidebar.

With **off**, herdr-ai adds no instructions at all: no appended system prompt, developer instructions, or opening prompt. The agents start exactly as the picker would start them. Only the iMCP server stays out of Claude and Codex, since five panes connect at once.

With **individual**, the agents share context through one notes file per tab in the local state directory (`herdr-ai-shared/<tab>.md`). Each reads it before a task, appends a line when it starts (the task and the files it is touching) and when it finishes or learns something the others need, and checks a peer's pane with `herdr agent read` before editing a file that peer has claimed. They do not prompt each other. With handoff on, an agent near its limit hands off only its own pane.

**Orchestrator-crew** is the lead and four cheaper crew described below.

Team leads always run Sol 6.1 or Opus 5.5. Claude, Codex, Cursor and Kiro lead their own crew on their team model. Antigravity's Gemini Flash never orchestrates: an Antigravity team is led by the first provider with quota in the order Codex, Claude, then Cursor or Kiro (whichever resets sooner), with all four crew on Gemini Flash. Other teams put Gemini Flash in crew4 when Antigravity is installed, so trivial work (finding files, running commands and summarizing output, exactly specified edits) runs on Antigravity's separate quota. The lead is told which workers are which and to keep design, debugging and review away from Gemini. A team's usage pane shows every provider on the team.

Automatic handoff is off by default. Turn on "Hand off at 90%" in `herdr-ai --settings` to enable it; while it is off, lead and solo agents get no usage instructions and `herdr-ai --handoff` refuses to run. Crew are always told to stop near a limit and tell their lead. With it on, every lead or solo agent `herdr-ai` starts, including ones picked from the selector inside herdr, is told how to check its usage and what to do near a limit. `herdr-ai --usage` prints each provider's windows, marks the caller, and names who would take over. At 90% of any limit with the user's goal unfinished, an agent writes handoff notes and runs `herdr-ai --handoff "<goal>" <notes file>`. Once its turn ends, a detached helper restarts its pane on the next provider with quota (the same order as above, Gemini excluded) and submits `/goal <goal>` pointing at the notes. A team lead whose crew share its quota moves the whole team (except a Gemini crew4); the lead of an Antigravity team is replaced alone. Crew never hand off; they report the limit to the lead. The helper logs to `herdr-ai-handoff.log` in the local state directory.

Press `s` in the `herdr-ai` setup menu to open its settings, or run `herdr-ai --settings` from any terminal to change them without restarting or rerouting agents. **Hide crew agents** defaults to **on**: crew panes remain running and available to the lead, but only the lead appears in Herdr's Agents sidebar. Use Left/Right or Enter to toggle, then Esc to return; the preference is saved immediately. Existing teams are migrated from saved pane identities or their exact team launch brief. This changes the Agents view only, so `herdr agent list`, delegation, usage counts, and notifications still include crew.

**Agent order** is a separate setting in the same panel and defaults to **priority**. It works together with crew hiding: blocked agents come first, then finished agents, working agents, idle agents, and unknown agents. Within a priority level, recent state changes come first. Choose **grouped** to order agents by workspace, tab, and pane. Crew visibility is independent, so all four combinations of priority/grouped and crew hidden/shown are available.

Herdr 0.9.3 or newer is required for sidebar filtering. The first `herdr-ai` run links a local **Herdr AI** startup hook to restore the saved visibility after a server restart. The hook only refreshes role metadata and the view filter; it does not launch agents. Herdr supports one active Agents view filter, so setting crew visibility replaces another plugin's view filter. Turning hiding off keeps the selected ordering active. The sidebar keeps Herdr's normal Agents heading. The local plugin also provides a settings popup through `herdr-ai --open-settings`. To bind it, add this to Herdr's `config.toml` and reload the config:

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "local.herdr-ai.settings"
description = "Herdr AI settings"
```

Press Ctrl+B, then Shift+S to open it; Esc saves and closes the popup. Herdr 0.9.3 does not expose custom buttons beside the Agents heading.

| Team | Lead | Crew (x4) |
| --- | --- | --- |
| Claude | Opus 5.5, high | Haiku 5.5, high |
| Codex | GPT-6.1-Sol, high | GPT-6-Luna, high |
| Kiro | Opus 5.5, high | Sonnet 5.5, low (Kiro has no Haiku 5.5) |
| Cursor | Opus 5.5, high | Grok 4.7, high |
| Antigravity | Gemini 3.8 Flash High | Gemini 3.8 Flash Low |

The pairs are the `team` entry of each profile in `agents.json`. Orchestrator-crew keeps them apart from the picker's model: with orchestrator-crew selected, press → on a provider's row in the setup menu to change the lead's and the crew's model and effort (←→ on Role switches between them). They save as that provider's `team` in the local config, one role at a time, and never change what `agent` launches. The other picker defaults, such as permissions, still apply. The lead is told to hand searches, test runs, builds, research and well specified edits to the crew panes through `herdr agent prompt`, run them in parallel when they can be, ask for replies of at most 15 lines, read only the end of each reply, and keep planning, decisions and the final check for itself, so its own context and quota stay small. The crew are told to stay in the scope they are given and to finish with that short summary. Claude takes these instructions as an appended system prompt and Codex as developer instructions. Kiro, Cursor and Antigravity have no such flag, so their lead receives its instructions as an opening prompt (one short turn) and passes the reply rules to the crew inside each task. Claude and Codex team panes start without the iMCP server, which drops every client when several connect at once.

A small background observer keeps the expanded Agents sidebar's team indicators current. An idle or finished orchestrator shows an orange working indicator while any crew agent on its tab is working, then returns to its own state when the last crew member stops. An orchestrator asking for approval keeps its blocked indicator. Completed work stays marked finished until its pane is focused. The indicators and priority ranks come from display-only metadata; provider lifecycle hooks, session identity, prompts, waits, and notifications remain native. The observer polls once per second, refreshes short-lived metadata, and uses a per-server lock to avoid duplicate observers. It starts with `herdr-ai` and after server restores, and preserves the existing task-label rows and other Herdr configuration.

The expanded Spaces sidebar derives its indicators from orchestrators and standalone agents only. Crew completions and approval prompts do not set a space indicator. A lead still inherits its orange working state while its crew is working, so the space reflects that displayed lead state. Space indicators use the same priority and completion acknowledgement as the Agents sidebar, regardless of whether crew rows are shown. The compact sidebar rail and mobile status badges remain native Herdr aggregates.

Run `herdr-ai` again from a tab it already laid out, usually from the bottom terminal, to refresh it without adding panes or clearing conversations. Idle agents restart with their exact conversation IDs and existing native launch arguments, preserving their provider, model, effort, permission flags, and team instructions. This supports Claude, Codex, Cursor, Kiro, Antigravity, and a Star mesh (each idle StarCLI restarts with `--resume-id`, so its conversation, name, role, provider pin, tier, and plan mode come back). Old opening prompts are removed so refreshing never submits the same task or team introduction again. Quitting a Standard agent still opens the selector.

Working or blocked agents stay running so refresh cannot interrupt unfinished work or approval requests. A pane whose exact session ID or launch options cannot be safely identified is also left unchanged, with the reason printed in the bottom terminal. Refresh never guesses the latest session, which could belong to a different pane in the same directory. Panes left at the shell with a recorded agent session are retained rather than silently replaced by a blank agent. The caller's pane and unrelated programs are left alone.

The selector and usage panel reload on every refresh so local UI fixes appear immediately. After a `cd`, restarted panes use the new directory; retained panes keep their current directory until they can be refreshed. The restart lines are typed as raw text rather than key presses, because an agent stopped mid-session can leave Herdr encoding Ctrl+C and Enter as kitty keyboard sequences that the shell would only print. The installed Mac launcher reads this checkout directly, so source changes need no reinstall.

`agent-route` explicitly reroutes every agent Herdr detects on the current tab, including ones `herdr-ai` did not start. Each one restarts with its original command line in the directory you ran `agent-route` from. Unlike `herdr-ai` refresh, this does not add conversation-resume arguments and can start a fresh conversation or interrupt working agents. Panes running something other than an agent are left alone, as is the pane you run it from.

`herdr-reset` closes every other pane on the current tab, agents included, and leaves only the pane you run it from, usually the bottom terminal, with its history and directory intact. Run `herdr-ai` from there to lay the tab out again.

To switch a tab between Standard and a team, to a different team, or between system prompt modes, run `herdr-reset` and then `herdr-ai`; `herdr-ai --setup` refuses a tab laid out differently, and a team whose lead pane was closed is not rerouted.

Rerouting stops the agents, so unsaved conversation state in them is lost. herdr-ai remembers each tab's panes in `herdr-ai-tabs.json` in the local state directory.

## Workbench agent priority

The middle-bottom agent pane automatically chooses Cursor or Kiro. Usable quota resetting within five days takes priority, with the earlier reset winning when both are close. Otherwise it chooses the larger remaining percentage divided by days until reset. Cursor uses the Auto pool for `--model auto` and the external-model pool for named models; Kiro uses its monthly pool. Exhausted pools do not outrank usable ones. The chooser uses the shared usage cache, gives stale readers up to two seconds to refresh, and ignores data older than a day or whose reset has passed. With no usable data it falls back to an installed provider, preferring Cursor on ties. It prints the choice and reason before launching with your existing defaults. `agent --picker-priority` explains the current choice without opening an agent session. The choice is evaluated at launch, including when rerouting restarts this pane; running sessions stay on their provider.

## Agent selector defaults

Codex has a **Fast Mode** toggle below Bypass Permissions. Highlight Codex, press `→`, move down to Fast Mode, and use `←→`, Space, or Enter to toggle it. Esc offers Save settings or Discard changes. The choice is saved locally and applies to future picker and team launches; it starts Off. On uses Codex's `service_tier=fast` with `features.fast_mode=true`; Off explicitly selects `service_tier=default`. The selector shows `fast` beside the model when enabled. Existing sessions keep their current mode.

Fast mode gives faster replies in exchange for higher usage, not higher usage limits. Speed depends on the model; the documented 1.5x speedup applies to GPT-5.6 and GPT-5.5. Included subscription usage is consumed at 2.5x the Standard rate; purchased credits and Enterprise pay-as-you-go usage use a 2x rate. Availability depends on the model and account. See [OpenAI's speed documentation](https://learn.chatgpt.com/docs/agent-configuration/speed).

Choices come from bundled model catalogs, refreshed from installed CLIs or their local caches in the background. Your current model stays selectable. Cursor, Claude, and Codex effort choices follow their model catalogs.

Changes are saved in the machine-local config as `agents.<Provider>.defaults`, preserving local paths and unrelated arguments. Turning Bypass Permissions Off removes that provider's bypass flags and skips new picker folder-trust writes. Claude uses its default permission mode; Codex uses on-request approvals and the workspace-write sandbox.

## Shared defaults and local overrides

[agents.json](agents.json) contains the original launch settings, colors, and display labels. These model identifiers and flags were copied from the working Mac command; they are configurable preferences, not a promise that every provider version or account supports them.

| Provider | Default model argument | Effort |
| --- | --- | --- |
| Claude | opus | medium |
| Codex | gpt-6.1-sol | high |
| Cursor | grok-4.7-high | encoded in model argument |
| Kiro | claude-opus-5.5 | medium |
| Antigravity | gemini-3.8-flash-high | encoded in model argument |

The defaults retain the original permission bypass flags and automatic current-folder trust behavior. Turn **Bypass Permissions** Off in a provider's defaults to remove its bypass flags and skip new folder-trust writes for that launch. Claude explicitly uses its default permission mode; Codex uses on-request approvals with the workspace-write sandbox. Set `AGENT_PICKER_NO_PRETRUST=1` to skip all picker folder-trust writes. Only the selected provider's current directory is pre-trusted when bypass is On.

Use the config path printed by `--picker-doctor` for machine-specific executable paths or launch arguments. It is normally `~/.config/agent-picker/config.json`.

```json
{
  "agents": {
    "Cursor": {
      "command": "C:\\Users\\Example\\.local\\bin\\agent.exe"
    }
  }
}
```

Use `command` to override only the executable path while continuing to inherit shared model and flag changes. An `argv` override replaces the entire argument array and intentionally pins that machine's launch settings. Do not set a provider command to bare `agent`, which would reopen this picker. Use an absolute vendor path for Cursor when its CLI is named `agent`. `AGENT_PICKER_CONFIG` can select a different local config file, and `AGENT_PICKER_STATE_HOME` can select a different local state directory. Shared edits to `agents.json` apply to both computers unless the corresponding field has a local override.

## Usage meters

Usage collection runs in background threads and never refreshes authentication tokens. Failure does not block launching a provider. The previous successful numbers are cached locally and dim when stale. A dash means no usable usage data is available, not zero usage.

| Provider | Source |
| --- | --- |
| Claude | Existing Keychain credentials or credentials file |
| Codex | Local session rate-limit records |
| Cursor | Existing macOS Keychain token |
| Kiro | CLI `/usage` output |
| Antigravity | Existing macOS Keychain token |

Claude's [authentication documentation](https://code.claude.com/docs/en/authentication#credential-management) describes its platform-specific credential files. `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are respected by the usage readers. Cursor and Antigravity usage endpoints and credential formats come from the original Mac implementation and may change. Login, expired credentials, provider-specific model access, and live usage accuracy need verification.

## Agent catalog

[agents.json](agents.json) is a catalog of agent CLIs: the five original profiles plus OpenCode, Qwen Code, GitHub Copilot CLI, Amp, Aider, Goose, Crush, Factory Droid, Cline, Auggie, Kimi CLI, and Continue. The picker lists only the ones installed on this computer, found on PATH or in common install folders that aren't always on it (`~/.opencode/bin`, `~/.bun/bin`, npm-global, cargo, Homebrew). The rest stay in the catalog with their color schemes and appear as soon as their CLI is installed. `agent --picker-catalog` shows every entry with its colors and status.

Catalog entries other than the original five launch their CLI with no arguments, so they use that CLI's own default model and permission prompts. Add model or permission flags in the local config once a CLI is installed and you have checked its flags. To add a CLI, append a profile with `name`, `color`, `pastel`, `gradient`, `short`, `detail`, `argv`, and `detect` (the names it shows up under in Herdr and in process lists). Usage limits are read only for Claude, Codex, Antigravity, Cursor, and Kiro; other CLIs' cards say no usage reader exists yet.

## Usage panel

The usage panel is a screen of the same command (press `u`), a full-pane dashboard of the meters meant to stay open in a Herdr pane. `usage` opens on the usage panel. `agent` opens on the picker; set **Opens on** in settings to start it on the usage panel instead. A shell opened before the `usage` alias changed still runs the old one until you run `source ~/.zshrc` or open a new shell.

The panel shows a card for each provider with agents open in its own Herdr tab or tmux window, counting agents Herdr recognizes plus panes whose foreground process is a catalog CLI. Three scope settings (settings, Usage panel section) each choose among this tab, this workspace, and every workspace; under tmux those are the window, the session, and every session. **Universal display** sets where a provider has to be open to get a card: **off** (the default) means this tab, so a Kiro-only `herdr-ai` team in another tab adds no Kiro card here; **this workspace** also counts other tabs of the panel's workspace; **on** counts every workspace. **Agent count** sets the number in parentheses beside a provider's name: its agents open in **all workspaces** (the default), **this workspace**, or a **single tab**. The **Usage cards** section of the **Visibility** page (below) lists every provider found on this computer. Enter or Space turns a provider's card on or off; an off card never appears, except while Tab is held (unless its CLI is hidden on the Visibility page). `←→` sets where an on card counts working agents: **all workspaces** (the default), so Codex agents working in another tab show as working on the Codex card here too; **this workspace**; **this tab**; or **always shown**, which keeps the card up even when none of that provider's agents are open and counts every workspace. Esc returns to the main settings. Without Herdr or tmux, every mode counts the whole computer. With no card to show, the panel shows an empty state; cards appear and disappear as agents open and close. Limits are still read for every installed provider, so the picker's meters stay current. Its live readers start with the command, so the picker's meters are live too, and both screens share one cache. Cards stack in tall panes and sit side by side in wide, short ones; very small panes fall back to one line per provider.

Refreshing follows the agents. Every two seconds the panel asks Herdr (`herdr pane list`) which panes are working. A provider with a working agent is read often (Codex every 5 seconds from its local logs, the web endpoints every 45 to 60 seconds). It is read again a few seconds after one of its agents finishes a turn, and only every 5 to 15 minutes while idle. Failed reads back off. Without Herdr it falls back to fixed intervals between those two.

By default each bar depletes: it starts full, empties as the limit is used, and the percentage shows what is left. The empty track is brighter down to where an even pace through the window would have left the bar by now, so a bright gap means usage is ahead of pace. Set **Bars** (Usage panel section) to **filling** to have bars grow toward 100% and show the percentage used, with the track brighter up to the even-pace point. The agent selector's meters follow the same setting. Every row shows its reset countdown. A limit is tinted a muted red only when it is at least 70% used and its reset is still far off: more than 2 hours away for the 5-hour limit, 2 days for weekly, and 5 days for monthly (a monthly limit resetting within 5 days is never flagged). When several instances of one provider are working, its card shows a spinner per instance, a shimmering count, and one light pulse per instance along its bars. History for a burn-rate sparkline is still recorded, but drawing it is commented out in `usage_panel.py`.

Filled-bar gradients and light pulses move left in depleting mode and right in filling mode. Background and border waves keep their usual direction.

Keys: `t` or `→` next theme, `T` or `←` previous theme, `r` refresh now, `a` toggle animation (off redraws once a second), hold Tab to see every connected provider (each installed provider with a usage reader) until Tab is let go, `s` settings, `u` or Esc back to the picker, `q` quit.

Holding Tab relies on key release events. On the usage screen the panel turns on the kitty keyboard protocol, which Ghostty, kitty, WezTerm, and Herdr support, and the peek ends the moment Tab is released. Where releases never arrive (other terminals), the peek follows key repeat instead: it opens on the press and closes about a quarter second after the repeats stop, so a quick tap shows it for just over half a second.

Brand uses an animated gray outer border and each provider's own title, count, status, and bar colors. Each working provider gets the same animated border as its row in the agent selector. Every other theme gives all providers one shared palette: the panel title, provider names and instance counts, status dots and spinners, working count and text, borders, glow, and bars follow the selected gradient in both full cards and compact rows. Rainbow uses Antigravity as its reference: all providers share its pastel bar palette and working glow, rainbow borders and titles, white status dots, spinners, working counts, and usage percentages. Filled bars keep the pastel palette while idle and shimmer while working, in full cards and compact rows. Star’s combined meter follows the same palette in Rainbow. Short panes drop the top and bottom of provider borders to keep the limits visible. Border space is reserved even while idle, so titles, bars, and card positions stay fixed as providers start or stop working. The layout changes only when the pane size or installed providers change. The theme name appears in the outer border for 2.5 seconds after changing themes, then disappears.

## Settings

Provider defaults are saved as an `agents.<Provider>.defaults` object in the machine-local config, separately from the general settings below. This preserves local executable paths and unrelated arguments without changing `agents.json` or writing your preferences into Git.

**Antigravity (agy) System Prompts:** highlight Antigravity in `agent`, press `→`, select **System Prompts**, and toggle On/Off. Esc reviews and saves the change. On is the default and launches agy normally. Off selects a managed native custom agent with no conversation system prompt sections, including agy's built-in instructions and automatically included rules, skills, workspace and MCP descriptions. Its normal tools, MCP connections and hooks remain available. This also applies to Antigravity turns launched by StarCLI. The profile is registered at `~/.gemini/config/agents/agent-picker-no-system-prompt-v1.md`; existing files with different contents are never overwritten. A conflicting explicit `--agent` is rejected while Off.

This setting is independent of **Herdr AI → System prompt**. With agy Off and Herdr Individual or Orchestrator enabled, Herdr still adds its opening instructions. With agy On and Herdr Off, agy keeps its normal system prompt without Herdr instructions. Both Off gives a plain agy conversation without either prompt layer. The provider toggle affects new sessions; a resumed conversation can retain its original agent configuration and history. It does not change a bare `agy` command launched outside the picker. Tool schemas still consume tokens, agy can still run its small internal title-generation prompt, and shorter context does not guarantee a particular response time. Removing coding instructions can also change how the agent uses its tools. Requires agy with `excludeDefaultComponents` support (CLI 1.2.1 or later).

Press `s` on any screen. `↑↓` choose a setting, `←→` or Enter change it, Esc returns to the screen you came from. Changes apply immediately and are saved to `preferences.json` in the local state directory.

**Visibility** (under General) is one page shared by the agent selector, the usage panel and `herdr-ai --settings`. Its **Agent selector and herdr-ai** section shows or hides each installed CLI everywhere it is offered: the agent selector, herdr-ai's setup menu, and the cards the usage panel adds while Tab is held. A hidden CLI still runs through `--picker-launch` and `herdr-ai --setup NAME`, and its card still shows when it is open and its usage card is on. Its **Usage cards** section turns each provider's usage card on or off (see the usage panel above). StarCLI starts hidden, because its `agents.json` entry has `"shown": false`; show it here to list it again. Any entry can start hidden the same way.

Settings live in [src/preferences.py](src/preferences.py). To give a new feature a setting, register it where the feature lives and read it with `preferences.get`; the settings screen lists it under its section with no other changes:

```python
import preferences

SOUND = preferences.register(preferences.Setting(
    "sound", "General", "Sound", [False, True], False,
    hint="Play a chime when an agent finishes.", names={False: "off", True: "on"}))

if preferences.get(SOUND.key):
    ...
```

## Iterate

```sh
git pull --ff-only
# Edit agents.json or src/, then:
python3 -m unittest discover -s tests -v
git add agents.json src tests docs README.md scripts .github
git commit -m "Describe the command update"
git push
```

Then run:

```text
agent --picker-update
```

The next invocation uses the new code. If installer or shell-integration behavior changed, rerun the relevant installer after pulling. Machine-specific local overrides stay in the local config.

GitHub Actions tests macOS with Python 3.9 and 3.13, including process argument forwarding, exit codes, rendering bounds, and configuration. Tests use fake providers and do not open paid agent sessions. CI does not verify authentication or provider model availability.
