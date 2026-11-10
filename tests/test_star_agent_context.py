"""Position context reaches the actual provider request in Star's routed turns."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import starcli


class StarAgentContextTests(unittest.TestCase):
    def test_routed_requests_keep_mesh_context_and_refresh_position_context(self):
        for member in (None, SimpleNamespace(brief=lambda: "Existing mesh rules", role="crew")):
            with self.subTest(mesh=member is not None):
                star = starcli.Star.__new__(starcli.Star)
                star.member, star.plan, star.lease = member, False, None
                star.route_line = lambda *args: "route"
                star.convo = starcli.Conversation()
                engine = Mock()
                engine.argv.return_value = ["fake-provider"]
                star.engines = {"Codex": engine}
                choice = SimpleNamespace(provider="Codex", model="fixture", effort="low", tier="standard")
                task = SimpleNamespace(needs_mcp=False)

                with patch.object(starcli.herdr_agent_context, "brief", side_effect=["agent 2 = pane-a", "agent 2 = pane-b"]), \
                        patch.object(starcli, "bypass_enabled", return_value=True), \
                        patch.object(starcli, "Printer"), \
                        patch.object(starcli.star_engines, "Turn", side_effect=OSError("fake launch boundary")), \
                        patch("builtins.print"):
                    star.turn(task, choice, "Look at agent 2")
                    first = engine.argv.call_args.args[0]
                    star.turn(task, choice, "Look at agent 2 again")
                    second = engine.argv.call_args.args[0]

                self.assertIn("agent 2 = pane-a", first)
                self.assertIn("agent 2 = pane-b", second)
                self.assertNotIn("agent 2 = pane-a", second)
                self.assertIn("Look at agent 2", first)
                if member:
                    self.assertIn("Existing mesh rules", first)
                    self.assertIn("Existing mesh rules", second)


if __name__ == "__main__":
    unittest.main()
