import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import agent_picker
import herdr_ai
import herdr_core
import preferences
import settings


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap, package_release, herdr_bin = load("bootstrap"), load("package_release"), load("herdr_bin")


class Folder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()


class SessionTests(Folder):
    def test_bundled_binary_is_found_only_once_it_exists(self):
        with patch.object(herdr_core, "ROOT", self.root):
            self.assertIsNone(herdr_core.bundled())
            path = self.root / "bin" / ("herdr.exe" if sys.platform == "win32" else "herdr")
            path.parent.mkdir()
            path.write_text("")
            self.assertEqual(herdr_core.bundled(), str(path))
            with patch.dict(os.environ, {"HERDR_BIN_PATH": "/own/herdr"}):
                self.assertEqual(herdr_core.find(), "/own/herdr")       # a pane's own Herdr wins
            clean = {k: v for k, v in os.environ.items() if k != "HERDR_BIN_PATH"}
            with patch.dict(os.environ, clean, clear=True), patch.object(herdr_core.shutil, "which", return_value=None):
                self.assertEqual(herdr_core.find(), str(path))

    def test_another_herdr_on_the_machine_gets_a_session_of_its_own(self):
        mine, theirs = self.root / "mine", self.root / "theirs"
        mine.write_text("")
        theirs.write_text("")
        self.assertIsNone(herdr_core.session_for(str(mine), {}, which=lambda name: None))
        self.assertIsNone(herdr_core.session_for(str(mine), {}, which=lambda name: str(mine)))
        self.assertEqual(herdr_core.session_for(str(mine), {}, which=lambda name: str(theirs)), herdr_core.SESSION)
        self.assertEqual(herdr_core.session_for(str(mine), {"HERDR_SESSION": "chosen"}, which=lambda name: str(theirs)),
                         "chosen")

    def test_adopt_leaves_the_pane_it_was_typed_in(self):
        mine, theirs = self.root / "mine", self.root / "theirs"
        mine.write_text("")
        theirs.write_text("")
        environ = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/live.sock", "HERDR_PANE_ID": "w1:p1", "KEEP": "yes"}
        with patch.object(herdr_core.shutil, "which", return_value=str(theirs)):
            self.assertEqual(herdr_core.adopt(str(mine), environ), herdr_core.SESSION)
        self.assertEqual(environ, {"KEEP": "yes", "HERDR_BIN_PATH": str(mine), "HERDR_SESSION": herdr_core.SESSION})

    def test_server_is_started_once_and_waited_for(self):
        answers = iter(["status: not running\n", "status: not running\n", "  status: running\nversion: 1\n"])
        started = []
        run = lambda argv, **kw: SimpleNamespace(stdout=next(answers))
        self.assertTrue(herdr_core.ensure_server("herdr", run=run, sleep=lambda s: None,
                                                 spawn=lambda argv, **kw: started.append(argv)))
        self.assertEqual(started, [["herdr", "server"]])
        started.clear()
        running = lambda argv, **kw: SimpleNamespace(stdout="status: running\n")
        self.assertTrue(herdr_core.ensure_server("herdr", run=running, spawn=lambda argv, **kw: started.append(argv)))
        self.assertEqual(started, [])

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        self.assertFalse(herdr_core.ensure_server("herdr", run=missing, spawn=missing))


