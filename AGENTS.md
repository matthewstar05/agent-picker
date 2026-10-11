# Working on agent-picker

- Avoid em dashes in responses and documentation.
- Read README.md before changing install behavior.
- macOS only. Windows support was removed; do not add Windows code paths, scripts, or CI jobs.
- Shared defaults belong in agents.json. Machine paths belong in the local config shown by --picker-doctor.
- Preserve the current model, effort, permission flags, animation, and key bindings unless the user requests changes.
- Never commit credentials, caches, session logs, shell profiles, or account exports.
- Never use the generic agent executable as a provider fallback. An absolute vendor path is required to prevent recursion.
- Do not install or authenticate providers without user authorization. Missing providers stay disabled.
- Use python3 -m unittest discover -s tests -v, then inspect the macOS CI jobs.
- Test provider launches with fake executables. Real launches may create sessions or spend quota.
- Push authorized changes to this repository. Installs follow main through --picker-update.
- Keep update operations fast-forward only. Do not reset, stash, or discard local work automatically.
