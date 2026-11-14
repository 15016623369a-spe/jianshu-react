"""Regression coverage for the fixed policy-to-SQL workflow and its API boundary."""
from __future__ import annotations

import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from app import policy_workflow
from app.commerce_demo import prepare_database
from app.policy_workflow import PolicyRevenueRequest, run_policy_workflow
from app.tools.db import ReadOnlyDB

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

OCTOBER = {"start": "2026-10-01", "end_exclusive": "2026-11-01"}
FIRST_POLICY = "refund-2026-10-a"
SECOND_POLICY = "refund-2026-10-b"
API = "/api/v2/policy-revenue"


class PolicyWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.database = prepare_database(Path(directory))

    def run_request(self, **scope):
        return run_policy_workflow(self.database, PolicyRevenueRequest.model_validate(scope))

    def test_cross_version_total_uses_distinct_policy_and_sql_evidence(self):
        result = self.run_request(**OCTOBER, region="华南")
        self.assertEqual(result["kind"], "answer")
        self.assertEqual(result["total_cents"], 17700)
        self.assertEqual(result["scope"], {**OCTOBER, "region": "华南"})
        self.assertEqual(result["metrics"]["model_calls"], 0)
        segments = result["segments"]
        self.assertEqual([
            (item["start"], item["end_exclusive"], item["policy_id"], item["revenue_cents"], item["order_count"])
            for item in segments
        ], [
            ("2026-10-01", "2026-10-15", FIRST_POLICY, 10000, 1),
            ("2026-10-15", "2026-11-01", SECOND_POLICY, 7700, 1),
        ])
        self.assertEqual(result["metrics"]["sql_queries"], len(segments))
        policies = {item["evidence_id"]: item for item in result["evidence"]["policies"]}
        queries = {item["evidence_id"]: item for item in result["evidence"]["queries"]}
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(policies), 2)
        with closing(sqlite3.connect(self.database)) as db:
            for item in segments:
                policy = policies[item["policy_evidence_id"]]
                self.assertEqual(policy["id"], item["policy_id"])
                self.assertTrue(policy["quote"].strip())
                query = queries[item["sql_evidence_id"]]
                for value in (item["start"], item["end_exclusive"], "华南"):
                    self.assertIn(value, query["parameters"])
                    self.assertNotIn(value, query["sql"])
                with closing(db.execute(query["sql"], query["parameters"])) as cursor:
                    self.assertEqual([list(row) for row in cursor], [list(row) for row in query["rows"]])

    def test_conflicting_policy_versions_are_rejected_before_querying(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        corpus = Path(policy_workflow.__file__).with_name("policy_corpus")
        for name in (FIRST_POLICY, SECOND_POLICY):
            filename = f"{name}.md"
            document = (corpus / filename).read_text(encoding="utf-8")
            if name == SECOND_POLICY:
                header, metadata, body = document.split("---\n", 2)
                new_metadata = {**json.loads(metadata), "effective_from": "2026-10-14"}
                document = "---\n".join((header, json.dumps(new_metadata) + "\n", body))
            (directory / filename).write_text(document, encoding="utf-8")
        with patch("app.policy_workflow._POLICY_DIR", directory), patch("app.policy_workflow.ReadOnlyDB") as db:
            result = self.run_request(**OCTOBER)
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "policy_conflict"))
        self.assertIsNone(result["total_cents"])
        self.assertFalse(result["evidence"]["queries"])
        db.assert_not_called()

    def test_query_failure_on_later_segment_hides_the_partial_result(self):
        execute = policy_workflow._execute_segment

        def execute_with_timeout(db, segment, region, number):
            if number == 2:
                raise TimeoutError("simulated second-segment timeout")
            return execute(db, segment, region, number)

        with patch("app.policy_workflow._execute_segment", side_effect=execute_with_timeout), self.assertLogs(
            "app.policy_workflow", level="WARNING",
        ):
            result = self.run_request(**OCTOBER)
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "source_unavailable"))
        self.assertIsNone(result["total_cents"])
        self.assertFalse(result["segments"])
        self.assertFalse(result["evidence"]["queries"])

    def test_effective_day_is_counted_once_with_the_new_rule(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executemany(
                "INSERT INTO orders(id, ordered_on, region, amount_cents, refund_cents) VALUES (?, ?, ?, ?, ?)",
                [(2001, "2026-10-14", "华北", 1000, 600), (2002, "2026-10-15", "华北", 1000, 600)],
            )
        answer = self.run_request(start="2026-10-14", end_exclusive="2026-10-16", region="华北")
        self.assertEqual(answer["kind"], "answer")
        self.assertEqual(answer["total_cents"], 1100)
        self.assertEqual([(s["policy_id"], s["revenue_cents"], s["order_count"]) for s in answer["segments"]],
                         [(FIRST_POLICY, 400, 1), (SECOND_POLICY, 700, 1)])

    def test_cap_is_applied_to_each_order_before_sum(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO orders(id, ordered_on, region, amount_cents, refund_cents) VALUES (?, ?, ?, ?, ?)",
                (2003, "2026-10-22", "华南", 1000, 250),
            )
        answer = self.run_request(start="2026-10-21", end_exclusive="2026-10-23", region="华南")
        self.assertEqual(answer["kind"], "answer")
        self.assertEqual(answer["total_cents"], 8450)
        segment, = answer["segments"]
        self.assertEqual((segment["order_count"], segment["gross_cents"], segment["deducted_cents"]), (2, 9000, 550))

    def test_verifier_abstains_and_hides_numbers_if_any_evidence_is_corrupted(self):
        execute = policy_workflow._execute_segment

        def corrupt_arithmetic(evidence):
            evidence["rows"][0][-1] += 1

        def corrupt_scope(evidence):
            evidence["parameters"][-1] = "2026-11-02"

        for tamper in (corrupt_arithmetic, corrupt_scope):
            def execute_with_tampered_evidence(db, segment, region, number):
                evidence = execute(db, segment, region, number)
                if number == 2:
                    tamper(evidence)
                return evidence

            with self.subTest(tamper=tamper.__name__), patch(
                "app.policy_workflow._execute_segment", side_effect=execute_with_tampered_evidence,
            ), self.assertLogs("app.policy_workflow", level="WARNING"):
                result = self.run_request(**OCTOBER)
            self.assertEqual((result["kind"], result["reason"]), ("abstained", "verification_failed"))
            self.assertIsNone(result["total_cents"])
            self.assertEqual(result["segments"], [])
            self.assertEqual(result["evidence"], {"policies": [], "queries": []})

    def test_zero_orders_still_has_a_covered_policy_and_sql_result(self):
        answer = self.run_request(start="2026-10-15", end_exclusive="2026-10-16", region="华北")
        self.assertEqual(answer["kind"], "answer")
        self.assertEqual(answer["total_cents"], 0)
        self.assertEqual(answer["segments"][0]["order_count"], 0)
        self.assertEqual(answer["segments"][0]["policy_id"], SECOND_POLICY)
        self.assertEqual(answer["metrics"]["sql_queries"], 1)

    def test_bind_parameters_preserve_literal_values_and_the_write_guard(self):
        payload = "华南' OR 1=1 --"
        sql = "SELECT COUNT(*) FROM orders WHERE region = ?"
        with ReadOnlyDB(self.database) as db:
            db.dry_run(sql, parameters=(payload,))
            _, rows = db.query(sql, parameters=(payload,))
            self.assertEqual(rows, [(0,)])
            with self.assertRaises(sqlite3.DatabaseError):
                db.dry_run("DELETE FROM orders WHERE region = ?", parameters=("华南",))
            with self.assertRaises(sqlite3.DatabaseError):
                db.query("DELETE FROM orders WHERE region = ?", parameters=("华南",))

    def test_invalid_scope_and_partial_coverage_do_not_open_database(self):
        cases = (
            ({}, "clarification", "missing_dates"),
            ({"start": "2026-10-01"}, "clarification", "missing_dates"),
            ({"start": "2026-11-01", "end_exclusive": "2026-11-01"}, "clarification", "invalid_period"),
            ({"start": "2026-11-02", "end_exclusive": "2026-10-01"}, "clarification", "invalid_period"),
            ({"start": "2026-09-30", "end_exclusive": "2026-10-02"}, "abstained", "policy_gap"),
            ({"start": "2026-11-30", "end_exclusive": "2026-12-02"}, "abstained", "policy_gap"),
        )
        for request, kind, reason in cases:
            with self.subTest(request=request), patch("app.policy_workflow.ReadOnlyDB") as database:
                result = self.run_request(**request)
                self.assertEqual((result["kind"], result["reason"]), (kind, reason))
                self.assertIsNone(result.get("total_cents"))
                self.assertFalse(result["segments"])
                self.assertFalse(result["evidence"]["queries"])
                self.assertEqual(result["metrics"]["sql_queries"], 0)
                database.assert_not_called()


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class PolicyWorkflowAPITests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory)
        self.app = app
        self.enterContext(patch.dict(os.environ, {
            "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "", "RAG_DATA_DIR": directory,
            "RAG_LANGSMITH_EXPORT": "false", "RAG_BIRD_ROOT": "", "RAG_SANDBOX_SOCKET": "",
            "RAG_POSTGRES_HOST": "", "LLM_API_KEY": "", "LLM_BASE_URL": "",
        }))

    def test_invalid_inputs_cannot_supply_sql_or_database_and_are_size_bounded(self):
        with TestClient(self.app) as client:
            for changes in (
                {"sql": "DROP TABLE orders"},
                {"database": "/tmp/arbitrary.sqlite"},
                {"region": "华南' OR 1=1 --"},
                {"start": "2026-02-30"},
            ):
                with self.subTest(changes=changes):
                    response = client.post(API, json={**OCTOBER, **changes})
                    self.assertEqual(response.status_code, 422)
            response = client.post(API, json={"oversized": "x" * 20000})
            self.assertEqual(response.status_code, 413)
            response = client.post(API, json=OCTOBER)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total_cents"], 47700)

    def test_gateway_identity_and_cross_site_rules_apply_to_the_new_endpoint(self):
        key = "a" * 64
        headers = {"X-Rag-Gateway-Key": key, "X-Rag-User": "alice"}
        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": key}):
            with TestClient(self.app) as client:
                self.assertEqual(client.post(API, json=OCTOBER).status_code, 401)
                cross_site = {**headers, "Origin": "https://other.invalid"}
                self.assertEqual(client.post(API, headers=cross_site, json=OCTOBER).status_code, 403)
                response = client.post(API, headers=headers, json=OCTOBER)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json()["total_cents"], 47700)

    def test_policy_orders_are_separate_from_the_legacy_conversation_database(self):
        with TestClient(self.app) as client:
            response = client.post("/api/v1/chat", json={
                "session_id": str(uuid4()), "question": "上月净营收是多少？",
            })
            self.assertEqual(response.status_code, 200)
            events = (json.loads(line) for line in response.iter_lines())
            result = next(event["data"] for event in events if event["event"] == "result")
            self.assertEqual(result["revenue_cents"], 47500)
            legacy_db = self.root / "conversation" / "commerce.sqlite"
            with closing(sqlite3.connect(legacy_db)) as db, db:
                self.assertEqual(db.execute("UPDATE orders SET amount_cents = 1 WHERE id = 1001").rowcount, 1)
        with TestClient(self.app) as client:
            result = client.post(API, json=OCTOBER)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["total_cents"], 47700)
