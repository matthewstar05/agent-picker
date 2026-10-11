# StarCLI parity with the Herdr workbench

The goal remains: a `herdr-ai --setup star` mesh can do everything the
existing `herdr-ai` setup does. Passing a smaller set of tests does not establish
full parity. Keep the original workbench, native provider behavior, and both
platforms in scope. Do not commit unless the user asks.

## Requirements and current evidence

| Requirement | Current evidence | Remaining verification or work |
| --- | --- | --- |
| Lead, four crew, usage panel, bottom terminal, picker return, safe repeated setup | Isolated native Herdr 0.9.3 grid: seven panes, five Star members, usage count, exact idle resume and picker return; fresh process/pane/server receipts cover missing native session refs | Native Windows grid; repeatable Unix check is `tests/native_star_herdr.py` |
| Strong lead and independent parallel crew tasks | Real PTYs and Star processes: three crew turns overlap; one fails on Claude after writing a partial file, recovers on fake Codex, and the lead verifies every output and a preserved sentinel | Native provider inference remains untested; launch fixtures use fake providers |
| Provider shell, edits, tests, web, MCP, account integrations | Headless vendor commands retain provider tools; Kiro standard/heavy MCP profiles now inherit servers through the stdio schema bridge; installed Kiro validates a generated fixture profile | Native Kiro tool-call inference has not been exercised with the bridge; remote HTTP MCP servers are inherited directly and do not use schema adaptation |
| Short external requests retain their tools | MCP stays enabled by default; only clear local work, greetings and arithmetic omit it on light turns. Unknown service and URL regressions pass | Classification remains local and conservative; ambiguous requests can cost more startup tokens |
| Quota-aware routing and recovery without replacing the Star pane | Pins cannot bypass limits; shared cache preserves fresh timestamps across processes; quota failures rejoin after their recorded reset or a newer recovered meter; authentication failures require explicit retry | Native quota rollover during a mesh task; preserve the existing near-reset routing policy |
| Context and user constraints survive provider changes and refresh | Saved provider sessions, failed attempts, routing state, earlier user requests, and compacted summaries; tests include 70 turns and `/compact` | Long native conversations and provider-managed compaction still need live evidence |
| Unfinished tasks and delivered answers survive refresh/crash | Requests persist before launch and native session IDs persist on arrival; a real crash/reopen fixture resumes the exact fake native session and running goal while retaining partial effects; task/reply receipts prevent replaying saved answers | Effects can precede a crash, so recovery asks the model to inspect them. Do not claim exactly-once effects |
| One writer and no overlapping abandoned provider after crash | OS conversation lease; separate guardian lease lasts through provider cleanup; real fake-provider tests kill the owner and verify provider/tool exit before resume. Resume reads saved state under its lease | Windows job implementation awaits a native Windows run; deliberate detached Unix processes can escape a provider process group |
| Provider permission settings and native approval controls | Claude asks inline through `--permission-prompt-tool` and a per-turn stdio MCP server (`star_approve.py`): y, a (session), n, Esc; pane reports blocked while waiting. A fake Claude drives the real server end to end. Other providers with approvals required are routed around; if none remains, the request is blocked and saved with an `/open` hint; plan mode stays read-only Codex uses `star_codex_app.py`, a `codex app-server` client built from `codex app-server generate-json-schema`: thread start/resume with the profile's approval policy and sandbox, command, file and permission approvals, unsupported server requests declined. A fake app-server test covers accept, decline and resume; the real app-server accepted initialize and an ephemeral thread/start with on-request and workspace-write (no turn sent) Kiro uses `star_kiro_acp.py`, an Agent Client Protocol client for `kiro-cli acp`: session/new or session/load (replayed history hidden), `session/request_permission` answered with the matching allow or reject option, the profile's `--trust-tools` kept, file system and terminal requests refused. A fake ACP agent test covers this; the real `kiro-cli acp` 2.29.1 accepted initialize and session/new (loadSession and image prompts advertised, no prompt sent) | No real approval turn has run for any provider (no real inference). Cursor and Antigravity headless modes cannot ask, so their native approvals remain available only through `/open` |
| Goals continue, pause for steering, retain blockers, and finish only after integration | Final plain verdict parser; goals continue past the previous 20-turn cutoff; durable blocked state; async crew waits and integration; real PTY Esc stops a provider and preserves fragmented Unicode steering | Native Windows interruption and long crew waits |
| Sidebar state, crew hiding, ordering, usage counts, completion indicators | Native Herdr reports working, idle and completion sequence; lead metadata inherits working crew state; blocked goals persist and refresh retains their processes; sidebar filter/order unit tests pass | No visual TUI comparison of filtering/order; native Herdr 0.9.3 `agent prompt` rejects custom Star agents, so delegation uses mesh send |
| Input, images, native-session opening, local memory, resume/export | Editor, clipboard, image routing, session, and command tests; Windows surrogate-key and PowerShell delegation regressions; corrupted saved files are skipped by resume and refused by refresh | Native Windows arrows, paste, Esc during a turn; confirm image flags before enabling Cursor or Antigravity image turns |
| Provider commands and skills typed by name (`/deep-research`, `/design:ux-copy`, `.claude/commands`, Codex prompts) | Discovery of Claude, plugin, Codex, Cursor, Kiro and `.agents` skills and command files; project entries win; built-in Star commands win; expansion follows `$ARGUMENTS` and `$1` rules; mesh tasks starting with a name expand; editor menu shows frontmatter descriptions (PTY check) | Provider-specific frontmatter such as `allowed-tools` and `model` is not enforced; `!` shell lines inside command templates are not executed by StarCLI |
| `agent-route` and `herdr-reset` on a Star tab | `agent-route` restarts every Star member fresh in the new directory with its mesh name and role (test) | Windows panes now stop with ctrl+c three times, because the first only clears a typed draft in Claude, Codex and StarCLI (a PTY test shows two leave Star running and three quit it without reaching a provider). Not run on a Windows host |
| A provider's leftover background tool must not hold a turn open | The POSIX guardian relays provider output and stops when the provider has exited and its pipes go quiet; the tool keeps running. Regression covers 300 KB output plus a tool holding both pipes | Windows ends leftover tools with the guardian's kill-on-close job |
| Windows prompts through vendor `.cmd` shims | npm, pnpm and older npm shims run their script with Node directly; other batch launchers refuse prompts containing cmd syntax before any process starts | Exercised with real shim templates on macOS, not on a Windows host |
| Windows and macOS launch/install/update behavior | Shared suite; fake providers now launch through Python on both platforms; Windows process liveness uses process handles | Current uncommitted Star changes have no new Windows CI run. Previous green CI belongs to an older committed revision |

