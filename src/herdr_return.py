"""Reopen the agent selector when an agent started outside the picker quits.

herdr-ai's own panes run each agent inside the picker (--picker-return), which
reopens the selector when the agent quits. An agent typed at a pane's shell, or
started any other way, has no picker around it, so quitting it would leave a
bare shell. herdr-ai's background watcher calls Watch.tick once a second: in a
tab herdr-ai laid out, a pane that was running such an agent and is back at its
shell prompt gets the selector, as if the picker had started the agent. The
tab's bottom terminal stays a shell, as does a pane whose agent the picker ran
(the picker already handled its quit, and Esc there means "give me the shell").
"""

import herdr_ai

SHELL_ROLES = {"terminal"}


def at_prompt(info):
    return bool(info.get("shell_pid")) and not herdr_ai.busy(info)


def wrapped(info):
    """Whether the picker runs the pane's foreground job."""
    return any(herdr_ai.ENTRY.name in " ".join(p.get("argv") or [p.get("cmdline") or ""])
               for p in info.get("foreground_processes", []))


class Client(herdr_ai.Herdr):
    """herdr commands that raise instead of exiting, so a failed call skips one
    tick rather than stopping the watcher."""

    def __call__(self, *args):
        try:
            return super().__call__(*args)
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from None


def client():
    binary = herdr_ai.find_herdr()
    return Client(binary, "herdr-ai") if binary else None


class Watch:
    def __init__(self, herdr, socket_path, load_state=herdr_ai.load_state):
        self.herdr, self.socket, self.load_state = herdr, socket_path, load_state
        self.seen = {}    # pane -> (terminal, agent, session) last checked
        self.armed = {}   # pane -> terminal of a running agent the picker did not start

    def tabs(self):
        """tab id -> the set of panes herdr-ai keeps a shell in, for this server's laid-out tabs."""
        found = {}
        for key, entry in self.load_state().items():
            socket, _, tab = key.partition("|")
            if socket == self.socket:
                panes = entry.get("panes", {})
                found[tab] = {pane for role, pane in panes.items() if role in SHELL_ROLES}
        return found

    def tick(self, snapshot):
        tabs = self.tabs()
        agents = {a["pane_id"]: a for a in snapshot.get("agents", []) if a.get("agent")}
        live = set()
        for pane in snapshot.get("panes", []):
            pid, tab, terminal = pane.get("pane_id"), pane.get("tab_id"), pane.get("terminal_id")
            if tab not in tabs or pid in tabs[tab]:
                continue
            live.add(pid)
            agent = agents.get(pid)
            if agent:
                key = (terminal, agent["agent"], (agent.get("agent_session") or {}).get("value"))
                if self.seen.get(pid) != key:
                    self.seen[pid] = key
                    if wrapped(self.herdr.process_info(pid)):
                        self.armed.pop(pid, None)
                    else:
                        self.armed[pid] = terminal
            elif self.armed.get(pid) == terminal:
                if not at_prompt(self.herdr.process_info(pid)):
                    continue   # still exiting, or something else took the foreground
                del self.armed[pid]
                self.seen.pop(pid, None)
                herdr_ai.type_line(self.herdr, pid, herdr_ai.picker_command(["--picker-return"]))
                print(f"herdr-ai: reopened the selector in {pid}", flush=True)
        for pid in set(self.armed) - live:
            del self.armed[pid]
        for pid in set(self.seen) - live:
            del self.seen[pid]
