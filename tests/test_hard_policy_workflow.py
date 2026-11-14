"""Verify scoped retrieval, ledger changes and safe plan reuse."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app import hard_policy_workflow as workflow
from app.hard_policy_data import prepare_hard_policy_database

OCT = ("2026-10-01", "2026-11-01")
NOV = ("2026-11-01", "2026-12-01")
LATEST = ("find_latest_bulletin", "inspect_ledger", "query")
TITLED = ("find_titled_bulletin", "inspect_ledger", "query")


def plans(*responses):
    return Mock(side_effect=[
        {"choices": [{"message": {"content": json.dumps(value)}}],
         "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}}
        for value in responses
    ])


class HardPolicyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db = prepare_hard_policy_database(Path(self.enterContext(TemporaryDirectory())))
        self.cache = workflow.HardPlanCache()

    def run_case(self, product="电水壶", region="华南", window=OCT, mode="fixed", choose=None):
        scope = workflow.HardPolicyRequest(product=product, region=region, start=window[0], end_exclusive=window[1])
        return workflow.run_hard_policy(self.db, scope, mode=mode, choose=choose, cache=self.cache)

    def test_cache_reuses_sequence_but_reresolves_document_and_ledger(self):
        model = plans({"steps": LATEST})
        october = self.run_case(mode="auto", choose=model)
        self.assertEqual((october["kind"], october["total_cents"]), ("answer", 29280))
        self.assertEqual(october["evidence"]["query"]["ledger"], "refunds")
        november = self.run_case(window=NOV, mode="auto")
        self.assertEqual((november["kind"], november["total_cents"]), ("answer", 24640))
        self.assertEqual(november["evidence"]["query"]["ledger"], "chargebacks")
        self.assertNotEqual(october["evidence"]["bulletin"]["id"], november["evidence"]["bulletin"]["id"])
        self.assertEqual((november["metrics"]["cache_hits"], november["metrics"]["model_calls"]), (1, 0))
        model.assert_called_once()

    def test_query_joins_only_verified_ledger_and_policy_specificity(self):
        for product, ledger in (("电水壶", "orders"), ("空气炸锅", "chargebacks")):
            with self.subTest(product=product):
                answer = self.run_case(product=product, region="华东")
                self.assertEqual(answer["kind"], "answer")
                self.assertEqual(answer["evidence"]["query"]["ledger"], ledger)
                self.assertEqual(any(t["tool"] == "inspect_ledger" and t["status"] == "ok"
                                     for t in answer["tool_trace"]), ledger != "orders")

    def test_missing_or_conflicting_authority_stops_without_sql_or_amount(self):
        for product, area, reason in (
            ("清仓手表", "华东", "policy_missing"),
            ("蓝牙耳机", "华东", "bulletin_missing"),
            ("帐篷", "华东", "bulletin_conflict"),
        ):
            with self.subTest(product=product):
                result = self.run_case(product=product, region=area)
                self.assertEqual((result["kind"], result["reason"]), ("abstained", reason))
                self.assertEqual(result["metrics"]["sql_queries"], 0)
                self.assertIsNone(result["total_cents"])
                self.assertFalse(any(result["evidence"].values()))

    def test_incomplete_plan_preserves_bulletin_for_one_replan(self):
        model = plans({"steps": ["find_titled_bulletin"]}, {"steps": ["inspect_ledger", "query"]})
        result = self.run_case("帐篷", "华北", mode="plan", choose=model)
        self.assertEqual((result["kind"], result["total_cents"]), ("answer", 144200))
        self.assertEqual((result["metrics"]["model_calls"], result["metrics"]["replans"]), (2, 1))
        self.assertEqual(sum(t["tool"] == "find_titled_bulletin" for t in result["tool_trace"]), 1)
        latest_context = json.loads(model.call_args_list[-1].args[0][-1]["content"])
        self.assertEqual(latest_context["state"]["resolved_ledger"], "refunds")
        self.assertEqual(self.run_case("帐篷", "华北", mode="plan")["metrics"]["cache_hits"], 1)

    def test_invalid_plan_cannot_execute_or_be_cached(self):
        model = plans({"steps": ["query"], "sql": "DROP TABLE orders"}, {"steps": ["query"]})
        result = self.run_case(mode="plan", choose=model)
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "plan_incomplete"))
        self.assertEqual((result["metrics"]["model_calls"], result["metrics"]["sql_queries"]), (2, 0))
        self.assertNotIn("DROP TABLE", json.dumps(result))
        self.assertEqual(self.run_case(mode="plan")["reason"], "model_unconfigured")

    def test_bad_verified_binding_does_not_promote_plan(self):
        aggregate = workflow.aggregate_hard_group

        def tampered(*args, **kwargs):
            return {**aggregate(*args, **kwargs), "parameters": []}

        with patch.object(workflow, "aggregate_hard_group", side_effect=tampered):
            invalid = self.run_case(mode="plan", choose=plans({"steps": LATEST}))
        self.assertEqual(invalid["reason"], "verification_failed")
        self.assertEqual(self.run_case(mode="plan")["reason"], "model_unconfigured")

    def test_incompatible_ledger_schema_stops_before_query(self):
        schema = {"ledger": "refunds", "tables": [{"name": "refunds", "columns": ["order_id"]}]}
        with patch.object(workflow, "inspect_ledger", return_value=schema):
            result = self.run_case(mode="plan", choose=plans({"steps": LATEST}))
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "verification_failed"))
        self.assertEqual(result["metrics"]["sql_queries"], 0)
        self.assertIsNone(result["total_cents"])


if __name__ == "__main__":
    unittest.main()