## Verification commands

```sh
python3 -m unittest discover -s tests -v
python3 src/agent_picker.py --picker-doctor
git diff --check
# Optional native Unix check, from a Herdr-managed pane:
python3 tests/native_star_herdr.py
```

Use fake provider executables for launch tests, as required by `AGENTS.md`.
The PTY mesh test and fake MCP server verify local orchestration and transport;
they do not prove a native model understands every tool or spends fewer tokens.

The earlier handoff also requests a light-Claude token comparison before and
after `--strict-mcp-config`. No token benchmark has been run. An initial native
fixture did not propagate its local config to split panes and reached the real
Claude CLI once. That owned session was stopped; subsequent fixtures put the
fake-provider config in the server environment and verify every pane before
submitting work. All owned test servers and their observers are stopped on exit.

Kiro's current [MCP configuration reference](https://kiro.dev/docs/mcp/configuration/)
documents server precedence and per-tool disabling. Keep disabled servers,
disabled tools, transport settings, and environment values intact in generated
profiles. Generated profiles containing local server configuration stay outside
the repository and are written with private permissions.

## Next concrete work

1. Native mesh delegation, draft protection and exact refresh pass on macOS
   Herdr 0.9.3 with the process guardian and output relay. Latest run:
   `/tmp/starcli-native-claude.log` (temporary directory starcli-native-na86ghtw).
2. Workbench commands audited: `herdr-ai --setup star`, refresh, `agent-route`,
   `herdr-reset`, `--usage`, `--handoff` (refused, the mesh routes instead),
   the settings pane and sidebar sync all have Star coverage. Provider slash
   commands and skills, and approvals for Claude, Codex and Kiro, were the
   remaining functional gaps and are implemented.
3. HTTP MCP servers stay native in Kiro, as in a native Kiro pane; a schema
   bridge for them would be an improvement beyond parity and must keep OAuth.
4. Needs the user or other hardware: a Windows host run of the console, grid,
   batch-shim and job-object fixtures; a CI run of the current changes (needs
   an authorized commit and push); a real approval turn and the light-Claude
   token comparison (both spend quota, so they need explicit authorization).

Latest local suite: 395 tests, OK (skipped=3), exit 0.
Log: `/tmp/starcli-claude-current.log`. The native fixture above
uses fake providers. Current dirty changes still have no Windows CI run.
Windows console setup/restoration, Ctrl+C handling, surrogate input and
PowerShell delegation now have regression coverage, pending native verification.
