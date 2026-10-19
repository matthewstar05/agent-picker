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
- Installs follow main through --picker-update.
- Keep update operations fast-forward only. Do not reset, stash, or discard local work automatically.

## Auto-commit and push (standing authorization)

The owner has given standing permission to commit and push. Do not ask for confirmation. At the end of every prompt where you created, modified, moved, or deleted files in this repo:

Only commit and push changes you made yourself. Other agents often work in this repo at the same time. Their edits and commits are theirs to push.

1. Run the tests (`python3 -m unittest discover -s tests -v`). If they fail because of your change, fix it before committing. If they fail for an unrelated reason, commit anyway and say so.
2. Stage only your own changes: `git add -- <paths>` (deletions too: `git add -A -- <paths>`). Never run a bare `git add -A`, `git add .`, or `git commit -a`, and never stage a file you didn't edit, unless the user names it.
3. Commit once per prompt with a short imperative subject that describes the behavior change, matching the existing log (e.g. `Reopen the selector when an agent started outside the picker quits`). Use whatever co-author or attribution trailer your harness requires.
4. Sync: `git pull --rebase --autostash origin main`.
5. Check that you'd only push your own work: `git log --format='%h %s' origin/main..HEAD` must list only the commit(s) you just made. If it shows anyone else's unpushed commit, do not push. Leave your commit local and tell the user.
6. Push: `git push origin main`.
7. Report the result in one line, e.g. `Committed abc1234 and pushed.` If nothing changed, skip the commit and say nothing about git.

### Guardrails

- Never force-push, rewrite published history, amend someone else's commit, or use `--no-verify`.
- If the autostash can't be re-applied cleanly, stop and tell the user. The stash holds other agents' uncommitted edits, so never drop it or resolve it yourself.
- If the rebase or push fails (conflict, auth, or rejected), stop. Leave the commit local and tell the user what failed. Don't resolve another agent's conflicting edits yourself.
- Leave files you didn't touch alone, even when they show up as modified or untracked. They may belong to another agent's work in progress.
