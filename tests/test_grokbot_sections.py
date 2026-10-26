import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import grokbot_sections as sections

TODAY = datetime.date(2026, 10, 26)


def ms(year, month, day, hour=12):
    return int(datetime.datetime(year, month, day, hour).timestamp() * 1000)


def bot(ident, created, cli=True):
    return {"id": ident, "createdAt": created, "name": ("CLI · repo" if cli else "Research"),
            "description": sections.DESCRIPTION if cli else "A bot the user made"}


class FakeGateway:
    def __init__(self, roster, stored=None):
        self.roster, self.stored, self.mirrored = roster, list(stored or []), None

    def agents(self):
        return list(self.roster)

    def call(self, method, body=None):
        if method == "getHostSettings":
            return {"sidebarSections": self.stored}
        assert method == "setHostSettings" and set(body) == {"sidebarSections"}
        self.stored = body["sidebarSections"]
        return {"sidebarSections": self.stored}

    def account(self, method, body=None):
        assert method == "UpdateGrokBotUserRuntimeSettings"
        self.mirrored = body["sidebarSections"]["sections"]


class WeekSectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, AGENT_PICKER_STATE_HOME=self.temp.name)
        env.start()
        self.addCleanup(env.stop)

    def test_cli_bots_are_grouped_by_monday_week_newest_first(self):
        roster = [bot("a", ms(2026, 10, 19)), bot("b", ms(2026, 10, 25)), bot("c", ms(2026, 10, 26)), bot("x", ms(2026, 10, 26), cli=False)]
        result = sections.arrange([], roster, TODAY)
        self.assertEqual([(s["id"], s["name"], s["agentIds"]) for s in result], [
            ("cli-week-2026-10-26", "CLI · Oct 26 – Nov 1", ["c"]),
            ("cli-week-2026-10-19", "CLI · Oct 19 – 25", ["b", "a"])])

    def test_user_sections_and_bots_the_user_filed_are_left_alone(self):
        mine = {"id": "s1", "name": "Work", "agentIds": ["a", "x"], "isCollapsed": True}
        roster = [bot("a", ms(2026, 10, 20)), bot("b", ms(2026, 10, 20)), bot("x", 1, cli=False)]
        result = sections.arrange([mine], roster, TODAY)
        self.assertEqual(result[0], mine)
        self.assertEqual(result[1]["agentIds"], ["b"])

    def test_deleted_bots_and_empty_weeks_are_pruned_and_unchanged_returns_none(self):
        stale = [{"id": "cli-week-2026-10-12", "name": "CLI · Oct 12 – 18", "agentIds": ["gone"]},
                 {"id": "cli-week-2026-10-19", "name": "Renamed by user", "agentIds": ["a"]}]
        result = sections.arrange(stale, [bot("a", ms(2026, 10, 20))], TODAY)
        self.assertEqual(result, [stale[1]])
        self.assertIsNone(sections.arrange(result, [bot("a", ms(2026, 10, 20))], TODAY))

    def test_weeks_stay_in_place_and_new_ones_go_above_unassigned(self):
        unassigned = {"id": "__agents__", "name": "Unassigned", "agentIds": []}
        mine = {"id": "s1", "name": "Work", "agentIds": []}
        result = sections.arrange([mine, unassigned], [bot("a", ms(2026, 10, 20))], TODAY)
        self.assertEqual([s["id"] for s in result], ["s1", "cli-week-2026-10-19", "__agents__"])
        result = sections.arrange([unassigned] + result[1:2] + [mine], [bot("a", ms(2026, 10, 20)), bot("b", ms(2026, 10, 26))], TODAY)
        self.assertEqual([s["id"] for s in result], ["__agents__", "cli-week-2026-10-26", "cli-week-2026-10-19", "s1"])

    def test_other_years_are_labelled(self):
        self.assertEqual(sections.week_name(datetime.date(2025, 12, 29), TODAY), "CLI · Dec 29 – Jan 4, 2025")

    def test_organize_writes_the_box_and_mirrors_the_account_like_the_app(self):
        gateway = FakeGateway([bot("a", ms(2026, 10, 20))], [{"id": "s1", "name": "Work", "agentIds": []}])
        self.assertEqual(sections.organize(gateway), 1)
        self.assertEqual([s["id"] for s in gateway.stored], ["s1", "cli-week-2026-10-19"])
        self.assertEqual(gateway.mirrored[1], {"id": "cli-week-2026-10-19", "name": "CLI · Oct 19 – 25", "agentIds": ["a"]})
        self.assertIsNone(sections.organize(gateway))


if __name__ == "__main__":
    unittest.main()
