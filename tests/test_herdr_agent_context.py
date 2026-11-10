import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import herdr_agent_context as context
import hidden_briefs
import model_catalog
import settings
import team


class HerdrAgentContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.env = {
            "AGENT_PICKER_STATE_HOME": str(self.state),
            "HERDR_ENV": "1",
            "HERDR_TAB_ID": "tab-a",
            "HERDR_PANE_ID": "pane-2",
        }
        for root, folder in (("_state_root", "briefs"), ("_temp_root", "temp-briefs")):
            hidden = patch.object(hidden_briefs, root, return_value=self.state / folder)
            hidden.start()
            self.addCleanup(hidden.stop)

    def save(self, entry, key="default|tab-a"):
        (self.state / "herdr-ai-tabs.json").write_text(json.dumps({key: entry}), encoding="utf-8")

    def test_outside_herdr_or_without_tab_or_pane_has_no_context(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(context.brief(), "")
        for missing in ("HERDR_TAB_ID", "HERDR_PANE_ID"):
            env = dict(self.env)
            env.pop(missing)
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(context.brief(), "")

    def test_standard_layout_maps_screen_positions_independent_of_provider(self):
        self.save({"team": None, "panes": {"agy": "p1", "codex": "p2", "claude": "p3",
                                             "picker": "p4", "cursor": "p5"}})
        env = {**self.env, "HERDR_PANE_ID": "p2"}
        with patch.dict(os.environ, env, clear=True):
            text = context.brief()
        self.assertIn("top row from left to right is agent 1, agent 2, agent 3", text)
        self.assertIn("bottom row from left to right is agent 4, agent 5", text)
        self.assertIn("agent 1 = p1; agent 2 = p2; agent 3 = p3; agent 4 = p4; agent 5 = p5", text)
        self.assertIn("You are agent 2 in pane p2", text)
        self.assertIn("herdr agent read p2 --source recent-unwrapped --lines 30", text)
        self.assertIn("saved map can be stale", text)
        self.assertIn("do not interrupt", text)

    def test_team_layout_maps_crew_and_lead_to_visual_positions(self):
        self.save({"team": "Codex", "panes": {"crew1": "c1", "crew2": "c2", "lead": "l",
                                                  "crew3": "c3", "crew4": "c4"}})
        env = {**self.env, "HERDR_PANE_ID": "l"}
        with patch.dict(os.environ, env, clear=True):
            text = context.brief()
        self.assertIn("agent 1 = c1; agent 2 = c2; agent 3 = l; agent 4 = c3; agent 5 = c4", text)
        self.assertIn("You are agent 3 in pane l", text)
        self.assertIn("herdr agent read c2", text)

    def test_socket_specific_state_key_is_used(self):
        self.save({"team": "Grok Bot", "panes": {"crew1": "g1", "crew2": "g2", "lead": "g3",
                                                     "crew3": "g4", "crew4": "g5"}},
                  key="socket-a|tab-a")
        with patch.dict(os.environ, {**self.env, "HERDR_SOCKET_PATH": "socket-a"}, clear=True):
            self.assertIn("agent 3 = g3", context.brief())

    def test_missing_or_malformed_layout_keeps_general_guidance(self):
        with patch.dict(os.environ, self.env, clear=True):
            text = context.brief()
        self.assertIn("inspect the live layout before guessing", text)
        (self.state / "herdr-ai-tabs.json").write_text("{bad json", encoding="utf-8")
        with patch.dict(os.environ, self.env, clear=True):
            text = context.brief()
        self.assertIn("inspect the live layout before guessing", text)
        self.assertNotIn("Saved agent pane map", text)

    def test_annotate_copies_non_star_profile_without_mutating_it(self):
        self.save({"team": None, "panes": {"agy": "p1", "codex": "p2", "claude": "p3",
                                             "picker": "p4", "cursor": "p5"}})
        source = {"name": "Codex", "argv": ["codex"]}
        with patch.dict(os.environ, self.env, clear=True):
            result = context.annotate(source)
        self.assertIsNot(result, source)
        self.assertEqual(source, {"name": "Codex", "argv": ["codex"]})
        self.assertEqual(result["argv"], source["argv"])
        self.assertIn("_herdr_context", result)
        team.instruct(result, [], "", opening=True, shared=False)
        self.assertEqual(source["argv"], ["codex"])

    def test_annotate_leaves_star_and_outside_profiles_unchanged(self):
        star = {"name": "Star", "argv": ["starcli"]}
        with patch.dict(os.environ, self.env, clear=True):
            self.assertIs(context.annotate(star), star)
        ordinary = {"name": "Codex"}
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(context.annotate(ordinary), ordinary)

    def test_position_context_reaches_every_native_provider_in_supported_brief_channel(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {**self.env, "AGENT_PICKER_CONFIG": str(Path(folder) / "missing.json")}, clear=True), \
                patch.object(model_catalog, "load_rows", return_value=[]):
            profiles = {agent["name"]: agent for agent in settings.load_agents()}
            layout = {"team": "Cursor", "panes": {"crew1": "crew-a", "crew2": "crew-b", "lead": "lead-a",
                                                       "crew3": "crew-c", "crew4": "crew-d"}}
            self.save(layout)
            cases = {
                "Claude": ["--picker-peer", "crew-a", "--picker-peers", "crew-a,crew-b,lead-a,crew-c,crew-d"],
                "Codex": ["--picker-peer", "crew-a", "--picker-peers", "crew-a,crew-b,lead-a,crew-c,crew-d"],
                "Antigravity": ["--picker-peer", "crew-a", "--picker-peers", "crew-a,crew-b,lead-a,crew-c,crew-d"],
                "Cursor": ["--picker-peer", "crew-a", "--picker-peers", "crew-a,crew-b,lead-a,crew-c,crew-d"],
                "Grok Bot": ["--picker-peer", "crew-a", "--picker-peers", "crew-a,crew-b,lead-a,crew-c,crew-d"],
                "Kiro": ["--picker-peer", "crew-a", "--picker-peers", "crew-a,crew-b,lead-a,crew-c,crew-d"],
            }
            for name, args in cases.items():
                original = profiles[name]
                prepared, tail = team.prepare(context.annotate(original), args)
                content = " ".join(prepared["argv"] + tail)
                for flag in ("--add-dir", "--plugin-dir"):   # a brief kept out of the transcript
                    if flag in prepared["argv"]:
                        folder = Path(prepared["argv"][prepared["argv"].index(flag) + 1])
                        content += (folder / hidden_briefs.MARKER).read_text(encoding="utf-8")
                        self.assertNotIn("agent 2 = crew-b", " ".join(tail), name)
                self.assertIn("agent 2 = crew-b", content, name)
                self.assertEqual(original.get("_herdr_context"), None, name)
            lead, tail = team.prepare(context.annotate(profiles["Claude"]),
                                      ["--picker-lead", "crew-a,crew-b,crew-c,crew-d"])
            self.assertIn("agent 2 = crew-b", lead["argv"][lead["argv"].index("--append-system-prompt") + 1])
            self.assertEqual(tail, [])

    def test_flagless_crew_gets_context_as_opening_prompt_and_plain_mode_gets_context(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {**self.env, "AGENT_PICKER_CONFIG": str(Path(folder) / "missing.json")}, clear=True), \
                patch.object(model_catalog, "load_rows", return_value=[]):
            profiles = {agent["name"]: agent for agent in settings.load_agents()}
            self.save({"team": "Kiro", "panes": {"crew1": "p1", "crew2": "p2", "lead": "p3",
                                                     "crew3": "p4", "crew4": "p5"}})
            kiro, tail = team.prepare(context.annotate(profiles["Kiro"]), ["--picker-crew", "p3"])
            self.assertIn("agent 2 = p2", tail[-1])
            self.assertIn("standing instructions for this session", tail[-1])
            plain, _ = team.prepare(context.annotate(profiles["Claude"]), ["--picker-plain"])
            self.assertIn("agent 2 = p2", plain["argv"][plain["argv"].index("--append-system-prompt") + 1])

    def test_handoff_off_still_adds_only_position_context_and_no_context_keeps_existing_behavior(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {**self.env, "AGENT_PICKER_CONFIG": str(Path(folder) / "missing.json")}, clear=True), \
                patch.object(model_catalog, "load_rows", return_value=[]), \
                patch.object(team.handoff, "enabled", return_value=False):
            profiles = {agent["name"]: agent for agent in settings.load_agents()}
            self.save({"team": "Claude", "panes": {"crew1": "p1", "crew2": "p2", "lead": "p3",
                                                        "crew3": "p4", "crew4": "p5"}})
            claude, _ = team.prepare(context.annotate(profiles["Claude"]), ["--picker-solo"])
            prompt = claude["argv"][claude["argv"].index("--append-system-prompt") + 1]
            self.assertIn("agent 2 = p2", prompt)
            self.assertNotIn("Usage limits", prompt)
            plain_kiro, tail = team.prepare(profiles["Kiro"], ["--picker-crew", "p3"])
            self.assertEqual(tail, [])
            self.assertFalse(any("Herdr agent positions" in value for value in plain_kiro["argv"]))


if __name__ == "__main__":
    unittest.main()
