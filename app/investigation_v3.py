"""Shared investigation request and evidence format for document and ledger tasks."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.hard_policy_data import PassageSource, TextPassage, _normalize, _passages, _terms
from app.policy_documents import PinnedDocument
from app.policy_investigation import InvestigationRequest
from app.rag.models import DocumentMetadata, Evidence, Passage
from app.rag.rerank import build_reranker
from app.rag.service import RetrievalService

_MANUAL_ROOT = Path(__file__).with_name("manual_corpus")
_MANUAL_LATEST = 2
_VERSION_MENTION = re.compile(r"第\s*([0-9]{1,2}|一|二)\s*版")
_TOPIC_PHRASES = re.compile(r"请问|我想知道|远望m1|演示手册|手册|有什么方法|怎么办|怎么|怎样|如何|步骤|吗|呢")


class PolicyInvestigationV3(InvestigationRequest):
    domain: Literal["policy"]
    investigation_id: UUID | None = None


class RetailInvestigationV3(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["retail"]
    question: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
    investigation_id: UUID | None = None


class ManualInvestigationV3(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["manual"]
    question: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
    document_id: UUID | None = None
    version: Annotated[int, Field(strict=True, ge=1, le=20)] | None = None
    investigation_id: UUID | None = None


@dataclass(frozen=True)
class InvestigationEvidence:
    source: PassageSource
    document_id: str
    version: str
    title: str
    file: str
    line_start: int
    line_end: int
    quote: str
    locator: str | None = None


def investigation_result(domain: Literal["policy", "manual", "retail"], result: dict,
                         sources: tuple[InvestigationEvidence, ...] = ()) -> dict:
    """Publish an answer only when it has well-formed source references."""
    if result["kind"] == "answer" and (
        not sources or any(not item.quote.strip() or not 1 <= item.line_start <= item.line_end for item in sources)
    ):
        result = {"kind": "abstained", "reason": "missing_evidence", "message": "没有足够的可核对依据。"}
        sources = ()
    return {**result, "domain": domain, "evidence": [asdict(item) for item in sources] if result["kind"] == "answer" else []}


def policy_evidence(result: dict) -> tuple[InvestigationEvidence, ...]:
    indexed = {}
    for scenario in ("actual", "counterfactual"):
        for period in result.get(scenario, ()):
            for segment in period["segments"]:
                for document in segment["documents"]:
                    file = document["file"]
                    doc_id, version = document["id"], result["document_version"]
                    parts = file.split("/")
                    if len(parts) == 4 and parts[0] == "uploads":
                        doc_id, version = parts[1], parts[2][1:]
                    reference = InvestigationEvidence(
                        document["source"], doc_id, version, document["title"], file,
                        document["line_start"], document["line_end"], document["quote"],
                    )
                    indexed[file, document["line_start"]] = reference
    return tuple(indexed.values())


def _manual_passages(version: int) -> tuple[tuple[TextPassage, ...], str]:
    filename = f"guide-v{version}.md"
    raw = (_MANUAL_ROOT / filename).read_bytes()
    path = f"app/manual_corpus/{filename}"
    passages = tuple(replace(item, file=path) for item in _passages(raw, "manual", filename))
    return passages, sha256(raw).hexdigest()


def _matches_topic(query: str, passage: TextPassage | Passage | Evidence) -> bool:
    title = _TOPIC_PHRASES.sub("", _normalize(passage.title))
    question = _TOPIC_PHRASES.sub("", _normalize(query))
    if len(question) < 2 or len(title) < 2:
        return False
    if question in title or (title in question and len(title) / len(question) >= 0.65):
        return True
    question_terms = _terms(question)
    title_terms = _terms(title)
    hits = question_terms & title_terms
    return len(hits) >= 2 and len(hits) / len(question_terms) >= 0.65 and len(hits) / len(title_terms) >= 0.55


def run_manual_investigation(request: ManualInvestigationV3, document: PinnedDocument | None) -> dict:
    if request.document_id is not None and (document is None or document.id != str(request.document_id)):
        raise ValueError("Manual source was not pinned")
    if document is not None:
        passages, digest = document.passages(), document.digest
        doc_id, version = document.id, document.version
    else:
        version = request.version if request.version is not None else _MANUAL_LATEST
        if version not in (1, 2):
            return investigation_result("manual", {
                "kind": "clarification", "reason": "unknown_version", "message": "请选择演示手册第 1 或第 2 版。",
            })
        passages, digest = _manual_passages(version)
        doc_id = "demo-manual-m1"

    result = {"document_version": str(version), "content_sha256": digest, "metrics": {"model_calls": 0, "sql_queries": 0}}
    version_labels = {"一": "1", "二": "2"}
    mentioned = {int(version_labels.get(item, item)) for item in _VERSION_MENTION.findall(request.question)}
    if mentioned and mentioned != {version}:
        return investigation_result("manual", {
            **result, "kind": "clarification", "reason": "version_conflict", "message": "请在手册版本中选择问题指定的一个版本。",
        })
    question = _VERSION_MENTION.sub("", request.question)
    indexed = tuple(
        Passage(
            document_id=doc_id, version=int(version), title=item.title, section=item.title,
            text=item.quote, line_start=item.line_start, line_end=item.line_end,
            metadata=DocumentMetadata(
                document_id=doc_id, title=item.title, source="manual", version=int(version),
                attributes={"file": item.file},
            ), source_file=item.file, locator=item.locator,
        )
        for item in passages
    )
    matching = [
        hit for hit in RetrievalService.from_passages(indexed, reranker=build_reranker(indexed)).search(question, top_k=5)
        if _matches_topic(question, hit)
    ]
    if not matching:
        return investigation_result("manual", {
            **result, "kind": "abstained", "reason": "no_matching_section", "message": "当前手册版本未找到足够匹配的章节。",
        })
    if len(matching) > 1:
        return investigation_result("manual", {
            **result, "kind": "clarification", "reason": "multiple_sections", "message": "找到多个相关章节，请指定要查询的操作。",
        })
    passage = matching[0]
    return investigation_result("manual", {
        **result, "kind": "answer", "reason": None, "message": "已找到相关手册原文。", "answer": passage.quote,
    }, (InvestigationEvidence("manual", doc_id, str(version), passage.title, passage.source_file or "",
                              passage.line_start, passage.line_end, passage.quote, passage.locator),))
