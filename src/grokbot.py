#!/usr/bin/env python3
"""Cursor-style terminal conversations on Grok Bot's own included usage."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

from grokbot_gateway import Gateway, GrokError, entry_text, request_id
import grokbot_sessions as store

VERSION = "0.1.0"
COMMANDS = [("help", "", "show commands"), ("usage", "", "Cursor models, external models, and Grok Bot limits"),
            ("status", "", "account, machine and conversation"), ("new", "", "start a separate conversation"),
            ("resume", "[id]", "resume a saved conversation"), ("sessions", "", "list saved conversations"),
            ("stop", "", "interrupt the current bot"), ("quit", "", "exit")]
CONTROL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))|[\x00-\x08\x0b-\x1f\x7f]")


def clean(text):
    return CONTROL.sub("", str(text or ""))


def marker(nonce):
    return "<!-- grokbot-cli:" + nonce + ":done -->"


def select_machine(machines, requested=None):
    rows = [m for m in machines if m.get("connected") and m.get("kind", "desktop") not in ("ios", "android")]
    if requested:
        rows = [m for m in rows if m.get("machineId") == requested or m.get("label") == requested]
    else:
        names = {socket.gethostname().lower(), socket.getfqdn().lower()}
        names |= {n.removesuffix(".local") if hasattr(n, "removesuffix") else n.rsplit(".local", 1)[0] for n in names}
        rows = [m for m in rows if str(m.get("label", "")).lower() in names
                or str(m.get("label", "")).lower().rsplit(".local", 1)[0] in names]
    if len(rows) != 1:
        raise GrokError("No unique connected machine. Open Grok Bot on this Mac; use grokbot machines and --machine ID to select it.")
    if rows[0].get("localToolPermission") == "never":
        raise GrokError("Grok Bot local execution is disabled for this machine. Change it in Grok Bot's settings before starting local work.")
    return rows[0]


class Output:
    def __init__(self, kind="text", interactive=False):
        self.kind, self.interactive = kind, interactive
        self.answer = ""

    def event(self, event):
        if self.kind == "stream-json":
            print(json.dumps(event, ensure_ascii=False), flush=True)
        elif self.kind == "text":
            if event["type"] == "assistant":
                text = "".join(p.get("text", "") for p in event["message"]["content"])
                print(clean(text), flush=True)
            elif event["type"] == "tool_call" and self.interactive:
                print("  ◇ " + clean(event["label"])[:180], flush=True)
            elif event["type"] == "notice":
                print(clean(event["text"]), file=sys.stderr, flush=True)

    def text(self, text):
        self.answer += ("\n\n" if self.answer else "") + text
        self.event({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

    def result(self, session, status="success"):
        event = {"type": "result", "subtype": status, "is_error": status != "success",
                 "session_id": session["id"], "result": self.answer}
        if self.kind == "json":
            print(json.dumps(event, ensure_ascii=False), flush=True)
        elif self.kind == "stream-json":
            self.event(event)


class Conversation:
    def __init__(self, gateway, output, cwd, machine=None, session=None, timeout=1800, poll=1.5):
        self.gateway, self.output, self.cwd = gateway, output, str(Path(cwd).resolve())
        self.machine, self.session = machine, session
        self.timeout, self.poll = timeout, poll

    def report(self, state):
        pane = os.environ.get("HERDR_PANE_ID")
        if os.environ.get("HERDR_ENV") != "1" or not pane:
            return
        cmd = ["herdr", "pane", "report-agent", pane, "--source", "grokbot-cli", "--agent", "grok", "--state", state]
        if self.session:
            cmd += ["--agent-session-id", self.session["id"]]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass

    def release(self):
        pane = os.environ.get("HERDR_PANE_ID")
        if os.environ.get("HERDR_ENV") == "1" and pane:
            try:
                subprocess.run(["herdr", "pane", "release-agent", pane, "--source", "grokbot-cli", "--agent", "grok"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass

    def ensure_session(self):
        if self.session:
            if self.session["cwd"] != self.cwd:
                raise GrokError("This session belongs to " + self.session["cwd"] + ". Use --workspace with that directory.")
            if self.machine and self.session.get("machine") != self.machine["machineId"]:
                raise GrokError("The selected machine differs from this conversation's saved machine.")
            return
        row = self.gateway.create("CLI · " + Path(self.cwd).name,
                                  "Coding agent for the Grok Bot terminal CLI. Use the user's selected machine and project.")
        self.session = {"id": row["id"], "cwd": self.cwd, "machine": self.machine["machineId"] if self.machine else None,
                        "machine_label": self.machine.get("label") if self.machine else "cloud",
                        "created": time.time(), "messages": [], "pending": None}
        store.save(self.session)

    def prompt(self, text, nonce):
        machine = self.session.get("machine")
        location = ("Work on the user's registered machine " + json.dumps(machine) + " using the native machine-targeted Shell tool. "
                    "Every filesystem read, edit, and command for this task must run on that machine, in " + json.dumps(self.cwd) + ". "
                    "The cloud computer /home/box is a different machine; do not create a substitute project there. "
                    "Read the project's AGENTS.md and preserve existing changes. "
                    "Use Grok Bot's native local execution policy and approval cards. Do not launch another AI CLI to do this task. "
                    if machine else "Work on your cloud computer. This session has no local machine access selected. ")
        return ("You are responding through a terminal coding agent interface. " + location
                + "Carry out the user's task, verify results, and explain the changes. "
                "When this specific request is finished or needs user input, end your final user-facing message with the exact hidden marker "
                + marker(nonce) + ". Include the marker only in your final message, never in progress or tool calls.\n\nUser request:\n" + text)

    def turn(self, text=None):
        self.ensure_session()
        with store.lease(self.session):
            # Read after taking the lease so another pane cannot leave an unseen turn.
            self.session = store.load(self.session["id"], self.cwd)
            if self.session.get("pending") and text:
                raise GrokError("A previous request is still pending. Resume it without a prompt before submitting more work.")
            if not self.session.get("pending"):
                if not text:
                    return
                remote = self.gateway.agent(self.session["id"])
                if not remote:
                    raise GrokError("This bot no longer exists. Start a new CLI session.")
                if remote.get("isRunningTurn") or remote.get("isRunning"):
                    raise GrokError("The bot is already working. Resume observation or stop it before submitting another task.")
                pending = {"nonce": request_id(), "text": text, "root": None, "started": time.time(),
                           "before": [e.get("id") for e in self.gateway.transcript(self.session["id"])], "shown": []}
                self.session["pending"] = pending
                store.save(self.session)  # A crash or unknown receipt must never replay a prompt.
                try:
                    pending["root"] = self.gateway.send(self.session["id"], self.prompt(text, pending["nonce"]),
                                                         pending["nonce"], machine=self.session.get("machine"))
                except GrokError as exc:
                    if exc.delivery != "unknown":
                        self.session["pending"] = None
                        store.save(self.session)
                        raise
                    # A lost receipt does not mean execution failed. Observe the
                    # exact nonce's final reply instead of replaying the request.
                    pending["delivery"] = "unknown"
                    self.output.event({"type": "notice", "text": "Receipt not confirmed; checking the existing request without resending."})
                store.save(self.session)
            self.output.event({"type": "system", "subtype": "init", "session_id": self.session["id"],
                               "model": "provider-managed", "cwd": self.cwd})
            self.report("working")
            try:
                self.observe()
            finally:
                self.report("idle")

    def observe(self):
        pending = self.session["pending"]
        deadline = time.monotonic() + self.timeout
        shown = set(pending.get("shown", []))
        done, final_text, settled = False, "", 0
        while time.monotonic() < deadline:
            entries = self.gateway.transcript(self.session["id"])
            accepted = next((e for e in entries if e.get("clientNonce") == pending["nonce"]
                             and e.get("role") == "user"), None)
            if accepted:
                pending["root"] = accepted.get("id")
                pending["request_id"] = accepted.get("requestId")
                pending["delivery"] = "accepted"
            ids = [e.get("id") for e in entries]
            root = pending.get("root")
            # The receipt anchor must still be covered. Never mistake old messages
            # or another turn's response for this request after a transcript gap.
            if root and root not in ids:
                if any(marker(pending["nonce"]) in entry_text(e) and e.get("kind") == "send-message" for e in entries):
                    pass  # the request-specific final marker still proves correlation
                elif entries and not set(pending.get("before", [])).intersection(ids):
                    raise GrokError("Transcript coverage was lost. Inspect the bot in Grok Bot; the pending request has been preserved.")
            start = ids.index(root) + 1 if root in ids else 0
            for entry in entries[start:]:
                ident = entry.get("id")
                if not ident or ident in pending.get("before", []):
                    continue
                if not root and marker(pending["nonce"]) not in entry_text(entry):
                    continue  # Wait for the nonce anchor before displaying or approving anything.
                if pending.get("request_id") and entry.get("requestId") != pending["request_id"]:
                    continue
                kind, message = entry.get("kind"), entry.get("message") or {}
                if kind == "send-message" and isinstance(message, dict):
                    approval = message.get("approval") if message.get("type") == "auto-review-approval" else message.get("ask")
                    if isinstance(approval, dict) and approval.get("status") == "pending":
                        approval_id = str(ident) + ":" + str(approval.get("requestId"))
                        if approval_id not in pending.get("approvals", []):
                            self.approve(entry, approval)
                            pending.setdefault("approvals", []).append(approval_id)
                            store.save(self.session)
                        continue
                text = entry_text(entry)
                if kind == "send-message" and marker(pending["nonce"]) in text:
                    done, final_text = True, text.replace(marker(pending["nonce"]), "").strip()
                if ident in shown:
                    continue
                if kind == "send-message" and text and (not isinstance(message, dict) or message.get("type") in (None, "text")):
                    self.output.text(text.replace(marker(pending["nonce"]), "").strip())
                elif kind in ("tool-call", "tool-result"):
                    label = entry.get("toolName") or entry.get("name") or entry.get("tool") or kind
                    self.output.event({"type": "tool_call", "subtype": "started" if kind == "tool-call" else "completed",
                                       "label": str(label), "entry_id": ident})
                shown.add(ident)
            pending["shown"] = list(shown)
            store.save(self.session)
            remote = self.gateway.agent(self.session["id"])
            if remote is None:
                raise GrokError("The bot disappeared while this request was running.")
            idle = remote.get("isRunningTurn") is False and not remote.get("isRunning") and not remote.get("isComposingMessage")
            settled = settled + 1 if done and idle else 0
            if settled >= 2:
                self.session["messages"].append({"user": pending["text"], "assistant": final_text, "at": time.time()})
                self.session["messages"] = self.session["messages"][-100:]
                self.session["pending"] = None
                store.save(self.session)
                self.output.answer = final_text
                self.output.result(self.session)
                return
            time.sleep(self.poll)
        raise GrokError("Still waiting for a correlated final reply. The request is saved; use grokbot --continue to keep watching.")

    def approve(self, entry, card):
        details = {key: card[key] for key in ("command", "action", "reason", "workingDirectory", "machineId") if key in card}
        self.output.event({"type": "notice", "text": "Approval required: " + json.dumps(details, ensure_ascii=False)})
        if not self.output.interactive:
            raise GrokError("Approval required in Grok Bot. The pending request is saved; resume after deciding in the app.")
        self.report("blocked")
        answer = input("Allow this action once? [y/N] ").strip().lower()
        # Refresh to avoid acting on a stale card.
        fresh = next((e for e in self.gateway.transcript(self.session["id"]) if e.get("id") == entry["id"]), None)
        if fresh:
            self.gateway.approval(self.session["id"], fresh, answer == "y")
        self.report("working")

    def stop(self):
        if not self.session:
            return
        with store.lease(self.session):
            self.session = store.load(self.session["id"], self.cwd)
            row = self.gateway.agent(self.session["id"])
            if not self.session.get("pending") and not (row and (row.get("isRunningTurn") or row.get("isRunning"))):
                return
            self.gateway.interrupt(self.session["id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                row = self.gateway.agent(self.session["id"])
                if row and row.get("isRunningTurn") is False and not row.get("isRunning"):
                    pending = self.session.get("pending")
                    if pending:
                        self.session["messages"].append({"user": pending["text"], "assistant": self.output.answer,
                                                         "at": time.time(), "status": "interrupted"})
                    self.session["pending"] = None
                    store.save(self.session)
                    self.output.event({"type": "notice", "text": "Bot interrupted. Earlier file changes are preserved."})
                    return
                time.sleep(0.5)
            self.output.event({"type": "notice", "text": "Stop requested; the bot has not confirmed it yet. Resume to inspect the saved turn."})


def usage():
    import agent_picker as ap
    windows = ap.usage_cursor()
    if not windows:
        raise GrokError("No current usage reading. Check your Cursor login.")
    names = {"cr": "Cursor models", "ex": "External models", "gb": "Grok Bot weekly"}
    for window in windows:
        print(f"{names.get(window['label'], window['label']):18} {max(0, 100 - window['pct']):5.1f}% left  resets in {ap.until(window.get('resets'))}")
    return windows


def list_sessions(cwd):
    rows = store.sessions(str(cwd))
    for row in rows:
        pending = " · pending" if row.get("pending") else ""
        print(f"{row['id']}  {clean(row.get('machine_label', 'cloud'))}{pending}")
    if not rows:
        print("No saved conversations in this project.")


def banner(cwd, session=None):
    color = "\x1b[38;2;255;160;165m" if sys.stdout.isatty() else ""
    reset = "\x1b[0m" if color else ""
    print("\n  " + color + "Grok Bot Agent" + reset + "\n  v" + VERSION)
    print("  " + clean(str(cwd)))
    if session:
        print("  Session " + session["id"])
    print("  Cursor Grok Bot usage · /help for commands\n")


def parser():
    p = argparse.ArgumentParser(prog="grokbot", description="Run agentic tasks using your Grok Bot usage, in a Cursor-style terminal.")
    p.add_argument("prompt", nargs="*", help="initial task")
    p.add_argument("-v", "--version", action="version", version="grokbot " + VERSION)
    p.add_argument("-p", "--print", action="store_true", dest="printing", help="run one task and exit")
    p.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text")
    p.add_argument("--stream-partial-output", action="store_true", help="emit messages as they arrive in stream-json mode")
    p.add_argument("--workspace", type=Path, default=Path.cwd(), help="local project directory")
    p.add_argument("--resume", nargs="?", const="latest", help="resume a saved project session; a pending task is observed without resending")
    p.add_argument("-c", "--continue", action="store_true", dest="continuing", help="resume the last project conversation")
    p.add_argument("--machine", help="registered machine ID or exact label")
    p.add_argument("--cloud", action="store_true", help="work on the bot's cloud computer instead of this Mac")
    p.add_argument("--model", choices=("auto",), default="auto", help="Grok Bot manages model selection")
    p.add_argument("--timeout", type=float, default=1800, help="seconds to observe a turn before leaving it saved")
    return p


def main(argv=None):
    args_in = list(sys.argv[1:] if argv is None else argv)
    command = args_in.pop(0) if args_in and args_in[0] in ("status", "whoami", "machines", "usage", "ls", "resume", "agent") else None
    args = parser().parse_args(args_in)
    cwd = args.workspace.expanduser().resolve()
    if not cwd.is_dir():
        parser().error("--workspace must be an existing directory")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser().error("--timeout must be finite and positive")
    if args.cloud and args.machine:
        parser().error("--cloud and --machine cannot be combined")
    if args.output_format != "text" and not args.printing:
        parser().error("--output-format requires --print")
    gateway, conversation = Gateway(), None
    output = Output(args.output_format, interactive=not args.printing and sys.stdin.isatty())
    try:
        if command == "usage":
            usage()
            return 0
        if command == "ls":
            list_sessions(cwd)
            return 0
        if command == "machines":
            for machine in gateway.machines():
                print(f"{machine['machineId']}  {clean(machine.get('label'))}  "
                      f"{'connected' if machine.get('connected') else 'offline'} · {machine.get('localToolPermission', 'ask')}")
            return 0
        if command in ("status", "whoami"):
            gateway.connect()
            machine = select_machine(gateway.machines(), args.machine)
            print("Grok Bot gateway: connected\nLocal machine: " + clean(machine.get("label"))
                  + "\nLocal execution policy: " + machine.get("localToolPermission", "ask"))
            return 0
        session = None
        if args.resume or args.continuing or command == "resume":
            ident = args.resume if args.resume not in (None, "latest") else None
            if command == "resume" and args.prompt:
                ident = args.prompt.pop(0)
            session = store.load(ident, str(cwd))
            if args.cloud and session.get("machine"):
                raise GrokError("This is a local-machine conversation. Resume without --cloud, or start a new cloud conversation.")
        machine = None if args.cloud or (session and not session.get("machine")) else select_machine(
            gateway.machines(), args.machine or (session or {}).get("machine"))
        conversation = Conversation(gateway, output, cwd, machine, session, args.timeout)
        prompt = " ".join(args.prompt).strip()
        if args.printing and not prompt and not (session and session.get("pending")) and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if args.printing:
            if not prompt and not (session and session.get("pending")):
                parser().error("--print requires a prompt or a pending session")
            conversation.turn(prompt or None)
            return 0
        if not sys.stdin.isatty():
            raise GrokError("Use --print for non-interactive tasks.")
        from grokbot_ui import CursorInput
        banner(cwd, session)
        editor = CursorInput(COMMANDS, history=[m["user"] for m in (session or {}).get("messages", [])],
                             image_dir=str(store.directory().parent / "attachments"))
        conversation.report("idle")
        if session and session.get("pending"):
            conversation.turn()
        while True:
            if not prompt:
                try:
                    prompt = editor.read(cwd=str(cwd), hint=lambda: ("Grok Bot · Auto", "Cloud" if machine is None
                                         else "Local · " + machine.get("localToolPermission", "ask")))
                except EOFError:
                    break
                if prompt is None:
                    break
            prompt = prompt.strip()
            if prompt.startswith("/"):
                verb, _, rest = prompt[1:].partition(" ")
                if verb in ("quit", "exit"):
                    break
                elif verb == "help":
                    print("\n".join("/" + name + (" " + arg if arg else "") + "  " + desc for name, arg, desc in COMMANDS))
                elif verb == "usage":
                    usage()
                elif verb == "sessions":
                    list_sessions(cwd)
                elif verb == "new":
                    conversation.session = None
                    print("New conversation. Your next task creates a separate bot.")
                elif verb == "resume":
                    resumed = store.load(rest.strip() or None, str(cwd))
                    machine = select_machine(gateway.machines(), resumed["machine"]) if resumed.get("machine") else None
                    conversation.session, conversation.machine = resumed, machine
                    if conversation.session.get("pending"):
                        conversation.turn()
                    print("Resumed " + conversation.session["id"])
                elif verb == "status":
                    print("Session: " + ((conversation.session or {}).get("id") or "new") + "\nWorkspace: " + str(cwd))
                elif verb == "stop":
                    conversation.stop()
                else:
                    print("Unknown command. Use /help.")
            elif prompt:
                output.answer = ""
                attachments = editor.attached(prompt)
                for number, path in editor.images.items():
                    prompt = prompt.replace("[Image #" + str(number) + "]", json.dumps(path))
                if attachments:
                    prompt += "\nAttached local file paths: " + json.dumps(attachments)
                try:
                    conversation.turn(prompt)
                except KeyboardInterrupt:
                    conversation.stop()
                except GrokError as exc:
                    print(clean(str(exc)), file=sys.stderr)
            prompt = ""
        return 0
    except KeyboardInterrupt:
        if conversation:
            try:
                conversation.stop()
            except GrokError:
                print("Could not confirm interruption. The bot may still be working; inspect it in Grok Bot.", file=sys.stderr)
        return 130
    except (GrokError, OSError) as exc:
        if args.output_format != "text":
            print(json.dumps({"type": "result", "is_error": True, "subtype": "error", "result": str(exc),
                              "session_id": ((conversation.session or {}) if conversation else {}).get("id")}))
        else:
            print("grokbot: " + clean(str(exc)), file=sys.stderr)
        return 1
    finally:
        if conversation:
            conversation.release()


if __name__ == "__main__":
    sys.exit(main())
