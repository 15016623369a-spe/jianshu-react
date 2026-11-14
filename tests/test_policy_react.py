"""Regression tests for controlled policy tool selection and the shared verifier."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from app.commerce_demo import prepare_database
from app.policy_workflow import PolicyRevenueRequest, run_policy_workflow

OCTOBER = {"start": "2026-10-01", "end_exclusive": "2026-11-01"}
ONE_POLICY = {"start": "2026-10-01", "end_exclusive": "2026-10-15", "region": "华南"}
SEARCH = {"tool": "search_policies"}
FINISH = {"tool": "finish"}
FIRST = {"tool": "run_segment", "segment_id": "segment:1"}
SECOND = {"tool": "run_segment", "segment_id": "segment:2"}
USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
API = "/api/v2/policy-revenue"


def _selector(actions):
    return Mock(side_effect=[
        {
            "choices": [{"message": {"content": json.dumps(action), "reasoning_content": "not for display"}}],
            "usage": USAGE,
        }
        for action in actions
    ])


class PolicyReActTests(unittest.TestCase):
    def setUp(self):
        self.database = prepare_database(Path(self.enterContext(TemporaryDirectory())))

    def run_request(self, scope=OCTOBER, *, mode="react", choose=None):
        return run_policy_workflow(self.database, PolicyRevenueRequest.model_validate(scope), mode=mode, choose=choose)

    def assert_no_answer(self, result, reason):
        self.assertEqual((result["kind"], result["reason"]), ("abstained", reason))
        self.assertIsNone(result["total_cents"])
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["evidence"], {"policies": [], "queries": []})

    def test_real_tools_support_multiple_valid_action_orders_with_fixed_evidence(self):
        fixed = self.run_request(mode="fixed")
        for first, second in ((FIRST, SECOND), (SECOND, FIRST)):
            with self.subTest(first=first):
                choose = _selector([SEARCH, first, second, FINISH])
                result = self.run_request(choose=choose)
                self.assertEqual((result["kind"], result["total_cents"], result["strategy"]), ("answer", 47700, "react"))
                for field in ("scope", "currency", "segments", "evidence"):
                    self.assertEqual(result[field], fixed[field])
                self.assertEqual([row["segment_id"] for row in result["tool_trace"] if row["tool"] == "run_segment"],
                                 [first["segment_id"], second["segment_id"]])
                self.assertEqual(choose.call_count, 4)
                self.assertEqual(result["metrics"]["model_calls"], 4)
                self.assertEqual(result["metrics"]["total_tokens"], 440)
                self.assertEqual(result["metrics"]["sql_queries"], 2)
                context = json.loads(choose.call_args_list[1].args[0][1]["content"])
                observation, = context["observations"]
                self.assertEqual(observation["tool"], "search_policies")
                self.assertEqual([row["id"] for row in observation["observation"]["segments"]],
                                 ["segment:1", "segment:2"])
                self.assertNotIn("not for display", json.dumps(result))

    def test_hybrid_fast_path_ignores_selector_for_one_fully_covered_policy(self):
        choose = Mock(side_effect=AssertionError("the fast path should not call the model"))
        result = self.run_request(ONE_POLICY, mode="hybrid", choose=choose)
        fixed = self.run_request(ONE_POLICY, mode="fixed")
        self.assertEqual((result["kind"], result["total_cents"]), ("answer", 10000))
        self.assertEqual((result["strategy"], result["routing_reason"]), ("fixed", "single_policy"))
        self.assertEqual(result["evidence"], fixed["evidence"])
        self.assertEqual((result["metrics"]["model_calls"], result["metrics"]["sql_queries"]), (0, 1))
        choose.assert_not_called()

    def test_early_finish_cannot_publish_partial_evidence_or_fall_back(self):
        for mode in ("react", "hybrid"):
            for actions, sql_queries in (([FINISH], 0), ([SEARCH, FIRST, FINISH], 1)):
                with self.subTest(mode=mode, actions=actions), self.assertLogs("app.policy_workflow", level="WARNING"):
                    result = self.run_request(mode=mode, choose=_selector(actions))
                    self.assert_no_answer(result, "verification_failed")
                    self.assertEqual(result["strategy"], "react")
                    self.assertEqual(result["metrics"]["sql_queries"], sql_queries)

    def test_pure_react_detects_coverage_gap_before_opening_sqlite(self):
        scope = {"start": "2026-09-30", "end_exclusive": "2026-10-02"}
        choose = _selector([SEARCH])
        with patch("app.policy_workflow.ReadOnlyDB") as database:
            result = self.run_request(scope, choose=choose)
        self.assert_no_answer(result, "policy_gap")
        self.assertEqual((result["metrics"]["model_calls"], result["metrics"]["sql_queries"]), (1, 0))
        self.assertEqual(result["tool_trace"][-1]["reason"], "policy_gap")
        database.assert_not_called()

    def test_unconfigured_model_and_missing_dates_short_circuit(self):
        for mode in ("react", "hybrid"):
            with self.subTest(mode=mode), patch("app.policy_workflow.ReadOnlyDB") as database:
                result = self.run_request(mode=mode)
                self.assert_no_answer(result, "model_unconfigured")
                self.assertEqual(result["metrics"]["model_calls"], 0)
                choose = Mock(side_effect=AssertionError("validate scope before calling the model"))
                clarification = self.run_request({"start": "2026-10-01"}, mode=mode, choose=choose)
                self.assertEqual((clarification["kind"], clarification["reason"]), ("clarification", "missing_dates"))
                database.assert_not_called()
                choose.assert_not_called()

    def test_duplicate_and_injected_model_actions_never_reach_sql(self):
        injection = {"tool": "run_segment", "segment_id": "segment:1", "sql": "DELETE FROM orders"}
        for actions, invalid, duplicate in (
            ([SEARCH, SEARCH, SEARCH], 0, 2),
            ([injection, {"tool": "shell"}], 2, 0),
            ([FIRST, {"tool": "run_segment", "segment_id": "segment:99"}], 2, 0),
        ):
            with self.subTest(actions=actions), patch("app.policy_workflow.ReadOnlyDB") as database:
                result = self.run_request(choose=_selector(actions))
                self.assert_no_answer(result, "no_progress")
                self.assertEqual(result["metrics"]["invalid_actions"], invalid)
                self.assertEqual(result["metrics"]["duplicate_actions"], duplicate)
                self.assertEqual(result["metrics"]["sql_queries"], 0)
                self.assertNotIn("DELETE FROM orders", json.dumps(result))
                database.assert_not_called()

    def test_decision_limit_stops_before_incomplete_evidence_can_be_returned(self):
        choose = _selector([SEARCH, FIRST])
        with patch("app.policy_react._MAX_DECISIONS", 2):
            result = self.run_request(choose=choose)
        self.assert_no_answer(result, "max_steps")
        self.assertEqual(choose.call_count, 2)
        self.assertEqual(result["metrics"]["sql_queries"], 1)

    def test_provider_failure_retains_status_and_hides_raw_error(self):
        secrets = "provider raw detail and credential"
        for problem, reason, trace_reason in (
            (HTTPError("https://example.invalid/" + secrets, 429, secrets, None, None), "model_error", "http_429"),
            (TimeoutError(secrets), "timeout", "timeout"),
        ):
            with self.subTest(problem=type(problem)), patch("app.policy_workflow.ReadOnlyDB") as database:
                result = self.run_request(choose=Mock(side_effect=problem))
                self.assert_no_answer(result, reason)
                self.assertEqual(result["tool_trace"][-1]["reason"], trace_reason)
                self.assertEqual(result["metrics"]["model_calls"], 1)
                self.assertFalse(result["metrics"]["usage_complete"])
                self.assertNotIn(secrets, json.dumps(result))
                database.assert_not_called()


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "FastAPI testing dependencies are not installed")
class PolicyReActAPITests(unittest.TestCase):
    def test_modes_are_validated_and_offline_fast_path_still_works(self):
        from app.product_api import app

        directory = self.enterContext(TemporaryDirectory())
        with patch.dict(os.environ, {
            "RAG_DATA_DIR": directory, "RAG_GATEWAY_KEY": "", "RAG_DEPLOYMENT": "local",
            "LLM_API_KEY": "", "LLM_BASE_URL": "",
        }), TestClient(app) as client:
            self.assertEqual(client.post(API, params={"mode": "unknown"}, json=OCTOBER).status_code, 422)
            pure = client.post(API, params={"mode": "react"}, json=OCTOBER)
            self.assertEqual(pure.status_code, 200)
            self.assertEqual((pure.json()["kind"], pure.json()["reason"]), ("abstained", "model_unconfigured"))
            hybrid = client.post(API, params={"mode": "hybrid"}, json=ONE_POLICY)
            self.assertEqual(hybrid.status_code, 200)
            self.assertEqual((hybrid.json()["kind"], hybrid.json()["total_cents"]), ("answer", 10000))
            self.assertEqual(hybrid.json()["metrics"]["model_calls"], 0)
