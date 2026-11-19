"""Environment-driven optional reranking for the shared retrieval path."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Callable, Iterable

from app.rag.models import Passage

Reranker = Callable[[str, Passage], float]
_CACHE: dict[tuple[str, str, str, tuple[tuple[str, int, int], ...]], Reranker | None] = {}


def rerank_mode() -> str:
    return os.getenv("RAG_RERANK_MODE", "off").strip().lower()


def rerank_configured() -> bool:
    return rerank_mode() not in {"", "off", "none", "0", "false"}


def _key(passages: Iterable[Passage]) -> tuple[tuple[str, int, int], ...]:
    return tuple((item.document_id, item.line_start, item.line_end) for item in passages)


def build_reranker(passages: Iterable[Passage]) -> Reranker | None:
    """Build a reranker only when explicitly enabled in the environment.

    Modes:
      off/empty   no rerank (default)
      embedding   BGE/dense embedding rerank; uses RAG_DENSE_MODEL
      on/cross    CrossEncoder rerank; uses RAG_RERANK_MODEL; falls back to embedding
    """
    mode = rerank_mode()
    if mode in {"", "off", "none", "0", "false"}:
        return None
    items = tuple(passages)
    if not items:
        return None
    dense_model = os.getenv("RAG_DENSE_MODEL", "BAAI/bge-m3")
    cross_model = os.getenv("RAG_RERANK_MODEL", "")
    cache_key = (mode, dense_model, cross_model, _key(items))
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    result: Reranker | None = None
    if mode in {"on", "cross", "cross-encoder", "auto"} and cross_model:
        try:
            from app.rag.dense import CrossEncoderReranker
            result = CrossEncoderReranker(model_name=cross_model, local_files_only=True)
        except (ImportError, OSError, RuntimeError, ValueError):
            result = None
    if result is None and mode in {"on", "cross", "cross-encoder", "auto", "embedding"}:
        try:
            from app.rag.dense import SentenceTransformerDense
            from app.rag.models import DocumentMetadata  # noqa: F401  (kept for type context)

            class _EmbeddingReranker:
                def __init__(self, rows: tuple[Passage, ...]):
                    self.index = SentenceTransformerDense(
                        rows, model_name=dense_model, local_files_only=True,
                    )
                    self._scores: dict[str, dict[tuple[str, int, int], float]] = {}

                def __call__(self, query: str, passage: Passage) -> float:
                    if query not in self._scores:
                        import numpy as np
                        vector = np.asarray(self.index.model.encode([query], normalize_embeddings=True)[0])
                        scores = self.index._matrix @ vector
                        self._scores[query] = {
                            (item.document_id, item.line_start, item.line_end): float(score)
                            for item, score in zip(self.index.passages, scores, strict=True)
                        }
                    return self._scores[query].get(
                        (passage.document_id, passage.line_start, passage.line_end), 0.0,
                    )

            result = _EmbeddingReranker(items)
        except (ImportError, OSError, RuntimeError, ValueError):
            result = None
    _CACHE[cache_key] = result
    return result


def clear_rerank_cache() -> None:
    _CACHE.clear()
