"""Generic retrieval-augmented investigation primitives.

The package is intentionally independent of the existing policy/manual demo
workflows. It provides the stable pieces V3 needs: document metadata, passages,
evidence, ingestion, and lexical retrieval with metadata filtering.
"""
from app.rag.ingest import chunk_document, content_sha256, parse_html, parse_markdown
from app.rag.models import DocumentMetadata, Evidence, Passage
from app.rag.dense import CrossEncoderReranker, SentenceTransformerDense
from app.rag.domains import DomainPack, DomainRegistry, default_registry
from app.rag.hybrid import HybridIndex, reciprocal_rank_fusion
from app.rag.retrieval import BM25Index, RetrievalHit, tokenize
from app.rag.service import RetrievalService

__all__ = [
    "BM25Index",
    "CrossEncoderReranker",
    "DomainPack",
    "SentenceTransformerDense",
    "HybridIndex",
    "DomainRegistry",
    "RetrievalService",
    "DocumentMetadata",
    "Evidence",
    "Passage",
    "RetrievalHit",
    "chunk_document",
    "reciprocal_rank_fusion",
    "default_registry",
    "content_sha256",
    "parse_html",
    "parse_markdown",
    "tokenize",
]
