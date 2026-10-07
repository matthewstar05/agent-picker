# Working on agent-picker

- Avoid em dashes in responses and documentation.
- Read README.md and docs/WINDOWS_AGENT.md before changing install behavior.
- Keep one shared renderer and launch profile list for macOS and Windows.
- Shared defaults belong in agents.json. Machine paths belong in the local config shown by --picker-doctor.
- Preserve the current model, effort, permission flags, animation, and key bindings unless the user requests changes.
- Never commit credentials, caches, session logs, shell profiles, or account exports.
- Never use the generic agent executable as a provider fallback. An absolute vendor path is required to prevent recursion.
- Do not install or authenticate providers without user authorization. Missing providers stay disabled.
- Use python -m unittest discover -s tests -v, then inspect the macOS and Windows CI jobs.
- Test provider launches with fake executables. Real launches may create sessions or spend quota.
- Push authorized changes to this repository. Both computers use the same main branch and --picker-update.
- Keep update operations fast-forward only. Do not reset, stash, or discard local work automatically.
