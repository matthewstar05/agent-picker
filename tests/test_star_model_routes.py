import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import star_model_routes


def provider(model, effort="medium", pool=""):
    return {"tiers": {"standard": {"model": model, "effort": effort, "pool": pool}}}


CONFIG = {
    "Claude": provider("claude-sonnet-5-5"),
    "Codex": provider("gpt-6.2-sol"),
    "Cursor": provider("auto", pool="cr"),
    "Kiro": provider("claude-sonnet-5.5"),
    "Antigravity": provider("gemini-3.8-flash-high"),
}

ROWS = {
    "Claude": [
        {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "efforts": ["high", "xhigh"],
         "variants": {"high": "claude-opus-5-5-high", "xhigh": "claude-opus-5-5-xhigh"}},
        {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "efforts": ["low", "medium"],
         "variants": {"low": "claude-haiku-5-5-low", "medium": "claude-haiku-5-5-medium"}},
        {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "efforts": ["low", "medium"],
         "variants": {"low": "claude-sonnet-5-5-low", "medium": "claude-sonnet-5-5-medium"}},
    ],
    "Codex": [
        {"id": "gpt-6.2-sol", "name": "GPT-6.2 Sol", "efforts": ["medium", "xhigh"],
         "variants": {"medium": "gpt-6.2-sol-medium", "xhigh": "gpt-6.2-sol-xhigh"}},
    ],
    "Cursor": [
        {"id": "claude-opus-5.5", "name": "Claude Opus 5.5", "efforts": ["high", "xhigh"],
         "variants": {"high": "claude-opus-5.5-high", "xhigh": "claude-opus-5.5-xhigh"}},
        {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash", "efforts": ["low", "high"],
         "variants": {"low": "gemini-3.8-flash-low", "high": "gemini-3.8-flash-high"}},
        {"id": "gemini-4-carbon", "name": "Gemini 4 Carbon", "efforts": ["low", "high"],
         "variants": {"low": "gemini-4-carbon-low", "high": "gemini-4-carbon-high"}},
        {"id": "gemini-4-argon", "name": "Gemini 4 Argon", "efforts": ["low", "high"],
         "variants": {"low": "gemini-4-argon-low", "high": "gemini-4-argon-high"}},
    ],
    "Kiro": [
        {"id": "claude-opus-5.5", "name": "Claude Opus 5.5", "efforts": ["high", "max"],
         "variants": {}},
        {"id": "gpt-6.2-sol", "name": "GPT-6.2 Sol", "efforts": ["medium", "xhigh"],
         "variants": {}},
    ],
    "Antigravity": [
        {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash", "efforts": ["high"],
         "variants": {"high": "gemini-3.8-flash-high"}},
    ],
}


