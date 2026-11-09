# Install agent-picker on Windows or macOS

Download the source ZIP from the release and extract it to a permanent folder.
The picker runs from that folder. It does not contain anyone else's account,
credentials, preferences, or provider installations.

## Windows 10 or 11

1. Install Python 3.9 or later from https://www.python.org/downloads/windows/.
   Include the Python launcher when the Python installer offers it.
2. Extract the release ZIP. Double-click **Install agent-picker.cmd** inside it.
   The installer creates a private Python environment in that folder, adds the
   small timezone packages Windows needs, and creates commands in
   `%LOCALAPPDATA%\agent-picker\bin`. Administrator access is unnecessary.
3. Close and reopen your terminal. Windows Terminal is recommended for the
   animated colors and Unicode characters. Run `agent --picker-connect`.
4. Select a provider and press **o** for its official installation guide. Install
   a CLI that supports your operating system. Press **l** to sign in where that
   provider offers a CLI login, or sign in through its own app. Press **v** to
   check that its executable runs, then **r** to refresh and Escape to return.

The picker never signs you in or buys an account automatically. Usage readings
can be unavailable when a provider does not expose its account limits. The CLI
check verifies the executable; it does not spend AI quota or verify a paid plan.

If `agent` is not found, run
`%LOCALAPPDATA%\agent-picker\bin\agent.cmd --picker-doctor` and reopen your
terminal. The installer changes only your user PATH, never the system PATH.

## macOS, Apple Silicon or Intel

1. Install Python 3.9 or later from https://www.python.org/downloads/macos/ if
   `python3 --version` is unavailable.
2. Extract the ZIP to a permanent folder. In Terminal, change to that folder
   and run `python3 scripts/install-macos.py`.
3. Open a new Terminal window and run `agent --picker-connect`. Use **o** for a
   provider's installation guide, **l** to sign in, **v** to check its executable,
   and **r** to refresh. Escape returns to the selector.

The macOS installer backs up changed launchers and shell profiles before adding
its managed aliases. It preserves the current provider flags and preferences.

## Everyday use

- `agent`: selector. **c** opens Connections, **s** settings, **u** usage.
- `usage`: usage screen. **c** also opens Connections here.
- `agent --picker-doctor`: executable paths and local configuration paths.
- `schedule`: scheduled prompts. The dispatcher runs while logged in and awake.
  Its first saved schedule enables a macOS LaunchAgent or a Windows user-login
  startup entry. Logging out stops dispatch; missed runs coalesce after login.
- `star`: the bundled StarCLI. Connect at least one supported provider first.
- `grok`: bundled Grok Bot client. Sign in to Cursor and Grok Bot on this
  computer, then enable its local connection in Grok Bot. Windows reads the
  existing signed-in Cursor desktop profile; macOS uses its Keychain item.
  `CURSOR_ACCESS_TOKEN` can override discovery. Never share this token.
- `fs`: a Finder-style file browser in the terminal, with icon and details
  views; `fs DIR` opens that folder. herdr-ai shows it in icon view in each
  tab's bottom-left pane.

Herdr and provider CLIs are separate products. Only installed providers appear
in the selector. Follow each vendor's platform requirements; a provider that
requires WSL needs that environment, rather than a native Windows executable.
Herdr features require the separately installed Herdr terminal.

## Data, updates and removal

Windows stores state in `%LOCALAPPDATA%\agent-picker` and configuration in
`%APPDATA%\agent-picker\config.json`. macOS keeps state in
`~/.local/state/agent-picker` and configuration in `~/.config/agent-picker/config.json`.
`AGENT_PICKER_STATE_HOME` and `AGENT_PICKER_CONFIG` override these locations.
Credentials remain in the provider's existing account store.

A Git clone supports `agent --picker-update` with fast-forward updates only.
For an extracted release ZIP, extract a newer release to a permanent folder and
run its installer. Existing state and configuration remain in place.

To remove commands on Windows, remove the `agent-picker\bin` entry from your
user PATH and delete that bin folder. If schedules were enabled, also remove
`agent-picker-scheduler.cmd` from `shell:startup`, then log out to stop its
running dispatcher. Keep your state folder if you want to retain schedules and
preferences. On macOS remove the managed blocks from `.zshrc` and `.bashrc`, the
managed launchers from `~/.local/bin`, and the scheduler LaunchAgent if enabled.
Do not delete provider CLI installations or account stores.
