"""M5 gate keeps the executable multi-domain slice green."""
from __future__ import annotations

import unittest

from evaluation.run_m5_gate import evaluate


class M5GateTests(unittest.TestCase):
    def test_full_offline_m5_suite_has_no_wrong_numbers(self):
        report = evaluate()
        self.assertEqual(report["executed"], 150)
        self.assertEqual(report["wrong_numeric"], 0)
        self.assertEqual(report["citation_precision"], 1.0)
        self.assertEqual(report["abstain_pass"], 1.0)
        # Four buried-backlink cases intentionally require the model fallback.
        self.assertEqual(
            {row["id"] for row in report["failed"]},
            {
                "policy_grounding_holdout-humidifier-buried-backlink",
                "policy_grounding_holdout-humidifier-compare-buried-backlink",
                "policy_grounding_holdout-router-buried-backlink",
                "policy_grounding_holdout-router-compare-buried-backlink",
            },
        )
