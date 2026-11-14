"""Contract and guard regressions for multi-source Plan-and-Execute."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.multisource_data import prepare_multisource_database
from app.multisource_workflow import MultiSourceRequest, run_multisource_workflow


COMPLETE_PLAN = {
    "steps": [
        {"tool": "discover_groups"},
        {"tool": "lookup_policies"},
        {"tool": "lookup_bulletins", "repeat": 2},
        {"tool": "aggregate_groups"},
    ],
}
USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def select_plans(*plans: dict) -> Mock:
    return Mock(side_effect=[
        {"choices": [{"message": {"content": json.dumps(plan)}}], "usage": USAGE}
        for plan in plans
    ])


class PlanExecuteWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.database = prepare_multisource_database(Path(self.enterContext(TemporaryDirectory())))

    def run_case(self, start="2026-10-01", end="2026-10-13", region=None, *, mode="plan", choose=None):
        request = MultiSourceRequest(start=start, end_exclusive=end, region=region)
        return run_multisource_workflow(self.database, request, mode=mode, choose=choose)

    def assert_no_amount(self, result):
        self.assertEqual(result["kind"], "abstained")
        self.assertIsNone(result["total_cents"])
        self.assertEqual(result["groups"], [])
        self.assertFalse(any(result["evidence"].values()))

    def test_one_plan_binds_discovered_ids_and_returns_identical_verified_evidence(self):
        choose = select_plans(COMPLETE_PLAN)
        planned = self.run_case(choose=choose)
        fixed = self.run_case(mode="fixed")

        self.assertEqual((planned["mode"], planned["kind"], planned["total_cents"]), ("plan", "answer", 81040))
        for key in ("scope", "groups", "evidence"):
            self.assertEqual(planned[key], fixed[key])
        self.assertEqual(planned["metrics"]["model_calls"], 1)
        self.assertEqual(planned["metrics"]["total_tokens"], USAGE["total_tokens"])
        self.assertEqual(planned["metrics"]["sql_queries"], fixed["metrics"]["sql_queries"])
        choose.assert_called_once()
        events = planned["tool_trace"]
        self.assertIn("plan", [event["tool"] for event in events if event["status"] == "ok"])
        bulletin_reads = [event["ids"] for event in events if event["tool"] == "lookup_bulletins"]
        self.assertEqual(len(bulletin_reads), 2)
        self.assertCountEqual(bulletin_reads[0], ["bulletin-outdoor-north", "bulletin-gift-heading"])
        self.assertEqual(bulletin_reads[1], ["bulletin-gift-special"])

    def test_invalid_dependency_order_can_be_replanned_once(self):
        reversed_plan = {"steps": [{"tool": "aggregate_groups"}, {"tool": "discover_groups"}]}
        choose = select_plans(reversed_plan, COMPLETE_PLAN)

        result = self.run_case(choose=choose)

        self.assertEqual((result["kind"], result["total_cents"]), ("answer", 81040))
        self.assertEqual(result["metrics"]["model_calls"], 2)
        self.assertEqual(result["metrics"]["sql_queries"], 5)
        self.assertIn("replan", [event["tool"] for event in result["tool_trace"] if event["status"] == "ok"])

    def test_model_cannot_supply_sql_or_choose_unobserved_ids(self):
        for forged in (
            {"steps": [{"tool": "discover_groups", "sql": "DROP TABLE orders"}]},
            {"steps": [{"tool": "discover_groups"}, {"tool": "lookup_policies", "ids": ["group:9"]}]},
            {"steps": [{"tool": "discover_groups"}, {"tool": "lookup_bulletins", "repeat": 3}]},
        ):
            with self.subTest(plan=forged), patch("app.multisource_workflow.discover_groups") as discovery:
                choose = select_plans(forged, forged)
                result = self.run_case(choose=choose)

                self.assert_no_amount(result)
                self.assertEqual(result["reason"], "plan_incomplete")
                self.assertEqual(result["metrics"]["model_calls"], 2)
                self.assertEqual(result["metrics"]["sql_queries"], 0)
                self.assertNotIn("DROP TABLE", json.dumps(result))
                discovery.assert_not_called()

    def test_plan_cannot_publish_amount_if_final_query_evidence_is_missing(self):
        without_aggregation = {"steps": COMPLETE_PLAN["steps"][:-1]}
        choose = select_plans(without_aggregation, without_aggregation)

        result = self.run_case(choose=choose)

        self.assert_no_amount(result)
        self.assertEqual(result["reason"], "plan_incomplete")
        self.assertEqual(result["metrics"]["model_calls"], 2)
        self.assertFalse(any(event["tool"] == "aggregate_groups" for event in result["tool_trace"]))

    def test_new_referral_triggers_replan_with_retained_policy_and_query_evidence(self):
        first_plan = {"steps": [
            {"tool": "discover_groups"}, {"tool": "lookup_policies"},
            {"tool": "lookup_bulletins"}, {"tool": "aggregate_groups"},
        ]}
        remaining = {"steps": [{"tool": "lookup_bulletins"}, {"tool": "aggregate_groups"}]}
        choose = select_plans(first_plan, remaining)

        result = self.run_case(choose=choose)

        self.assertEqual((result["kind"], result["total_cents"]), ("answer", 81040))
        self.assertEqual(result["metrics"]["model_calls"], 2)
        self.assertEqual(result["metrics"]["sql_queries"], 5)
        self.assertEqual(result["evidence"], self.run_case(mode="fixed")["evidence"])
        before, after = (json.loads(call.args[0][1]["content"]) for call in choose.call_args_list)
        self.assertNotIn("bulletin-gift-special", json.dumps(before))
        self.assertEqual(after["available"]["lookup_bulletins"], ["bulletin-gift-special"])
        self.assertFalse(after["available"]["discover_groups"])
        self.assertFalse(after["available"]["lookup_policies"])
        reads = [event for event in result["tool_trace"] if event["tool"] == "discover_groups"]
        self.assertEqual(len(reads), 1)

    def test_missing_source_does_not_trigger_replan_or_leak_partial_amount(self):
        interleaved_plan = {"steps": [
            {"tool": "discover_groups"}, {"tool": "lookup_policies"},
            {"tool": "aggregate_groups"}, {"tool": "lookup_bulletins"},
        ]}
        choose = select_plans(interleaved_plan)

        result = self.run_case("2026-10-25", "2026-10-30", "华东", choose=choose)

        self.assert_no_amount(result)
        self.assertEqual(result["reason"], "bulletin_missing")
        self.assertEqual(result["metrics"]["sql_queries"], 2)
        self.assertTrue(any(event["tool"] == "aggregate_groups" and event["status"] == "ok"
                            for event in result["tool_trace"]))
        self.assertEqual(result["metrics"]["model_calls"], 1)
        choose.assert_called_once()

    def test_clarification_skips_the_planner_and_sql(self):
        choose = Mock(side_effect=AssertionError("Do not call the model for incomplete dates"))

        result = self.run_case(end=None, choose=choose)

        self.assertEqual((result["kind"], result["reason"]), ("clarification", "missing_dates"))
        self.assertEqual(result["metrics"]["sql_queries"], 0)
        self.assertEqual(result["metrics"]["model_calls"], 0)
        choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
