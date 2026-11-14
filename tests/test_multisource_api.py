"""Check the multi-source endpoint's API contract and isolation."""
from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

API = "/api/v2/multisource"
SCOPE = {"start": "2026-10-01", "end_exclusive": "2026-10-13"}


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class MultisourceAPITests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        self.app = app
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "", "RAG_DATA_DIR": root,
            "RAG_LANGSMITH_EXPORT": "false", "RAG_BIRD_ROOT": "", "RAG_SANDBOX_SOCKET": "",
            "RAG_POSTGRES_HOST": "", "LLM_API_KEY": "", "LLM_BASE_URL": "",
        }))

    def test_fixed_uses_sql_policy_and_bulletin_evidence_and_preserves_policy_default(self):
        with TestClient(self.app) as client:
            response = client.post(API, json=SCOPE)
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual((result["kind"], result["mode"], result["total_cents"]), ("answer", "fixed", 81040))
            self.assertEqual(result["metrics"]["model_calls"], 0)
            self.assertEqual(len(result["groups"]), 4)
            self.assertEqual({doc["source"] for doc in result["evidence"]["documents"]}, {"policy", "bulletin"})
            self.assertEqual(len(result["evidence"]["queries"]), 4)
            ordinary = client.post("/api/v2/policy-revenue", json={
                "start": "2026-10-01", "end_exclusive": "2026-11-01",
            }).json()
            self.assertEqual((ordinary["kind"], ordinary["strategy"], ordinary["total_cents"]), ("answer", "fixed", 47700))

    def test_model_driven_modes_require_configuration_and_invalid_requests_are_bounded(self):
        with TestClient(self.app) as client:
            for mode in ("react", "plan"):
                with self.subTest(mode=mode):
                    response = client.post(API, params={"mode": mode}, json=SCOPE)
                    self.assertEqual(response.status_code, 200)
                    result = response.json()
                    self.assertEqual((result["mode"], result["kind"], result["reason"]),
                                     (mode, "abstained", "model_unconfigured"))
                    self.assertIsNone(result["total_cents"])
            self.assertEqual(client.post(API, params={"mode": "unknown"}, json=SCOPE).status_code, 422)
            self.assertEqual(client.post(API, json={**SCOPE, "sql": "select * from orders"}).status_code, 422)
            self.assertEqual(client.post(API, json={**SCOPE, "region": "unknown"}).status_code, 422)
            self.assertEqual(client.post(API, json={"oversized": "x" * (17 * 1024)}).status_code, 413)

    def test_explicit_plan_routes_through_configured_model(self):
        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key", "LLM_BASE_URL": "https://provider.invalid",
        }), TestClient(self.app) as client, patch("app.product_api.multisource_selector") as build_selector, patch(
            "app.product_api.run_multisource_workflow", return_value={"mode": "plan"},
        ) as run:
            response = client.post(API, params={"mode": "plan"}, json=SCOPE)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mode"], "plan")
            build_selector.assert_called_once()
            self.assertEqual(run.call_args.kwargs, {"mode": "plan", "choose": build_selector.return_value})

    def test_production_mode_requires_gateway_identity_and_same_origin(self):
        key = "b" * 64
        gateway = {"X-Rag-Gateway-Key": key, "X-Rag-User": "alice"}
        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": key}), TestClient(self.app) as client:
            self.assertEqual(client.post(API, json=SCOPE).status_code, 401)
            self.assertEqual(client.post(API, headers={**gateway, "Origin": "https://foreign.invalid"}, json=SCOPE).status_code, 403)
            response = client.post(API, headers=gateway, json=SCOPE)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
