"""The frozen manual holdout is part of the release gate."""
from __future__ import annotations

import unittest

from evaluation.run_manual_holdout import evaluate


class ManualHoldoutTests(unittest.TestCase):
    def test_frozen_manual_holdout_passes(self):
        report = evaluate()
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["passed"], report["total"])
        self.assertGreaterEqual(report["total"], 12)
