import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import keep_awake


class DockedDisplayProbeTests(unittest.TestCase):
    def probe(self, power_output="", builtin=False, display_error=0):
        def displays(capacity, output, count):
            output[0] = 3
            count._obj.value = 1
            return display_error

        graphics = SimpleNamespace(CGGetOnlineDisplayList=Mock(side_effect=displays),
                                   CGDisplayIsBuiltin=Mock(return_value=builtin))
        power = SimpleNamespace(returncode=0, stdout=power_output)
        with patch.object(keep_awake, "SUPPORTED", True), \
                patch.object(keep_awake.ctypes.util, "find_library", return_value="CoreGraphics"), \
                patch.object(keep_awake.ctypes, "CDLL", return_value=graphics), \
                patch.object(keep_awake.subprocess, "run", return_value=power) as run:
            result = keep_awake._external_display_on_ac()
        return result, run

    def test_external_display_requires_confirmed_ac_power(self):
        for output, expected in [("Now drawing from 'AC Power'\n", True),
                                 ("Now drawing from 'Battery Power'\n", False),
                                 ("", None), ("unrecognized power source", None)]:
            with self.subTest(output=output):
                result, run = self.probe(output)
                self.assertIs(result, expected)
                run.assert_called_once()

    def test_builtin_display_does_not_count_as_docked(self):
        result, run = self.probe("Now drawing from 'AC Power'\n", builtin=True)
        self.assertIs(result, False)
        run.assert_not_called()

    def test_failed_display_enumeration_is_unknown(self):
        result, run = self.probe(display_error=1000)
        self.assertIsNone(result)
        run.assert_not_called()

    def test_other_platforms_do_not_load_macos_frameworks(self):
        with patch.object(keep_awake, "SUPPORTED", False), \
                patch.object(keep_awake.ctypes, "CDLL") as load:
            self.assertIs(keep_awake._external_display_on_ac(), False)
            load.assert_not_called()
