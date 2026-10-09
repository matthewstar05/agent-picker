"""Active animations share a continuous 60 fps clock; idle cards stay cached."""

import sys
import io
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import usage_panel as panel


class UsageBorderTests(unittest.TestCase):
    def test_usage_frame_clock_includes_render_time(self):
        if hasattr(panel, "frame_time"):
            self.assertEqual(panel.frame_time(SimpleNamespace(anim=True, peeking=lambda: False)), 1 / 60)
            self.assertEqual(panel.frame_time(SimpleNamespace(anim=False, peeking=lambda: False)), 1.0)
            return
        clock, waits = [0.0], []
        class Terminal:
            def __enter__(self):
                return self
            def write(self, text):
                pass
            def wait(self, delay):
                waits.append(delay)
                clock[0] += delay
                return len(waits) == 3
            def read_key(self):
                return "q"
        def render(*args):
            clock[0] += 0.004
            return []
        with patch.object(panel, "Terminal", Terminal), patch.object(panel, "render", render), \
                patch.object(panel.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(sys, "stdout", io.StringIO()):
            panel.run(SimpleNamespace(anim=True), [])
        for wait in waits:
            self.assertAlmostEqual(wait, 1 / 60 - 0.004)

    def test_all_provider_borders_change_on_each_60fps_frame(self):
        view = SimpleNamespace(agents=panel.ap.AGENTS, anim=True, theme=0)
        with patch.object(panel, "card", return_value=["fixed card contents"] * 3):
            for index in range(len(view.agents)):
                frames = [panel.working_card(view, index, 50, 1 + k / 60, 2000)[0]
                          for k in range(60)]
                self.assertEqual(len(set(frames)), 60, view.agents[index]["name"])

    def test_disabled_animation_keeps_provider_borders_still(self):
        view = SimpleNamespace(agents=panel.ap.AGENTS, anim=False, theme=0)
        with patch.object(panel, "card", return_value=["fixed card contents"] * 3):
            first = panel.working_card(view, 0, 50, 1, 2000)
            later = panel.working_card(view, 0, 50, 2, 2000)
        self.assertEqual(first, later)

    def test_idle_cards_keep_content_cached(self):
        agent = next(a for a in panel.ap.AGENTS if a["name"] == "Claude")
        view = SimpleNamespace(agents=[agent], anim=True, theme=0, style=lambda i: agent)
        with patch.object(panel, "WORKING", {}), patch.object(panel, "USAGE", {}), \
                patch.object(panel, "INFLIGHT", set()), patch.object(panel, "CARDS", {}), \
                patch.object(panel, "_card", wraps=panel._card) as draw:
            first = panel.working_card(view, 0, 50, 1.001, 2000)
            later = panel.working_card(view, 0, 50, 1.001 + 1 / 60, 2000)
            self.assertEqual(draw.call_count, 1)
            self.assertNotEqual(first[0], later[0])

    def test_all_provider_internal_waves_change_on_each_60fps_frame(self):
        now = time.time()
        for agent in panel.ap.AGENTS:
            view = SimpleNamespace(agents=[agent], anim=True, theme=0, style=lambda i: agent)
            entry = {"fetched": now, "windows": [{"label": "5h", "pct": 35, "resets": now + 3600}]}
            for workers in (1, 3):
                with patch.object(panel, "WORKING", {agent["name"]: workers}), \
                        patch.object(panel, "USAGE", {agent["name"]: entry}), \
                        patch.object(panel, "INFLIGHT", set()), patch.object(panel, "CARDS", {}):
                    frames = ["".join(panel.card(view, 0, 50, 1 + k / 60, now)) for k in range(60)]
                self.assertEqual(len(set(frames)), 60, (agent["name"], workers))

    def test_animation_off_keeps_internal_waves_still(self):
        agent = panel.ap.AGENTS[0]
        view = SimpleNamespace(agents=[agent], anim=False, theme=0, style=lambda i: agent)
        with patch.object(panel, "WORKING", {agent["name"]: 1}), patch.object(panel, "USAGE", {}), \
                patch.object(panel, "INFLIGHT", set()), patch.object(panel, "CARDS", {}):
            self.assertEqual(panel.card(view, 0, 50, 1, 2000), panel.card(view, 0, 50, 2, 2000))

    def test_gradients_keep_palette_stops_and_loop_without_a_color_jump(self):
        for agent in panel.ap.AGENTS:
            stops = agent["gradient"]
            if stops != "rainbow":
                for index, color in enumerate(stops):
                    self.assertEqual(panel.ap.grad(agent, index / len(stops)), tuple(color))
            for pos in (0.0, 0.17, 0.53, 0.91):
                self.assertEqual(panel.ap.grad(agent, pos), panel.ap.grad(agent, pos + 1))
            before, after = panel.ap.grad(agent, 1 - 0.001), panel.ap.grad(agent, 0.001)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(before, after)), 1)

    def test_fast_color_math_preserves_existing_rgb_values(self):
        colors = [(0, 0, 0), (255, 255, 255), (12, 192, 64), (230, 45, 173)]
        for a in colors:
            for b in colors:
                for amount in (0, 0.01, 0.13, 0.35, 0.5, 0.85, 1):
                    expected = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
                    self.assertEqual(panel.ap.mix(a, b, amount), expected)
                    expected = tuple(round(x + (y - x) * amount) for x, y in zip((28, 28, 32), a))
                    self.assertEqual(panel.ap.tint(a, amount), expected)


if __name__ == "__main__":
    unittest.main()
