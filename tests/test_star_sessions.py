import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import star_sessions
import starcli


class ConversationLeaseTests(unittest.TestCase):

    def test_concurrent_mesh_startups_claim_distinct_names_and_release_after_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            script = ("import sys; sys.path.insert(0, " + repr(str(ROOT / "src")) + "); "
                      "import star_mesh; member = star_mesh.Member(sys.argv[1] or None); "
                      "print(member.name, flush=True); sys.stdin.readline(); member.leave()")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
            env["AGENT_PICKER_STATE_HOME"] = folder
            procs = []
            def launch(name=""):
                proc = subprocess.Popen([sys.executable, "-c", script, name], env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True)
                procs.append(proc)
                return proc
            try:
                for _ in range(6):
                    launch()
                names = [proc.stdout.readline().strip() for proc in procs]
                self.assertEqual(set(names), {"star" + str(n) for n in range(1, 7)})
                first = procs[0]
                first.kill()
                first.wait(timeout=5)
                replacement = launch(names[0])
                self.assertEqual(replacement.stdout.readline().strip(), names[0])
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.stdin.write("quit\n")
                        proc.stdin.flush()
                    proc.wait(timeout=5)
                    proc.stdin.close()
                    proc.stdout.close()
                    proc.stderr.close()

    def test_failed_switch_keeps_the_original_lock(self):
        with tempfile.TemporaryDirectory() as folder, star_sessions.Lease(folder) as first, \
                star_sessions.Lease(folder) as second, star_sessions.Lease(folder) as third:
            first.claim("first")
            second.claim("second")
            with self.assertRaises(star_sessions.ConversationBusy):
                first.claim("second")
            self.assertEqual(first.ident, "first")
            with self.assertRaises(star_sessions.ConversationBusy):
                third.claim("first")
            first.claim("third")
            third.claim("first")
            self.assertEqual(third.ident, "first")

    def test_a_crashed_process_releases_its_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            script = ("import sys; sys.path.insert(0, " + repr(str(ROOT / "src")) + "); import star_sessions; "
                      "lease = star_sessions.Lease(sys.argv[1]); lease.claim('shared'); "
                      "print('ready', flush=True); sys.stdin.read()")
            proc = subprocess.Popen([sys.executable, "-c", script, folder],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                with star_sessions.Lease(folder) as contender:
                    with self.assertRaises(star_sessions.ConversationBusy):
                        contender.claim("shared")
                    proc.kill()
                    proc.wait(timeout=5)
                    contender.claim("shared")
                    self.assertEqual(contender.ident, "shared")
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()

    def test_missing_explicit_resume_never_launches_a_fresh_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "picker.json"
            config.write_text(json.dumps({"agents": {}}))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "STAR_"))}
            env.update(AGENT_PICKER_CONFIG=str(config), AGENT_PICKER_STATE_HOME=str(root / "state"))
            for args in (["--resume-id", "missing"], ["-c"], ["--resume-id", "../outside"]):
                with self.subTest(args=args):
                    proc = subprocess.run([sys.executable, str(ROOT / "src/starcli.py"), *args, "-p", "do work"],
                                          env=env, input="", capture_output=True, text=True, timeout=10)
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn("saved conversation is missing or invalid", proc.stderr)
                    self.assertFalse((root / "state/star/conversations").exists())

    def test_resume_rejects_mismatched_or_corrupt_saved_identity(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(starcli, "CONVERSATIONS", Path(folder)):
            path = Path(folder) / "wanted.json"
            for data in ({"id": "different"}, [], {"id": "wanted", "turns": [None]}, None,
                         {"id": "wanted", "sessions": []}, {"id": "wanted", "seen": {"Claude": "bad"}},
                         {"id": "wanted", "inflight": {"provider": "Claude", "prompt": 123}}):
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    self.assertIsNone(starcli.Conversation.load("wanted"))

    def test_resume_list_skips_corrupt_files_without_hiding_valid_older_sessions(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(starcli, "CONVERSATIONS", Path(folder)), \
                patch.object(starcli, "LAST", Path(folder) / "last"):
            valid = starcli.Conversation({"cwd": folder})
            valid.notes.append("keep this valid session")
            valid.save()
            for number, data in enumerate(([], {"id": "wrong"}, {"sessions": []})):
                (Path(folder) / ("bad" + str(number) + ".json")).write_text(json.dumps(data))
            self.assertEqual([entry[0] for entry in starcli.saved_conversations(1)], [valid.id])

    def test_binding_a_saved_member_restores_routing_before_saving_its_identity(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(starcli, "CONVERSATIONS", Path(folder)), \
                patch.object(starcli, "LAST", Path(folder) / "last"), patch.object(starcli, "report_herdr"):
            routing = {"pinned": "Claude", "tier": "heavy", "plan": True}
            convo = starcli.Conversation({"cwd": folder, "goal": "saved objective", "routing": routing})
            star = starcli.Star(convo, plan=False, member=Mock())
            self.assertEqual((star.pinned, star.tier, star.plan), ("Claude", "heavy", True))
            self.assertEqual(starcli.Conversation.load(convo.id).routing, routing)

    def test_resume_reads_the_latest_state_only_after_owning_the_conversation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(starcli, "CONVERSATIONS", Path(folder)), \
                patch.object(starcli, "LAST", Path(folder) / "last"):
            original = starcli.Conversation({"cwd": folder})
            original.notes.append("initial")
            original.save()
            stale = starcli.Conversation.load(original.id)
            original.notes.append("saved just before the prior writer closed")
            original.save()
            with star_sessions.Lease(Path(folder) / "locks") as lease:
                star = starcli.Star(stale, lease=lease)
                self.assertIn("saved just before the prior writer closed", star.convo.notes)
                original_path = Path(folder) / (original.id + ".json")
                original_path.unlink()
                with self.assertRaisesRegex(star_sessions.ConversationBusy, "disappeared"):
                    star.bind(stale)
                self.assertEqual(lease.ident, original.id)