class AgentCommandTests(Folder):
    def setUp(self):
        super().setUp()
        for patcher in (patch.object(preferences, "PATH", self.root / "preferences.json"),
                        patch.object(preferences, "_values", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def terminal(self, interactive=True):
        return (patch.object(agent_picker.sys.stdin, "isatty", return_value=interactive),
                patch.object(agent_picker.sys.stdout, "isatty", return_value=interactive))

    def test_a_bare_agent_opens_the_workspace_only_when_set_to(self):
        outside = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        a, b = self.terminal()
        with a, b, patch.dict(os.environ, outside, clear=True):
            self.assertFalse(agent_picker.opens_workspace([]))               # the selector stays the default
            preferences.put(agent_picker.RUNS.key, "workspace")
            self.assertTrue(agent_picker.opens_workspace([]))
            self.assertFalse(agent_picker.opens_workspace(["fix the tests"]))   # arguments go to the selector
            self.assertFalse(agent_picker.opens_workspace([], returning=True))
            with patch.dict(os.environ, {"HERDR_ENV": "1"}):
                self.assertFalse(agent_picker.opens_workspace([]))           # a pane's agent is the selector
        a, b = self.terminal(False)
        with a, b, patch.dict(os.environ, outside, clear=True):
            self.assertFalse(agent_picker.opens_workspace([]))

    def test_the_workspace_runs_the_bundled_herdr_with_selectors(self):
        with patch.object(herdr_core, "bundled", return_value="/app/bin/herdr"), \
             patch.object(herdr_core, "adopt") as adopt, patch.object(herdr_ai, "main", return_value=0) as main:
            self.assertEqual(herdr_ai.agent_workspace("/work"), 0)
        adopt.assert_called_once_with("/app/bin/herdr")
        self.assertEqual(main.call_args.args, (["--selectors"], "/app/bin/herdr", "/work"))
        with patch.object(herdr_core, "bundled", return_value=None), patch.object(herdr_core, "find", return_value=None):
            self.assertIsNone(herdr_ai.agent_workspace("/work"))            # no Herdr: the caller opens the selector

    def test_release_installs_update_by_running_the_installer_again(self):
        self.assertEqual(settings.release_update_command("darwin", "https://x/y")[-1], "curl -fsSL https://x/y/install.sh | sh")
        self.assertIn("irm https://x/y/install.ps1 | iex", settings.release_update_command("win32", "https://x/y"))
        (self.root / "VERSION").write_text("2.0.0\n")
        ran = []
        with patch.object(settings, "ROOT", self.root):
            settings.update_release(run=lambda argv, env: ran.append(env["AGENT_PICKER_HOME"]) or SimpleNamespace(returncode=0))
            self.assertEqual(ran, [str(self.root)])
            with self.assertRaises(RuntimeError):
                settings.update_release(run=lambda argv, env: SimpleNamespace(returncode=1))


class BootstrapTests(Folder):
    def test_herdr_becomes_a_command_only_when_the_machine_has_none(self):
        app, bindir = self.root / "app", self.root / "commands"
        binary = app / "bin" / ("herdr.exe" if sys.platform == "win32" else "herdr")
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        (app / "src").mkdir()
        (app / "src" / "agent_picker.py").write_text("")
        name = "herdr.cmd" if sys.platform == "win32" else "herdr"
        self.assertEqual(bootstrap.link_herdr(bindir, None), "none")
        self.assertEqual(bootstrap.link_herdr(bindir, binary, which=lambda n: "/usr/local/bin/herdr"), "kept")
        self.assertFalse((bindir / name).exists())
        self.assertEqual(bootstrap.link_herdr(bindir, binary, which=lambda n: None), "linked")
        self.assertTrue(bootstrap.ours(bindir / name))
        # Running the installer again finds its own command and refreshes it.
        self.assertEqual(bootstrap.link_herdr(bindir, binary, which=lambda n: str(bindir / name)), "linked")
        (bindir / name).unlink()
        (bindir / name).write_text("someone else's herdr")
        self.assertEqual(bootstrap.link_herdr(bindir, binary, which=lambda n: None), "kept")
        self.assertEqual((bindir / name).read_text(), "someone else's herdr")

    def test_a_first_install_sets_agent_to_open_the_workspace_and_later_ones_keep_your_choice(self):
        with patch.object(preferences, "PATH", self.root / "preferences.json"), patch.object(preferences, "_values", None):
            self.assertTrue(bootstrap.choose_workspace())
            self.assertEqual(preferences.get("agent.opens"), "workspace")
            preferences.put("agent.opens", "selector")
            self.assertFalse(bootstrap.choose_workspace())
            self.assertEqual(preferences.get("agent.opens"), "selector")


class PackagingTests(Folder):
    def test_bundle_holds_the_checkout_and_the_binary_but_not_the_herdr_source(self):
        names = package_release.tracked()
        self.assertIn("src/agent_picker.py", names)
        self.assertIn("scripts/bootstrap.py", names)
        self.assertFalse([n for n in names if n.startswith(("herdr/", "tests/", ".github/"))])
        binary = self.root / "herdr-macos-aarch64"
        binary.write_bytes(b"mach-o")
        packed = package_release.bundle("macos-aarch64", binary, "9.9.9", self.root / "dist")
        with tarfile.open(packed) as archive:
            members = {m.name: m for m in archive.getmembers()}
            self.assertEqual(archive.extractfile("VERSION").read(), b"9.9.9\n")
            self.assertEqual(archive.extractfile("bin/herdr").read(), b"mach-o")
        self.assertTrue(members["bin/herdr"].mode & 0o111)
        self.assertIn("bin/HERDR-LICENSE", members)
        self.assertIn("scripts/install.py", members)
        self.assertFalse([n for n in members if n.startswith("herdr/")])

        zipped = self.root / "herdr-windows-x86_64.zip"
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr("herdr.exe", b"MZ")
            archive.writestr("conpty/conpty.dll", b"dll")
        packed = package_release.bundle("windows-x86_64", zipped, "9.9.9", self.root / "dist")
        with zipfile.ZipFile(packed) as archive:
            self.assertEqual(archive.read("bin/herdr.exe"), b"MZ")
            self.assertEqual(archive.read("bin/conpty/conpty.dll"), b"dll")
            self.assertIn("src/herdr_core.py", archive.namelist())
        with self.assertRaises(SystemExit):
            package_release.bundle("plan9-mips", binary, "9.9.9", self.root / "dist")

    def test_manifest_lists_checksums_and_what_herdr_update_reads(self):
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "herdr-macos-aarch64").write_bytes(b"mach-o")
        (dist / "install.sh").write_text("#!/bin/sh\n")
        lines = package_release.manifest(dist, "v9.9.9")
        version, protocol, generation = package_release.herdr_facts()
        saved = json.loads((dist / "herdr-latest.json").read_text())
        self.assertEqual((saved["version"], saved["protocol"], saved["endpoint_generation"]), (version, protocol, generation))
        self.assertEqual(saved["assets"], {"macos-aarch64": "https://github.com/matthewstar05/agent-picker/releases/"
                                                            "download/v9.9.9/herdr-macos-aarch64"})
        self.assertEqual(saved["sha256"]["macos-aarch64"], hashlib.sha256(b"mach-o").hexdigest())
        self.assertIn(version, saved["releases"])
        sums = (dist / "SHA256SUMS.txt").read_text().splitlines()
        self.assertEqual(sums, lines)
        self.assertEqual({line.split()[1] for line in sums}, {"herdr-macos-aarch64", "install.sh", "herdr-latest.json"})

    def test_the_fork_reads_its_updates_from_this_repository(self):
        for path in ("herdr/src/update.rs", "herdr/src/remote/attach.rs"):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("matthewstar05/agent-picker/releases/latest/download/herdr-latest.json", text, path)
            self.assertNotIn('"https://herdr.dev/latest.json"', text, path)


class FetchTests(Folder):
    def test_target_names_match_the_release_files(self):
        self.assertEqual(herdr_bin.target("darwin", "arm64"), "macos-aarch64")
        self.assertEqual(herdr_bin.target("darwin", "x86_64"), "macos-x86_64")
        self.assertEqual(herdr_bin.target("win32", "AMD64"), "windows-x86_64")
        with self.assertRaises(SystemExit):
            herdr_bin.target("linux", "x86_64")

    @unittest.skipIf(sys.platform == "win32", "the Windows download is a zip, covered by the bundle test")
    def test_fetch_checks_the_download_before_placing_it(self):
        name = "herdr-" + herdr_bin.target()
        good = b"a herdr build"
        files = {"SHA256SUMS.txt": f"{hashlib.sha256(good).hexdigest()}  {name}\n".encode(), name: good}
        asked = []

        def get(url):
            asked.append(url)
            return files[url.rsplit("/", 1)[1]]

        with patch.object(herdr_bin, "BIN", self.root / "bin"):
            placed = herdr_bin.fetch("v9.9.9", get)
            self.assertEqual(placed.read_bytes(), good)
            self.assertTrue(os.access(placed, os.X_OK))
            self.assertIn("/releases/download/v9.9.9/", asked[0])
            self.assertEqual([p.name for p in (self.root / "bin").iterdir()], ["herdr"])
            files[name] = b"tampered"
            with self.assertRaises(SystemExit):
                herdr_bin.fetch(None, get)
            self.assertEqual(placed.read_bytes(), good)
            self.assertIn("/releases/latest/download/", asked[-1])


if __name__ == "__main__":
    unittest.main()
