"""Dense adapter behavior with an injected encoder (no model download)."""
from __future__ import annotations

import unittest

import numpy as np

from app.rag.dense import SentenceTransformerDense
from app.rag.models import DocumentMetadata, Passage


class FakeEncoder:
    def encode(self, texts, *, normalize_embeddings=True):
        vectors = []
        for text in texts:
            vectors.append([1.0 if "Wi-Fi" in text else 0.0, 1.0 if "退款" in text else 0.0])
        matrix = np.asarray(vectors, dtype=float)
        return matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)


def item(identifier: str, text: str, acl: frozenset[str] = frozenset({"alice"})) -> Passage:
    metadata = DocumentMetadata(identifier, text, source="manual", acl=acl)
    return Passage(identifier, 1, text, text, text, 1, 1, metadata)


class DenseTests(unittest.TestCase):
    def setUp(self):
        self.wifi = item("wifi", "Wi-Fi 连接步骤")
        self.refund = item("refund", "退款政策说明")
        self.index = SentenceTransformerDense((self.wifi, self.refund), model=FakeEncoder())

    def test_dense_ranking_and_acl(self):
        self.assertEqual(self.index.search("Wi-Fi", top_k=1)[0].passage.document_id, "wifi")
        self.assertEqual(self.index.search("退款", top_k=1)[0].passage.document_id, "refund")
        self.assertEqual(self.index.search("退款", top_k=1, acl=frozenset({"bob"})), [])

    def test_dense_metadata_filter(self):
        self.assertEqual(self.index.search("退款", top_k=1, filters={"source": "policy"}), [])