class StarModelRoutesTests(unittest.TestCase):
    def setUp(self):
        self.rows = {provider: list(rows) for provider, rows in ROWS.items()}
        self.catalog = patch.object(star_model_routes.model_catalog, "load_rows",
                                    side_effect=lambda entry: self.rows[entry["name"]])
        self.catalog.start()
        self.addCleanup(self.catalog.stop)

    def test_explicit_opus_version_and_effort_selects_matching_cli_models(self):
        intent = {"provider": "Claude", "family": "opus", "version": "5.5",
                  "effort": "xhigh", "ultracode": False, "provider_explicit": True}
        choices = star_model_routes.resolve(CONFIG, "standard", intent)
        self.assertEqual(choices["Claude"]["model"], "claude-opus-5-5-xhigh")
        self.assertEqual(choices["Cursor"]["model"], "claude-opus-5.5-xhigh")
        self.assertNotIn("Kiro", choices)  # xhigh is unsupported by this Kiro model
        self.assertEqual(choices["Cursor"]["pool"], "ex")

    def test_unsupported_explicit_effort_does_not_downgrade(self):
        config = {"Claude": provider("claude-opus-5-5", "high")}
        with patch.object(star_model_routes.model_catalog, "load_rows", return_value=[
            {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "efforts": ["high"]}
        ]):
            choices = star_model_routes.resolve(
                config, "standard", {"provider": "Claude", "family": "opus",
                                      "version": "5.5", "effort": "xhigh",
                                      "provider_explicit": True})
        self.assertEqual(choices, {})

    def test_sol_version_and_effort_route_only_to_solvers_that_offer_it(self):
        intent = {"provider": None, "family": "sol", "version": "6.2", "effort": "xhigh"}
        choices = star_model_routes.resolve(CONFIG, "standard", intent)
        self.assertEqual(set(choices), {"Codex", "Kiro"})
        self.assertEqual(choices["Codex"]["model"], "gpt-6.2-sol-xhigh")
        self.assertEqual(choices["Kiro"]["model"], "gpt-6.2-sol")
        self.assertEqual(choices["Kiro"]["effort"], "xhigh")

    def test_provider_only_claude_includes_available_claude_compatible_routes(self):
        intent = {"provider": "claude", "family": "opus", "version": "5.5",
                  "provider_explicit": True}
        choices = star_model_routes.resolve(CONFIG, "standard", intent)
        self.assertEqual(set(choices), {"Claude", "Cursor", "Kiro"})

    def test_provider_only_cursor_stays_on_cursor(self):
        choices = star_model_routes.resolve(
            CONFIG, "standard", {"provider": "cursor", "provider_explicit": True})
        self.assertEqual(set(choices), {"Cursor"})
        self.assertEqual(choices["Cursor"]["pool"], "cr")

    def test_explicit_family_uses_latest_common_version_across_catalogs(self):
        rows = dict(ROWS)
        rows["Claude"] = list(ROWS["Claude"]) + [
            {"id": "claude-opus-6-0", "name": "Claude Opus 6.0",
             "efforts": ["high", "xhigh"], "variants": {}}
        ]
        self.rows = rows
        choices = star_model_routes.resolve(
            CONFIG, "standard", {"family": "opus", "provider_explicit": False})
        self.assertEqual(choices["Claude"]["model"], "claude-opus-6-0")
        self.assertNotIn("Cursor", choices)

    def test_antigravity_provider_only_keeps_its_active_model_at_any_tier(self):
        config = {"Antigravity": {"tiers": {"light": {"model": "gemini-3.8-flash-high"}}}}
        rows = {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash",
                "efforts": ["low", "medium", "high"],
                "variants": {"low": "gemini-3.8-flash-low",
                             "medium": "gemini-3.8-flash-medium",
                             "high": "gemini-3.8-flash-high"}}
        with patch.object(star_model_routes.model_catalog, "load_rows", return_value=[rows]):
            choices = star_model_routes.resolve(
                config, "heavy", {"provider": "Antigravity", "provider_explicit": True})
        self.assertEqual(choices["Antigravity"]["model"], "gemini-3.8-flash-high")

    def test_antigravity_keeps_active_version_and_cursor_can_fall_back_on_same_one(self):
        rows = {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash",
                "efforts": ["low", "high"],
                "variants": {"low": "gemini-3.8-flash-low", "high": "gemini-3.8-flash-high"}}
        self.rows["Antigravity"] = [rows]
        choices = star_model_routes.resolve(
            CONFIG, "standard", {"provider": "Antigravity", "provider_explicit": True})
        self.assertEqual(choices["Antigravity"]["model"], "gemini-3.8-flash-high")
        self.assertIn("Cursor", choices)
        self.assertEqual(choices["Cursor"]["model"], "gemini-3.8-flash-high")

    def test_model_id_keeps_gemini_subtype_across_same_version(self):
        choices = star_model_routes.resolve(
            CONFIG, "standard", {"provider": "Cursor", "family": "gemini",
                                  "version": "4", "model_id": "gemini-4-carbon"})
        self.assertEqual(set(choices), {"Cursor"})
        self.assertEqual(choices["Cursor"]["model"], "gemini-4-carbon-high")

    def test_cursor_external_route_drops_included_auto_cost(self):
        choices = star_model_routes.resolve(
            CONFIG, "standard", {"family": "opus", "version": "5.5"})
        self.assertEqual(choices["Cursor"]["pool"], "ex")
        self.assertNotIn("cost", choices["Cursor"])

    def test_claude_provider_only_keeps_haiku_and_uses_compatible_tier_models(self):
        light_config = {name: {"tiers": dict(spec["tiers"])} for name, spec in CONFIG.items()}
        light_config["Claude"]["tiers"]["light"] = {"model": "claude-haiku-5-5", "effort": "low"}
        light_config["Kiro"]["tiers"]["light"] = {"model": "claude-sonnet-5.5", "effort": "low"}
        light_config["Cursor"]["tiers"]["light"] = {"model": "auto", "pool": "cr"}
        self.rows = dict(ROWS)
        self.rows["Kiro"] = [row for row in ROWS["Kiro"]
                             if star_model_routes._row_family(row) != "haiku"]
        self.rows["Cursor"] = [row for row in ROWS["Cursor"]
                               if star_model_routes._row_family(row) != "haiku"]
        choices = star_model_routes.resolve(
            light_config, "light", {"provider": "Claude", "provider_explicit": True})
        self.assertEqual(choices["Claude"]["model"], "claude-haiku-5-5-low")
        self.assertEqual(choices["Kiro"]["model"], "claude-opus-5.5")
        self.assertEqual(choices["Cursor"]["pool"], "ex")
        self.assertIn("opus", choices["Cursor"]["model"])

    def test_ultracode_is_claude_only(self):
        choices = star_model_routes.resolve(
            CONFIG, "standard", {"provider": "Codex", "ultracode": True})
        self.assertEqual(set(choices), {"Claude"})
        self.assertTrue(choices["Claude"]["ultracode"])

    def test_claude_provider_falls_back_to_sonnet_when_haiku_is_unavailable(self):
        rows_without_haiku = dict(ROWS)
        rows_without_haiku["Claude"] = [ROWS["Claude"][0]]
        rows_without_haiku["Kiro"] = [ROWS["Kiro"][0]]
        rows_without_haiku["Cursor"] = [ROWS["Cursor"][0]]
        self.catalog.stop()
        self.catalog = patch.object(
            star_model_routes.model_catalog, "load_rows",
            side_effect=lambda entry: rows_without_haiku[entry["name"]])
        self.catalog.start()
        light_config = {name: {"tiers": dict(spec["tiers"])} for name, spec in CONFIG.items()}
        light_config["Claude"]["tiers"]["light"] = {"model": "claude-haiku-5-5", "effort": "low"}
        light_config["Kiro"]["tiers"]["light"] = {"model": "claude-sonnet-5.5", "effort": "low"}
        light_config["Cursor"]["tiers"]["light"] = {"model": "auto", "pool": "cr"}
        choices = star_model_routes.resolve(
            light_config, "light", {"provider": "Claude", "provider_explicit": True})
        self.assertEqual(choices["Claude"]["model"], "claude-opus-5-5-high")
        self.assertEqual(choices["Kiro"]["model"], "claude-opus-5.5")

    def test_unavailable_exact_selector_does_not_substitute_another_family(self):
        choices = star_model_routes.resolve(
            CONFIG, "standard", {"provider": "Claude", "family": "opus", "version": "9.9"})
        self.assertEqual(choices, {})


if __name__ == "__main__":
    unittest.main()
