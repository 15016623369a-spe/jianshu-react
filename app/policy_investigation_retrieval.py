"""Resolve applicable policy passages, then recover explicit links on a lookup miss."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path

from app import hard_policy_data as rules
from app.rag.models import DocumentMetadata, Passage
from app.rag.service import RetrievalService

_ROOT = Path(__file__).resolve().parents[1]
_SUPPLEMENT = Path(__file__).with_name("investigation_corpus")
_MAX_REFERENCES = 4
_MAX_SEGMENTS = 16
_MAX_QUERY_LENGTH = 120
RETRIEVAL_TOP_K = 5
_ALIAS = re.compile(
    r"^本(?:通知|公告|文件)(?:(?:过去|此前)也称|接续旧规|承接旧规|取代旧规)"
    r"《([^《》\n]{2,70})》(。|，[^\n]{1,100}。)$",
    re.MULTILINE,
)
_NATURAL_REFERENCE = re.compile(r"(?:请查|参见|请参照)(?:此前|过去)称为「([^「」\n]{2,70})」的(?:通知|公告|文件)")
_EFFECTIVE_DATES = re.compile(
    rf"有效期为(?P<start>{rules._CHINESE_DATE})至(?P<end>{rules._CHINESE_DATE})（含首尾日期）"
)
_PER_ORDER_CAP = re.compile(r"同一订单最多抵扣\s*(?:人民币\s*)?(\d+(?:\.\d{1,2})?)\s*元")
_LEDGER_PHRASES = {
    "订单售后实退台账": "售后退款台账",
    "支付撤销对账台账": "争议扣回台账",
    "原订单的退款金额字段": "原订单退款列",
}



@dataclass(frozen=True)
class EvidenceGap:
    start: date
    end_exclusive: date
    product: str
    region: str
    category: str
    referring: dict
    query: str
    candidates: tuple[dict, ...]


@dataclass(frozen=True)
class ResolvedPolicy:
    start: date
    end_exclusive: date
    policy: dict
    bulletins: tuple[dict, ...]
    ledger: rules.Ledger
    cap_cents: int | None


class RetrievalStop(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _contents(path: Path) -> bytes:
    with path.open("rb") as file:
        contents = file.read(rules._MAX_SOURCE_BYTES + 1)
    if len(contents) > rules._MAX_SOURCE_BYTES:
        raise ValueError("Policy source exceeds its size bound")
    return contents


def load_investigation_snapshot(directory: Path | None = None) -> rules.DocumentSnapshot:
    """Pin the bundled Markdown and catalog, or a separate local corpus for evaluation."""
    if directory is None:
        base = rules.load_document_snapshot()
        catalog = base.catalog
        older_passages = base.passages
        version_parts = [base.version.encode("ascii")]
        directory = _SUPPLEMENT
    else:
        catalog_bytes = _contents(directory / "catalog.json")
        catalog = tuple(rules._catalog(catalog_bytes).items())
        older_passages = ()
        version_parts = [catalog_bytes]

    passages = []
    for source, filename in (("policy", "policies.md"), ("bulletin", "bulletins.md")):
        path = directory / filename
        contents = _contents(path)
        version_parts.append(contents)
        location = path.resolve().relative_to(_ROOT).as_posix() if path.resolve().is_relative_to(_ROOT) else filename
        passages.extend(replace(passage, file=location) for passage in rules._passages(contents, source, filename))
    return rules.DocumentSnapshot(
        version=sha256(b"\0".join(version_parts)).hexdigest(),
        catalog=catalog,
        passages=(*older_passages, *passages),
    )


def _scope_matches(passage: rules.TextPassage, product: str, category: str, region: str) -> bool:
    if "不得作为结算依据" in passage.quote:
        return False
    scope = rules._SCOPE.search(passage.quote)
    product_scope = rules._PRODUCT_SCOPE.search(passage.quote)
    return bool(
        scope and (scope["category"], scope["region"]) == (category, region)
        and (product_scope is None or rules._normalize(product_scope["product"]) == rules._normalize(product))
    )


def _date_prose(quote: str) -> str:
    return _EFFECTIVE_DATES.sub(lambda match: f"从{match['start']}起执行，到{match['end']}为止（含当天）", quote)


def _date_boundaries(
    snapshot: rules.DocumentSnapshot, product: str, category: str, region: str,
    start: date, end_exclusive: date,
) -> list[date]:
    points = {start, end_exclusive}
    for passage in snapshot.passages:
        if not _scope_matches(passage, product, category, region):
            continue
        validity = rules._EFFECTIVE_INTERVAL.search(_date_prose(passage.quote))
        if validity is None:
            continue
        first = rules._date_in_prose(validity["start"])
        last = rules._date_in_prose(validity["end"]) + timedelta(days=bool(validity["inclusive"]))
        points.update(boundary for boundary in (first, last) if start < boundary < end_exclusive)
    if len(points) > _MAX_SEGMENTS + 1:
        raise RetrievalStop("scope_too_complex")
    return sorted(points)


def _read_rule(candidate: rules._ApplicablePassage) -> rules._Document:
    # Normalize equivalent phrases for the parser. The citation keeps the original text.
    terms = _NATURAL_REFERENCE.sub(lambda match: f"见《{match[1]}》", candidate.rule_text)
    terms = _PER_ORDER_CAP.sub(lambda match: f"每笔最多扣除人民币 {match[1]} 元", terms)
    for phrase, ledger_term in _LEDGER_PHRASES.items():
        terms = terms.replace(phrase, ledger_term)
    return rules._extract_rule(replace(candidate, rule_text=terms))


def _select(candidates: list[rules._ApplicablePassage], conflict: str) -> dict | None:
    if not candidates:
        return None

    def priority(candidate: rules._ApplicablePassage) -> tuple[bool, date]:
        return candidate.product is not None, candidate.start

    winning = max(priority(candidate) for candidate in candidates)
    selected = [candidate for candidate in candidates if priority(candidate) == winning]
    if len(selected) != 1:
        raise RetrievalStop(conflict)
    try:
        return _read_rule(selected[0]).model_dump(mode="json")
    except (rules.AmbiguousDocument, ValueError) as exc:
        raise RetrievalStop(conflict) from exc


def _matching(
    passages: list[rules.TextPassage], product: str, category: str, region: str, start: date, end_exclusive: date,
    conflict: str,
) -> list[rules._ApplicablePassage]:
    try:
        candidates = []
        for passage in passages:
            normalized = replace(passage, quote=_date_prose(passage.quote))
            if applicable := rules._applicable_passage(normalized, product, category, region, start, end_exclusive):
                candidates.append(replace(applicable, passage=passage))
        return candidates
    except (rules.AmbiguousDocument, ValueError) as exc:
        raise RetrievalStop(conflict) from exc


def _citation(value: rules.TextPassage | dict) -> dict:
    if isinstance(value, dict):
        return {field: value[field] for field in ("id", "title", "file", "line_start")}
    number = rules._DOCUMENT_NUMBER.search(value.quote)
    return {"id": number["id"] if number else None, "title": value.title, "file": value.file,
            "line_start": value.line_start}


def _log_retrieval(
    track: Callable[[dict], None] | None, stage: str, source: rules.DocumentSource,
    reference: dict | None, start: date, end: date, query: str,
    matches: list[rules.TextPassage], selected: dict | None,
) -> None:
    if track is not None:
        track({
            "stage": stage, "source": source, "referrer_id": reference["id"] if reference else None,
            "scope": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
            "query": query, "candidates": [_citation(passage) for passage in matches[:RETRIEVAL_TOP_K]],
            "selected": _citation(selected) if selected else None,
        })


def _rank_policy_passages(passages: tuple[rules.TextPassage, ...], source: rules.PassageSource,
                         query: str) -> list[rules.TextPassage]:
    """Rank policy passages through the shared BM25 service.

    Exact title/topic filters and applicability/verification stay in this module;
    only the ranking primitive is shared.
    """
    selected = [passage for passage in passages if passage.source == source]
    if not selected:
        return []
    generic = tuple(
        Passage(
            document_id=f"{source}-{index}", version=None, title=passage.title, section=passage.title,
            text=passage.quote, line_start=passage.line_start, line_end=passage.line_end,
            metadata=DocumentMetadata(
                document_id=f"{source}-{index}", title=passage.title, source=source,
                attributes={"file": passage.file},
            ), source_file=passage.file,
        )
        for index, passage in enumerate(selected)
    )
    hits = RetrievalService.from_passages(generic).search(query, top_k=len(generic))
    by_key = {
        (passage.title, passage.quote, passage.file, passage.line_start): passage
        for passage in selected
    }
    return [by_key[(hit.title, hit.quote, hit.source_file, hit.line_start)] for hit in hits
            if (hit.title, hit.quote, hit.source_file, hit.line_start) in by_key]


def _find_rule(
    snapshot: rules.DocumentSnapshot, product: str, category: str, region: str,
    start: date, end_exclusive: date, *, reference: dict | None = None, track: Callable[[dict], None] | None = None,
) -> dict | None:
    source: rules.DocumentSource = "bulletin" if reference else "policy"
    title = reference["ref_title"] if reference else None
    topic = reference["ref_topic"] if reference else None
    query = f"{title or topic or ''} {product} {category} {region} 退款".strip()
    matches = _rank_policy_passages(snapshot.passages, source, query)
    filtered = [
        match for match in matches
        if (title is None or rules._normalize(match.title) == rules._normalize(title))
        and (topic is None or rules._normalize(topic) in rules._normalize(match.title))
    ]
    conflict = "bulletin_conflict" if reference else "policy_conflict"
    verified = None
    try:
        verified = _select(_matching(filtered, product, category, region, start, end_exclusive, conflict), conflict)
        return verified
    finally:
        _log_retrieval(track, "exact", source, reference, start, end_exclusive, query, matches, verified)


def _recover_title(
    snapshot: rules.DocumentSnapshot, reference: dict, product: str, category: str, region: str,
    start: date, end_exclusive: date, *, lexical: bool, rewrite: Callable[[EvidenceGap], str | None] | None,
    observe: Callable[[str, str, str], None], track: Callable[[dict], None] | None,
) -> dict | None:
    title = reference["ref_title"]
    if not title:
        return None
    original = rules._normalize(title)
    candidates = _matching(
        [
            passage for passage in snapshot.passages if passage.source == "bulletin"
            and any(rules._normalize(match[1]) == original for match in _ALIAS.finditer(passage.quote))
        ],
        product, category, region, start, end_exclusive, "bulletin_conflict",
    )
    # Check all explicitly linked, applicable candidates for conflicts before accepting any ranked result.
    verified = _select(candidates, "bulletin_conflict")
    if lexical:
        matches = _rank_policy_passages(snapshot.passages, "bulletin", title)
        identity = _citation(verified) if verified else None
        selected = verified if identity is not None and any(
            _citation(match) == identity for match in matches[:RETRIEVAL_TOP_K]
        ) else None
        _log_retrieval(track, "lexical", "bulletin", reference, start, end_exclusive, title, matches, selected)
        observe("retrieve_lexical", "found" if selected else "miss", selected["title"] if selected else title)
        if selected:
            return selected

    if verified is None or rewrite is None:
        return None
    gap = EvidenceGap(
        start=start, end_exclusive=end_exclusive, product=product, region=region, category=category,
        referring=reference, query=title,
        candidates=tuple({"title": candidate.passage.title, "quote": candidate.passage.quote[:380]} for candidate in candidates[:6]),
    )
    edited_query = rewrite(gap)
    if not isinstance(edited_query, str) or not 1 <= len(edited_query) <= _MAX_QUERY_LENGTH:
        return None

    matches = _rank_policy_passages(snapshot.passages, "bulletin", edited_query)
    # In the model tier the rewritten query must independently retrieve the same source
    # whose applicability and textual link were checked above.
    title_terms = rules._terms(verified["title"])
    query_terms = rules._terms(edited_query)
    is_alias = rules._normalize(edited_query) == original
    enough_title_overlap = len(title_terms & query_terms) >= min(2, len(title_terms)) and bool(title_terms)
    identity = _citation(verified)
    selected = verified if (is_alias or enough_title_overlap) and any(
        _citation(match) == identity for match in matches[:RETRIEVAL_TOP_K]
    ) else None
    _log_retrieval(track, "model", "bulletin", reference, start, end_exclusive, edited_query, matches, selected)
    observe("retrieve_rewritten", "found" if selected else "miss", selected["title"] if selected else title)
    return selected


def resolve_policy_window(
    snapshot: rules.DocumentSnapshot, product: str, region: str, start: date, end_exclusive: date, *,
    lexical: bool = False, rewrite: Callable[[EvidenceGap], str | None] | None = None,
    observe: Callable[[str, str, str], None] | None = None, track: Callable[[dict], None] | None = None,
) -> tuple[ResolvedPolicy, ...]:
    """Require applicable evidence for a complete date range before returning executable rules."""
    if start >= end_exclusive:
        raise RetrievalStop("invalid_period")
    category = rules.product_category(product, snapshot=snapshot)
    if category is None:
        raise RetrievalStop("product_unknown")
    if observe is None:
        def observe(stage, status, detail):
            pass

    boundaries = _date_boundaries(snapshot, product, category, region, start, end_exclusive)
    resolved = []
    for first, last in zip(boundaries, boundaries[1:]):
        policy = _find_rule(snapshot, product, category, region, first, last, track=track)
        observe("retrieve_policy", "found" if policy else "miss", policy["title"] if policy else f"{first} 至 {last}")
        if policy is None:
            raise RetrievalStop("policy_missing")
        current = policy
        bulletins = []
        while current["ref_kind"]:
            if len(bulletins) >= _MAX_REFERENCES:
                raise RetrievalStop("referral_limit")
            bulletin = _find_rule(snapshot, product, category, region, first, last, reference=current, track=track)
            observe(
                "retrieve_bulletin", "found" if bulletin else "miss",
                bulletin["title"] if bulletin else current["ref_title"] or current["ref_topic"] or current["title"],
            )
            if bulletin is None and (lexical or rewrite is not None):
                bulletin = _recover_title(
                    snapshot, current, product, category, region, first, last,
                    lexical=lexical, rewrite=rewrite, observe=observe, track=track,
                )
            if bulletin is None:
                raise RetrievalStop("bulletin_missing")
            if bulletin["id"] in {policy["id"], *(document["id"] for document in bulletins)}:
                raise RetrievalStop("referral_cycle")
            bulletins.append(bulletin)
            current = bulletin
        if current["ledger"] is None:
            raise RetrievalStop("verification_failed")
        segment = ResolvedPolicy(first, last, policy, tuple(bulletins), current["ledger"], current["cap_cents"])
        if resolved and (resolved[-1].policy, resolved[-1].bulletins) == (segment.policy, segment.bulletins):
            resolved[-1] = replace(resolved[-1], end_exclusive=last)
        else:
            resolved.append(segment)
    return tuple(resolved)
