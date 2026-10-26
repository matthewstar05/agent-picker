"""Where Grok Bot works: this Mac, its cloud computer, or chosen per request.

One preference, shown in every settings screen (agent selector, usage panel,
herdr-ai --settings) and read by every Grok Bot launch, since they all run the
grokbot CLI. --where or --cloud overrides it for one run; a saved conversation
keeps the mode it started with.
"""

import json
import re
import subprocess

import preferences

LOCAL, AUTO, CLOUD = "local", "auto", "cloud"
WHERE = preferences.Setting(
    "grokbot.where", "Grok Bot", "Works on", [LOCAL, AUTO, CLOUD], LOCAL,
    hint="Auto lets Grok Bot pick per request: its cloud computer for compute and GitHub repository work, this Mac for anything only this Mac has.",
    names={LOCAL: "this Mac", AUTO: "auto · by request", CLOUD: "cloud computer"})
DETAIL = {LOCAL: "local machine tools", AUTO: "local or cloud by request", CLOUD: "cloud computer"}


def detail(agent):
    """Grok Bot's picker line, which names the current mode."""
    text = agent.get("detail", "")
    if agent.get("name") != "Grok Bot":
        return text
    return text.split(" · ")[0] + " · " + DETAIL[preferred()]


def preferred():
    preferences.register(WHERE)
    return preferences.get(WHERE.key)


def git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def repo_facts(cwd):
    """What the bot needs to decide: the remote it could clone, and local work the cloud would not see."""
    if git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return "The project directory is not a Git repository, so its files exist only on this machine."
    remote = git(cwd, "remote", "get-url", "origin")
    # Never pass credentials embedded in a remote URL into the conversation.
    remote = re.sub(r"(https?://)[^/@]+@", r"\1", remote) if remote else None
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    changes = git(cwd, "status", "--porcelain")
    ahead = git(cwd, "rev-list", "--count", "@{upstream}..HEAD") if remote else None
    facts = ["The project is a Git checkout on branch " + json.dumps(branch) + "."]
    facts.append("Its origin remote is " + json.dumps(remote) + "." if remote else "It has no origin remote.")
    facts.append(("It has uncommitted local changes (" + str(len(changes.splitlines())) + " paths).") if changes
                 else "Its working tree is clean.")
    if ahead and ahead != "0":
        facts.append(ahead + " local commits are not pushed yet.")
    elif ahead is None and remote:
        facts.append("The branch has no upstream, so the remote may not have this work.")
    return " ".join(facts)


def auto_location(machine, cwd):
    return ("Choose where to do this request, then say in one short line which you chose and why. "
            "You can work on the user's registered machine " + json.dumps(machine) + " with the native machine-targeted Shell tool, "
            "in " + json.dumps(cwd) + ", or on your own cloud computer. " + repo_facts(cwd) + " "
            "Use the cloud computer for computation, research, and changes to a project that lives entirely in its GitHub repository: "
            "clone or pull the remote there, follow the repository's AGENTS.md, make and verify the change, then commit and push. "
            "Do not edit the local checkout for that work; tell the user the pushed commit so they can pull it. "
            "Use the local machine when the request needs anything only that machine has: uncommitted or unpushed changes, "
            "files outside the repository, installed apps, browsers, devices, local services or credentials, "
            "or a result that must appear on that machine. "
            "If the cloud computer cannot clone or push the repository, do the work on the local machine instead. "
            "On the local machine, read the project's AGENTS.md, preserve existing changes, and use Grok Bot's native local "
            "execution policy and approval cards. Do not launch another AI CLI to do this task. ")
