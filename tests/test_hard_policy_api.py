"""Check the opt-in policy exploration API boundaries and default route."""
from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

API = "/api/v2/policy-exploration"
OCTOBER = {"start": "2026-10-01", "end_exclusive": "2026-11-01", "product": "电水壶", "region": "华南"}


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class HardPolicyAPITests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        self.app = app
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "", "RAG_DATA_DIR": root,
            "RAG_LANGSMITH_EXPORT": "false", "RAG_BIRD_ROOT": "", "RAG_SANDBOX_SOCKET": "",
            "RAG_POSTGRES_HOST": "", "LLM_API_KEY": "", "LLM_BASE_URL": "",
        }))

    def test_exploration_defaults_to_auto_and_preserves_policy_default(self):
        with TestClient(self.app) as client:
            response = client.post(API, json=OCTOBER)
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual((result["mode"], result["kind"], result["total_cents"]), ("auto", "answer", 29280))
            self.assertEqual(result["metrics"]["model_calls"], 0)
            self.assertTrue(result["evidence"]["policy"])
            self.assertTrue(result["evidence"]["bulletin"])
            self.assertTrue(result["evidence"]["query"]["sql"])

            ordinary = client.post("/api/v2/policy-revenue", json={
                "start": "2026-10-01", "end_exclusive": "2026-11-01",
            }).json()
            self.assertEqual((ordinary["kind"], ordinary["strategy"], ordinary["total_cents"]), ("answer", "fixed", 47700))

    def test_modes_are_validated_and_payload_has_no_sql_or_oversize_access(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post(API, params={"mode": "unknown"}, json=OCTOBER).status_code, 422)
            self.assertEqual(client.post(API, json={**OCTOBER, "sql": "SELECT * FROM orders"}).status_code, 422)
            self.assertEqual(client.post(API, json={**OCTOBER, "region": "未知地区"}).status_code, 422)
            self.assertEqual(client.post(API, json={"product": "x" * (17 * 1024)}).status_code, 413)

    def test_production_mode_requires_identity_and_same_origin(self):
        key = "d" * 64
        gateway = {"X-Rag-Gateway-Key": key, "X-Rag-User": "alice"}
        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": key}), TestClient(self.app) as client:
            self.assertEqual(client.post(API, json=OCTOBER).status_code, 401)
            self.assertEqual(client.post(API, headers={**gateway, "Origin": "https://foreign.invalid"}, json=OCTOBER).status_code, 403)
            response = client.post(API, headers=gateway, params={"mode": "fixed"}, json=OCTOBER)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_configured_default_passes_selector_to_auto_mode(self):
        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key", "LLM_BASE_URL": "https://provider.invalid",
        }), TestClient(self.app) as client, patch("app.product_api.multisource_selector") as selector, patch(
            "app.product_api.run_hard_policy", return_value={"mode": "auto"},
        ) as run:
            self.assertEqual(client.post(API, json=OCTOBER).json(), {"mode": "auto"})
            selector.assert_called_once()
            self.assertEqual(run.call_args.kwargs, {"mode": "auto", "choose": selector.return_value})
