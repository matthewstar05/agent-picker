# agent-picker

The shared source for my `agent` command on macOS and Windows. One repository controls both installations: edit, commit, and push from either computer, then run `agent --picker-update` on each computer to pull the change. Installed launchers run the checkout directly, so copying or reinstalling the application is unnecessary after ordinary code and profile updates.

The picker preserves the original animated gradients, usage meters, remembered selection, adaptive layout, launch animation, arrow keys, j/k navigation, number keys, Enter, and q/Escape. Extra arguments go to the selected CLI. Windows uses native console input and ANSI output rather than Unix-only termios APIs.

## Requirements

- Git and access to this private GitHub repository.
- Python 3.9 or later. No third-party Python packages are required.
- At least one provider CLI installed and signed in on that computer.
- An interactive terminal. Use Windows Terminal with a PowerShell tab on Windows.

The installer installs this picker, not the provider CLIs or their account credentials. Providers missing from PATH are disabled in the menu. Credentials and usage caches remain local.

## Windows setup

Paste this into a PowerShell window. Skip the first line if Git is already installed. The clone opens a browser to sign in to GitHub because the repository is private.

```powershell
winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
git clone https://github.com/matthewstar05/agent-picker.git "$HOME\Projects\agent-picker"
powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Projects\agent-picker\scripts\setup-windows.ps1"
```

