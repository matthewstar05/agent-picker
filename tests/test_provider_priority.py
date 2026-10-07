import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import provider_priority as priority

NOW = 1800000000
AGENTS = [{"name": "Cursor", "argv": ["cursor-agent", "--model", "grok-test"]},
          {"name": "Kiro", "argv": ["kiro-cli", "chat"]}]


def meter(name, used, days, now=NOW):
    return {"fetched": now, "windows": [{"label": "ex" if name == "Cursor" else "mo",
                                          "pct": used, "resets": now + days * priority.DAY}]}


class PriorityTests(unittest.TestCase):
    def choice(self, cursor_used=50, cursor_days=15, kiro_used=50, kiro_days=15):
        usage = {"Cursor": meter("Cursor", cursor_used, cursor_days),
                 "Kiro": meter("Kiro", kiro_used, kiro_days)}
        return priority.choose(AGENTS, usage, NOW)[0]

    def test_near_reset_overrides_larger_other_allowance(self):
        self.assertEqual(self.choice(99, 1, 0, 6), "Cursor")
        self.assertEqual(self.choice(0, 6, 99, 1), "Kiro")

    def test_earlier_reset_wins_when_both_are_near(self):
        self.assertEqual(self.choice(99, 2, 0, 3), "Cursor")
        self.assertEqual(self.choice(0, 3, 99, 2), "Kiro")

    def test_five_day_boundary_is_inclusive(self):
        self.assertEqual(self.choice(99, 5, 0, 5.1), "Cursor")

    def test_remaining_quota_per_day_guides_distant_resets(self):
        self.assertEqual(self.choice(8, 10, 0.2, 25), "Cursor")
        self.assertEqual(self.choice(90, 10, 10, 20), "Kiro")

    def test_exhaustion_does_not_get_priority_from_a_near_reset(self):
        self.assertEqual(self.choice(100, 1, 40, 20), "Kiro")
        self.assertEqual(self.choice(30, 20, 110, 1), "Cursor")

    def test_cursor_uses_only_the_selected_model_pool(self):
        usage = {"Cursor": meter("Cursor", 100, 1), "Kiro": meter("Kiro", 30, 20)}
        usage["Cursor"]["windows"].append({"label": "cr", "pct": 0, "resets": NOW + priority.DAY})
        self.assertEqual(priority.choose(AGENTS, usage, NOW)[0], "Kiro")
        auto = [{"name": "Cursor", "argv": ["cursor-agent", "--model=auto"]}, AGENTS[1]]
        self.assertEqual(priority.choose(auto, usage, NOW)[0], "Cursor")

    def test_unknown_quota_is_not_assumed_to_be_unused(self):
        self.assertEqual(priority.choose(AGENTS, {"Kiro": meter("Kiro", 90, 20)}, NOW)[0], "Kiro")
        self.assertEqual(priority.choose(AGENTS, {"Cursor": meter("Cursor", 100, 1)}, NOW)[0], "Kiro")
        self.assertEqual(priority.choose(AGENTS, {}, NOW)[0], "Cursor")

    def test_expired_stale_and_invalid_data_are_ignored(self):
        for entry in (None, [], {"fetched": NOW, "windows": None},
                      meter("Cursor", 1, -1), meter("Cursor", float("nan"), 1),
                      meter("Cursor", -1, 1),
                      {**meter("Cursor", 1, 3), "fetched": NOW - priority.DAY - 1},
                      {**meter("Cursor", 1, 3), "fetched": NOW + 60}):
            with self.subTest(entry=entry):
                usage = {"Cursor": entry, "Kiro": meter("Kiro", 40, 20)}
                self.assertEqual(priority.choose(AGENTS, usage, NOW)[0], "Kiro")

    def test_ties_are_deterministic_and_missing_providers_are_excluded(self):
        self.assertEqual(self.choice(), "Cursor")
        self.assertEqual(priority.choose([AGENTS[1]], {}, NOW)[0], "Kiro")
        with self.assertRaisesRegex(RuntimeError, "Neither Cursor nor Kiro"):
            priority.choose([], {}, NOW)

    def test_recent_cache_does_not_query_providers(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "usage.json"
            cache.write_text(json.dumps({n: meter(n, 10, 10, time.time()) for n in priority.PROVIDERS}))
            fetchers = {n: Mock(side_effect=AssertionError("unexpected read")) for n in priority.PROVIDERS}
            picker = SimpleNamespace(AGENTS=AGENTS, installed=lambda a: True, USAGE_CACHE=cache, FETCHERS=fetchers)
            self.assertEqual(priority.select(picker)[0], "Cursor")
            for reader in fetchers.values():
                reader.assert_not_called()

    def test_stale_reads_refresh_but_failure_keeps_valid_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "usage.json"
            cache.write_text(json.dumps({n: meter(n, 50, 20, time.time() - priority.FRESH - 1)
                                         for n in priority.PROVIDERS}))
            fetchers = {"Cursor": Mock(side_effect=OSError("offline")),
                        "Kiro": Mock(return_value=meter("Kiro", 10, 1, time.time())["windows"])}
            picker = SimpleNamespace(AGENTS=AGENTS, installed=lambda a: True, USAGE_CACHE=cache, FETCHERS=fetchers)
            self.assertEqual(priority.select(picker)[0], "Kiro")
            for reader in fetchers.values():
                reader.assert_called_once()

    def test_slow_readers_do_not_hold_up_launch(self):
        release = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            picker = SimpleNamespace(AGENTS=AGENTS, installed=lambda a: True,
                                     USAGE_CACHE=Path(folder) / "missing",
                                     FETCHERS={n: lambda: release.wait(2) and None for n in priority.PROVIDERS})
            try:
                start = time.monotonic()
                with patch.object(priority, "REFRESH_WAIT", 0.02):
                    self.assertEqual(priority.select(picker)[0], "Cursor")
                self.assertLess(time.monotonic() - start, 1)
            finally:
                release.set()

    def test_corrupt_cache_and_uninstalled_candidate_fall_back(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "usage.json"
            fetchers = {n: Mock(return_value=None) for n in priority.PROVIDERS}
            picker = SimpleNamespace(AGENTS=AGENTS, installed=lambda a: a["name"] == "Kiro",
                                     USAGE_CACHE=cache, FETCHERS=fetchers)
            for raw in ("broken json", "[]", "null"):
                cache.write_text(raw)
                self.assertEqual(priority.select(picker)[0], "Kiro")
            fetchers["Cursor"].assert_not_called()

    def test_priority_launch_uses_vendor_args_cwd_and_exit_status(self):
        with tempfile.TemporaryDirectory(prefix="picker priority ") as folder:
            folder = Path(folder)
            script, output = folder / "fake.py", folder / "out.json"
            script.write_text("import json,os,sys\nfrom pathlib import Path\n"
                              "Path(sys.argv[1]).write_text(json.dumps({'args':sys.argv[2:], 'cwd':os.getcwd()}))\n"
                              "sys.exit(7)\n")
            config, state = folder / "config.json", folder / "state"
            state.mkdir()
            config.write_text(json.dumps({"agents": {n: {"argv": [sys.executable, str(script), str(output), n]}
                                                       for n in priority.PROVIDERS}}))
            (state / "usage.json").write_text(json.dumps({"Cursor": meter("Cursor", 20, 20, time.time()),
                                                         "Kiro": meter("Kiro", 20, 1, time.time())}))
            env = dict(os.environ, AGENT_PICKER_CONFIG=str(config), AGENT_PICKER_STATE_HOME=str(state),
                       AGENT_PICKER_NO_PRETRUST="1")
            cmd = [sys.executable, str(ROOT / "src/agent_picker.py")]
            info = subprocess.run(cmd + ["--picker-priority"], env=env, cwd=folder,
                                  capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(info.returncode, 0, info.stderr)
            self.assertIn("workbench chose Kiro", info.stdout)
            self.assertFalse(output.exists())
            launched = subprocess.run(cmd + ["--picker-launch-priority", "--", "hello world", "--flag"],
                                      env=env, cwd=folder, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(launched.returncode, 7, launched.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(data["args"], ["Kiro", "hello world", "--flag"])
            self.assertEqual(Path(data["cwd"]).resolve(), folder.resolve())
            self.assertEqual((state / "last").read_text(), "Kiro")


if __name__ == "__main__":
    unittest.main()
