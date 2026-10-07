"""Border cadence is independent of the slower usage-card contents."""

import sys
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import usage_panel as panel


class UsageBorderTests(unittest.TestCase):
    def test_usage_frame_clock_includes_render_time(self):
        if hasattr(panel, "frame_time"):
            self.assertEqual(panel.frame_time(SimpleNamespace(anim=True)), 1 / 60)
            self.assertEqual(panel.frame_time(SimpleNamespace(anim=False)), 1.0)
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
        view = SimpleNamespace(agents=panel.ap.AGENTS, anim=True)
        with patch.object(panel, "card", return_value=["fixed card contents"] * 3):
            for index in range(len(view.agents)):
                frames = [panel.working_card(view, index, 50, 1 + k / 60, 2000)[0]
                          for k in range(60)]
                self.assertEqual(len(set(frames)), 60, view.agents[index]["name"])

    def test_disabled_animation_keeps_provider_borders_still(self):
        view = SimpleNamespace(agents=panel.ap.AGENTS, anim=False)
        with patch.object(panel, "card", return_value=["fixed card contents"] * 3):
            first = panel.working_card(view, 0, 50, 1, 2000)
            later = panel.working_card(view, 0, 50, 2, 2000)
        self.assertEqual(first, later)

    def test_smooth_borders_keep_card_content_cached(self):
        agent = next(a for a in panel.ap.AGENTS if a["name"] == "Claude")
        view = SimpleNamespace(agents=[agent], anim=True, theme=0, style=lambda i: agent)
        with patch.object(panel, "WORKING", {"Claude": 1}), patch.object(panel, "USAGE", {}), \
                patch.object(panel, "INFLIGHT", set()), patch.object(panel, "CARDS", {}), \
                patch.object(panel, "_card", wraps=panel._card) as draw:
            first = panel.working_card(view, 0, 50, 1.001, 2000)
            later = panel.working_card(view, 0, 50, 1.001 + 1 / 60, 2000)
            self.assertEqual(draw.call_count, 1)
            self.assertNotEqual(first[0], later[0])


if __name__ == "__main__":
    unittest.main()
