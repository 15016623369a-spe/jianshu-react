"""M5 gate keeps the executable multi-domain slice green."""
from __future__ import annotations

import unittest

from evaluation.run_m5_gate import evaluate


class M5GateTests(unittest.TestCase):
    def test_executable_m5_slice_passes(self):
        report = evaluate()
        self.assertEqual(report["executed"], 108)
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["wrong_numeric"], 0)
        self.assertEqual(report["citation_precision"], 1.0)
        self.assertEqual(report["abstain_pass"], 1.0)
