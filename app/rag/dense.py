"""Optional dense and cross-encoder adapters for HybridIndex.

Models are injected lazily; the product does not download or enable them unless
an operator configures an evaluated model.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Protocol

import numpy as np

from app.rag.models import DocumentMetadata, Passage
from app.rag.retrieval import RetrievalHit


class _Encoder(Protocol):
    def encode(self, texts: list[str], *, normalize_embeddings: bool = True) -> np.ndarray:
        ...


def _passes(passage: Passage, filters: object = None, acl: frozenset[str] | None = None) -> bool:
    if acl is not None and not passage.supports(acl):
        return False
    if filters is None:
        return True
    if callable(filters):
        return bool(filters(passage))
    for key, expected in filters.items():
        actual = getattr(passage.metadata, key, None)
        if actual is None:
            actual = passage.metadata.attributes.get(key)
        if actual != expected:
            return False
    return True


class SentenceTransformerDense:
    """Dense passage index backed by an injected sentence-transformers model."""

    def __init__(self, passages: Iterable[Passage], *, model: _Encoder | None = None,
                 model_name: str | None = None, local_files_only: bool = True) -> None:
        self.passages = tuple(passages)
        if not self.passages:
            raise ValueError("Dense index requires at least one passage")
        if model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is not installed") from exc
            model = SentenceTransformer(
                model_name or os.getenv("RAG_DENSE_MODEL", "BAAI/bge-m3"),
                local_files_only=local_files_only,
            )
        self.model = model
        self._matrix = np.asarray(model.encode(
            [f"{item.title}\n{item.text}" for item in self.passages], normalize_embeddings=True,
        ))

    def search(self, query: str, *, top_k: int = 5, filters: object = None,
               acl: frozenset[str] | None = None) -> list[RetrievalHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        vector = np.asarray(self.model.encode([query], normalize_embeddings=True)[0])
        scores = self._matrix @ vector
        ranked = []
        for passage, score in zip(self.passages, scores, strict=True):
            if _passes(passage, filters, acl):
                ranked.append(RetrievalHit(passage, round(float(score), 9)))
        ranked.sort(key=lambda hit: (-hit.score, hit.passage.document_id, hit.passage.line_start))
        return ranked[:top_k]


class CrossEncoderReranker:
    """Optional cross-encoder reranker; import and model load stay lazy."""

    def __init__(self, model=None, *, model_name: str | None = None, local_files_only: bool = True) -> None:
        if model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is not installed") from exc
            model = CrossEncoder(
                model_name or os.getenv("RAG_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
                local_files_only=local_files_only,
            )
        self.model = model

    def __call__(self, query: str, passage: Passage) -> float:
        score = self.model.predict([(query, f"{passage.title}\n{passage.text}")])
        return float(score[0])
