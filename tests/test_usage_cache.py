import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap
import starcli
import usage_cache
import usage_panel as panel


class UsageCacheTests(unittest.TestCase):
    def test_concurrent_panes_keep_each_others_freshest_readings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.json"
            script = (
                "import sys, time; from pathlib import Path; "
                f"sys.path.insert(0, {str(ROOT / 'src')!r}); import usage_cache\n"
                "path, name = Path(sys.argv[1]), sys.argv[2]\n"
                "deadline = time.monotonic() + 10\n"
                "while not path.with_suffix('.go').exists():\n"
                "    if time.monotonic() > deadline: raise RuntimeError('start gate timed out')\n"
                "    time.sleep(0.01)\n"
                "for n in range(20):\n"
                "    usage_cache.write(path, {name: {'fetched': 1000 + n, 'windows': [{'pct': n}]}})\n"
                "# A pane holding a stale snapshot must not undo a newer reading.\n"
                "usage_cache.write(path, {name: {'fetched': 900, 'windows': [{'pct': 0}]}})\n"
            )
            processes = []
            try:
                for name in ("Claude", "Codex", "Cursor", "Kiro"):
                    processes.append(subprocess.Popen([sys.executable, "-c", script, str(path), name],
                                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                path.with_suffix(".go").touch()
                for proc in processes:
                    stdout, stderr = proc.communicate(timeout=20)
                    self.assertEqual(proc.returncode, 0, stdout + stderr)
                data = usage_cache.read(path)
                self.assertEqual(set(data), {"Claude", "Codex", "Cursor", "Kiro"})
                self.assertTrue(all(entry["fetched"] == 1019 and entry["windows"][0]["pct"] == 19
                                    for entry in data.values()))
                self.assertFalse(list(Path(folder).glob("*.tmp")))
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate(timeout=5)

    def test_picker_panel_and_star_share_fresh_readings_without_rolling_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.json"
            usage_cache.write(path, {"Codex": {"fetched": 200, "windows": [{"label": "5h", "pct": 95}]},
                                     "Claude": {"fetched": 200, "windows": [{"label": "wk", "pct": 55}]}})
            memory = {"Codex": {"fetched": 100, "windows": [{"label": "5h", "pct": 10}]}}
            with patch.object(ap, "USAGE_CACHE", path), patch.object(ap, "USAGE", memory), \
                    patch.object(panel, "USAGE", memory):
                ap.load_usage()
                self.assertEqual(memory["Codex"]["windows"][0]["pct"], 95)
                starcli.save_meter("Claude", [{"label": "5h", "pct": 80}])
                self.assertEqual({w["label"] for w in memory["Claude"]["windows"]}, {"5h", "wk"})
                panel.merge_cache()
                self.assertEqual(usage_cache.read(path), memory)
                usage_cache.write(path, {"Codex": {"fetched": 50, "windows": []}})
                ap.load_usage()
                self.assertEqual(memory["Codex"]["fetched"], 200)

    def test_bad_cache_is_replaced_without_losing_valid_local_readings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.json"
            for broken in ("broken", "[]", '{"Codex": null}'):
                path.write_text(broken)
                self.assertEqual(usage_cache.read(path), {})
                kept = {"Claude": {"fetched": 100, "windows": []}}
                self.assertEqual(usage_cache.write(path, kept), kept)
                self.assertEqual(json.loads(path.read_text()), kept)
