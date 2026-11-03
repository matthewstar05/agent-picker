"""Provider setup that remains available before any CLI is installed."""
import subprocess
import sys
import threading
import webbrowser

from platform_runtime import resolve_argv

# Vendor pages own installation instructions, platform support and account login.
DOCS = {
    "Claude": ("https://code.claude.com/docs/en/setup", ["auth", "login"]),
    "Codex": ("https://developers.openai.com/codex/cli", ["login"]),
    "Antigravity": ("https://antigravity.google/download", None),
    "Cursor": ("https://cursor.com/docs/cli/installation", ["login"]),
    "Kiro": ("https://kiro.dev/docs/cli/", ["login"]),
    "Grok Bot": ("https://cursor.com/docs/grok-bot", None),
    "OpenCode": ("https://opencode.ai/docs/", ["auth", "login"]),
    "Amp": ("https://ampcode.com/manual", None),
    "Droid": ("https://docs.factory.ai/cli/getting-started/quickstart", None),
    "Aider": ("https://aider.chat/docs/install.html", None),
    "Star": ("https://github.com/matthewstar05/agent-picker#starcli", None),
}


class Menu:
    def __init__(self, agents):
        self.agents = agents
        self.index = 0
        self.notice = "Install a provider, sign in with your own account, then refresh."
        self.busy = False

    @property
    def agent(self):
        return self.agents[self.index]

    def handle(self, key):
        if key in ("up", "k"):
            self.index = (self.index - 1) % len(self.agents)
        elif key in ("down", "j", "\t"):
            self.index = (self.index + 1) % len(self.agents)
        elif key in ("esc", "q", "c"):
            return "close"
        elif key in ("o", "\r", "\n"):
            url, _ = DOCS.get(self.agent["name"], (None, None))
            if url:
                webbrowser.open(url)
                self.notice = "Opened the provider's setup guide in your browser."
            else:
                self.notice = "See this provider's documentation to install its CLI."
        elif key == "r":
            self.notice = "Detection refreshed. Return to the selector when ready."
            return "refresh"
        elif key == "l":
            argv = resolve_argv(self.agent)
            _, login = DOCS.get(self.agent["name"], (None, None))
            if not argv:
                self.notice = "Install this provider first. Press o for the setup guide."
            elif login is None:
                self.notice = "Sign in using the provider's own app or setup guide (o)."
            else:
                return ("login", [argv[0], *login])
        elif key == "v" and not self.busy:
            argv = resolve_argv(self.agent)
            if not argv:
                self.notice = "CLI not found. Install it, restart this terminal, then refresh."
            else:
                self.busy = True
                self.notice = "Checking the executable without starting an AI session…"
                threading.Thread(target=self.check, args=(argv[0],), daemon=True).start()
        return None

    def check(self, binary):
        try:
            result = subprocess.run([binary, "--version"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            self.notice = ("CLI responds. Use l to sign in; account limits appear on the usage screen."
                           if result.returncode == 0 else "The CLI did not pass its version check. Open its setup guide (o).")
        except (OSError, subprocess.SubprocessError):
            self.notice = "The CLI could not run. Open its setup guide (o)."
        finally:
            self.busy = False


def render(menu, ap, cols, rows):
    width = max(12, min(76, cols - 4))
    lines = [ap.BOLD + "Connections" + ap.RESET, ""]
    # Scroll so every provider remains reachable in a short terminal.
    count = max(1, rows - 10)
    top = max(0, min(menu.index - count // 2, len(menu.agents) - count))
    for i in range(top, min(top + count, len(menu.agents))):
        agent = menu.agents[i]
        status = "detected" if resolve_argv(agent) else "install needed"
        text = ("› " if i == menu.index else "  ") + agent["name"].ljust(15) + status
        lines.append(ap.fg(agent["color"]) + text[:width] + ap.RESET)
    import textwrap
    lines += [""] + textwrap.wrap(menu.notice, width)[:3]
    lines += ["", "↑↓ provider · o setup guide · l sign in · v check CLI"[:width],
              "r refresh · Esc return · q close"[:width]]
    return lines
