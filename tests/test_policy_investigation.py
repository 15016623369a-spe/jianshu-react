"""Protect cross-period reconciliation and the natural-language API boundary."""
from __future__ import annotations

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import policy_investigation as investigation
from app.hard_policy_data import TextPassage, prepare_hard_policy_database
from app.product_api import app

API = "/api/v2/policy-investigation"
QUESTION = "华南电水壶 2026 年 10 月和 11 月的净营收差了多少？如果两个月都按 10 月口径重算，差额又是多少？"


class PolicyInvestigationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.db = prepare_hard_policy_database(root)

    def ask(self, question=QUESTION, **kwargs):
        return investigation.run_policy_investigation(self.db, investigation.InvestigationRequest(question=question), **kwargs)

    def test_flagship_uses_actual_ledgers_and_recalculates_same_orders(self):
        model = Mock(side_effect=AssertionError("explicit policy path should avoid the model"))
        result = self.ask(choose=model)
        self.assertEqual(result["kind"], "answer")
        self.assertEqual([part["net_cents"] for part in result["actual"]], [29280, 24640])
        self.assertEqual([part["net_cents"] for part in result["counterfactual"]], [29280, 25200])
        self.assertEqual((result["actual_delta_cents"], result["counterfactual_delta_cents"], result["policy_effect_cents"]),
                         (-4640, -4080, -560))
        self.assertEqual(result["metrics"]["sql_queries"], 3)
        self.assertEqual(result["scope"]["baseline_period"], 0)
        self.assertEqual([p["segments"][0]["query"]["ledger"] for p in result["actual"]], ["refunds", "chargebacks"])
        november_counter = result["counterfactual"][1]["segments"][0]
        self.assertEqual(november_counter["query"]["parameters"], ["电水壶", "华南", "2026-11-01", "2026-12-01", 500])
        self.assertEqual(november_counter["documents"][-1]["id"], "bulletin-south-appliances-oct")
        model.assert_not_called()

    def test_single_month_cross_year_baseline(self):
        result = self.ask(
            "2027 年 1 月华南电水壶净营收是多少？如果都按 2026 年 10 月口径重算呢？",
        )
        self.assertEqual(result["kind"], "answer")
        self.assertEqual([part["net_cents"] for part in result["actual"]], [21100])
        self.assertEqual([part["net_cents"] for part in result["counterfactual"]], [22000])
        self.assertEqual(result["single_period_delta_cents"], -900)
        self.assertEqual(result["scope"]["baseline_rule_period"]["start"], "2026-10-01")
        self.assertEqual(result["metrics"]["sql_queries"], 2)

    def test_comparison_direction_reverses_delta_and_unified_delta(self):
        result = self.ask(
            "华南电水壶 2026 年 10 月比 11 月高多少？如果都按 10 月口径重算呢？",
        )
        self.assertEqual(result["kind"], "answer")
        self.assertEqual(result["scope"]["comparison_sign"], -1)
        self.assertEqual(result["actual_delta_cents"], 4640)
        self.assertEqual(result["counterfactual_delta_cents"], 4080)
        self.assertEqual(result["policy_effect_cents"], 560)
        self.assertEqual([part["net_cents"] for part in result["actual"]], [29280, 24640])

    def test_auto_uses_verified_lexical_hit_without_model(self):
        question = "华南电水壶2026年12月净营收是多少？"
        select = Mock(side_effect=AssertionError("a verified lexical match should avoid the model"))
        result = self.ask(question, choose=select)
        self.assertEqual((result["kind"], result["actual"][0]["net_cents"]), ("answer", 6000))
        self.assertEqual((result["metrics"]["lexical_hits"], result["metrics"]["model_fallbacks"]), (1, 0))
        lexical = [step for step in result["retrieval_trace"] if step["stage"] == "lexical"]
        self.assertEqual(lexical[0]["selected"]["id"], "bulletin-south-kettle-dec-investigation")
        self.assertLessEqual(len(lexical[0]["candidates"]), 5)
        select.assert_not_called()

    def test_model_runs_after_lexical_cutoff_but_still_needs_grounded_candidate(self):
        question = "华南电水壶2026年12月净营收是多少？"
        snapshot = investigation.load_investigation_snapshot()
        names = ("摘要", "问答", "背景", "笔记", "说明", "培训")
        decoys = tuple(TextPassage(
            title=f"冬季原路实退对账{name}",
            quote=f"文号：decoy-{index}。\n不得作为结算依据。",
            source="bulletin", file="test/decoys.md", line_start=index + 1, line_end=index + 1,
        ) for index, name in enumerate(names))
        snapshot = replace(snapshot, passages=snapshot.passages + decoys)
        alias_only = self.ask(question, mode="lexical", snapshot=snapshot)
        self.assertEqual((alias_only["kind"], alias_only["metrics"]["sql_queries"]), ("abstained", 0))

        def proposed(title):
            return Mock(return_value={
                "choices": [{"message": {"content": json.dumps({"query": title}, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 23, "completion_tokens": 5},
            })

        select = proposed("华南电水壶年末订单售后实退通知")
        result = self.ask(question, choose=select, snapshot=snapshot)
        self.assertEqual((result["kind"], result["actual"][0]["net_cents"]), ("answer", 6000))
        self.assertEqual(result["metrics"]["model_fallbacks"], 1)
        phases = [step for step in result["retrieval_trace"] if step["source"] == "bulletin"]
        self.assertEqual([step["stage"] for step in phases], ["exact", "lexical", "model"])
        self.assertIsNone(phases[1]["selected"])
        self.assertEqual(phases[-1]["selected"]["id"], "bulletin-south-kettle-dec-investigation")
        select.assert_called_once()

        bad = self.ask(question, choose=proposed("冬季原路实退对账摘要"), snapshot=snapshot)
        self.assertEqual((bad["kind"], bad["metrics"]["sql_queries"], bad["actual"]), ("abstained", 0, []))

    def test_both_delta_and_baseline_preserve_direction_on_reversed_month_mentions(self):
        result = self.ask("比较华南电水壶十一月和十月净营收。两个期间统一按十一月政策重算。")
        self.assertEqual(result["kind"], "answer")
        self.assertEqual(result["scope"]["assumed_year"], 2026)
        self.assertEqual(result["scope"]["baseline_period"], 1)
        self.assertEqual(result["actual_delta_cents"], -4640)
        self.assertEqual(result["counterfactual"][1]["net_cents"], 24640)

    def test_missing_later_period_authority_prevents_all_query_execution(self):
        result = self.ask("2026年10月与12月华东电水壶净营收相差多少？")
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "policy_missing"))
        self.assertEqual((result["actual"], result["counterfactual"]), ([], []))
        self.assertEqual(result["metrics"]["sql_queries"], 0)
        self.assertIsNone(result["actual_delta_cents"])

    def test_rejected_unsupported_filters_and_counterfactual_scope(self):
        for question, reason in (
            ("2026年11月华南电水壶净营收，请只算订单金额一百元以上。", "unsupported_scope"),
            ("华南电水壶2026年10月和11月的净营收差额，统一口径重算。", "missing_baseline"),
            ("华南电水壶2026年10月和11月净营收差额，两个月都按9月口径重算。", "missing_baseline"),
            ("电水壶2026年10月和11月净营收差额？", "missing_region"),
            ("华南华东电水壶2026年10月净营收多少？", "ambiguous_region"),
        ):
            with self.subTest(reason=reason, question=question):
                result = self.ask(question)
                self.assertEqual((result["kind"], result["reason"], result["metrics"]["sql_queries"]),
                                 ("clarification", reason, 0))

    def test_query_mismatch_does_not_publish_partial_results(self):
        execute = investigation.aggregate_hard_group

        def tamper_november(*args, **kwargs):
            result = execute(*args, **kwargs)
            if result["ledger"] == "chargebacks":
                result["parameters"] = []
            return result

        with patch.object(investigation, "aggregate_hard_group", side_effect=tamper_november):
            result = self.ask()
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "verification_failed"))
        self.assertEqual((result["actual"], result["counterfactual"]), ([], []))


class PolicyInvestigationAPITests(unittest.TestCase):
    def test_natural_entry_is_default_auto_and_rejects_user_sql_and_big_json(self):
        with TemporaryDirectory() as root, patch.dict(os.environ, {
            "RAG_DATA_DIR": root, "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "", "RAG_POSTGRES_HOST": "",
            "RAG_LANGSMITH_EXPORT": "false", "LLM_API_KEY": "", "LLM_BASE_URL": "",
        }), TestClient(app) as client:
            response = client.post(API, json={"question": QUESTION})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((response.json()["kind"], response.json()["mode"]), ("answer", "auto"))
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.post(API, json={"question": QUESTION, "sql": "SELECT * FROM orders"}).status_code, 422)
            self.assertEqual(client.post(API, json={"question": "x" * (17 * 1024)}).status_code, 413)
            self.assertEqual(client.post(API, params={"mode": "bogus"}, json={"question": QUESTION}).status_code, 422)
