import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from copy import deepcopy
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap
import platform_runtime
import preferences
import prompt_scheduler as scheduler
import scheduler_menu as ui
import usage_panel


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, AGENT_PICKER_STATE_HOME=str(self.home / "state"),
                              AGENT_PICKER_CONFIG=str(self.home / "config.json"))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pref = patch.object(preferences, "PATH", self.home / "preferences.json")
        self.pref.start()
        self.addCleanup(self.pref.stop)
        for key, value in (("_values", None), ("_stamp", None), ("_checked", 0)):
            p = patch.object(preferences, key, value)
            p.start()
            self.addCleanup(p.stop)

    def values(self, **changes):
        value = {"provider": "Codex", "prompt": "safe fake test", "cwd": str(self.home), "repeat": "once",
                 "anchor": "2030-01-02T09:30:00", "timezone": "America/Los_Angeles"}
        value.update(changes)
        return value

    def fake(self, name="codex", code=0, delay=0):
        path = self.home / name
        path.write_text(f'#!{sys.executable}\nimport json, os, sys, time\ntime.sleep({delay})\n'
                        'print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read(), '
                        '"nested": os.getenv("CLAUDECODE")}))\n' + f'sys.exit({code})\n')
        path.chmod(0o755)
        return path

    def profile(self, exe, provider="Codex", flags=None):
        name = "Antigravity" if provider == "Gemini" else provider
        (self.home / "config.json").write_text(json.dumps({"agents": {name: {"argv": [str(exe), *(flags or [])], "executables": []}}}))

    def due(self, **changes):
        job = scheduler.save_job(self.values(**changes), now=0)
        scheduler.mutate(lambda jobs: jobs[0].update(next_at=time.time() - 60))
        return job

    def test_private_atomic_store_and_completed_one_shot(self):
        self.profile(self.fake())
        job = self.due()
        self.assertEqual(scheduler.run_job(job["id"]), 0)
        done = scheduler.read()["jobs"][0]
        self.assertEqual(done["last"]["status"], "success")
        self.assertIsNone(done["next_at"])
        self.assertFalse(done["running"])
        self.assertEqual(scheduler.due_ids(), [])
        self.assertEqual(scheduler.run_job(job["id"]), 0)
        self.assertEqual(len(list(scheduler.directory().glob("*.log"))), 1)
        self.assertEqual(scheduler.schedule_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(scheduler.directory().stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(done["last"]["log"]).stat().st_mode & 0o777, 0o600)
        output = Path(done["last"]["log"]).read_text()
        self.assertIn('"stdin": ""', output)
        self.assertIn(str(self.home), output)

    def test_fake_providers_preserve_flags_and_literal_prompt(self):
        prompt = '-literal; $(touch DONT_CREATE) `touch NEITHER`\nsecond line café'
        for provider in scheduler.PROVIDERS:
            with self.subTest(provider=provider):
                exe = self.fake(name=provider.lower())
                flags = ["--model", "test", "--custom-permission"]
                self.profile(exe, provider, flags)
                job = self.due(provider=provider, prompt=prompt)
                with patch.dict(os.environ, CLAUDECODE="nested"):
                    self.assertEqual(scheduler.run_job(job["id"]), 0)
                done = scheduler.read()["jobs"][-1]
                data = json.loads(Path(done["last"]["log"]).read_text().splitlines()[-1])
                argv = data["argv"]
                self.assertEqual(argv[-1], prompt)
                self.assertIn("--custom-permission", argv)
                self.assertIsNone(data["nested"])
                self.assertFalse((self.home / "DONT_CREATE").exists())
                self.assertFalse((self.home / "NEITHER").exists())
                if provider == "Codex":
                    self.assertEqual(argv[0], "exec")
                    self.assertEqual(argv[-2], "--")
                else:
                    self.assertEqual(argv[-2], "-p")
                scheduler.mutate(lambda jobs: jobs.clear())

    def test_failed_cli_is_recorded_without_retry(self):
        self.profile(self.fake(code=7))
        job = self.due()
        self.assertEqual(scheduler.run_job(job["id"]), 7)
        done = scheduler.read()["jobs"][0]
        self.assertEqual(done["last"]["exit_code"], 7)
        self.assertEqual(done["last"]["status"], "failed")
        self.assertFalse(done["running"])
        self.assertEqual(scheduler.due_ids(), [])

    def test_gemini_keeps_antigravity_prompt_preparation(self):
        self.profile(self.fake(name="agy"), "Gemini")
        with patch("agy_prompts.prepare", side_effect=lambda agent, argv: argv + ["--agent", "prepared"]) as prepare:
            argv = scheduler.command(self.values(provider="Gemini"))
        self.assertIn("prepared", argv)
        prepare.assert_called_once()

    def test_sleep_catchup_repeats_once_and_skips_backlog(self):
        self.profile(self.fake())
        job = self.due(repeat="daily", anchor="2020-01-31T09:30:00")
        self.assertEqual(scheduler.run_job(job["id"]), 0)
        done = scheduler.read()["jobs"][0]
        self.assertGreater(done["next_at"], time.time())
        self.assertLess(done["next_at"], time.time() + 90000)
        self.assertEqual(scheduler.due_ids(), [])
        self.assertEqual(len(list(scheduler.directory().glob("*.log"))), 1)

    def test_repeats_keep_wall_clock_dst_month_and_weekdays(self):
        def local(value):
            return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        for repeat, anchor, after, expected in (
            ("daily", "2026-03-07T09:30:00", "2026-03-07T10:00:00", "2026-03-08T09:30:00"),
            ("daily", "2026-03-07T02:30:00", "2026-03-07T10:00:00", "2026-03-09T02:30:00"),
            ("daily", "2026-10-31T09:30:00", "2026-10-31T10:00:00", "2026-11-01T09:30:00"),
            ("weekdays", "2026-10-16T09:30:00", "2026-10-16T10:00:00", "2026-10-19T09:30:00"),
            ("weekly", "2026-10-16T09:30:00", "2026-10-17T10:00:00", "2026-10-23T09:30:00"),
            ("monthly", "2026-01-31T09:30:00", "2026-02-01T10:00:00", "2026-02-28T09:30:00"),
            ("monthly", "2026-01-31T09:30:00", "2026-02-28T10:00:00", "2026-03-31T09:30:00"),
        ):
            with self.subTest(repeat=repeat, after=after):
                value = scheduler.next_occurrence(self.values(repeat=repeat, anchor=anchor), local(after).timestamp())
                self.assertEqual(datetime.fromtimestamp(value, ZoneInfo("America/Los_Angeles")), local(expected))
        value = scheduler.next_occurrence(self.values(repeat="hourly"), local("2030-01-02T11:47:00").timestamp())
        self.assertEqual(datetime.fromtimestamp(value, ZoneInfo("America/Los_Angeles")).strftime("%H:%M"), "12:30")

    def test_bad_time_past_date_or_directory_does_not_save(self):
        for changes in ({"prompt": " "}, {"cwd": "/missing/scheduler/test"}, {"provider": "bad"},
                        {"anchor": "2026-03-08T02:30:00"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scheduler.save_job(self.values(**changes), now=0)
        with self.assertRaisesRegex(ValueError, "future"):
            scheduler.save_job(self.values(), now=9999999999)
        self.assertEqual(scheduler.read()["jobs"], [])
        for value, expected in (("1430", "14:30"), ("2:30 pm", "14:30"), ("12 AM", "00:00"), ("23:59", "23:59")):
            self.assertEqual(scheduler.parse_time(value).strftime("%H:%M"), expected)
        for value in ("25:00", "garbage", "99"):
            with self.assertRaises(ValueError):
                scheduler.parse_time(value)

    def test_corrupt_data_is_preserved(self):
        scheduler.schedule_path().write_text('{"version": 1, "jobs": [{}]}')
        with self.assertRaises(ValueError):
            scheduler.save_job(self.values(), now=0)
        self.assertEqual(scheduler.schedule_path().read_text(), '{"version": 1, "jobs": [{}]}')

    def test_pause_edit_delete_and_no_lost_changes(self):
        a = scheduler.save_job(self.values(prompt="first"), now=0)
        b = scheduler.save_job(self.values(prompt="second"), now=0)
        scheduler.manage(a["id"], "toggle")
        scheduler.save_job(self.values(prompt="edited"), a["id"], now=0)
        jobs = scheduler.read()["jobs"]
        self.assertFalse(jobs[0]["enabled"])
        self.assertEqual(jobs[1]["prompt"], "second")
        scheduler.manage(b["id"], "delete")
        self.assertEqual(len(scheduler.read()["jobs"]), 1)
        scheduler.mutate(lambda jobs: jobs[0].update(running=True))
        for action in ("edit", "delete"):
            with self.assertRaises(ValueError):
                if action == "edit":
                    scheduler.save_job(self.values(), a["id"], now=0)
                else:
                    scheduler.manage(a["id"], action)

    def test_worker_lock_prevents_duplicate_and_recovery_of_live_job(self):
        job = self.due()
        with scheduler.lock(f"run-{job['id']}.lock"):
            self.assertEqual(scheduler.run_job(job["id"]), 0)
            self.assertIsNone(scheduler.read()["jobs"][0]["last"])
            scheduler.mutate(lambda jobs: jobs[0].update(running=True, last={"status": "running"}))
            scheduler.recover()
            self.assertTrue(scheduler.read()["jobs"][0]["running"])
        scheduler.recover()
        done = scheduler.read()["jobs"][0]
        self.assertEqual(done["last"]["status"], "interrupted")
        self.assertFalse(done["enabled"])
        self.assertEqual(scheduler.due_ids(), [])

    def test_parallel_workers_claim_one_occurrence(self):
        self.profile(self.fake(delay=0.2))
        job = self.due()
        processes = [subprocess.Popen([sys.executable, str(ROOT / "src/prompt_scheduler.py"), "--run", job["id"]],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(3)]
        for process in processes:
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
        self.assertEqual(len(list(scheduler.directory().glob("*.log"))), 1)
        self.assertEqual(scheduler.read()["jobs"][0]["last"]["status"], "success")

    def test_daemon_dispatches_due_jobs_and_stops_cleanly(self):
        self.profile(self.fake())
        job = self.due()
        proc = subprocess.Popen([sys.executable, str(ROOT / "src/prompt_scheduler.py"), "--daemon"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                row = scheduler.read()["jobs"][0]
                if (row.get("last") or {}).get("status") == "success":
                    break
                time.sleep(0.05)
            else:
                self.fail("daemon did not dispatch the fake due prompt")
            self.assertEqual(row["id"], job["id"])
            self.assertEqual(len(list(scheduler.directory().glob("*.log"))), 1)
        finally:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)

    def test_service_definition_is_local_and_idempotent(self):
        path = self.home / "LaunchAgents/scheduler.plist"
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(scheduler, "plist_path", return_value=path), \
             patch.object(scheduler, "service_status", side_effect=[False, True]), \
             patch.object(scheduler.subprocess, "run", side_effect=run) as call:
            scheduler.install_service()
            scheduler.install_service()
        self.assertEqual(call.call_count, 1)
        data = scheduler.plistlib.loads(path.read_bytes())
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(data["ProgramArguments"][-1], "--daemon")
        self.assertEqual(data["ProcessType"], "Background")
        self.assertNotIn("ANTHROPIC_API_KEY", data["EnvironmentVariables"])

    def menu(self):
        with patch.object(scheduler, "service_status", return_value=True):
            return ui.Menu(cwd=self.home)

    def test_ui_tabs_calendar_time_prompt_repeat_save_and_delete(self):
        menu = self.menu()
        self.assertEqual(menu.name, "Claude")
        menu.handle("\t")
        self.assertEqual(menu.name, "Codex")
        menu.handle("n")
        menu.handle("\r")
        menu.handle("paste:first line\nsecond café")
        self.assertEqual(menu.mode, "text")
        menu.handle("\r")
        self.assertEqual(menu.draft["prompt"], "first line\nsecond café")
        menu.field = 2
        menu.handle("\r")
        menu.day = datetime(2030, 1, 31).date()
        menu.handle("]")
        self.assertEqual(str(menu.day), "2030-02-28")
        menu.handle("right")
        menu.handle("\r")
        self.assertTrue(menu.draft["anchor"].startswith("2030-03-01"))
        menu.field = 3
        menu.handle("\r")
        menu.handle("\x15")
        for key in "2:30 PM":
            menu.handle(key)
        menu.handle("up")
        menu.handle("\r")
        self.assertEqual(menu.anchor().hour, 14)
        self.assertEqual(menu.anchor().minute, 35)
        menu.field = 4
        menu.handle("\r")
        menu.handle("down")
        menu.handle("down")
        menu.handle("\r")
        self.assertEqual(menu.draft["repeat"], "daily")
        self.profile(self.fake())
        menu.profiles = scheduler.profiles()
        menu.field = 5
        menu.handle("\r")
        self.assertEqual(menu.mode, "list", menu.error)
        self.assertEqual(len(menu.jobs), 1)
        menu.index = 0
        menu.handle("p")
        self.assertFalse(menu.jobs[0]["enabled"])
        menu.handle("d")
        menu.handle("\r")  # keep is the default
        self.assertEqual(len(menu.jobs), 1)
        menu.handle("d")
        menu.handle("right")
        menu.handle("\r")
        self.assertEqual(menu.jobs, [])
        self.assertEqual(menu.handle("\\"), "agents")

    def test_clock_setting_and_render_fits_all_views(self):
        preferences.put(ui.CLOCK.key, "12h")
        self.assertEqual(ui.clock(datetime(2030, 1, 1, 14, 30)), "2:30 PM")
        preferences.put(ui.CLOCK.key, "24h")
        self.assertEqual(ui.clock(datetime(2030, 1, 1, 9, 5)), "09:05")
        menu = self.menu()
        scheduler.save_job(self.values(provider="Claude"), now=0)
        menu.refresh()
        menu.detail = menu.jobs[0]
        menu.editor()
        menu.buffer = "Prompt café\nsecond line"
        menu.day = datetime(2030, 1, 31).date()
        menu.date_buffer = ""
        menu.frequency = 0
        menu.confirm_delete = False
        menu.log_lines = ["line"] * 50
        for theme in ("brand", "rainbow", "sakura"):
            preferences.put(usage_panel.THEME.key, theme)
            for mode in ("list", "edit", "text", "date", "time", "repeat", "detail", "delete", "log"):
                menu.mode = mode
                for cols in (1, 8, 24, 40, 60, 80, 120):
                    for rows in (1, 5, 10, 24, 40):
                        lines = ui.render(menu, cols, rows, 0.4)
                        self.assertLessEqual(len(lines), rows)
                        for line in lines:
                            self.assertLessEqual(ap.visible_len(line), cols, (mode, cols, rows))

    def test_terminal_unicode_and_multiline_bracketed_paste(self):
        for raw, expected in (("é".encode(), "é"),
                              (b"\x1b[200~first\nsecond\x1b[201~", "paste:first\nsecond")):
            terminal = platform_runtime.Terminal()
            terminal._pending = raw
            terminal.fd = -1
            with patch.object(terminal, "wait", side_effect=lambda seconds: bool(terminal._pending)):
                self.assertEqual(terminal.read_key(), expected)
        self.assertNotIn("\x1b", ui.clean("\x1b[31mred\x1b[0m"))

    def test_selector_scheduler_navigation_and_settings_return(self):
        keys = iter(["\\", "\t", "s", "esc", "n", "\r", "paste:local draft", "\r", "esc", "\\", "q"])
        output = []
        class Terminal:
            def __enter__(self):
                return self
            def wait(self, seconds):
                return True
            def read_key(self):
                return next(keys)
            def keyboard(self, enhanced):
                pass
            def write(self, text):
                output.append(text)
        with patch.object(ap, "Terminal", Terminal), patch.object(ap, "installed", return_value=True), \
             patch.object(ap.VISIBILITY, "children", deepcopy(ap.VISIBILITY.children)), \
             patch.object(ap, "load_usage"), patch.object(usage_panel, "start"), \
             patch.object(scheduler, "service_status", return_value=True), \
             patch.object(ap.sys, "stdout", io.StringIO()), \
             patch.object(ap.shutil, "get_terminal_size", return_value=(80, 24)):
            self.assertIsNone(ap.choose("agents"))
        rendered = ap._ANSI.sub("", "".join(output))
        self.assertIn("prompt scheduler", rendered)
        self.assertIn("[Codex]", rendered)
        self.assertIn("local draft", rendered)
        self.assertEqual(scheduler.read()["jobs"], [])

    def test_weekday_start_skips_weekend(self):
        job = scheduler.save_job(self.values(repeat="weekdays", anchor="2030-01-05T09:30:00"), now=0)
        self.assertEqual(datetime.fromtimestamp(job["next_at"], ZoneInfo(job["timezone"])).weekday(), 0)


if __name__ == "__main__":
    unittest.main()
