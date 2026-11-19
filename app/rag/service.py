"""High-level retrieval service used by future domain packs."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Callable

from app.rag.ingest import SourceFormat, chunk_document
from app.rag.models import DocumentMetadata, Evidence, Passage
from app.rag.retrieval import BM25Index

DocumentInput = tuple[str, DocumentMetadata, SourceFormat]


class RetrievalService:
    """Ingest documents once, then search and return citation-ready evidence."""

    def __init__(self, documents: Iterable[DocumentInput], *, reranker=None) -> None:
        passages = []
        for text, metadata, source_format in documents:
            passages.extend(chunk_document(text, metadata, format=source_format))
        self._set_passages(passages, reranker=reranker)

    @classmethod
    def from_passages(cls, passages: Iterable[Passage], *, reranker=None) -> RetrievalService:
        service = cls.__new__(cls)
        service._set_passages(tuple(passages), reranker=reranker)
        return service

    def _set_passages(self, passages: Iterable[Passage], *, reranker=None) -> None:
        passages = tuple(passages)
        if not passages:
            raise ValueError("RetrievalService requires at least one non-empty passage")
        self.index = BM25Index(passages)
        self.reranker = reranker

    def search(self, query: str, *, top_k: int = 5,
               filters: Mapping[str, object] | Callable[[object], bool] | None = None,
               acl: frozenset[str] | None = None) -> tuple[Evidence, ...]:
        candidate_k = top_k if self.reranker is None else max(top_k * 4, top_k)
        hits = self.index.search(query, top_k=candidate_k, filters=filters, acl=acl)
        if self.reranker is not None:
            hits = sorted(
                hits,
                key=lambda hit: (-self.reranker(query, hit.passage), hit.passage.document_id, hit.passage.line_start),
            )
        return tuple(Evidence.from_passage(hit.passage, score=hit.score) for hit in hits[:top_k])
