"""Typed fixture, document retrieval, and read-only query tools for the policy lab."""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from math import log
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from app.commerce_demo import Region
from app.tools.db import ReadOnlyDB

Ledger = Literal["orders", "refunds", "chargebacks"]
_CENTS = Annotated[int, Field(ge=0, le=100_000_000)]
_TEXT = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_ID = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
_LEDGERS = frozenset(("orders", "refunds", "chargebacks"))
_CORPUS = Path(__file__).with_name("hard_policy_corpus")
_APPLICATION_ID = 0x52414748
_MAX_SOURCE_BYTES = 65_536
_SEED_FILES = ("catalog", "orders", "refunds", "chargebacks")
DocumentSource = Literal["policy", "bulletin"]
_DOCUMENT_NUMBER = re.compile(r"^文号[：:]\s*(?P<id>[a-z][a-z0-9-]{0,63})。$", re.MULTILINE)
_REFERENCED_TITLE = re.compile(r"《([^《》]+)》")
_REF_TOPIC = re.compile(r"“(?P<topic>[^”]+)”专题最新公告")
_SCOPE = re.compile(r"(?P<region>华南|华东|华北)(?:销售的|地区的)(?P<category>[^，。\n]+?)类商品")
_PRODUCT_SCOPE = re.compile(r"具体限于(?P<product>[^，。\n]+)")
_CHINESE_DATE = r"\d{4}年\s*\d{1,2}月\s*\d{1,2}日"
_EFFECTIVE_INTERVAL = re.compile(
    rf"(?:自|从)\s*(?P<start>{_CHINESE_DATE})\s*起(?:生效|执行)，(?:到|至)\s*"
    rf"(?P<end>{_CHINESE_DATE})(?:\s*前有效|(?P<inclusive>\s*为止（含当天）))"
)
_CAP_AMOUNT = re.compile(
    r"(?:每笔|逐笔)(?:扣减)?\s*(?:不超过|上限(?:为)?|最多扣除)\s*(?:人民币\s*)?(\d+(?:\.\d{1,2})?)\s*元"
)
_FULL_REFUND_RULE = re.compile(r"[一-鿿]+订单逐笔扣除原订单记录中的全部退款。")
_LEDGER_TERMS: dict[Ledger, tuple[str, ...]] = {
    "orders": ("原订单退款列", "原单退款列", "原订单记录"),
    "refunds": ("售后退款台账",),
    "chargebacks": ("争议扣回台账",),
}
_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    ordered_on TEXT NOT NULL,
    product TEXT NOT NULL,
    region TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    refund_cents INTEGER NOT NULL CHECK (refund_cents BETWEEN 0 AND amount_cents)
);
CREATE INDEX orders_scope ON orders (product, region, ordered_on);
CREATE TABLE refunds (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
);
CREATE INDEX refunds_order ON refunds (order_id);
CREATE TABLE chargebacks (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
);
CREATE INDEX chargebacks_order ON chargebacks (order_id);
"""


class AmbiguousDocument(ValueError):
    """More than one authoritative document has the winning applicability."""


class _CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product: _TEXT
    category: _TEXT


class _Order(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(gt=0)
    ordered_on: date
    product: _TEXT
    region: Region
    amount_cents: _CENTS
    refund_cents: _CENTS

    @model_validator(mode="after")
    def valid_refund(self):
        if self.refund_cents > self.amount_cents:
            raise ValueError("Refund exceeds order amount")
        return self


class _LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(gt=0)
    order_id: int = Field(gt=0)
    amount_cents: _CENTS


@dataclass(frozen=True)
class TextPassage:
    title: str
    quote: str
    source: DocumentSource
    file: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class DocumentSnapshot:
    version: str
    catalog: tuple[tuple[str, str], ...]
    passages: tuple[TextPassage, ...]


@dataclass(frozen=True)
class _ApplicablePassage:
    passage: TextPassage
    product: str | None
    category: str
    region: str
    start: date
    end_exclusive: date
    rule_text: str


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: _ID
    title: _TEXT
    product: _TEXT | None
    category: _TEXT
    region: Region
    start: date
    end_exclusive: date
    source: DocumentSource
    authority: Literal["official"]
    ref_kind: Literal["latest", "title"] | None
    ref_title: _TEXT | None
    ref_topic: _TEXT | None
    cap_cents: _CENTS | None
    ledger: Ledger | None
    quote: str
    file: str
    line_start: int
    line_end: int


_CATALOG = TypeAdapter(Annotated[list[_CatalogEntry], Field(min_length=1, max_length=32)])
_ORDERS = TypeAdapter(Annotated[list[_Order], Field(max_length=256)])
_EVENTS = TypeAdapter(Annotated[list[_LedgerEntry], Field(max_length=512)])


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum())


def _terms(value: str) -> set[str]:
    normalized = _normalize(value)
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)} if len(normalized) > 1 else {normalized}


def _read(filename: str) -> bytes:
    with (_CORPUS / filename).open("rb") as source:
        content = source.read(_MAX_SOURCE_BYTES + 1)
    if len(content) > _MAX_SOURCE_BYTES:
        raise ValueError("Policy lab source exceeds its size bound")
    return content


def _catalog(content: bytes) -> dict[str, str]:
    rows = _CATALOG.validate_json(content)
    names = {row.product: row.category for row in rows}
    if len({_normalize(product) for product in names}) != len(rows):
        raise ValueError("Duplicate product name")
    return names


def _passages(content: bytes, source: DocumentSource, filename: str) -> tuple[TextPassage, ...]:
    lines = content.decode("utf-8").splitlines()
    headings = [index for index, line in enumerate(lines) if line.startswith("## ")]
    passages = []
    for start, end in zip(headings, headings[1:] + [len(lines)]):
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        passages.append(TextPassage(
            title=lines[start][3:].strip(),
            quote="\n".join(lines[start + 1:end]).lstrip("\n"),
            source=source,
            file=f"app/hard_policy_corpus/{filename}",
            line_start=start + 1,
            line_end=end,
        ))
    return tuple(passages)


def load_document_snapshot() -> DocumentSnapshot:
    """Read each content file once so retrieval and version checks share a snapshot."""
    catalog = _read("catalog.json")
    policies = _read("policies.md")
    bulletins = _read("bulletins.md")
    version = sha256(b"\0".join((catalog, policies, bulletins))).hexdigest()
    return DocumentSnapshot(
        version=version,
        catalog=tuple(_catalog(catalog).items()),
        passages=(
            *_passages(policies, "policy", "policies.md"),
            *_passages(bulletins, "bulletin", "bulletins.md"),
        ),
    )


def document_version() -> str:
    return load_document_snapshot().version


def product_category(product: str, *, snapshot: DocumentSnapshot | None = None) -> str | None:
    """Resolve a product from the catalog, without inferring unknown categories."""
    catalog = dict(snapshot.catalog) if snapshot else _catalog(_read("catalog.json"))
    return catalog.get(product)


def _rank_passages(passages: tuple[TextPassage, ...], source: DocumentSource, query: str) -> list[TextPassage]:
    """Retrieve ranked lexical hits from headings and raw Markdown prose."""
    documents = [passage for passage in passages if passage.source == source]
    document_terms = [_terms(passage.title + "\n" + passage.quote) for passage in documents]
    frequencies = Counter(term for terms in document_terms for term in terms)
    query_terms = _terms(query)
    weights = {
        term: log(1 + (len(documents) - count + 0.5) / (count + 0.5))
        for term, count in frequencies.items()
        if term in query_terms
    }

    matches = []
    for passage, terms in zip(documents, document_terms):
        heading_terms = _terms(passage.title)
        score = sum(
            weights[term] * (2 if term in heading_terms else 1)
            for term in sorted(query_terms & terms)
        )
        if score:
            matches.append((score, passage))
    return [passage for _, passage in sorted(matches, key=lambda match: (-match[0], match[1].line_start))]


def _date_in_prose(value: str) -> date:
    return date(*(int(part) for part in re.findall(r"\d+", value)))


def _applicable_passage(
    passage: TextPassage, product: str, category: str, region: str, start: date, end: date,
) -> _ApplicablePassage | None:
    quote = passage.quote
    if "不得作为结算依据" in quote:
        return None
    scope = _SCOPE.search(quote)
    if not scope or scope["category"] != category or scope["region"] != region:
        return None
    specific = _PRODUCT_SCOPE.search(quote)
    scoped_product = specific["product"] if specific else None
    if scoped_product and _normalize(scoped_product) != _normalize(product):
        return None

    validity = _EFFECTIVE_INTERVAL.search(quote)
    if not validity:
        raise AmbiguousDocument("The retrieved rule does not state a recognizable effective interval")
    effective_start = _date_in_prose(validity["start"])
    effective_end = _date_in_prose(validity["end"])
    if validity["inclusive"]:
        effective_end += timedelta(days=1)
    if not effective_start <= start < end <= effective_end:
        return None

    return _ApplicablePassage(
        passage, scoped_product, category, region, effective_start, effective_end,
        quote[validity.end():].lstrip("。\n"),
    )


def _extract_rule(candidate: _ApplicablePassage) -> _Document:
    passage = candidate.passage
    document_number = _DOCUMENT_NUMBER.search(passage.quote)
    if not document_number:
        raise AmbiguousDocument("The retrieved rule does not have a document number")
    terms = candidate.rule_text.strip()
    titles = _REFERENCED_TITLE.findall(terms)
    latest = "最新公告" in terms
    topic = _REF_TOPIC.search(terms)
    ledgers = [ledger for ledger, names in _LEDGER_TERMS.items() if any(name in terms for name in names)]
    amounts = _CAP_AMOUNT.findall(terms)
    if titles or latest:
        if len(titles) > 1 or (titles and latest) or ledgers or amounts:
            raise AmbiguousDocument("The retrieved rule gives conflicting deductions or referrals")
    else:
        full_refund = ledgers == ["orders"] and _FULL_REFUND_RULE.fullmatch(terms) is not None
        if len(ledgers) != 1 or len(amounts) > 1 or (not amounts and not full_refund):
            raise AmbiguousDocument("The retrieved rule does not establish a ledger and a deduction amount")
    return _Document(
        id=document_number["id"],
        title=passage.title,
        product=candidate.product,
        category=candidate.category,
        region=candidate.region,
        start=candidate.start,
        end_exclusive=candidate.end_exclusive,
        source=passage.source,
        authority="official",
        ref_kind="title" if titles else "latest" if latest else None,
        ref_title=titles[0] if titles else None,
        ref_topic=topic["topic"] if topic else None,
        cap_cents=int(Decimal(amounts[0]) * 100) if amounts else None,
        ledger=ledgers[0] if ledgers else None,
        quote=passage.quote,
        file=passage.file,
        line_start=passage.line_start,
        line_end=passage.line_end,
    )


def _select(documents: list[_ApplicablePassage]) -> dict | None:
    if not documents:
        return None

    def priority(document: _ApplicablePassage) -> tuple[bool, date]:
        return document.product is not None, document.start

    winning_priority = max(priority(document) for document in documents)
    winners = [document for document in documents if priority(document) == winning_priority]
    if len(winners) != 1:
        raise AmbiguousDocument("More than one policy source has the best applicability")
    return _extract_rule(winners[0]).model_dump(mode="json")


def search_policy(
    product: str, category: str, region: str, start: date, end_exclusive: date,
    *, snapshot: DocumentSnapshot | None = None,
) -> dict | None:
    """Return the most specific authority found in the policy text."""
    snapshot = snapshot or load_document_snapshot()
    matches = _rank_passages(snapshot.passages, "policy", f"{product} {category} {region} 退款政策")
    documents = [
        document for passage in matches
        if (document := _applicable_passage(passage, product, category, region, start, end_exclusive)) is not None
    ]
    return _select(documents)


def search_bulletin(
    policy: dict, category: str, region: str, start: date, end_exclusive: date,
    *, product: str | None = None, snapshot: DocumentSnapshot | None = None,
) -> dict | None:
    """Follow a title or topic recovered from the referring passage."""
    kind = policy["ref_kind"]
    if kind not in ("title", "latest"):
        return None
    snapshot = snapshot or load_document_snapshot()
    title = policy["ref_title"] if kind == "title" else None
    if kind == "title" and not title:
        return None
    topic = policy.get("ref_topic") if kind == "latest" else None
    product = product or policy["product"] or ""
    query = f"{title or topic or ''} {product} {category} {region} 退款公告"
    matches = _rank_passages(snapshot.passages, "bulletin", query)
    documents = [
        document for passage in matches
        if (title is None or _normalize(passage.title) == _normalize(title))
        and (topic is None or _normalize(topic) in _normalize(passage.title))
        and (document := _applicable_passage(passage, product, category, region, start, end_exclusive)) is not None
    ]
    return _select(documents)


def prepare_hard_policy_database(root: Path) -> Path:
    """Build a versioned experiment database in a private state directory."""
    seeds = {name: _read(f"{name}.json") for name in _SEED_FILES}
    fingerprint = sha256(b"\0".join(seeds.values())).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True)
    database = root / f"hard-policy-{fingerprint}.sqlite"
    if not database.exists():
        products = _catalog(seeds["catalog"])
        orders = _ORDERS.validate_json(seeds["orders"])
        if any(order.product not in products for order in orders):
            raise ValueError("Order product is absent from the catalog")
        with TemporaryDirectory(dir=root) as staging:
            candidate = Path(staging) / database.name
            with closing(sqlite3.connect(candidate)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(_SCHEMA)
                connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", [
                    (order.id, order.ordered_on.isoformat(), order.product, order.region, order.amount_cents,
                     order.refund_cents)
                    for order in orders
                ])
                for ledger in ("refunds", "chargebacks"):
                    entries = _EVENTS.validate_json(seeds[ledger])
                    connection.executemany(f"INSERT INTO {ledger} VALUES (?, ?, ?)", [
                        (event.id, event.order_id, event.amount_cents) for event in entries
                    ])
                    excessive = connection.execute(
                        f"SELECT 1 FROM {ledger} JOIN orders ON order_id = orders.id "
                        f"GROUP BY order_id HAVING SUM({ledger}.amount_cents) > MAX(orders.amount_cents) LIMIT 1"
                    ).fetchone()
                    if excessive:
                        raise ValueError("Ledger total exceeds order amount")
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            try:
                os.link(candidate, database)
            except FileExistsError:
                pass
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,):
            raise ValueError("Invalid hard policy experiment database")
    return database


def inspect_ledger(database: Path, ledger: Ledger, *, timeout: float = 5.0) -> dict:
    """Read the schemas needed for one whitelisted, resolved ledger."""
    if ledger not in _LEDGERS:
        raise ValueError("Unknown ledger")
    tables = ["orders"] if ledger == "orders" else ["orders", ledger]
    with ReadOnlyDB(database, timeout=timeout) as connection:
        details = []
        for table in tables:
            columns, _ = connection.query(f"SELECT * FROM {table} LIMIT 0", limit=0, max_columns=6)
            details.append({"name": table, "columns": columns})
    return {"ledger": ledger, "tables": details}


def aggregate_statement(
    product: str, region: str, start: date, end_exclusive: date, ledger: Ledger, cap_cents: int | None,
) -> tuple[str, tuple]:
    """Generate scoped SQL from a verified ledger and per-order amount, never model SQL."""
    if ledger not in _LEDGERS:
        raise ValueError("Unknown ledger")
    if cap_cents is not None and (type(cap_cents) is not int or cap_cents < 0):
        raise ValueError("Invalid per-order cap")
    scope_parameters = (product, region, start.isoformat(), end_exclusive.isoformat())
    source_cte = ""
    refund_column = "selected_orders.refund_cents"
    joined_source = "selected_orders"
    if ledger != "orders":
        source_cte = f""",
