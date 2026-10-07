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
agent --picker-doctor                 inspect detected providers and local paths
agent --picker-update                 pull shared updates from GitHub
agent --picker-catalog                every known CLI, its colors, and whether it is installed
agent --picker-usage                  open on the usage panel
agent --picker-help                   show picker help
agent -- --picker-help                pass a management-looking argument to the CLI
usage                                the same command as agent
herdr-ai [label]                     open a herdr workbench of every agent in this folder
```

Inside the command: `u` switches between the agent selector and the usage panel, and `s` opens settings from either one. Under **Agents** in settings, set an installed CLI to hidden to keep it out of the selector; `--picker-launch` and `herdr-ai` still run it.

Updates are explicit, use `git pull --ff-only`, and refuse a dirty checkout. If the two machines have diverging commits, resolve them with normal Git operations before updating. The command does not commit, stash, reset, push, or update provider CLIs automatically.

## Agent selector defaults

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
| Antigravity | gemini-3.8-flash-high | high |

The defaults retain the original permission bypass flags and automatic current-folder trust behavior. To use normal approvals, turn **Bypass Permissions** Off in that provider's defaults. Set `AGENT_PICKER_NO_PRETRUST=1` to skip the picker’s folder-trust writes. Only the selected provider's current directory is pre-trusted.

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

The usage panel is a screen of the same command (press `u`), a full-pane dashboard of the meters meant to stay open in a Herdr pane. `agent` and `usage` both open on the picker; set **Opens on** in settings to start on the usage panel instead.

The panel shows only agents running beside it: in the same Herdr tab (agents Herdr recognizes, plus panes whose foreground process is a catalog CLI), in the same tmux window, or anywhere on the computer when neither is in use. With no agent open there, it shows an empty state; cards appear and disappear as agents open and close. Limits are still read for every installed provider, so the picker's meters stay current. Its live readers start with the command, so the picker's meters are live too, and both screens share one cache. Cards stack in tall panes and sit side by side in wide, short ones; very small panes fall back to one line per provider.

Refreshing follows the agents. Every two seconds the panel asks Herdr (`herdr pane list`) which panes are working. A provider with a working agent is read often (Codex every 5 seconds from its local logs, the web endpoints every 45 to 60 seconds). It is read again a few seconds after one of its agents finishes a turn, and only every 5 to 15 minutes while idle. Failed reads back off. Without Herdr it falls back to fixed intervals between those two.

Each bar fills to the percentage used, and the empty track is brighter up to where an even pace through the window would be by now. Every row shows its reset countdown. A limit is tinted a muted red only when it is at least 70% used and its reset is still far off: more than 2 hours away for the 5-hour limit, 2 days for weekly, and 5 days for monthly (a monthly limit resetting within 5 days is never flagged). When several instances of one provider are working, its card shows a spinner per instance, a shimmering count, and one light pulse per instance along its bars. History for a burn-rate sparkline is still recorded, but drawing it is commented out in `usage_panel.py`.

Keys: `t` or `→` next theme, `T` or `←` previous theme, `r` refresh now, `a` toggle animation (off redraws once a second), `s` settings, `u` or Esc back to the picker, `q` quit.

Brand uses an animated gray outer border. Each working provider gets the same animated border as its row in the agent selector; choosing another theme changes both the outer border and provider borders to that theme. Provider titles and their dots retain their brand colors in every theme. Short panes drop the top and bottom of provider borders to keep the limits visible. Border space is reserved even while idle, so titles, bars, and card positions stay fixed as providers start or stop working. The layout changes only when the pane size or installed providers change. The theme name appears in the outer border for 2.5 seconds after changing themes, then disappears.

## herdr-ai workbench

`herdr-ai` opens a [herdr](https://herdr.dev) tab rooted in the current folder: Antigravity, Codex, Cursor, and Claude panes, an agent selector, a usage panel, and a plain terminal. Inside herdr it adds a tab to the current workspace; outside, it creates a workspace and attaches. Each agent pane runs `agent --picker-launch <Name>`, so it uses your saved model, effort, and permission defaults. A provider that is not installed leaves an error in its pane and the rest still open.

```text
herdr-ai                  label the tab with the folder name
herdr-ai my-label         choose the label
HERDR_AI_DRY=1 herdr-ai   build the layout with plain shells
```

It lives in [src/herdr_ai.py](src/herdr_ai.py) and needs only herdr and Python, so both computers run the same file and pick up changes with `agent --picker-update`. The installers add the `herdr-ai` launcher (`~/.local/bin/herdr-ai` on macOS, `herdr-ai.cmd` on Windows). On Windows, herdr is in beta. If your Python path contains spaces, panes run the installer's `agent-picker.cmd`, which must be on the PATH herdr started with; run `herdr server stop` once after installing if herdr was already running.

## Settings

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
