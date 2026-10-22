"""/goal for the Grok Bot CLI: keep taking turns until the bot reports the goal
complete or blocked, the way StarCLI's /goal and Codex's goals work."""

import re
import time

MAX_STEPS = 40      # a runaway goal pauses here; /goal resume continues it


def verdict(reply):
    """(COMPLETE|BLOCKED|CONTINUES, detail) from the reply's final plain line only,
    never from a quoted example or a line inside a code block."""
    lines = (reply or "").rstrip().splitlines()
    if not lines:
        return "CONTINUES", ""
    fence = None
    for line in lines[:-1]:
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            token = match.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
    if fence:
        return "CONTINUES", ""
    match = re.fullmatch(r"GOAL (COMPLETE|BLOCKED|CONTINUES)(?::[ \t]*(.*))?", lines[-1].strip())
    return (match.group(1), (match.group(2) or "").strip()) if match else ("CONTINUES", "")


RULE = ("End your reply with exactly one of these lines: `GOAL COMPLETE` once the goal is fully done and "
        "verified; `GOAL BLOCKED: <what you need from the user>` if you cannot go on without them; or "
        "`GOAL CONTINUES: <the next step>` otherwise. Write the status as the final plain line, without "
        "quotes, Markdown formatting, or a code block. Before reporting complete, check every requirement "
        "against the current files and by running the checks. A plan, partial change, or unverified claim "
        "is not completion.")


class Goal:
    def __init__(self, data=None):
        data = data or {}
        self.text = data.get("text", "")
        self.step = int(data.get("step", 0) or 0)
        self.started = data.get("started") or 0.0
        self.took = data.get("took") or 0.0
        self.detail = data.get("detail", "")
        # A goal that was running when the CLI closed comes back paused.
        state = data.get("state", "")
        self.state = "paused" if state == "running" else state if self.text else ""

    def data(self):
        return {"text": self.text, "step": self.step, "started": self.started, "took": self.took,
                "detail": self.detail, "state": self.state} if self.text else None

    def start(self, text):
        self.text, self.step, self.started, self.took, self.detail = text, 0, time.time(), 0.0, ""
        self.state = "running"

    def resume(self):
        if not self.text:
            return False
        self.state, self.detail = "running", ""
        if self.step >= MAX_STEPS:
            self.step = 0
        return True

    def pause(self, why=""):
        self.state, self.detail = "paused", why

    def clear(self):
        self.__init__()

    def prompt(self):
        """The next step's message, counting it."""
        self.step += 1
        if self.step == 1:
            return (f"[Goal] Work autonomously toward this goal until it is fully done:\n\n{self.text}\n\n"
                    f"Do as much as you can in this turn: make the changes and run the checks. {RULE}")
        return (f"[Goal, step {self.step}] Keep going toward the goal: {self.text}\n\n"
                f"Pick up where the last step left off. {RULE}")

    def finish(self, reply):
        """Read the step's reply. Returns True while the goal should keep going."""
        kind, detail = verdict(reply)
        if kind == "COMPLETE":
            self.state, self.detail, self.took = "complete", detail, time.time() - self.started
        elif kind == "BLOCKED":
            self.state, self.detail = "blocked", detail or "the bot needs your input"
        elif self.step >= MAX_STEPS:
            self.pause(f"paused after {MAX_STEPS} steps")
        return self.state == "running"

    def status(self):
        """Footer text, like Codex's 'Goal achieved (15m)'."""
        if not self.text:
            return ""
        if self.state == "running":
            return f"Goal running · step {self.step}"
        if self.state == "complete":
            minutes = int(self.took // 60)
            return f"Goal achieved ({minutes}m)" if minutes else f"Goal achieved ({int(self.took)}s)"
        if self.state == "blocked":
            return "Goal blocked"
        return "Goal paused"
