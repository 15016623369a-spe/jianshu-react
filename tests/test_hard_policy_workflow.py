"""Verify scoped retrieval, ledger changes and safe plan reuse."""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app import hard_policy_data as data
from app import hard_policy_workflow as workflow
from app.hard_policy_data import prepare_hard_policy_database

OCT = ("2026-10-01", "2026-11-01")
NOV = ("2026-11-01", "2026-12-01")
JAN = ("2027-01-01", "2027-02-01")
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
        october = self.run_case(mode="plan", choose=model)
        self.assertEqual((october["kind"], october["total_cents"]), ("answer", 29280))
        self.assertEqual(october["evidence"]["query"]["ledger"], "refunds")
        november = self.run_case(window=NOV, mode="plan")
        self.assertEqual((november["kind"], november["total_cents"]), ("answer", 24640))
        self.assertEqual(november["evidence"]["query"]["ledger"], "chargebacks")
        self.assertNotEqual(october["evidence"]["bulletin"]["id"], november["evidence"]["bulletin"]["id"])
        self.assertEqual((november["metrics"]["cache_hits"], november["metrics"]["model_calls"]), (1, 0))
        model.assert_called_once()

    def test_auto_uses_fixed_fast_route_for_single_reference(self):
        model = Mock(side_effect=AssertionError("fast path should avoid model calls"))
        self.run_case(mode="plan", choose=plans({"steps": LATEST}))
        for _ in range(2):
            result = self.run_case(mode="auto", choose=model)
            self.assertEqual((result["route"], result["total_cents"]), ("fixed_fast", 29280))
            self.assertEqual((result["metrics"]["cache_hits"], result["metrics"]["model_calls"]), (0, 0))
        model.assert_not_called()

    def test_multi_document_tasks_distinguish_one_hop_from_bounded_traversal(self):
        for product, region, ledger, amount, final_id in (
            ("电水壶", "华南", "chargebacks", 21100, "bulletin-south-appliances-jan-final"),
            ("帐篷", "华北", "refunds", 49300, "bulletin-north-outdoor-jan-final"),
        ):
            with self.subTest(product=product):
                fixed = self.run_case(product, region, JAN)
                self.assertEqual((fixed["reason"], fixed["metrics"]["sql_queries"]), ("workflow_unsupported", 0))
                uniform = self.run_case(product, region, JAN, mode="uniform")
                model = plans({"steps": ["follow_references", "inspect_ledger", "query"]})
                planned = self.run_case(product, region, JAN, mode="auto", choose=model)
                replay = self.run_case(product, region, JAN, mode="auto")
                for result in (uniform, planned, replay):
                    self.assertEqual((result["kind"], result["total_cents"]), ("answer", amount))
                    self.assertEqual(result["evidence"]["query"]["ledger"], ledger)
                    path = result["evidence"]["bulletins"]
                    self.assertEqual((len(path), path[-1]["id"]), (2, final_id))
                    self.assertEqual(path[-1]["file"], "app/hard_policy_corpus/bulletins.md")
                    self.assertGreaterEqual(path[-1]["line_end"], path[-1]["line_start"])
                self.assertEqual((planned["route"], replay["route"]), ("planned", "cache"))
                self.assertEqual((replay["metrics"]["model_calls"], replay["metrics"]["sql_queries"]), (0, 1))
                model.assert_called_once()

    def test_document_revision_invalidates_plan_and_recomputes_amount(self):
        path = Path(self.enterContext(TemporaryDirectory())) / "corpus"
        shutil.copytree(data._CORPUS, path)
        with patch.object(data, "_CORPUS", path):
            model = plans(*[{"steps": ["follow_references", "inspect_ledger", "query"]}] * 2)
            original = self.run_case(window=JAN, mode="auto", choose=model)
            before = self.run_case(window=JAN, mode="auto")
            self.assertEqual((before["route"], before["total_cents"]), ("cache", 21100))
            document = path / "bulletins.md"
            revised = document.read_text().replace("每笔最多扣除人民币 7 元", "每笔最多扣除人民币 6 元", 1)
            document.write_text(revised)
            after = self.run_case(window=JAN, mode="auto", choose=model)
            replay = self.run_case(window=JAN, mode="auto")
        self.assertNotEqual(original["document_version"], after["document_version"])
        self.assertEqual((after["route"], after["total_cents"]), ("planned", 21200))
        self.assertEqual((replay["route"], replay["total_cents"]), ("cache", 21200))
        self.assertEqual(model.call_count, 2)
        self.assertEqual((after["metrics"]["cache_hits"], after["metrics"]["cache_invalidated"]), (0, 1))
        self.assertIn("document_version_changed", [item["reason"] for item in after["tool_trace"]])

    def test_auto_replans_from_existing_evidence_and_refuses_circular_references(self):
        model = plans({"steps": ["follow_references"]}, {"steps": ["inspect_ledger", "query"]})
        result = self.run_case(window=JAN, mode="auto", choose=model)
        self.assertEqual((result["route"], result["total_cents"]), ("replan", 21100))
        self.assertEqual((result["metrics"]["replans"], result["metrics"]["sql_queries"]), (1, 1))
        self.assertEqual([event["tool"] for event in result["tool_trace"]].count("find_titled_bulletin"), 1)
        cycle = self.run_case("蓝牙耳机", "华东", JAN, mode="auto", choose=plans({"steps": ["follow_references"]}))
        self.assertEqual((cycle["reason"], cycle["total_cents"], cycle["metrics"]["sql_queries"]), ("referral_cycle", None, 0))

    def test_conditional_follow_does_not_fail_after_the_final_reference(self):
        model = plans({"steps": ["find_latest_bulletin", "follow_references", "inspect_ledger", "query"]})
        result = self.run_case(window=JAN, mode="auto", choose=model)
        self.assertEqual((result["route"], result["total_cents"]), ("planned", 21100))
        self.assertEqual([event["tool"] for event in result["tool_trace"]].count("find_latest_bulletin"), 1)
        self.assertEqual((result["metrics"]["replans"], result["metrics"]["sql_queries"]), (0, 1))

    def test_auto_replans_when_cached_suffix_cannot_satisfy_next_reference(self):
        original = self.run_case(window=JAN, mode="auto", choose=plans({"steps": [
            "follow_references", "inspect_ledger", "query",
        ]}))
        key = (workflow._VERSION, self.db.resolve(), "电水壶", "华南", original["evidence"]["policy"]["id"],
               original["document_version"])
        self.cache.put(key, TITLED)
        model = plans({"steps": ["follow_references", "inspect_ledger", "query"]})
        recovered = self.run_case(window=JAN, mode="auto", choose=model)
        self.assertEqual((recovered["route"], recovered["total_cents"]), ("replan", 21100))
        self.assertEqual(tuple(recovered["metrics"][field] for field in ("cache_hits", "cache_invalidated", "replans")),
                         (1, 1, 1))
        context = json.loads(model.call_args.args[0][-1]["content"])
        self.assertEqual(context["state"]["completed"], ["find_titled_bulletin"])
        self.assertEqual(self.run_case(window=JAN, mode="auto")["route"], "cache")

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
