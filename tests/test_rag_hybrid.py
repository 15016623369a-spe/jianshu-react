"""Hybrid retrieval keeps the lexical baseline and fuses optional dense hits."""
from __future__ import annotations

import unittest

from app.rag.hybrid import HybridIndex, reciprocal_rank_fusion
from app.rag.models import DocumentMetadata, Passage
from app.rag.retrieval import BM25Index, RetrievalHit


def passage(identifier: str, text: str, line: int) -> Passage:
    metadata = DocumentMetadata(identifier, "手册", source="manual", version=1)
    return Passage(identifier, 1, "手册", "章节", text, line, line, metadata, source_file="manual.md")


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.a = passage("a", "退款政策说明", 1)
        self.b = passage("b", "Wi-Fi 连接步骤", 2)
        self.c = passage("c", "电量检查方法", 3)

    def test_without_dense_is_exact_lexical_baseline(self):
        index = HybridIndex((self.a, self.b, self.c))
        hybrid = index.search("Wi-Fi", top_k=1)
        lexical = index.lexical.search("Wi-Fi", top_k=1)
        self.assertEqual(hybrid[0].passage, lexical[0].passage)

    def test_dense_candidates_are_fused(self):
        c, b = self.c, self.b

        class Dense:
            def search(self, query, *, top_k=20, filters=None, acl=None):
                return [RetrievalHit(c, 0.9), RetrievalHit(b, 0.8)]
        fused = reciprocal_rank_fusion(
            [RetrievalHit(self.b, 1.0)], [RetrievalHit(self.c, 0.9), RetrievalHit(self.b, 0.8)],
        )
        self.assertEqual([hit.passage.document_id for hit in fused], ["b", "c"])
        index = HybridIndex((self.a, self.b, self.c), dense=Dense())
        hits = index.search("Wi-Fi", top_k=2)
        self.assertEqual(hits[0].passage.document_id, "b")
        self.assertIn("c", {hit.passage.document_id for hit in hits})

    def test_reranker_can_reorder_fused_hits(self):
        first = passage("first", "退款规则 A", 4)
        second = passage("second", "退款规则 B", 5)
        index = HybridIndex((first, second), reranker=lambda _query, item: 1.0 if item.document_id == "second" else 0.0)
        self.assertEqual(index.search("退款", top_k=1)[0].passage.document_id, "second")

    def test_candidate_k_limits_dense_candidates(self):
        a = self.a

        class Dense:
            def __init__(self):
                self.calls = []
            def search(self, query, *, top_k=20, filters=None, acl=None):
                self.calls.append(top_k)
                return [RetrievalHit(a, 0.1)]
        dense = Dense()
        HybridIndex((self.a, self.b), dense=dense).search("退款", top_k=1, candidate_k=3)
        self.assertEqual(dense.calls, [3])
