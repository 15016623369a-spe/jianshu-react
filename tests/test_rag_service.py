"""Integration tests for the V3 retrieval service."""
from __future__ import annotations

import unittest

from app.rag import DocumentMetadata
from app.rag.service import RetrievalService


class RetrievalServiceTests(unittest.TestCase):
    def test_documents_are_chunked_indexed_and_returned_as_evidence(self):
        service = RetrievalService((
            ("# 手册\n\n## 连接 Wi-Fi\n\n指示灯为绿色。\n", DocumentMetadata(
                "manual-1", "设备手册", source="manual", version=2,
                acl=frozenset({"alice"}), attributes={"domain": "manual"},
            ), "markdown"),
            ("# 政策\n\n## 退款上限\n\n每单最多扣五元。\n", DocumentMetadata(
                "policy-1", "退款政策", source="policy", version=1,
                acl=frozenset({"alice"}), attributes={"domain": "policy"},
            ), "markdown"),
        ))
        hits = service.search("Wi-Fi 绿色", acl=frozenset({"alice"}), filters={"domain": "manual"})
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual((hit.document_id, hit.version, hit.section), ("manual-1", 2, "连接 Wi-Fi"))
        self.assertIn("绿色", hit.quote)
        self.assertTrue(hit.evidence_id.startswith("doc:manual-1#"))

    def test_empty_or_unreadable_documents_surface_as_no_hits_or_errors(self):
        with self.assertRaises(ValueError):
            RetrievalService(())
        service = RetrievalService((
            ("# 手册\n\n## 电量\n\n查看电池图标。\n", DocumentMetadata(
                "manual-1", "设备手册", source="manual", version=1,
                acl=frozenset({"alice"}),
            ), "markdown"),
        ))
        self.assertEqual(service.search("打印机"), ())
        self.assertEqual(service.search("电池", acl=frozenset({"bob"})), ())
        self.assertEqual(len(service.search("电池", acl=frozenset({"alice"}))), 1)
