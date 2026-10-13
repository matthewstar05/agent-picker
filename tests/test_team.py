import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_picker as picker
import model_catalog
import provider_defaults
import settings
import team

TEAM = ("Claude", "Codex", "Antigravity", "Cursor", "Kiro")


def catalog():
    """The shared profiles from agents.json, without this machine's config."""
    with tempfile.TemporaryDirectory() as folder:
        with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(Path(folder) / "missing.json")}):
            return {a["name"]: a for a in settings.load_agents()}


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.agents = catalog()
        # Cursor and Antigravity read cached model rows; keep them out of the result.
        rows = patch.object(model_catalog, "load_rows", return_value=[])
        rows.start()
        self.addCleanup(rows.stop)

    def prepare(self, name, role, panes):
        agent = self.agents[name]
        before = json.loads(json.dumps(agent))
        prepared, args = team.prepare(agent, [role, panes, "--extra"])
        self.assertEqual(agent, before, "prepare must not change the shared profile")
        return prepared, args

    def test_orchestrator_summaries_name_the_orchestrators_model(self):
        expected = {"Claude": "claude-opus-5-5", "Codex": "gpt-6.1-sol", "Antigravity": "gpt-6.1-sol",
                    "Cursor": "claude-opus-5-5", "Kiro": "claude-opus-5.5"}
        for name, text in expected.items():
            with patch.object(team, "mode", return_value=team.ORCHESTRATOR), \
                 patch.object(team.handoff, "orchestrator", return_value="Codex"):
                self.assertEqual(team.summary(self.agents[name]), text, name)
                self.assertEqual(team.option(self.agents[name])["short"], text, name)

    def test_individual_and_plain_summaries_name_the_picker_model(self):
        # Fixture picker defaults (Claude's "opus" alias); the label shows the model as launched.
        expected = {"Claude": "opus", "Codex": "gpt-6.1-sol", "Antigravity": "gemini-3.8-flash-high",
                    "Cursor": "grok-4.7", "Kiro": "claude-opus-5.5"}
        for name, text in expected.items():
            for kind in (team.INDIVIDUAL, team.OFF):
                self.assertEqual(team.summary(self.agents[name], kind), text, name)

    def test_system_prompt_off_launches_the_picker_model_with_no_instructions(self):
        for name in TEAM:
            agent = self.agents[name]
            before = json.loads(json.dumps(agent))
            prepared, args = team.prepare(agent, ["--picker-plain", "--extra"])
            self.assertEqual(agent, before, name)
            self.assertEqual(args, ["--extra"], name)   # no opening prompt either
            text = " ".join(prepared["argv"])
            for brief in ("--append-system-prompt", "developer_instructions", "herdr agent", "standing instructions"):
                self.assertNotIn(brief, text, name)
            values = provider_defaults.current(prepared)
            self.assertEqual(values, provider_defaults.current(agent), name)
        claude, _ = team.prepare(self.agents["Claude"], ["--picker-plain"])
        self.assertEqual(provider_defaults.current(claude)["effort"], "medium")
        self.assertIn("--settings", claude["argv"])   # iMCP still stays out

    def test_m_cycles_off_individual_orchestrator(self):
        for current, following in (("off", "individual"), ("individual", "orchestrator"), ("orchestrator", "off")):
            with patch.object(team, "mode", return_value=current):
                self.assertEqual(team.next_mode(), following)

    def peer(self, name, me="w:p2", peers="w:p2,w:p3,w:p4,w:p5,w:p6"):
        agent = self.agents[name]
        before = json.loads(json.dumps(agent))
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(team, "SHARED", Path(folder)), patch.dict(os.environ, {"HERDR_TAB_ID": "w:t1"}):
            prepared, args = team.prepare(agent, ["--picker-peer", me, "--picker-peers", peers,
                                                  "--picker-usage-pane", "w:u", "--extra"])
            notes = Path(folder) / "w_t1.md"
            self.assertTrue(notes.exists())
        self.assertEqual(agent, before, "prepare must not change the shared profile")
        return prepared, args, str(notes)

    def test_individual_peers_run_the_picker_model_and_effort(self):
        claude, _, _ = self.peer("Claude")
        values = provider_defaults.current(claude)
        self.assertEqual(values["model"], "opus")      # the picker's Claude model, not the lead's
        self.assertEqual(values["effort"], "medium")   # and its effort, not the lead's high
        self.assertTrue(values["bypass_permissions"])
        codex, _, _ = self.peer("Codex")
        self.assertEqual(provider_defaults.current(codex)["model"], "gpt-6.1-sol")
        self.assertTrue(codex["detail"].startswith("peer · "))

    def test_individual_peers_share_context_instead_of_delegating(self):
        claude, args, notes = self.peer("Claude")
        brief = claude["argv"][claude["argv"].index("--append-system-prompt") + 1]
        self.assertIn("You are pane w:p2; the others are w:p3, w:p4, w:p5, w:p6", brief)
        self.assertIn(notes, brief)
        self.assertIn("nobody leads", brief)
        self.assertNotIn("delegate", brief.lower())
        self.assertIn("--settings", claude["argv"])   # iMCP stays out of the five panes
        self.assertEqual(args, ["--extra"])
        codex, _, _ = self.peer("Codex")
        self.assertTrue(any(a.startswith("developer_instructions=") and "nobody leads" in a for a in codex["argv"]))
        self.assertIn("mcp_servers.iMCP.enabled=false", codex["argv"])
        kiro, args, _ = self.peer("Kiro")
        self.assertIn("nobody leads", args[-1])   # flagless CLIs read it as the opening prompt

    def test_peers_follow_a_model_changed_in_the_picker(self):
        provider_defaults.apply(self.agents["Claude"], {"model": "claude-sonnet-5-5", "effort": "low"})
        for args in (["--picker-plain"], ["--picker-peer", "w:p2"]):
            with tempfile.TemporaryDirectory() as folder, patch.object(team, "SHARED", Path(folder)):
                prepared, _ = team.prepare(self.agents["Claude"], args)
            values = provider_defaults.current(prepared)
            self.assertEqual((values["model"], values["effort"]), ("claude-sonnet-5-5", "low"), args)
        self.assertEqual(team.summary(self.agents["Claude"], team.INDIVIDUAL), "claude-sonnet-5-5")
        lead, _ = self.prepare("Claude", "--picker-lead", "w:p2")   # the team keeps its own models
        self.assertEqual(provider_defaults.current(lead)["model"], "claude-opus-5-5")

    def test_right_arrow_opens_picker_defaults_or_team_models_by_mode(self):
        with patch.object(team, "providers", return_value=list(self.agents.values())[1:6]):
            for kind in (team.OFF, team.INDIVIDUAL):
                with patch.object(team, "mode", return_value=kind):
                    menu = team.models_menu("Claude")
                self.assertIs(type(menu), provider_defaults.Menu, kind)
                self.assertIs(menu.agent, self.agents["Claude"], "edits the picker's own profile")
            with patch.object(team, "mode", return_value=team.ORCHESTRATOR):
                self.assertIsInstance(team.models_menu("Claude"), team.TeamMenu)
            self.assertIsNone(team.models_menu("Standard"))

    def test_team_menu_edits_lead_and_crew_and_saves_locally(self):
        claude = self.agents["Claude"]
        menu = team.TeamMenu(claude)
        self.assertEqual(menu.fields(), ("role", "model", "effort"))
        self.assertEqual((menu.values["model"], menu.values["effort"]), ("claude-opus-5-5", "high"))
        menu.handle("down"); menu.handle("down"); menu.handle("right")   # lead effort high -> xhigh
        menu.index = 0
        menu.handle("right")                                            # role: crew
        self.assertEqual(menu.values["model"], "claude-haiku-5-5")
        menu.handle("down"); menu.handle("\r")                          # browse models
        menu.model_index = next(i for i, r in enumerate(menu.models()) if r["id"] == "claude-opus-5-5")
        menu.handle("\r")
        self.assertTrue(menu.changed)
        _, delta = menu.review()
        self.assertEqual(delta, [("Lead effort", "high", "xhigh"), ("Crew model", "claude-haiku-5-5", "claude-opus-5-5")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"agents": {"Claude": {"defaults": {"model": "opus", "effort": "low",
                                                                          "bypass_permissions": True}}}}))
            menu.save(path)
            data = json.loads(path.read_text())["agents"]["Claude"]
            self.assertEqual(data["defaults"]["model"], "opus", "the picker's defaults stay put")
            self.assertEqual(data["team"], {"lead": {"model": "claude-opus-5-5", "effort": "xhigh"},
                                            "crew": {"model": "claude-opus-5-5", "effort": "high"}})
            self.assertFalse(menu.changed)
            with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(path)}):
                loaded = {a["name"]: a for a in settings.load_agents()}
        self.assertEqual(loaded["Claude"]["team"]["lead"]["effort"], "xhigh")
        self.assertEqual(provider_defaults.current(loaded["Claude"])["model"], "opus")
        self.assertEqual(claude["team"]["crew"]["model"], "claude-opus-5-5")   # the running menu sees it too
        lead, _ = team.prepare(claude, ["--picker-lead", "w:p2"])
        self.assertEqual(provider_defaults.current(lead)["effort"], "xhigh")

    def test_saved_team_role_leaves_the_other_role_shared(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"agents": {"Codex": {"team": {"crew": {"model": "gpt-6.1-sol", "effort": "low"}}}}}))
            with patch.dict(os.environ, {"AGENT_PICKER_CONFIG": str(path)}):
                codex = {a["name"]: a for a in settings.load_agents()}["Codex"]
        self.assertEqual(codex["team"]["lead"], self.agents["Codex"]["team"]["lead"])
        self.assertEqual(codex["team"]["crew"], {"model": "gpt-6.1-sol", "effort": "low"})

    def test_antigravity_team_menu_has_only_its_crew(self):
        menu = team.TeamMenu(self.agents["Antigravity"])
        self.assertEqual(menu.fields(), ("model",))   # one role, and Gemini encodes its effort

    def test_lead_and_crew_models_and_effort(self):
        for name in TEAM:
            for role, key in (("--picker-lead", "lead"), ("--picker-crew", "crew")):
                if key not in self.agents[name]["team"]:
                    continue   # Antigravity's crew are led by another provider
                prepared, _ = self.prepare(name, role, "w:p1")
                values = provider_defaults.current(prepared)
                config = self.agents[name]["team"][key]
                self.assertEqual(values["model"], config["model"], (name, role))
                self.assertEqual(values["effort"], config["effort"], (name, role))
                self.assertTrue(prepared["detail"].startswith(key + " · "), (name, role))
                self.assertTrue(values["bypass_permissions"], (name, role))

    def test_claude_appends_system_prompt(self):
        lead, args = self.prepare("Claude", "--picker-lead", "w:p2,w:p3,w:p4,w:p5")
        flag = lead["argv"].index("--append-system-prompt")
        brief = lead["argv"][flag + 1]
        self.assertIn("w:p2, w:p3, w:p4, w:p5", brief)
        self.assertIn("claude-haiku-5-5", brief)
        self.assertEqual(args, ["--extra"])
        crew, args = self.prepare("Claude", "--picker-crew", "w:p1")
        brief = crew["argv"][crew["argv"].index("--append-system-prompt") + 1]
        self.assertIn("herdr pane w:p1", brief)
        self.assertIn(f"at most {team.REPLY} lines", brief)
        self.assertEqual(args, ["--extra"])

    def test_codex_developer_instructions_are_a_toml_string(self):
        for role, needle in (("--picker-lead", "w:p2, w:p3"), ("--picker-crew", "herdr pane w:p1")):
            prepared, args = self.prepare("Codex", role, "w:p2,w:p3" if role == "--picker-lead" else "w:p1")
            argv = prepared["argv"]
            values = [argv[i + 1] for i, a in enumerate(argv[:-1])
                      if a == "-c" and argv[i + 1].startswith("developer_instructions=")]
            self.assertEqual(len(values), 1, role)
            raw = values[0].split("=", 1)[1]
            self.assertTrue(raw.startswith('"') and raw.endswith('"'))
            text = json.loads(raw)
            self.assertIn(needle, text)
            self.assertNotIn("\n", raw, "a TOML basic string cannot hold raw newlines")
            self.assertTrue(all(ord(c) >= 32 for c in raw), "control characters must be escaped")
            try:
                import tomllib
            except ImportError:
                tomllib = None
            if tomllib:
                self.assertEqual(tomllib.loads("v = " + raw)["v"], text)
            self.assertEqual(args, ["--extra"])

    def test_codex_team_turns_off_imcp(self):
        for role, target in (("--picker-lead", "w:p2,w:p3"), ("--picker-crew", "w:p1")):
            prepared, _ = self.prepare("Codex", role, target)
            argv = prepared["argv"]
            self.assertIn("mcp_servers.iMCP.enabled=false", argv, role)
            self.assertEqual(argv[argv.index("mcp_servers.iMCP.enabled=false") - 1], "-c", role)

    def test_claude_team_denies_imcp(self):
        for role, target in (("--picker-lead", "w:p2,w:p3"), ("--picker-crew", "w:p1")):
            prepared, _ = self.prepare("Claude", role, target)
            argv = prepared["argv"]
            with open(argv[argv.index("--settings") + 1], encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"deniedMcpServers": [{"serverName": "iMCP"}]}, role)

    def test_flagless_lead_gets_opening_prompt_last(self):
        for name in ("Kiro", "Cursor"):
            prepared, args = self.prepare(name, "--picker-lead", "w:p2,w:p3")
            self.assertEqual(args[0], "--extra")
            self.assertEqual(len(args), 2, name)
            self.assertIn("w:p2, w:p3", args[-1])
            self.assertTrue(args[-1].endswith("Reply only: ready."), name)
            self.assertFalse(any("herdr team" in a for a in prepared["argv"]), name)

    def test_gemini_never_leads_and_every_lead_is_sol_or_opus(self):
        with self.assertRaisesRegex(ValueError, "no team lead"):
            self.prepare("Antigravity", "--picker-lead", "w:p2")
        for name in TEAM:
            lead = self.agents[name]["team"].get("lead")
            if lead:
                self.assertRegex(lead["model"], r"^(gpt-6\.1-sol|claude-opus-5[-.]5)$", name)

    def test_star_takes_no_solo_brief(self):
        star = {"name": "Star", "argv": ["starcli"]}
        agent, args = team.prepare(star, ["--picker-solo", "--extra"])
        self.assertEqual((agent["argv"], args), (["starcli"], ["--extra"]))

    def test_solo_takes_no_brief_while_handoff_is_off(self):
        with patch.object(team.handoff, "enabled", return_value=False):
            agent, args = team.prepare(self.agents["Claude"], ["--picker-solo", "--extra"])
        self.assertNotIn("--append-system-prompt", agent["argv"])
        self.assertEqual(args, ["--extra"])

    @patch.object(team.handoff, "enabled", return_value=True)
    def test_antigravity_solo_uses_interactive_flag(self, _):
        _, args = team.prepare(self.agents["Antigravity"], ["--picker-solo", "--extra"])
        self.assertEqual(args[:2], ["--extra", "-i"])
        self.assertIn("herdr-ai", args[2])
        self.assertIn("--handoff", args[2])
        self.assertEqual(len(args), 3)

    def test_flagless_crew_get_no_opening_prompt(self):
        for name in ("Kiro", "Cursor", "Antigravity"):
            prepared, args = self.prepare(name, "--picker-crew", "w:p1")
            self.assertEqual(args, ["--extra"], name)
            self.assertFalse(any("herdr" in a for a in prepared["argv"]), name)

    def test_prepare_rejects_missing_panes_and_team(self):
        with self.assertRaisesRegex(ValueError, "needs pane ids"):
            team.prepare(self.agents["Claude"], ["--picker-lead"])
        plain = dict(self.agents["Claude"])
        plain.pop("team")
        with self.assertRaisesRegex(ValueError, "no team"):
            team.prepare(plain, ["--picker-crew", "w:p1"])

    def test_find_standard_and_providers(self):
        agents = list(self.agents.values())
        with patch.object(picker, "AGENTS", agents), patch.object(picker, "installed", return_value=True):
            self.assertEqual([a["name"] for a in team.providers()], list(TEAM))
            self.assertIsNone(team.find("standard"))
            self.assertIsNone(team.find("STANDARD"))
            self.assertEqual(team.find("Codex"), "Codex")
            self.assertEqual(team.find("kiro"), "Kiro")
            self.assertEqual(team.find("ANTIGRAVITY"), "Antigravity")
            with self.assertRaisesRegex(ValueError, "unknown setup 'nope'.*standard, star, claude"):
                team.find("nope")
            self.assertEqual([s["name"] for s in team.setups()], ["Standard"] + list(TEAM) + ["Star"])

    def test_main_launches_crew_with_fake_cli(self):
        with tempfile.TemporaryDirectory(prefix="team test ") as folder:
            folder = Path(folder)
            script = folder / "fake cli.py"
            output = folder / "args.json"
            script.write_text("import json, sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\nsys.exit(3)\n")
            config = folder / "config.json"
            config.write_text(json.dumps({"agents": {"Claude": {"argv": [sys.executable, str(script), str(output)]}}}))
            env = dict(os.environ, AGENT_PICKER_CONFIG=str(config), AGENT_PICKER_STATE_HOME=str(folder / "state"),
                       AGENT_PICKER_NO_PRETRUST="1")
            result = subprocess.run([sys.executable, str(ROOT / "src/agent_picker.py"), "--picker-launch", "Claude",
                                     "--picker-crew", "w:p1", "hello"],
                                    cwd=folder, env=env, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 3, result.stderr)
            args = json.loads(output.read_text())
            self.assertEqual(args[args.index("--model") + 1], "claude-haiku-5-5")
            self.assertEqual(args[args.index("--effort") + 1], "high")
            brief = args[args.index("--append-system-prompt") + 1]
            self.assertIn("herdr pane w:p1", brief)
            self.assertEqual(args[-1], "hello")
            self.assertNotIn("--picker-crew", args)
            self.assertEqual((folder / "state/last").read_text(), "Claude")


if __name__ == "__main__":
    unittest.main()
