"""Optional reranking is opt-in and reorders only retrieved candidates."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.rag.models import DocumentMetadata, Passage
from app.rag.rerank import build_reranker, clear_rerank_cache, rerank_configured, rerank_mode
from app.rag.service import RetrievalService


def passage(identifier: str, text: str, line: int) -> Passage:
    metadata = DocumentMetadata(identifier, text, source="manual")
    return Passage(identifier, 1, text, text, text, line, line, metadata, source_file="manual.md")


class RerankTests(unittest.TestCase):
    def test_default_is_off(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(rerank_mode(), "off")
            self.assertFalse(rerank_configured())
            self.assertIsNone(build_reranker((passage("a", "退款", 1),)))

    def test_retrieval_service_reranker_reorders(self):
        a, b = passage("a", "退款政策", 1), passage("b", "退款步骤", 2)
        service = RetrievalService.from_passages(
            (a, b), reranker=lambda _query, item: 1.0 if item.document_id == "b" else 0.0,
        )
        self.assertEqual(service.search("退款", top_k=1)[0].document_id, "b")

    def test_rerank_environment_is_visible_and_cacheable(self):
        with patch.dict("os.environ", {"RAG_RERANK_MODE": "embedding"}):
            self.assertTrue(rerank_configured())
            self.assertEqual(rerank_mode(), "embedding")
            clear_rerank_cache()
