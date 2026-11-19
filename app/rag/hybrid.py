"""Optional hybrid retrieval: BM25, optional dense candidates, and reranking.

The default product path stays lexical. Dense providers are injected so the
baseline can be evaluated without bundling a model or network dependency.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.rag.models import Passage
from app.rag.retrieval import BM25Index, RetrievalHit

Reranker = Callable[[str, Passage], float]


class DenseRetriever(Protocol):
    def search(self, query: str, *, top_k: int = 20,
               filters: object = None, acl: frozenset[str] | None = None) -> list[RetrievalHit]:
        ...


def _key(passage: Passage) -> tuple[str, int, int]:
    return passage.document_id, passage.line_start, passage.line_end


def reciprocal_rank_fusion(
    lexical: list[RetrievalHit], dense: list[RetrievalHit], *, constant: int = 60,
) -> list[RetrievalHit]:
    """Fuse two ranked lists with reciprocal rank fusion."""
    scores: dict[tuple[str, int, int], float] = {}
    passages: dict[tuple[str, int, int], Passage] = {}
    for ranked in (lexical, dense):
        for rank, hit in enumerate(ranked, 1):
            key = _key(hit.passage)
            passages[key] = hit.passage
            scores[key] = scores.get(key, 0.0) + 1.0 / (constant + rank)
    return [
        RetrievalHit(passages[key], round(score, 9))
        for key, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


class HybridIndex:
    """BM25 with optional dense candidates and an optional reranker.

    ``dense=None`` is exactly the lexical baseline. A dense provider can be a
    local BGE/Chroma adapter when its evaluation proves it is needed.
    """

    def __init__(self, passages: tuple[Passage, ...] | list[Passage], *,
                 dense: DenseRetriever | None = None, reranker: Reranker | None = None,
                 rrf_constant: int = 60) -> None:
        self.lexical = BM25Index(passages)
        self.dense = dense
        self.reranker = reranker
        self.rrf_constant = rrf_constant

    def search(self, query: str, *, top_k: int = 5, candidate_k: int = 20,
               filters: object = None, acl: frozenset[str] | None = None) -> list[RetrievalHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        lexical = self.lexical.search(query, top_k=candidate_k, filters=filters, acl=acl)
        if self.dense is None:
            fused = lexical
        else:
            dense = self.dense.search(query, top_k=candidate_k, filters=filters, acl=acl)
            fused = reciprocal_rank_fusion(lexical, dense, constant=self.rrf_constant)
        if self.reranker is not None:
            fused = sorted(fused, key=lambda hit: (-self.reranker(query, hit.passage), _key(hit.passage)))
        return fused[:top_k]
