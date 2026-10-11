import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import handoff

NOW = 1_000_000.0
ALL = {"Codex", "Claude", "Cursor", "Kiro", "Antigravity"}
HOUR, DAY = 3600, 86400


def meters(**used):
    """usage cache entries: Codex=95 -> Codex's 5h window 95% used."""
    usage = {}
    for name, pct in used.items():
        if name in ("Cursor", "Kiro"):
            label = "mo" if name == "Kiro" else "ex"
            pct, resets = pct
            usage[name] = {"fetched": NOW, "windows": [{"label": label, "pct": pct, "resets": NOW + resets}]}
        else:
            usage[name] = {"fetched": NOW, "windows": [{"label": "5h", "pct": pct, "resets": NOW + 2 * HOUR},
                                                       {"label": "wk", "pct": 10, "resets": NOW + 5 * DAY}]}
    return usage


class HandoffTests(unittest.TestCase):
    def setUp(self):
        # Cursor's pool depends on its configured model; name one so its "ex" meter applies.
        current = patch.object(handoff.provider_priority.provider_defaults, "current",
                               return_value={"model": "claude-opus-5-5"})
        current.start()
        self.addCleanup(current.stop)

    def target(self, caller, usage, installed=ALL):
        return handoff.target(caller, usage, installed, NOW)[0]

    def test_priority_is_codex_then_claude_then_cursor_or_kiro(self):
        usage = meters(Codex=10, Claude=10, Cursor=(10, 3 * DAY), Kiro=(10, 20 * DAY), Antigravity=0)
        self.assertEqual(self.target("Kiro", usage), "Codex")
        self.assertEqual(self.target("Codex", usage), "Claude")
        self.assertEqual(self.target("Claude", usage), "Codex")

    def test_skips_providers_near_their_limit(self):
        usage = meters(Codex=95, Claude=91, Cursor=(10, 3 * DAY), Kiro=(10, 20 * DAY))
        self.assertEqual(self.target("Antigravity", usage), "Cursor")

    def test_cursor_and_kiro_tie_breaks_on_the_sooner_reset(self):
        usage = meters(Codex=95, Claude=95, Cursor=(10, 20 * DAY), Kiro=(50, 2 * DAY))
        self.assertEqual(self.target("Claude", usage), "Kiro")
        usage = meters(Codex=95, Claude=95, Cursor=(10, 1 * DAY), Kiro=(50, 2 * DAY))
        self.assertEqual(self.target("Claude", usage), "Cursor")

    def test_gemini_never_takes_over(self):
        usage = meters(Codex=99, Claude=99, Cursor=(99, DAY), Kiro=(99, DAY), Antigravity=0)
        name, why = handoff.target("Codex", usage, ALL, NOW)
        self.assertIsNone(name)
        self.assertIn("90%", why)

    def test_known_quota_beats_an_unknown_meter(self):
        usage = meters(Claude=20)   # Codex unknown
        self.assertEqual(self.target("Kiro", usage), "Claude")
        self.assertEqual(self.target("Kiro", {}), "Codex")   # nothing known: priority order

    def test_uninstalled_providers_are_skipped(self):
        usage = meters(Codex=10, Claude=10)
        self.assertEqual(self.target("Kiro", usage, installed={"Claude", "Kiro"}), "Claude")

    def test_orchestrator_is_sol_or_opus_with_quota(self):
        self.assertEqual(handoff.orchestrator(meters(Codex=10, Claude=10), ALL, NOW), "Codex")
        self.assertEqual(handoff.orchestrator(meters(Codex=96, Claude=10), ALL, NOW), "Claude")
        # Everyone exhausted: still a capable lead, never Gemini.
        self.assertEqual(handoff.orchestrator(meters(Codex=99, Claude=99), {"Codex", "Claude", "Antigravity"}, NOW),
                         "Codex")

    def test_an_expired_window_counts_as_reset(self):
        usage = {"Codex": {"fetched": NOW, "windows": [{"label": "5h", "pct": 100, "resets": NOW - 1}]}}
        self.assertTrue(handoff.usable("Codex", usage, NOW))

    def test_handoff_is_off_by_default_and_leaves_leads_and_solo_without_rules(self):
        with patch.object(handoff.preferences, "_load", return_value={}):
            self.assertFalse(handoff.enabled())
        with patch.object(handoff, "enabled", return_value=False):
            self.assertEqual(handoff.rules("Claude", "lead", "w:p9"), "")
            self.assertEqual(handoff.rules("Kiro", "solo"), "")
            self.assertIn("--usage", handoff.rules("Claude", "crew"))
            text = handoff.report("Codex", meters(Codex=95, Claude=20), NOW)
        self.assertIn("Handoff is off", text)
        self.assertNotIn("hand off now", text)

    @patch.object(handoff, "enabled", return_value=True)
    def test_rules_tell_leads_to_hand_off_and_crew_not_to(self, _):
        lead = handoff.rules("Claude", "lead", "w:p9")
        self.assertIn("--usage", lead)
        self.assertIn("--handoff", lead)
        self.assertIn("90%", lead)
        self.assertIn("w:p9", lead)
        self.assertIn("/goal", lead)
        crew = handoff.rules("Claude", "crew")
        self.assertIn("--usage", crew)
        self.assertIn("Never run", crew)
        self.assertIn("--handoff", handoff.rules("Kiro", "solo"))

    @patch.object(handoff, "enabled", return_value=True)
    def test_report_marks_the_caller_and_names_the_next_provider(self, _):
        usage = meters(Codex=95, Claude=20, Antigravity=0)
        with patch.object(handoff.ap, "installed", side_effect=lambda a: a["name"] in ("Codex", "Claude", "Antigravity")):
            text = handoff.report("Codex", usage, NOW)
        self.assertIn("* Codex", text)
        self.assertIn("AT LIMIT", text)
        self.assertIn("trivial crew work only", text)
        self.assertIn("Next provider: Claude", text)

    def test_a_star_caller_is_not_told_to_hand_its_pane_off(self):
        usage = meters(Codex=10, Claude=10)
        with patch.object(handoff.ap, "installed", side_effect=lambda a: a["name"] in ("Codex", "Claude")):
            text = handoff.report("Star", usage, NOW)
        self.assertIn("You (*) are Star", text)
        self.assertIn("This pane stays", text)
        self.assertNotIn("Next provider", text)


if __name__ == "__main__":
    unittest.main()
