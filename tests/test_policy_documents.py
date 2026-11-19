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

from io import BytesIO

from docx import Document

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

    def test_preview_does_not_persist_and_duplicate_upload_is_idempotent(self):
        preview = self.client.post(f"{_DOCS}/preview", headers=self.alice, content=_V1,
                                   params={"filename": "政策.md", "source": "policy"})
        self.assertEqual(preview.status_code, 200, preview.text)
        preview = preview.json()
        self.assertEqual((preview["format"], preview["source"]), ("markdown", "policy"))
        self.assertGreaterEqual(len(preview["passages"]), 1)
        self.assertEqual(self.client.get(_DOCS, headers=self.alice).json()["documents"], [])
        first = self.upload()
        second = self.upload()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(second.json()["version"], 1)
        self.assertEqual(len(self.client.get(_DOCS, headers=self.alice).json()["documents"]), 1)

    def test_pdf_manual_preview_keeps_page_locator(self):
        source = Path(__file__).with_name("fixtures") / "manual-sample.pdf"
        preview = self.client.post(
            "/api/v3/manual-documents/preview", headers={**self.alice, "Content-Type": "application/pdf"},
            params={"filename": "manual.pdf"}, content=source.read_bytes(),
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview = preview.json()
        self.assertEqual(preview["format"], "pdf")
        self.assertEqual(preview["passages"][0]["locator"], "page")
        self.assertIn("Connect Wi-Fi", preview["passages"][0]["quote"])

    def test_pdf_manual_upload_answers_with_page_citation(self):
        source = Path(__file__).with_name("fixtures") / "manual-sample.pdf"
        upload = self.client.post(
            "/api/v3/manual-documents", headers={**self.alice, "Content-Type": "application/pdf"},
            params={"filename": "manual.pdf"}, content=source.read_bytes(),
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        document = upload.json()
        result = self.client.post("/api/v3/investigations", headers=self.alice, json={
            "domain": "manual", "question": "How to connect Wi-Fi?", "document_id": document["id"], "version": 1,
        })
        self.assertEqual(result.status_code, 200, result.text)
        result = result.json()
        self.assertEqual(result["kind"], "answer")
        reference, = result["evidence"]
        self.assertEqual((reference["locator"], reference["line_start"]), ("page", 1))
        self.assertIn("green", reference["quote"])
        self.assertEqual(reference["file"], f"uploads/{document['id']}/v1/manual.pdf")

    def test_docx_manual_upload_keeps_paragraph_locator(self):
        buffer = BytesIO()
        document = Document()
        document.add_heading("设备手册", level=1)
        document.add_heading("连接 Wi-Fi", level=2)
        document.add_paragraph("连接成功时指示灯为绿色。")
        document.save(buffer)
        upload = self.client.post(
            "/api/v3/manual-documents", headers={**self.alice, "Content-Type":
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
            params={"filename": "manual.docx"}, content=buffer.getvalue(),
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        source = upload.json()
        result = self.client.post("/api/v3/investigations", headers=self.alice, json={
            "domain": "manual", "question": "怎样连接 Wi-Fi？", "document_id": source["id"], "version": 1,
        })
        self.assertEqual(result.status_code, 200, result.text)
        result = result.json()
        self.assertEqual(result["kind"], "answer")
        reference, = result["evidence"]
        self.assertEqual(reference["locator"], "paragraph")
        self.assertIn("绿色", reference["quote"])

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

    def test_html_manual_upload_is_parsed_and_cited(self):
        raw = (
            "<html><body><h1>设备手册</h1>"
            "<h2>连接 Wi-Fi</h2><p>连接成功时指示灯为绿色。</p>"
            "<h2>检查电量</h2><p>在应用首页查看电池图标。</p>"
            "</body></html>"
        ).encode()
        upload = self.client.post(
            "/api/v3/manual-documents", headers={**self.alice, "Content-Type": "text/html"},
            params={"filename": "manual.html"}, content=raw,
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        document = upload.json()
        result = self.client.post("/api/v3/investigations", headers=self.alice, json={
            "domain": "manual", "question": "怎样连接 Wi-Fi？", "document_id": document["id"], "version": 1,
        })
        self.assertEqual(result.status_code, 200, result.text)
        result = result.json()
        self.assertIn("绿色", result["answer"])
        reference, = result["evidence"]
        self.assertEqual(reference["file"], f"uploads/{document['id']}/v1/manual.html")
        self.assertEqual((reference["line_start"], reference["line_end"]), (2, 3))
        accepted = self.upload(filename="policy.html", content=raw)
        self.assertEqual(accepted.status_code, 201)

    def test_html_policy_can_drive_the_policy_investigation(self):
        raw = (
            "<html><body><h1>上传 HTML 政策</h1>"
            "<h2>华南电水壶十月退款结算</h2>"
            "<p>文号：demo-html-kettle-oct。</p>"
            "<p>在华南销售的家电类商品适用本页规则，具体限于电水壶。从2026年10月1日起执行，到2026年10月31日为止（含当天）。</p>"
            "<p>按售后退款台账结算，同一订单先聚合退款，每笔扣减上限 5 元。</p>"
            "</body></html>"
        ).encode()
        upload = self.client.post(
            "/api/v2/policy-documents", headers={**self.alice, "Content-Type": "text/html"},
            params={"filename": "policy-html.html", "source": "policy"}, content=raw,
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        document = upload.json()
        result = self.client.post("/api/v2/policy-investigation", headers=self.alice, json={
            "question": "华南电水壶 2026 年 10 月净营收是多少？", "document_ids": [document["id"]],
        })
        self.assertEqual(result.status_code, 200, result.text)
        result = result.json()
        self.assertEqual((result["kind"], result["actual"][0]["net_cents"]), ("answer", 29280))
        wrapped = self.client.post("/api/v3/investigations", headers=self.alice, json={
            "domain": "policy", "question": "华南电水壶 2026 年 10 月净营收是多少？",
            "document_ids": [document["id"]],
        })
        self.assertEqual(wrapped.status_code, 200, wrapped.text)
        reference, = wrapped.json()["evidence"]
        self.assertIn("每笔扣减上限 5 元", reference["quote"])
        self.assertEqual(reference["file"], f"uploads/{document['id']}/v1/policy-html.html")
        self.assertGreater(reference["line_start"], 0)

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
