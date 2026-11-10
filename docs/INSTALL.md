# Install agent-picker on Windows or macOS

Install Python 3.9 or later, then extract the release ZIP or clone this repository
to a permanent folder. Run the installer from that folder:

```sh
# macOS
python3 scripts/install.py
```

```powershell
# Windows PowerShell or Command Prompt
py -3 scripts\install.py
```

The installer opens a terminal checklist. Type a component number to toggle it,
several numbers separated by spaces to toggle them together, `all` or `none` to
change every option, and Enter to install. `q` cancels before changing files.
The core `agent` and `agent-picker` commands are always installed.

| Option | Commands installed |
| --- | --- |
| Usage panel (`usage`) | `usage` |
| File browser (`fs`) | `fs`, `herdr-fs` |
| StarCLI (`starcli`) | `star`, `starcli` |
| Grok Bot CLI (`grokbotcli`) | `grok`, `grokbot`, `grokbotcli` |
| Prompt scheduler (`schedule`) | `schedule` |
| Herdr helpers (`herdr`) | `herdr-ai`, `agent-route`, `herdr-reset` |

Every selected name gets a real launcher, so short commands work in shells and
subprocesses without creating aliases by hand. These options control installed
command shortcuts. The full source remains in the checkout, and the picker's
built-in screens remain accessible. Selecting Herdr helpers does not install
Herdr itself. StarCLI and Grok Bot CLI need their own connected providers before
they can run tasks.

The double-click entry points, **Install agent-picker.cmd** on Windows and
**Install agent-picker.command** on macOS, open the same checklist. The original
`scripts/install-windows.py` and `scripts/install-macos.py` entry points accept
the same options.

## Scripted installs and reconfiguration

Use explicit selections to skip the checklist. On Windows replace `python3` with
`py -3` in these commands:

```sh
# Only the picker, usage panel and file browser
python3 scripts/install.py --components usage,fs

# Every bundled command
python3 scripts/install.py --all

# Everything except StarCLI and Grok Bot CLI
python3 scripts/install.py --without starcli,grokbotcli

# Only the core picker commands
python3 scripts/install.py --components none

# Show components without installing anything
python3 scripts/install.py --list-components
```

`--yes` accepts all default components without prompting. A noninteractive run
with no selection also installs all components for compatibility with existing
scripts. `--prefix DIR` chooses the launcher directory. `--no-path` skips shell
profile and user PATH changes, useful for isolated installs or managed setups.

Run the same command again to change the selected shortcuts. Deselected
launchers are removed only when marked as managed by this checkout; backups are
kept first. Custom commands and launchers belonging to other checkouts are left
in place. Existing provider installs, account credentials, models, flags,
preferences, and scheduled jobs are preserved. Turning off the scheduler shortcut
does not disable schedules that were already saved.

## Activate and check the commands

The installer prints the installed command names and their directory. Open a new
terminal, or use the activation command it prints to enable them in the current
one. Python cannot change the environment of the shell that launched it.

Then run:

```text
agent --picker-doctor
agent --picker-connect
```

In Connections, select a provider and press **o** for its installation guide,
**l** to sign in where supported, **v** to check its executable, and **r** to
refresh. Escape returns to the selector. The picker does not install vendor CLIs,
sign in, or buy accounts. Missing providers remain disabled. Connections is also
available with **c** in the selector or usage screen.

Usage readings can be unavailable when a provider does not expose account limits.
The executable check does not spend AI quota or verify a paid plan.

## Platform setup

On **Windows 10/11**, install Python from
https://www.python.org/downloads/windows/ and include the Python launcher.
Windows Terminal is recommended for animated colors and Unicode. The installer
creates a private Python environment in the checkout and installs the timezone
packages Windows needs. Launchers go in `%LOCALAPPDATA%\agent-picker\bin` by
default. Only your user PATH is changed; administrator access is unnecessary.
If `agent` cannot be found, run that directory's `agent.cmd --picker-doctor`
and activate PATH using the command printed by the installer.

On **macOS**, install Python from https://www.python.org/downloads/macos/ if
`python3 --version` is unavailable. No third-party Python packages are needed.
Launchers go in `~/.local/bin` by default. The installer backs up changed
launchers and shell profiles, then adds a managed PATH and alias block to
`.zshrc`, `.bashrc`, `.profile`, and an existing `.bash_profile` or `.bash_login`.
Existing profile content and Kiro's final integration block are preserved. New bash and zsh sessions
pick up the selected commands automatically.

Keep the checkout in place. Rerun the installer if it or its Python executable
moves. Herdr and provider CLIs are separate products with their own platform
requirements. A provider requiring WSL must run in that environment.

## Everyday use

- `agent`: selector. **c** opens Connections, **s** settings, **u** usage.
- `usage`: usage screen, when selected during installation.
- `fs DIR`: file browser with icon and details views.
- `star` or `starcli`: StarCLI, using connected providers.
- `grok`, `grokbot`, or `grokbotcli`: Grok Bot terminal client. Sign in to Cursor
  and Grok Bot on this computer, then enable its local connection in Grok Bot.
  Windows reads the signed-in Cursor desktop profile; macOS uses its Keychain
  item. `CURSOR_ACCESS_TOKEN` can override discovery. Keep tokens private.
- `schedule`: scheduled prompts. Saving the first schedule enables a macOS
  LaunchAgent or Windows user-login startup entry. Dispatch runs while logged
  in and awake; missed runs coalesce after login.

## Data, updates and removal

Windows stores state in `%LOCALAPPDATA%\agent-picker` and configuration in
`%APPDATA%\agent-picker\config.json`. macOS keeps state in
`~/.local/state/agent-picker` and configuration in `~/.config/agent-picker/config.json`.
`AGENT_PICKER_STATE_HOME` and `AGENT_PICKER_CONFIG` override these locations.
Credentials remain in the provider's existing account store.

A Git clone supports `agent --picker-update` with fast-forward updates only.
After installer changes, rerun the installer with your desired components.
For a release ZIP, extract a newer release to a permanent folder and run its
installer. State and configuration remain in place.

To remove optional commands, rerun with `--components none`. To remove all
commands on Windows, remove the installed bin directory from your user PATH
and delete its managed launchers. On macOS remove the managed profile blocks
and managed launchers from the install directory. If schedules were enabled,
also remove `agent-picker-scheduler.cmd` from Windows `shell:startup`, or the
scheduler LaunchAgent on macOS. Keep your state folder to retain preferences
and schedules. Do not delete provider installations or account stores.
