"""Verify versioned document retrieval and the account boundary."""
from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.policy_documents import MAX_DOCUMENT_BYTES, PolicyDocumentStore
from app.product_api import app

_DOCS = "/api/v2/policy-documents"
_ASK = "/api/v2/policy-investigation"
_QUESTION = "华南电水壶2026年10月的净营收是多少？"
_ROOT = Path(__file__).resolve().parents[1] / "examples" / "demo"
_V1 = (_ROOT / "policy-upload-v1.md").read_bytes()
_V2 = (_ROOT / "policy-upload-v2.md").read_bytes()
_GATEWAY_KEY = "a" * 64


def _headers(user: str) -> dict:
    return {"X-Rag-User": user, "X-Rag-Gateway-Key": _GATEWAY_KEY}


class PolicyDocumentAPITests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DATA_DIR": self.root, "RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": _GATEWAY_KEY,
            "RAG_POSTGRES_HOST": "", "RAG_LANGSMITH_EXPORT": "false", "LLM_BASE_URL": "https://example.invalid",
            "LLM_API_KEY": "test-only",
        }))
        self.client = self.enterContext(TestClient(app))
        self.alice, self.bob = _headers("alice"), _headers("bob")

    def upload(self, *, headers=None, filename="政策.md", content=_V1, source="policy"):
        return self.client.post(_DOCS, headers=self.alice if headers is None else headers, content=content,
                                params={"filename": filename, "source": source})

    def ask(self, ids, *, headers=None, question=_QUESTION, mode="auto"):
        return self.client.post(_ASK, params={"mode": mode}, headers=self.alice if headers is None else headers,
                                json={"question": question, "document_ids": ids})

    def test_latest_revision_drives_query_while_old_source_remains_auditable(self):
        first = self.upload()
        self.assertEqual(first.status_code, 201)
        doc = first.json()
        self.assertEqual((doc["filename"], doc["version"], doc["source"], doc["owned"]), ("政策.md", 1, "policy", True))
        self.assertEqual(first.headers["cache-control"], "no-store")
        url = f"{_DOCS}/{doc['id']}"
        with patch("app.product_api.multisource_selector", side_effect=AssertionError("private content stays local")):
            before = self.ask([doc["id"]]).json()
            changed = self.client.post(f"{url}/versions", headers=self.alice, content=_V2, params={"filename": "政策-v2.md"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["version"], 2)
            after = self.ask([doc["id"]]).json()

        self.assertEqual((before["kind"], after["kind"]), ("answer", "answer"))
        self.assertEqual((before["actual"][0]["net_cents"], after["actual"][0]["net_cents"]), (29280, 29480))
        self.assertNotEqual(before["document_version"], after["document_version"])
        citation = after["actual"][0]["segments"][0]["documents"][0]
        self.assertEqual(citation["file"], f"uploads/{doc['id']}/v2/政策-v2.md")
        original = self.client.get(f"{url}/versions/1", headers=self.alice).json()
        current = self.client.get(f"{url}/versions/2", headers=self.alice).json()
        self.assertEqual((original["content"], current["content"]), (_V1.decode(), _V2.decode()))
        self.assertNotEqual(original["sha256"], current["sha256"])
        lines = current["content"].splitlines()
        self.assertIn(citation["quote"], "\n".join(lines[citation["line_start"] - 1:citation["line_end"]]))
        versions = self.client.get(f"{url}/versions", headers=self.alice).json()["versions"]
        self.assertEqual([item["version"] for item in versions], [2, 1])
        self.assertEqual(self.ask([doc["id"]], mode="model").status_code, 422)

        retry = self.client.post(f"{url}/versions", headers=self.alice, content=_V2, params={"filename": "政策-v2.md"})
        self.assertEqual(retry.json()["version"], 2)

    def test_acl_covers_listing_versions_retrieval_and_revocation(self):
        doc = self.upload().json()
        url = f"{_DOCS}/{doc['id']}"
        self.assertEqual(self.client.get(_DOCS).status_code, 401)
        self.assertEqual(self.client.get(_DOCS, headers=self.bob).json()["documents"], [])
        self.assertEqual(self.client.get(f"{url}/versions", headers=self.bob).status_code, 404)
        with patch("app.product_api.run_policy_investigation", side_effect=AssertionError("do not execute before ACL")):
            self.assertEqual(self.ask([doc["id"]], headers=self.bob).status_code, 404)
        granted = self.client.put(f"{url}/readers", headers=self.alice, json={"users": ["bob", "bob", "alice"]})
        self.assertEqual(granted.json(), {"readers": ["bob"]})
        shared = self.client.get(_DOCS, headers=self.bob).json()["documents"][0]
        self.assertFalse(shared["owned"])
        self.assertNotIn("readers", shared)
        self.assertEqual(self.client.get(_DOCS, headers=self.alice).json()["documents"][0]["readers"], ["bob"])
        self.assertEqual(self.client.get(f"{url}/versions/1", headers=self.bob).json()["content"], _V1.decode())
        self.assertEqual(self.ask([doc["id"]], headers=self.bob).json()["kind"], "answer")
        self.assertEqual(self.client.post(f"{url}/versions", headers=self.bob, content=_V2,
                                          params={"filename": "政策-v2.md"}).status_code, 404)
        self.assertEqual(self.client.put(f"{url}/readers", headers=self.bob, json={"users": []}).status_code, 404)
        self.assertEqual(self.client.put(f"{url}/readers", headers=self.alice, json={"users": []}).status_code, 200)
        self.assertEqual(self.client.get(f"{url}/versions/1", headers=self.bob).status_code, 404)
        self.assertEqual(self.ask([doc["id"]], headers=self.bob).status_code, 404)

    def test_upload_scope_does_not_fill_gaps_from_demo_or_skip_unauthorized_documents(self):
        own = self.upload().json()["id"]
        private = self.upload(headers=self.bob).json()["id"]
        result = self.ask([own], question="华南电水壶2026年10月到11月的净营收差了多少？").json()
        self.assertEqual((result["kind"], result["reason"]), ("abstained", "policy_missing"))
        self.assertEqual(result["metrics"]["sql_queries"], 0)
        self.assertEqual(self.ask([own, private]).status_code, 404)
        self.assertEqual(self.ask([own, str(uuid4())]).status_code, 404)

    def test_file_boundary_and_bounded_acl_json(self):
        for filename, body, expected in (
            ("../政策.md", _V1, 422), ("政策.xls", _V1, 422), ("政策.md", b"\xff", 422),
            ("政策.md", b"# title\n<!--\n## hidden\ntext\n-->", 422),
            ("政策.md", b"x" * (MAX_DOCUMENT_BYTES + 1), 413),
        ):
            with self.subTest(filename=filename, expected=expected):
                self.assertEqual(self.upload(filename=filename, content=body).status_code, expected)
        doc = self.upload().json()
        readers = f"{_DOCS}/{doc['id']}/readers"
        self.assertEqual(self.client.put(readers, headers=self.alice, json={"users": ["../bob"]}).status_code, 422)
        self.assertEqual(self.client.put(readers, headers=self.alice, json={"users": ["x" * 20000]}).status_code, 413)
        self.assertEqual(self.client.put(readers, headers={**self.alice, "Origin": "https://elsewhere.invalid"},
                                         json={"users": []}).status_code, 403)
        self.assertEqual(self.client.get(f"{_DOCS}/{doc['id']}/versions/9999999999999999999999",
                                         headers=self.alice).status_code, 422)


class LocalPolicyDocumentTests(unittest.TestCase):
    def test_local_demo_ignores_account_headers_and_disables_grants(self):
        with TemporaryDirectory() as root, patch.dict(os.environ, {
            "RAG_DATA_DIR": root, "RAG_DEPLOYMENT": "local", "RAG_POSTGRES_HOST": "", "RAG_GATEWAY_KEY": "",
            "LLM_API_KEY": "", "LLM_BASE_URL": "",
        }), TestClient(app) as client:
            first = client.post(_DOCS, headers=_headers("alice"), content=_V1,
                                params={"filename": "政策.md", "source": "policy"})
            self.assertEqual(first.status_code, 201)
            doc = first.json()
            library = client.get(_DOCS, headers=_headers("bob")).json()
            self.assertFalse(library["sharing_enabled"])
            self.assertEqual(library["documents"][0]["id"], doc["id"])
            self.assertEqual(client.put(f"{_DOCS}/{doc['id']}/readers",
                                        headers=_headers("alice"), json={"users": ["bob"]}).status_code, 403)

    def test_concurrent_revisions_are_serialized_and_pinned_evidence_is_immutable(self):
        with TemporaryDirectory() as root:
            store = PolicyDocumentStore(Path(root) / "store.sqlite")
            owner = "local"
            document_id = UUID(store.create(owner, "政策.md", "policy", _V1)["id"])
            before = store.snapshot(owner, [document_id])
            with ThreadPoolExecutor(max_workers=2) as pool:
                updates = [pool.submit(store.revise, owner, document_id, name, body)
                           for name, body in (("政策2.md", _V2), ("政策3.md", _V2.replace(b"3 ", b"4 ")))]
                self.assertEqual(sorted(update.result()["version"] for update in updates), [2, 3])
            self.assertIn("/v1/", before.passages[0].file)
            self.assertIn("5 元", before.passages[0].quote)
            after = store.snapshot(owner, [document_id])
            self.assertIn("/v3/", after.passages[0].file)
            self.assertNotEqual(after.version, before.version)
            self.assertEqual([item["version"] for item in store.versions(owner, document_id)], [3, 2, 1])
