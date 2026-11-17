from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as ap


class RequiresAppTest(unittest.TestCase):
    """Grok Bot drives its Mac app, so a Mac without the app lists it as
    needing an install rather than as a launchable agent."""

    def check(self, app_where, platform="darwin"):
        agent = {"name": "Grok Bot", "argv": ["grokbot"], "requires_app": "Grok Bot.app"}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            system, home = root / "Applications", root / "home"
            (home / "Applications").mkdir(parents=True)
            system.mkdir()
            if app_where == "system":
                (system / "Grok Bot.app").mkdir()
            elif app_where == "home":
                (home / "Applications" / "Grok Bot.app").mkdir()
            real = Path
            def fake(*parts):
                path = real(*parts)
                return system if str(path) == "/Applications" else path
            with patch.object(ap.sys, "platform", platform), patch.object(ap, "Path", side_effect=fake) as P, \
                    patch.object(ap, "resolve_argv", return_value=["/bin/grokbot"]):
                P.home.return_value = home
                return ap.installed(agent)

    def test_the_app_in_either_applications_folder_counts(self):
        self.assertTrue(self.check("system"))
        self.assertTrue(self.check("home"))

    def test_without_the_app_the_launcher_alone_is_not_enough(self):
        self.assertFalse(self.check(None))

    def test_the_app_is_only_required_on_macos(self):
        self.assertTrue(self.check(None, platform="win32"))


if __name__ == "__main__":
    unittest.main()
