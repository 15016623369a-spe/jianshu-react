"""Stable data structures shared by ingestion, retrieval, and evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class DocumentMetadata:
    """Identity and routing metadata for one immutable document version."""

    document_id: str
    title: str
    source: str = "document"
    version: int | None = None
    tenant: str | None = None
    acl: frozenset[str] = frozenset()
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Passage:
    """One citable section of a document, with source line bounds."""

    document_id: str
    version: int | None
    title: str
    section: str
    text: str
    line_start: int
    line_end: int
    metadata: DocumentMetadata
    source_file: str | None = None

    @property
    def evidence_id(self) -> str:
        return f"doc:{self.document_id}#{self.line_start}-{self.line_end}"

    def supports(self, acl: frozenset[str] | None = None) -> bool:
        return acl is None or bool(self.metadata.acl & acl)


@dataclass(frozen=True)
class Evidence:
    """A source span that can be shown to a user and checked by a verifier."""

    evidence_id: str
    document_id: str
    version: int | None
    title: str
    section: str
    quote: str
    line_start: int
    line_end: int
    score: float | None = None
    source_file: str | None = None

    @classmethod
    def from_passage(cls, passage: Passage, *, score: float | None = None) -> Evidence:
        return cls(
            evidence_id=passage.evidence_id,
            document_id=passage.document_id,
            version=passage.version,
            title=passage.title,
            section=passage.section,
            quote=passage.text,
            line_start=passage.line_start,
            line_end=passage.line_end,
            score=score,
            source_file=passage.source_file,
        )
