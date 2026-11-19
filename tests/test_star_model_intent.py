import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import star_model_intent


class ParseIntentTests(unittest.TestCase):
    def test_explicit_provider_aliases(self):
        expected = {
            "use claude to edit this": "Claude",
            "use codex for this": "Codex",
            "use cursor please": "Cursor",
            "use kiro": "Kiro",
            "use agy": "Antigravity",
            "use antigravity": "Antigravity",
            "use gemini": "Antigravity",
        }
        for prompt, provider in expected.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(star_model_intent.parse(prompt).provider, provider)

    def test_model_aliases_and_versions(self):
        intent = star_model_intent.parse("using opus 55 for this task")
        self.assertIsNone(intent.provider)
        self.assertEqual((intent.family, intent.version), ("opus", "5.5"))

        intent = star_model_intent.parse("with opus 5.5 please")
        self.assertEqual((intent.family, intent.version), ("opus", "5.5"))

        intent = star_model_intent.parse("using sol xhigh for this task")
        self.assertEqual((intent.provider, intent.family, intent.effort), ("Codex", "sol", "xhigh"))

        intent = star_model_intent.parse("use GPT-6.2-Sol-xhigh for this task")
        self.assertEqual((intent.provider, intent.model_id, intent.effort),
                         ("Codex", "gpt-6.2-sol", "xhigh"))

        intent = star_model_intent.parse("using sol xhigh, fix the failing test")
        self.assertEqual((intent.provider, intent.family, intent.effort), ("Codex", "sol", "xhigh"))

        intent = star_model_intent.parse("use gemini-3.8-flash-high to summarize")
        self.assertEqual((intent.provider, intent.model_id, intent.version, intent.effort),
                         ("Antigravity", "gemini-3.8-flash-high", "3.8", "high"))

    def test_ultracode_marker_is_recorded(self):
        intent = star_model_intent.parse("use claude opus 55 uc")
        self.assertEqual((intent.provider, intent.family, intent.version, intent.ultracode),
                         ("Claude", "opus", "5.5", True))
        self.assertIn("ultracode", intent.label)

        intent = star_model_intent.parse("using opus 55 uc: fix it")
        self.assertEqual((intent.family, intent.version, intent.ultracode), ("opus", "5.5", True))

    def test_provider_modifiers_and_explicit_provider_model_pair(self):
        intent = star_model_intent.parse("use claude uc to check a typo")
        self.assertEqual((intent.provider, intent.ultracode, intent.provider_explicit),
                         ("Claude", True, True))

        intent = star_model_intent.parse("use claude xhigh to check a typo")
        self.assertEqual((intent.provider, intent.effort, intent.provider_explicit),
                         ("Claude", "xhigh", True))

        intent = star_model_intent.parse("use Cursor with sol xhigh to review")
        self.assertEqual((intent.provider, intent.family, intent.effort, intent.provider_explicit),
                         ("Cursor", "sol", "xhigh", True))

    def test_provider_led_prompt(self):
        intent = star_model_intent.parse("Claude: implement this change")
        self.assertEqual(intent.provider, "Claude")

        intent = star_model_intent.parse("opus 55 uc: implement this change")
        self.assertEqual((intent.family, intent.version, intent.ultracode, intent.provider_explicit),
                         ("opus", "5.5", True, False))

    def test_later_selector_and_trailing_model_selector(self):
        intent = star_model_intent.parse("Fix the bug with opus 5.5")
        self.assertEqual((intent.family, intent.version), ("opus", "5.5"))

        intent = star_model_intent.parse("Use the repo README, then use Codex to review")
        self.assertEqual(intent.provider, "Codex")

    def test_markdown_selector_and_quoted_example(self):
        self.assertEqual(star_model_intent.parse("Use **Codex** to review").provider, "Codex")
        self.assertEqual(star_model_intent.parse("Use `Codex` to review").provider, "Codex")
        self.assertIsNone(star_model_intent.parse("The README says `use Codex`"))

    def test_non_route_phrases_are_ignored(self):
        prompts = (
            "use the Claude API to read this",
            "use the logs from cursor to debug this",
            "do not use Codex for this",
            "compare Claude vs Codex",
            "Claude is broken",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(star_model_intent.parse(prompt))

    def test_conflicting_routes_require_a_choice(self):
        for prompt in ("Use Claude, then use Codex", "Use Claude or Codex"):
            with self.subTest(prompt=prompt):
                intent = star_model_intent.parse(prompt)
                self.assertIsNotNone(intent)
                self.assertIn("conflicting", intent.error)
                self.assertIsNone(intent.provider)

    def test_incidental_mentions_and_examples_do_not_route(self):
        prompts = (
            "Could you compare Claude with Codex for this task?",
            "The README says `use claude opus 55`.",
            'Please explain the phrase "use codex" to me.',
            "I like Opus 5.5, but please summarize this article.",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(star_model_intent.parse(prompt))

    def test_missing_or_non_text_prompt(self):
        self.assertIsNone(star_model_intent.parse(""))
        self.assertIsNone(star_model_intent.parse(None))


if __name__ == "__main__":
    unittest.main()
