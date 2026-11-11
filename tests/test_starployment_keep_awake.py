import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import keep_awake
import preferences


class StarploymentKeepAwakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def database(self, name="jobhub.db", mode="all_plus", setup=True):
        folder = self.root / Path(name).parent
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / Path(name).name
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        connection.executemany("INSERT INTO settings VALUES (?, ?)", [
            ("jarvis_mode", json.dumps(mode)),
            ("setup_complete", json.dumps(setup)),
            ("unrelated_secret", json.dumps("must not be selected")),
        ])
        connection.commit()
        connection.close()
        return path

    def process(self, comm="/Applications/Starployment.app/Contents/MacOS/Starployment", pid=123):
        class Done:
            returncode = 0
            stdout = f"{pid} {comm}\n"
        return Done()

    def run_detection(self, db_path=None, comm=None, ps=None):
        db_path = db_path or self.database()
        comm = comm or "/Applications/Starployment.app/Contents/MacOS/Starployment"

        def run(args, **kwargs):
            if args[:2] == ["ps", "-axo"]:
                return ps if ps is not None else self.process(comm)
            if args[0] == "lsof":
                class Done:
                    returncode = 0
                    stdout = "p123\nftxt\nn/Applications/Starployment.app/Contents/MacOS/Starployment\nf42\nn" + str(db_path) + "\n"
                return Done()
            raise AssertionError(args)

        with patch.object(keep_awake, "SUPPORTED", True), \
                patch.object(keep_awake.subprocess, "run", side_effect=run):
            return keep_awake._starployment_working()

    def test_active_all_plus_counts_without_cli_agents(self):
        self.assertEqual(self.run_detection(), ["Starployment JARVIS (pid 123)"])

    def test_off_or_setup_incomplete_does_not_count(self):
        self.assertEqual(self.run_detection(self.database("off/jobhub.db", mode="off")), [])
        self.assertEqual(self.run_detection(self.database("incomplete/jobhub.db", setup=False)), [])

    def test_invalid_mode_value_is_not_work(self):
        self.assertEqual(self.run_detection(self.database("invalid/jobhub.db", mode=[])), [])

    def test_stopped_process_does_not_count(self):
        class Empty:
            returncode = 0
            stdout = ""
        self.assertEqual(self.run_detection(ps=Empty()), [])

    def test_detection_is_skipped_off_macos(self):
        with patch.object(keep_awake, "SUPPORTED", False), \
                patch.object(keep_awake.subprocess, "run") as run:
            self.assertEqual(keep_awake._starployment_working(), [])
            run.assert_not_called()

    def test_missing_open_database_and_bad_database_fail_gracefully(self):
        with patch.object(keep_awake, "SUPPORTED", True), \
                patch.object(keep_awake.subprocess, "run", side_effect=[self.process(), OSError("lsof failed")]):
            self.assertEqual(keep_awake._starployment_working(), [])
        bad = self.root / "jobhub.db"
        bad.write_text("not sqlite")
        self.assertEqual(self.run_detection(bad), [])
        with patch.object(keep_awake, "SUPPORTED", True), \
                patch.object(keep_awake.subprocess, "run", side_effect=[self.process(), type("Done", (), {
                    "returncode": 0, "stdout": "p123\nf42\nn" + str(bad) + "\n"})()]), \
                patch.object(keep_awake.sqlite3, "connect", side_effect=PermissionError("unreadable")):
            self.assertEqual(keep_awake._starployment_working(), [])

    def test_legacy_jobhub_process_uses_its_actual_open_database(self):
        database = self.database("legacy-live/jobhub.db", mode="new")
        result = self.run_detection(database, "/Applications/JobHub.app/Contents/MacOS/JobHub")
        self.assertEqual(result, ["JobHub JARVIS (pid 123)"])
        stale = self.database("stale/jobhub.db", mode="off")
        self.assertEqual(self.run_detection(stale, "/Applications/JobHub.app/Contents/MacOS/JobHub"), [])

    def test_tick_keeps_session_when_only_starployment_is_active(self):
        database = self.database()
        amp = {"active": False, "commands": []}

        def tell(command):
            amp["commands"].append(command)
            if command == "session is active":
                return "true" if amp["active"] else "false"
            if command == "session time remaining":
                return "0"
            if command == "session is Trigger":
                return "false"
            if command.startswith("start new session"):
                amp["active"] = True
                return ""
            if command == "closed display mode enabled":
                return "true"
            if command == "enable closed display mode":
                return ""
            if command == "end session":
                amp["active"] = False
                return ""
            raise AssertionError(command)

        with patch.object(preferences, "PATH", self.root / "preferences.json"), \
                patch.object(preferences, "_values", {}), \
                patch.object(keep_awake, "STATE", self.root / "keep-awake.json"), \
                patch.object(keep_awake, "LOCK", self.root / "keep-awake.lock"), \
                patch.object(keep_awake, "installed", return_value=True), \
                patch.object(keep_awake, "state_dir", return_value=self.root), \
                patch.object(keep_awake, "_scheduled_running", return_value=[]), \
                patch.object(keep_awake, "_herdr_working", return_value=[]), \
                patch.object(keep_awake, "_live_turns", return_value=None), \
                patch.object(keep_awake, "_tell", side_effect=tell), \
                patch.object(keep_awake, "ensure_service"), \
                patch.object(keep_awake, "SUPPORTED", True), \
                patch.object(keep_awake.subprocess, "run", side_effect=lambda args, **kwargs:
                             self.process() if args[:2] == ["ps", "-axo"] else type("Done", (), {
                                 "returncode": 0,
                                 "stdout": "p123\nf42\nn" + str(database) + "\n"})()):
            preferences.put(keep_awake.KEEP_AWAKE.key, True)
            state = keep_awake.tick(now=1000, force=True)
        self.assertTrue(state["owned"])
        self.assertIn("Starployment JARVIS (pid 123)", state["reason"])
        self.assertTrue(amp["active"])


if __name__ == "__main__":
    unittest.main()
