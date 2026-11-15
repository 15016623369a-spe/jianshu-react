"""Resolve applicable policy passages, then recover explicit links on a lookup miss."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path

from app import hard_policy_data as rules

_ROOT = Path(__file__).resolve().parents[1]
_SUPPLEMENT = Path(__file__).with_name("investigation_corpus")
_MAX_REFERENCES = 4
_MAX_SEGMENTS = 16
_MAX_QUERY_LENGTH = 120
_ALIAS = re.compile(r"^本(?:通知|公告|文件)(?:过去|此前)也称《([^《》\n]{2,70})》。$", re.MULTILINE)
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


def _find_rule(
    snapshot: rules.DocumentSnapshot, product: str, category: str, region: str,
    start: date, end_exclusive: date, *, reference: dict | None = None,
) -> dict | None:
    source = "bulletin" if reference else "policy"
    title = reference["ref_title"] if reference else None
    topic = reference["ref_topic"] if reference else None
    matches = rules._rank_passages(snapshot.passages, source, f"{title or topic or ''} {product} {category} {region} 退款")
    filtered = [
        match for match in matches
        if (title is None or rules._normalize(match.title) == rules._normalize(title))
        and (topic is None or rules._normalize(topic) in rules._normalize(match.title))
    ]
    conflict = "bulletin_conflict" if reference else "policy_conflict"
    return _select(_matching(filtered, product, category, region, start, end_exclusive, conflict), conflict)


def _recover_title(
    snapshot: rules.DocumentSnapshot, reference: dict, product: str, category: str, region: str,
    start: date, end_exclusive: date, rewrite: Callable[[EvidenceGap], str | None],
    observe: Callable[[str, str, str], None],
) -> dict | None:
    title = reference["ref_title"]
    if not title:
        return None
    original = rules._normalize(title)
    candidates = _matching(
        [
            passage for passage in snapshot.passages if passage.source == "bulletin"
            and original in {rules._normalize(alias) for alias in _ALIAS.findall(passage.quote)}
        ],
        product, category, region, start, end_exclusive, "bulletin_conflict",
    )
    verified = _select(candidates, "bulletin_conflict")
    if verified is None:
        return None

    gap = EvidenceGap(
        start=start, end_exclusive=end_exclusive, product=product, region=region, category=category,
        referring=reference, query=title,
        candidates=tuple({"title": candidate.passage.title, "quote": candidate.passage.quote[:380]} for candidate in candidates[:5]),
    )
    edited_query = rewrite(gap)
    if not isinstance(edited_query, str) or not 1 <= len(edited_query) <= _MAX_QUERY_LENGTH:
        observe("retrieve_rewritten", "miss", title)
        return None

    # Search the proposed wording, but publish only a date/scope/authority-checked source that proves the original link.
    matches = rules._rank_passages(snapshot.passages, "bulletin", edited_query)
    title_terms = rules._terms(verified["title"])
    query_terms = rules._terms(edited_query)
    unchanged_alias = rules._normalize(edited_query) == original
    enough_title_overlap = len(title_terms & query_terms) >= min(2, len(title_terms)) and bool(title_terms)
    if (not unchanged_alias and not enough_title_overlap) or not any(
        match.file == verified["file"] and match.line_start == verified["line_start"] for match in matches
    ):
        observe("retrieve_rewritten", "miss", title)
        return None
    observe("retrieve_rewritten", "found", verified["title"])
    return verified


def resolve_policy_window(
    snapshot: rules.DocumentSnapshot, product: str, region: str, start: date, end_exclusive: date, *,
    rewrite: Callable[[EvidenceGap], str | None] | None = None,
    observe: Callable[[str, str, str], None] | None = None,
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
        policy = _find_rule(snapshot, product, category, region, first, last)
        observe("retrieve_policy", "found" if policy else "miss", policy["title"] if policy else f"{first} 至 {last}")
        if policy is None:
            raise RetrievalStop("policy_missing")
        current = policy
        bulletins = []
        while current["ref_kind"]:
            if len(bulletins) >= _MAX_REFERENCES:
                raise RetrievalStop("referral_limit")
            bulletin = _find_rule(snapshot, product, category, region, first, last, reference=current)
            observe(
                "retrieve_bulletin", "found" if bulletin else "miss",
                bulletin["title"] if bulletin else current["ref_title"] or current["ref_topic"] or current["title"],
            )
            if bulletin is None and rewrite is not None:
                bulletin = _recover_title(snapshot, current, product, category, region, first, last, rewrite, observe)
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
