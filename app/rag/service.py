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

    def __init__(self, documents: Iterable[DocumentInput]) -> None:
        passages = []
        for text, metadata, source_format in documents:
            passages.extend(chunk_document(text, metadata, format=source_format))
        self._set_passages(passages)

    @classmethod
    def from_passages(cls, passages: Iterable[Passage]) -> RetrievalService:
        service = cls.__new__(cls)
        service._set_passages(tuple(passages))
        return service

    def _set_passages(self, passages: Iterable[Passage]) -> None:
        passages = tuple(passages)
        if not passages:
            raise ValueError("RetrievalService requires at least one non-empty passage")
        self.index = BM25Index(passages)

    def search(self, query: str, *, top_k: int = 5,
               filters: Mapping[str, object] | Callable[[object], bool] | None = None,
               acl: frozenset[str] | None = None) -> tuple[Evidence, ...]:
        return tuple(
            Evidence.from_passage(hit.passage, score=hit.score)
            for hit in self.index.search(query, top_k=top_k, filters=filters, acl=acl)
        )