events_per_order AS (
  SELECT L.order_id, SUM(L.amount_cents) AS amount_cents
  FROM {ledger} AS L JOIN selected_orders AS O ON L.order_id = O.id
  GROUP BY L.order_id
)"""
        refund_column = "COALESCE(events_per_order.amount_cents, 0)"
        joined_source += " LEFT JOIN events_per_order ON selected_orders.id = events_per_order.order_id"
    refund = refund_column if cap_cents is None else f"MIN({refund_column}, ?)"
    statement = f"""WITH selected_orders AS (
  SELECT id, amount_cents, refund_cents
  FROM orders
  WHERE product = ? AND region = ? AND ordered_on >= ? AND ordered_on < ?
){source_cte},
deductions AS (
  SELECT selected_orders.amount_cents AS gross_cents, {refund} AS deducted_cents
  FROM {joined_source}
)
SELECT COUNT(*) AS order_count,
       COALESCE(SUM(gross_cents), 0) AS gross_cents,
       COALESCE(SUM(deducted_cents), 0) AS deducted_cents,
       COALESCE(SUM(gross_cents - deducted_cents), 0) AS net_cents
FROM deductions"""
    return statement, scope_parameters if cap_cents is None else (*scope_parameters, cap_cents)


def aggregate_hard_group(
    database: Path, product: str, region: str, start: date, end_exclusive: date,
    ledger: Ledger, cap_cents: int | None, *, timeout: float = 5.0,
) -> dict:
    sql, parameters = aggregate_statement(product, region, start, end_exclusive, ledger, cap_cents)
    with ReadOnlyDB(database, timeout=timeout) as connection:
        connection.dry_run(sql, parameters=parameters)
        columns, rows = connection.query(sql, limit=2, max_columns=4, parameters=parameters)
    expected = ["order_count", "gross_cents", "deducted_cents", "net_cents"]
    if columns != expected or len(rows) != 1 or any(type(value) is not int for value in rows[0]):
        raise ValueError("Unexpected aggregate shape")
    return {**dict(zip(columns, rows[0], strict=True)), "sql": sql, "parameters": list(parameters), "ledger": ledger}
