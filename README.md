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

Give a Windows coding agent [docs/WINDOWS_AGENT.md](docs/WINDOWS_AGENT.md), or use these commands in PowerShell after installing Python and Git and authenticating GitHub access:

```powershell
git clone https://github.com/matthewstar05/agent-picker.git "$HOME\Projects\agent-picker"
cd "$HOME\Projects\agent-picker"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

Open a new PowerShell tab, then:

```powershell
agent --picker-doctor
agent
```

If your execution policy prevents your own PowerShell profile from loading, use `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` where local policy permits it, or invoke `agent-picker.cmd`. Running the installer with a process-level Bypass does not change the persistent execution policy. An administrator-managed execution policy must be respected.

The installer backs up changed profiles and launchers under the local state directory. It adds a managed function to both PowerShell 7 and Windows PowerShell user profiles. It also adds `agent-picker.cmd` to your user PATH for Command Prompt. PowerShell's `agent` function takes precedence over Cursor's similarly named application. Cursor's existing executable is captured as an absolute local path when necessary; no vendor executable is moved or deleted.

Install Cursor before this picker when possible. If you install Cursor later, rerun the picker installer to capture its executable path. Existing local Cursor launch overrides are preserved. Cursor's [official installation instructions](https://prod.cursor.com/docs/cli/installation) cover native Windows and WSL. Kiro's [installation instructions](https://kiro.dev/docs/getting-started/installation/) list its Windows CLI requirements. For Antigravity, install the CLI that provides `agy`, or configure the actual executable you use locally. Installing an IDE alone does not guarantee its CLI is on PATH.

## macOS setup

```sh
git clone https://github.com/matthewstar05/agent-picker.git ~/Desktop/Projects/agent-picker
cd ~/Desktop/Projects/agent-picker
python3 scripts/install-macos.py
```

Open a new terminal, then run `agent --picker-doctor` and `agent`. The installer backs up the existing `~/.local/bin/agent-picker` and changed shell profiles, replaces the launcher with a checkout-linked wrapper, and adds a managed `agent` alias to `.zshrc` and `.bashrc`. On this Mac, it replaces the original alias and preserves Kiro's post integration at the bottom of `.zshrc`. Do not move or delete the checkout after installation; rerun the installer if you relocate it or your Python executable.

## Commands

```text
agent                                open the picker
agent "help me understand this repo"  forward this prompt to the chosen CLI
agent --picker-launch Codex           directly launch Codex
agent --picker-doctor                 inspect detected providers and local paths
agent --picker-update                 pull shared updates from GitHub
agent --picker-help                   show picker help
agent -- --picker-help                pass a management-looking argument to the CLI
usage                                live usage-limit panel for every provider
usage --theme vapor                  start the panel in a theme (remembered)
usage --themes                       list the panel's color themes
```

Updates are explicit, use `git pull --ff-only`, and refuse a dirty checkout. If the two machines have diverging commits, resolve them with normal Git operations before updating. The command does not commit, stash, reset, push, or update provider CLIs automatically.

## Agent selector defaults

In the agent selector, highlight a provider and press `→` to open its defaults. `↑↓` selects **Model**, **Effort**, or **Bypass Permissions**. On Model, `←→` cycles available models; Enter opens a scrollable list, where `↑↓` or `←→` selects a model and Enter returns to the fields. On Effort, `←→` cycles levels; on Bypass Permissions the same keys toggle On/Off. Space or Enter also changes either value. Esc saves locally and returns to the selector. If you then quit the selector with Esc or q after making changes, a confirmation opens with **Stay** selected. The saved defaults remain when you reopen `agent`.

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
| Cursor | Existing macOS Keychain token | No verified native credential reader yet |
| Kiro | CLI `/usage` output | CLI `/usage` output, when supported by the installed version |
| Antigravity | Existing macOS Keychain token | No verified native credential reader yet |

Claude's [authentication documentation](https://code.claude.com/docs/en/authentication#credential-management) describes its platform-specific credential files. `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are respected by the usage readers. Cursor and Antigravity usage endpoints and credential formats come from the original Mac implementation and may change. Windows meters for those providers show a dash until a verified reader is added; their launch commands still work when installed. Login, expired credentials, provider-specific model access, and live usage accuracy need verification on each computer.

## Usage panel

`usage` is a full-pane dashboard of the same meters, meant to stay open in a Herdr pane. It reads limits with the picker's readers and shares its cache, so both commands show the same numbers. Cards stack in tall panes and sit side by side in wide, short ones; very small panes fall back to one line per provider.

Refreshing follows the agents. Every two seconds the panel asks Herdr (`herdr pane list`) which panes are working. A provider with a working agent is read often (Codex every 5 seconds from its local logs, the web endpoints every 45 to 60 seconds). It is read again a few seconds after one of its agents finishes a turn, and only every 5 to 15 minutes while idle. Failed reads back off. Without Herdr it falls back to fixed intervals between those two.

Each bar fills to the percentage used. The empty track is brighter up to where an even pace through the window would be by now, and any filled part beyond that point turns hot. When the current pace would reach the limit before the window resets, the countdown changes from `↻ 2h 31m` to `out 1h 20m`. History for a burn-rate sparkline is still recorded, but drawing it is commented out in `usage_panel.py`.

Keys: `t` or `→` next theme, `T` or `←` previous theme, `r` refresh now, `a` toggle animation (off redraws once a second), `q` quit. The theme and animation choice are saved in the local state directory.

Brand uses an animated gray outer border. Each working provider gets the same animated border as its row in the agent selector; choosing another theme changes both the outer border and provider borders to that theme. Provider titles and their dots retain their brand colors in every theme. Short panes drop the top and bottom of provider borders to keep the limits visible. Border space is reserved even while idle, so titles, bars, and card positions stay fixed as providers start or stop working. The layout changes only when the pane size or installed providers change. The theme name appears in the outer border for 2.5 seconds after changing themes, then disappears.

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