`setup-windows.ps1` installs Python (winget, per-user) and [herdr](https://herdr.dev/docs/windows-beta/) only if they are missing, then runs `install-windows.ps1`. Pass `-SkipHerdr` to leave herdr out. It does not install provider CLIs or sign in to them. Open a new Windows Terminal tab, then:

```powershell
agent --picker-doctor
cd $HOME\Projects\some-project
herdr-ai
```

To install only the picker, without Python or herdr checks, run `scripts\install-windows.ps1` instead. Give a Windows coding agent [docs/WINDOWS_AGENT.md](docs/WINDOWS_AGENT.md) for a full verification pass.

If your execution policy prevents your own PowerShell profile from loading, use `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` where local policy permits it, or invoke `agent-picker.cmd`. Running the installer with a process-level Bypass does not change the persistent execution policy. An administrator-managed execution policy must be respected.

The installer backs up changed profiles and launchers under the local state directory. It adds a managed function to both PowerShell 7 and Windows PowerShell user profiles. It also adds `agent-picker.cmd` to your user PATH for Command Prompt. PowerShell's `agent` function takes precedence over Cursor's similarly named application. Cursor's existing executable is captured as an absolute local path when necessary; no vendor executable is moved or deleted.

Install Cursor before this picker when possible. If you install Cursor later, rerun the picker installer to capture its executable path. Existing local Cursor launch overrides are preserved. Cursor's [official installation instructions](https://prod.cursor.com/docs/cli/installation) cover native Windows and WSL. Kiro's [installation instructions](https://kiro.dev/docs/getting-started/installation/) list its Windows CLI requirements. For Antigravity, install the CLI that provides `agy`, or configure the actual executable you use locally. Installing an IDE alone does not guarantee its CLI is on PATH.

## macOS setup

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

## herdr-ai, agent-route and herdr-reset

Run `herdr-ai` in a herdr pane to lay out that tab as the workbench: Antigravity, Codex, Cursor or Kiro, the agent selector, Claude, the usage panel, and a terminal along the bottom. The pane you ran it from becomes that terminal. Nothing else on the tab is touched, and no new tab is created. Outside herdr it opens a new workspace and attaches. An optional label renames the tab; otherwise the tab is named after the directory.

In the five Standard agent panes, quitting an agent (`/quit`, `/exit`, or Ctrl+C or Ctrl+D) reopens the agent selector in that pane, so you can switch the pane to another provider. Esc or q in that selector leaves you at the pane's shell. Rerouting keeps the provider you switched to. Teams and plain `agent` runs still exit to the shell.

`herdr-ai` first opens a menu drawn like the agent selector, with each provider's usage beside it. **Standard** (sakura pink) is the workbench above, unchanged. Choosing a provider lays out a team of that provider instead: a lead agent on the right, four crew agents in a 2x2 grid on the left, the terminal under them, and a short usage panel for that provider alone under the lead. The launch animation and key bindings match the selector; Esc quits without touching the tab, and the menu remembers your last choice. `herdr-ai --setup NAME [label]` skips the menu (`standard`, `claude`, `codex`, `antigravity`, `cursor`, `kiro`), and without a terminal to draw in it lays out Standard.

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

The pairs are the `team` entry of each profile in `agents.json`; a local config can override them per machine. The other picker defaults, such as permissions, still apply. The lead is told to hand searches, test runs, builds, research and well specified edits to the crew panes through `herdr agent prompt`, run them in parallel when they can be, ask for replies of at most 15 lines, read only the end of each reply, and keep planning, decisions and the final check for itself, so its own context and quota stay small. The crew are told to stay in the scope they are given and to finish with that short summary. Claude takes these instructions as an appended system prompt and Codex as developer instructions. Kiro, Cursor and Antigravity have no such flag, so their lead receives its instructions as an opening prompt (one short turn) and passes the reply rules to the crew inside each task. Claude and Codex team panes start without the iMCP server, which drops every client when several connect at once. On Windows, a CLI behind a `.cmd` shim gets the instructions on one line without cmd metacharacters.

A small background observer keeps the expanded Agents sidebar's team indicators current. An idle or finished orchestrator shows an orange working indicator while any crew agent on its tab is working, then returns to its own state when the last crew member stops. An orchestrator asking for approval keeps its blocked indicator. Completed work stays marked finished until its pane is focused. The indicators and priority ranks come from display-only metadata; provider lifecycle hooks, session identity, prompts, waits, and notifications remain native. The observer polls once per second, refreshes short-lived metadata, and uses a per-server lock to avoid duplicate observers. It starts with `herdr-ai` and after server restores, and preserves the existing task-label rows and other Herdr configuration.

The expanded Spaces sidebar derives its indicators from orchestrators and standalone agents only. Crew completions and approval prompts do not set a space indicator. A lead still inherits its orange working state while its crew is working, so the space reflects that displayed lead state. Space indicators use the same priority and completion acknowledgement as the Agents sidebar, regardless of whether crew rows are shown. The compact sidebar rail and mobile status badges remain native Herdr aggregates.

Run `herdr-ai` again from a tab it already laid out, usually from the bottom terminal after a `cd`, and it reroutes instead of adding panes: the four agents and the selector, or a team's lead and crew, stop and restart in the new directory with the same setup. If the selector had launched an agent, that agent restarts with the same arguments. The usage panel keeps running; if its pane was left at the shell or is showing the selector instead, it is restarted as the usage panel. The restart lines are typed as raw text rather than key presses, because an agent killed mid-session can leave Herdr encoding Ctrl+C and Enter as kitty keyboard sequences that the shell would only print.

`agent-route` does the same for every agent herdr detects on the current tab, including ones `herdr-ai` did not start. Each one restarts with its original command line in the directory you ran `agent-route` from. Panes running something other than an agent are left alone, as is the pane you run it from.

`herdr-reset` closes every other pane on the current tab, agents included, and leaves only the pane you run it from, usually the bottom terminal, with its history and directory intact. Run `herdr-ai` from there to lay the tab out again.

To switch a tab between Standard and a team, or to a different team, run `herdr-reset` and then `herdr-ai`; `herdr-ai --setup` refuses a tab laid out differently, and a team whose lead pane was closed is not rerouted.

Rerouting stops the agents, so unsaved conversation state in them is lost. herdr-ai remembers each tab's panes in `herdr-ai-tabs.json` in the local state directory. On Windows, agents are stopped with Ctrl+C rather than a signal.

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

Use the config path printed by `--picker-doctor` for machine-specific executable paths or launch arguments. On macOS it is normally `~/.config/agent-picker/config.json`; on Windows it is `%APPDATA%\agent-picker\config.json`.

```json
{
  "agents": {
    "Cursor": {
      "command": "C:\\Users\\Example\\.local\\bin\\agent.exe"
    }
  }
}
```

Use `command` to override only the executable path while continuing to inherit shared model and flag changes. The Windows installer uses this form for Cursor. An `argv` override replaces the entire argument array and intentionally pins that machine's launch settings. Do not set a provider command to bare `agent`, which would reopen this picker. Use an absolute vendor path for Cursor when its CLI is named `agent`. `AGENT_PICKER_CONFIG` can select a different local config file, and `AGENT_PICKER_STATE_HOME` can select a different local state directory. Shared edits to `agents.json` apply to both computers unless the corresponding field has a local override.

## Usage meters

Usage collection runs in background threads and never refreshes authentication tokens. Failure does not block launching a provider. The previous successful numbers are cached locally and dim when stale. A dash means no usable usage data is available, not zero usage.

| Provider | macOS | Windows |
| --- | --- | --- |
| Claude | Existing Keychain credentials or credentials file | Existing credentials file |
| Codex | Local session rate-limit records | Local session rate-limit records |
| Cursor | Existing macOS Keychain token | Existing CLI login file (`%APPDATA%\Cursor\auth.json`) |
| Kiro | CLI `/usage` output | CLI `/usage` output, when supported by the installed version |
| Antigravity | Existing macOS Keychain token | Existing Credential Manager login (`gemini:antigravity`) |

Claude's [authentication documentation](https://code.claude.com/docs/en/authentication#credential-management) describes its platform-specific credential files. `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are respected by the usage readers. Cursor and Antigravity usage endpoints and credential formats come from the original Mac implementation and may change. On Windows both are read from where their CLIs store the login, without refreshing it, so a meter shows a dash while that login's access token has expired until the CLI next runs. Login, expired credentials, provider-specific model access, and live usage accuracy need verification on each computer.

## Agent catalog

[agents.json](agents.json) is a catalog of agent CLIs: the five original profiles plus OpenCode, Qwen Code, GitHub Copilot CLI, Amp, Aider, Goose, Crush, Factory Droid, Cline, Auggie, Kimi CLI, and Continue. The picker lists only the ones installed on this computer, found on PATH or in common install folders that aren't always on it (`~/.opencode/bin`, `~/.bun/bin`, npm-global, cargo, Homebrew). The rest stay in the catalog with their color schemes and appear as soon as their CLI is installed. `agent --picker-catalog` shows every entry with its colors and status.

Catalog entries other than the original five launch their CLI with no arguments, so they use that CLI's own default model and permission prompts. Add model or permission flags in the local config once a CLI is installed and you have checked its flags. To add a CLI, append a profile with `name`, `color`, `pastel`, `gradient`, `short`, `detail`, `argv`, and `detect` (the names it shows up under in Herdr and in process lists). Usage limits are read only for Claude, Codex, Antigravity, Cursor, and Kiro; other CLIs' cards say no usage reader exists yet.

## Usage panel

The usage panel is a screen of the same command (press `u`), a full-pane dashboard of the meters meant to stay open in a Herdr pane. `usage` opens on the usage panel. `agent` opens on the picker; set **Opens on** in settings to start it on the usage panel instead. A shell opened before the `usage` alias changed still runs the old one until you run `source ~/.zshrc` or open a new shell.

The panel shows a card for each provider with agents open in its own Herdr tab or tmux window, counting agents Herdr recognizes plus panes whose foreground process is a catalog CLI. Three scope settings (settings, Usage panel section) each choose among this tab, this workspace, and every workspace; under tmux those are the window, the session, and every session. **Universal display** sets where a provider has to be open to get a card: **off** (the default) means this tab, so a Kiro-only `herdr-ai` team in another tab adds no Kiro card here; **this workspace** also counts other tabs of the panel's workspace; **on** counts every workspace. **Agent count** sets the number in parentheses beside a provider's name: its agents open in **all workspaces** (the default), **this workspace**, or a **single tab**. Each provider's **card** setting decides where a shown card counts working agents: **all workspaces** (the default), so Codex agents working in another tab show as working on the Codex card here too; **this workspace**; **this tab**; or **always shown**, which keeps the card up even when none of that provider's agents are open and counts every workspace. Without Herdr or tmux, every mode counts the whole computer. With no card to show, the panel shows an empty state; cards appear and disappear as agents open and close. Limits are still read for every installed provider, so the picker's meters stay current. Its live readers start with the command, so the picker's meters are live too, and both screens share one cache. Cards stack in tall panes and sit side by side in wide, short ones; very small panes fall back to one line per provider.

Refreshing follows the agents. Every two seconds the panel asks Herdr (`herdr pane list`) which panes are working. A provider with a working agent is read often (Codex every 5 seconds from its local logs, the web endpoints every 45 to 60 seconds). It is read again a few seconds after one of its agents finishes a turn, and only every 5 to 15 minutes while idle. Failed reads back off. Without Herdr it falls back to fixed intervals between those two.

Each bar fills to the percentage used, and the empty track is brighter up to where an even pace through the window would be by now. Every row shows its reset countdown. A limit is tinted a muted red only when it is at least 70% used and its reset is still far off: more than 2 hours away for the 5-hour limit, 2 days for weekly, and 5 days for monthly (a monthly limit resetting within 5 days is never flagged). When several instances of one provider are working, its card shows a spinner per instance, a shimmering count, and one light pulse per instance along its bars. History for a burn-rate sparkline is still recorded, but drawing it is commented out in `usage_panel.py`.

Keys: `t` or `→` next theme, `T` or `←` previous theme, `r` refresh now, `a` toggle animation (off redraws once a second), hold Tab to see every connected provider (each installed provider with a usage reader) until Tab is let go, `s` settings, `u` or Esc back to the picker, `q` quit.

Holding Tab relies on key release events. On the usage screen the panel turns on the kitty keyboard protocol, which Ghostty, kitty, WezTerm, and Herdr support, and the peek ends the moment Tab is released. Where releases never arrive (other terminals, Windows), the peek follows key repeat instead: it opens on the press and closes about a quarter second after the repeats stop, so a quick tap shows it for just over half a second.

Brand uses an animated gray outer border. Each working provider gets the same animated border as its row in the agent selector; choosing another theme changes both the outer border and provider borders to that theme. Rainbow gives every provider rainbow titles, white status dots and spinners, and white usage percentages, including stale and high-usage values. Its filled usage bars are white while idle and turn rainbow while working. Other themes keep each provider's normal title, status, and percentage colors. Short panes drop the top and bottom of provider borders to keep the limits visible. Border space is reserved even while idle, so titles, bars, and card positions stay fixed as providers start or stop working. The layout changes only when the pane size or installed providers change. The theme name appears in the outer border for 2.5 seconds after changing themes, then disappears.

## Settings

Provider defaults are saved as an `agents.<Provider>.defaults` object in the machine-local config, separately from the general settings below. This preserves local executable paths and unrelated arguments without changing `agents.json` or writing your preferences into Git.

Press `s` on any screen. `↑↓` choose a setting, `←→` or Enter change it, Esc returns to the screen you came from. Changes apply immediately and are saved to `preferences.json` in the local state directory.

Settings live in [src/preferences.py](src/preferences.py). To give a new feature a setting, register it where the feature lives and read it with `preferences.get`; the settings screen lists it under its section with no other changes:

```python
import preferences

SOUND = preferences.register(preferences.Setting(
    "sound", "General", "Sound", [False, True], False,
    hint="Play a chime when an agent finishes.", names={False: "off", True: "on"}))

if preferences.get(SOUND.key):
    ...
```

## Iterate from either computer

```sh
# In this repo on whichever computer you are editing:
git pull --ff-only
# Edit agents.json or src/, then:
python3 -m unittest discover -s tests -v  # use python on Windows
git add agents.json src tests docs README.md scripts .github
git commit -m "Describe the command update"
git push
```

On both computers, run:

```text
agent --picker-update
```

The next invocation uses the new code. If installer or shell-integration behavior changed, rerun the relevant installer after pulling. Machine-specific local overrides remain on their own computer. Neither computer needs a separate platform fork.

GitHub Actions tests macOS and Windows with Python 3.9 and 3.13, including process argument forwarding, exit codes, Windows batch providers, rendering bounds, configuration, and reinstall behavior. Tests use fake providers and do not open paid agent sessions. CI does not verify authentication, provider model availability, or the visual appearance in your actual Windows terminal.
