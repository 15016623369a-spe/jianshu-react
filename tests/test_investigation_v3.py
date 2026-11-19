"""Protect both investigation domains, revision citations, and read grants."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.product_api import app

INVESTIGATE = "/api/v3/investigations"
MANUALS = "/api/v3/manual-documents"
POLICY_QUESTION = "华南电水壶2026年10月和11月净营收差了多少？如果都按10月口径重算，差额又是多少？"
MANUAL_QUESTION = "怎样连接 Wi-Fi？"
ROOT = Path(__file__).resolve().parents[1]


class InvestigationV3Tests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DATA_DIR": root, "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "", "RAG_POSTGRES_HOST": "",
            "RAG_LANGSMITH_EXPORT": "false", "LLM_API_KEY": "", "LLM_BASE_URL": "",
        }))

    def test_policy_envelope_preserves_v2_results_and_exposes_shared_citations(self):
        with TestClient(app) as client:
            old = client.post("/api/v2/policy-investigation", json={"question": POLICY_QUESTION}).json()
            response = client.post(INVESTIGATE, json={"domain": "policy", "question": POLICY_QUESTION})
        response.raise_for_status()
        result = response.json()
        self.assertEqual(result.pop("domain"), "policy")
        evidence = result.pop("evidence")
        for key in ("actual_delta_cents", "counterfactual_delta_cents", "policy_effect_cents", "actual", "counterfactual"):
            self.assertEqual(result[key], old[key])
        self.assertEqual(result["actual_delta_cents"], -4640)
        self.assertEqual({item["source"] for item in evidence}, {"policy", "bulletin"})
        self.assertTrue(all(item["quote"] and item["version"] for item in evidence))
        self.assertEqual(result["metrics"]["sql_queries"], 3)

    def test_demo_manual_cites_only_the_requested_revision(self):
        with TestClient(app) as client:
            for version, expected in ((1, "蓝色"), (2, "绿色")):
                with self.subTest(version=version):
                    response = client.post(INVESTIGATE, json={
                        "domain": "manual", "question": MANUAL_QUESTION, "version": version,
                    })
                    response.raise_for_status()
                    result = response.json()
                    self.assertEqual((result["domain"], result["kind"]), ("manual", "answer"))
                    reference, = result["evidence"]
                    self.assertEqual((reference["source"], reference["version"]), ("manual", str(version)))
                    self.assertIn(expected, result["answer"])
                    lines = (ROOT / reference["file"]).read_text().splitlines()
                    original = "\n".join(lines[reference["line_start"]:reference["line_end"]]).lstrip("\n")
                    self.assertEqual(result["answer"], original)
                    self.assertEqual(result["metrics"], {"model_calls": 0, "sql_queries": 0})

    def test_unsupported_question_and_version_have_no_answer(self):
        with TestClient(app) as client:
            missing = client.post(INVESTIGATE, json={"domain": "manual", "question": "如何修手机屏幕？"}).json()
            version = client.post(INVESTIGATE, json={
                "domain": "manual", "question": MANUAL_QUESTION, "version": 3,
            }).json()
            mismatch = client.post(INVESTIGATE, json={
                "domain": "manual", "question": "第一版怎样连接 Wi-Fi？", "version": 2,
            }).json()
        for result, reason in ((missing, "no_matching_section"), (version, "unknown_version"), (mismatch, "version_conflict")):
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["evidence"], [])
            self.assertNotIn("answer", result)

    def test_reader_grant_controls_historical_search_and_source_read(self):
        key = "a" * 64
        alice = {"X-Rag-Gateway-Key": key, "X-Rag-User": "alice"}
        bob = {"X-Rag-Gateway-Key": key, "X-Rag-User": "bob"}
        raw = "# 设备手册\n\n## 连接 Wi-Fi\n\n指示灯亮起蓝色。\n"
        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": key}), TestClient(app) as client:
            upload = client.post(MANUALS, headers=alice, params={"filename": "manual.md"}, content=raw.encode())
            self.assertEqual(upload.status_code, 201, upload.text)
            identity = upload.json()["id"]
            detail = f"{MANUALS}/{identity}"
            body = {"domain": "manual", "question": MANUAL_QUESTION, "document_id": identity, "version": 1}
            self.assertEqual(client.post(INVESTIGATE, headers=bob, json=body).status_code, 404)
            self.assertEqual(client.post(INVESTIGATE, json=body).status_code, 401)
            second = client.post(detail + "/versions", headers=alice, params={"filename": "manual.md"},
                                 content=raw.replace("蓝色", "绿色").encode())
            self.assertEqual(second.json()["version"], 2)
            self.assertEqual(client.put(detail + "/readers", headers=alice, json={"users": ["bob"]}).status_code, 200)
            latest = client.post(INVESTIGATE, headers=bob, json={**body, "version": None}).json()
            old = client.post(INVESTIGATE, headers=bob, json=body).json()
            self.assertIn("绿色", latest["answer"])
            self.assertIn("蓝色", old["answer"])
            self.assertEqual(old["evidence"][0]["file"], f"uploads/{identity}/v1/manual.md")
            self.assertEqual(client.get(detail + "/versions/1", headers=bob).json()["content"], raw)
            self.assertEqual(client.post("/api/v2/policy-investigation", headers=alice, json={
                "question": POLICY_QUESTION, "document_ids": [identity],
            }).status_code, 404)
            self.assertEqual(client.put(detail + "/readers", headers=alice, json={"users": []}).status_code, 200)
            self.assertEqual(client.get(detail + "/versions/1", headers=bob).status_code, 404)
            self.assertEqual(client.post(INVESTIGATE, headers=bob, json=body).status_code, 404)

    def test_completed_and_clarification_records_are_listed_and_readable(self):
        with TestClient(app) as client:
            completed = client.post(INVESTIGATE, json={"domain": "policy", "question": POLICY_QUESTION})
            clarification = client.post(INVESTIGATE, json={"domain": "manual", "question": "怎样连接 Wi-Fi？", "version": 3})
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertEqual(clarification.status_code, 200, clarification.text)
            completed, clarification = completed.json(), clarification.json()
            self.assertEqual((completed["status"], clarification["status"]), ("completed", "clarification"))
            self.assertTrue(completed["investigation_id"] and clarification["investigation_id"])
            listed = client.get("/api/v3/investigations").json()["investigations"]
            self.assertEqual([item["investigation_id"] for item in listed],
                             [clarification["investigation_id"], completed["investigation_id"]])
            loaded = client.get(f"/api/v3/investigations/{completed['investigation_id']}").json()
            self.assertEqual(loaded["actual_delta_cents"], -4640)
            self.assertEqual(loaded["evidence"], completed["evidence"])
            self.assertEqual(client.get("/api/v3/investigations/not-a-uuid").status_code, 404)

    def test_followup_links_to_parent_investigation(self):
        with TestClient(app) as client:
            first = client.post(INVESTIGATE, json={"domain": "policy", "question": "电水壶 2026 年 10 月净营收是多少？"}).json()
            self.assertEqual((first["kind"], first["status"]), ("clarification", "clarification"))
            second = client.post(INVESTIGATE, json={
                "domain": "policy", "question": f"{first['question']} 华南",
                "investigation_id": first["investigation_id"],
            }).json()
            self.assertEqual((second["kind"], second["status"]), ("answer", "completed"))
            self.assertEqual(second["parent_investigation_id"], first["investigation_id"])

    def test_history_exports_and_system_status(self):
        with TestClient(app) as client:
            created = client.post(INVESTIGATE, json={"domain": "policy", "question": POLICY_QUESTION}).json()
            identifier = created["investigation_id"]
            report = client.get(f"/api/v3/investigations/{identifier}/report.md")
            self.assertEqual(report.status_code, 200)
            self.assertIn("见数调查记录", report.text)
            self.assertIn("实际差额", report.text)
            self.assertIn("attachment;", report.headers["content-disposition"])
            amounts = client.get(f"/api/v3/investigations/{identifier}/amounts.csv")
            self.assertEqual(amounts.status_code, 200)
            self.assertTrue(amounts.content.startswith(b"\xef\xbb\xbf"))
            self.assertIn("actual", amounts.text)
            self.assertEqual(client.get(f"/api/v3/investigations/{identifier}/report.md").headers["cache-control"], "no-store")
            status = client.get("/api/v3/status").json()
            self.assertTrue(status["history_available"])
            self.assertIn("policy", status["domains"])
            self.assertIn("retrieval", status)

    def test_retail_domain_answers_from_the_demo_ledger(self):
        with TestClient(app) as client:
            result = client.post(INVESTIGATE, json={
                "domain": "retail", "question": "2026 年 10 月华南电水壶净营收是多少？",
            })
            self.assertEqual(result.status_code, 200, result.text)
            result = result.json()
            self.assertEqual((result["kind"], result["status"], result["scope"]["metric"]), ("answer", "completed", "net"))
            self.assertEqual((result["actual"][0]["order_count"], result["actual"][0]["net_cents"]), (3, 27400))
            self.assertEqual(result["evidence"][0]["source"], "retail")
            missing = client.post(INVESTIGATE, json={"domain": "retail", "question": "净营收是多少？"}).json()
            self.assertEqual((missing["kind"], missing["status"], missing["metrics"]["sql_queries"]), ("clarification", "clarification", 0))

    def test_investigation_metrics_endpoint_is_owner_scoped(self):
        with TestClient(app) as client:
            client.post(INVESTIGATE, json={"domain": "retail", "question": "2026 年 10 月华南电水壶净营收是多少？"})
            metrics = client.get("/api/v3/metrics").json()
            self.assertGreaterEqual(metrics["total"], 1)
            self.assertIn("domains", metrics)

    def test_domain_registry_is_exposed_for_v3_clients(self):
        with TestClient(app) as client:
            response = client.get("/api/v3/domains")
        self.assertEqual(response.status_code, 200)
        domains = response.json()["domains"]
        self.assertEqual(tuple(domains), ("policy", "manual", "retail"))
        self.assertTrue(domains["policy"]["supports_sql"])
        self.assertEqual(domains["manual"]["document_sources"], ["manual"])
        self.assertTrue(domains["retail"]["supports_sql"])
        self.assertFalse(domains["retail"]["supports_upload"])

    def test_request_boundary_rejects_unknown_domain_and_oversize(self):
        with TestClient(app) as client:
            self.assertEqual(client.post(INVESTIGATE, json={"domain": "unknown", "question": "查文档"}).status_code, 422)
            self.assertEqual(client.post(INVESTIGATE, json={"domain": "manual", "question": "x" * 17000}).status_code, 413)
