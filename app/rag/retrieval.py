"""Lexical retrieval over passages with explicit metadata filters."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from app.rag.models import Passage

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u3400-\u9fff]+")


def _cjk_terms(text: str) -> list[str]:
    terms: list[str] = []
    for run in _CJK.findall(text):
        terms.extend(run)
        terms.extend(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms = _ASCII_WORD.findall(normalized)
    terms.extend(_cjk_terms(normalized))
    return terms


@dataclass(frozen=True)
class RetrievalHit:
    passage: Passage
    score: float


class BM25Index:
    """A deterministic in-memory BM25 index.

    A hit must satisfy every supplied metadata criterion. ACL matching is
    explicit via ``acl``; an empty ACL on a passage means no account may read it.
    """

    def __init__(self, passages: Iterable[Passage], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.passages = tuple(passages)
        if not self.passages:
            raise ValueError("BM25Index requires at least one passage")
        self.k1, self.b = k1, b
        self._body_tokens = [tokenize(item.text) for item in self.passages]
        self._title_tokens = [tokenize(f"{item.title} {item.section}") for item in self.passages]
        self._lengths = [len(tokens) for tokens in self._body_tokens]
        self._average = sum(self._lengths) / len(self._lengths) or 1.0
        self._frequencies = [Counter(tokens) for tokens in self._body_tokens]
        # Count each term once per document, including title-only terms.
        document_terms = [
            set(body) | set(title)
            for body, title in zip(self._body_tokens, self._title_tokens, strict=True)
        ]
        document_frequency = Counter(term for terms in document_terms for term in terms)
        total = len(self.passages)
        self._idf = {
            term: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def _passes(self, passage: Passage, filters: Mapping[str, object] | Callable[[Passage], bool] | None,
                acl: frozenset[str] | None) -> bool:
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

    def search(self, query: str, *, top_k: int = 5,
               filters: Mapping[str, object] | Callable[[Passage], bool] | None = None,
               acl: frozenset[str] | None = None) -> list[RetrievalHit]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        terms = tokenize(query)
        if not terms or not self.passages:
            return []
        hits: list[RetrievalHit] = []
        for index, passage in enumerate(self.passages):
            if not self._passes(passage, filters, acl):
                continue
            frequencies = self._frequencies[index]
            title_frequencies = Counter(self._title_tokens[index])
            length = self._lengths[index]
            score = 0.0
            for term in terms:
                frequency = frequencies.get(term, 0)
                title_frequency = title_frequencies.get(term, 0)
                if not frequency and not title_frequency:
                    continue
                idf = self._idf.get(term, 0.0)
                norm = self.k1 * (1.0 - self.b + self.b * length / self._average)
                score += idf * ((frequency * (self.k1 + 1.0)) / (frequency + norm) if frequency else 0.0)
                score += 1.25 * idf * ((title_frequency * (self.k1 + 1.0)) / (title_frequency + norm) if title_frequency else 0.0)
            if score > 0:
                hits.append(RetrievalHit(passage, round(score, 9)))
        hits.sort(key=lambda hit: (-hit.score, hit.passage.document_id, hit.passage.line_start))
        return hits[:top_k]
