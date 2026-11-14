"""Guard and evidence regressions for the opt-in multi-source experiment."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.multisource_data import prepare_multisource_database
from app.multisource_workflow import MultiSourceRequest, run_multisource_workflow


def select_actions(*actions):
    return Mock(side_effect=[
        {"choices": [{"message": {"content": json.dumps(action)}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        for action in actions
    ])


class MultiSourceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.database = prepare_multisource_database(Path(self.enterContext(TemporaryDirectory())))

    def run_case(self, start="2026-10-11", end="2026-10-14", region=None, *, mode="fixed", choose=None):
        request = MultiSourceRequest(start=start, end_exclusive=end, region=region)
        return run_multisource_workflow(self.database, request, mode=mode, choose=choose)

    def test_react_follows_a_reference_discovered_on_the_second_source(self):
        choose = select_actions(
            {"tool": "discover_groups"}, {"tool": "lookup_policies", "ids": ["group:1"]},
            {"tool": "lookup_bulletins", "ids": ["bulletin-gift-heading"]},
            {"tool": "lookup_bulletins", "ids": ["bulletin-gift-special"]},
            {"tool": "aggregate_groups", "ids": ["group:1"]}, {"tool": "finish"},
        )
        react = self.run_case(mode="react", choose=choose)
        fixed = self.run_case()
        self.assertEqual((react["kind"], react["total_cents"]), ("answer", 10340))
        self.assertEqual((react["metrics"]["sql_queries"], react["metrics"]["model_calls"]), (2, 6))
        self.assertEqual(react["groups"], fixed["groups"])
        self.assertEqual(react["evidence"], fixed["evidence"])
        self.assertEqual([doc["id"] for doc in react["evidence"]["documents"]][-2:],
                         ["bulletin-gift-heading", "bulletin-gift-special"])
        before = json.loads(choose.call_args_list[2].args[0][1]["content"])
        after = json.loads(choose.call_args_list[3].args[0][1]["content"])
        self.assertNotIn("bulletin-gift-special", json.dumps(before))
        self.assertIn("bulletin-gift-special", json.dumps(after))

    def test_unobserved_ids_and_model_sql_are_blocked(self):
        choose = select_actions(
            {"tool": "lookup_bulletins", "ids": ["bulletin-gift-special"]},
            {"tool": "aggregate_groups", "ids": ["group:1"], "sql": "DELETE FROM orders"},
        )
        result = self.run_case(mode="react", choose=choose)
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "no_progress"))
        self.assertEqual((result["metrics"]["invalid_actions"], result["metrics"]["sql_queries"]), (2, 0))
        self.assertNotIn("DELETE", json.dumps(result))

    def test_early_finish_cannot_publish_partial_evidence(self):
        choose = select_actions(
            {"tool": "discover_groups"}, {"tool": "lookup_policies", "ids": ["group:1"]},
            {"tool": "finish"}, {"tool": "finish"},
        )
        result = self.run_case(mode="react", choose=choose)
        self.assertEqual(result["reason"], "no_progress")
        self.assertEqual(result["metrics"]["sql_queries"], 1)
        self.assertIsNone(result["total_cents"])
        self.assertFalse(any(result["evidence"].values()))

    def test_early_finish_can_recover_with_reported_ready_group(self):
        choose = select_actions(
            {"tool": "discover_groups"}, {"tool": "lookup_policies", "ids": ["group:1"]},
            {"tool": "finish"}, {"tool": "aggregate_groups", "ids": ["group:1"]}, {"tool": "finish"},
        )
        result = self.run_case("2026-10-01", "2026-10-07", "华南", mode="react", choose=choose)
        self.assertEqual((result["kind"], result["total_cents"]), ("answer", 25200))
        self.assertEqual(result["metrics"]["invalid_actions"], 1)
        before = json.loads(choose.call_args_list[3].args[0][1]["content"])
        after = json.loads(choose.call_args_list[4].args[0][1]["content"])
        self.assertEqual(before["available"]["aggregate_groups"], ["group:1"])
        self.assertFalse(before["available"]["finish"])
        self.assertTrue(after["available"]["finish"])

    def test_missing_bulletin_abstains_after_reading_first_document(self):
        result = self.run_case("2026-10-25", "2026-10-26", "华东")
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "bulletin_missing"))
        self.assertEqual(result["metrics"]["sql_queries"], 1)
        self.assertFalse(result["groups"])
        self.assertIsNone(result["total_cents"])

    def test_no_refunds_returns_zero_after_discovery(self):
        result = self.run_case("2026-10-05", "2026-10-06", "华北")
        self.assertEqual((result["kind"], result["total_cents"], result["groups"]), ("answer", 0, []))
        self.assertEqual(result["metrics"]["sql_queries"], 1)

    def test_verifier_rejects_tampered_query(self):
        from app import multisource_workflow

        def tampered(*args, **kwargs):
            return {**aggregate(*args, **kwargs), "parameters": ["invalid"]}

        aggregate = multisource_workflow.aggregate_group
        with patch.object(multisource_workflow, "aggregate_group", side_effect=tampered):
            result = self.run_case()
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "verification_failed"))
        self.assertFalse(any(result["evidence"].values()))


if __name__ == "__main__":
    unittest.main()
