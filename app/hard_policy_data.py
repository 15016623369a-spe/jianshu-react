"""Typed fixture, document retrieval, and read-only query tools for the policy lab."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date
from hashlib import sha256
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
_DOCUMENT_PARTS = re.compile(r"^## (?P<heading>[^\n]+)\n<!-- (?P<metadata>[^\n]+) -->\n(?P<excerpt>.+)$", re.DOTALL)
_REFERENCED_TITLE = re.compile(r"《([^《》]+)》")
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


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: _ID
    title: _TEXT
    product: _TEXT | None
    category: _TEXT
    region: Region
    start: date
    end_exclusive: date
    source: Literal["policy", "bulletin"]
    authority: Literal["official", "informational"]
    ref_kind: Literal["latest", "title"] | None
    ref_title: _TEXT | None
    cap_cents: _CENTS | None
    ledger: Ledger | None
    quote: Annotated[str, StringConstraints(min_length=1, max_length=400)]

    @model_validator(mode="after")
    def valid_rule(self):
        if self.start >= self.end_exclusive:
            raise ValueError("Document effective period must be positive")
        if self.authority == "informational":
            if self.ref_kind or self.ref_title or self.ledger or self.cap_cents is not None:
                raise ValueError("Informational text cannot authorize a deduction")
            return self
        if self.ref_kind is not None:
            if self.source != "policy" or self.ledger is not None or self.cap_cents is not None:
                raise ValueError("A policy referral cannot set a ledger or deduction amount")
            titles = _REFERENCED_TITLE.findall(self.quote)
            if self.ref_kind == "title":
                if not self.ref_title or [_normalize(title) for title in titles] != [_normalize(self.ref_title)]:
                    raise ValueError("Indexed heading must match the title in the policy prose")
            elif self.ref_title or "最新公告" not in self.quote:
                raise ValueError("Latest-bulletin referral must appear in the policy prose")
        elif self.ref_title is not None or self.ledger is None:
            raise ValueError("An executable rule requires a ledger and no referral title")
        return self


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


def product_category(product: str) -> str | None:
    """Resolve a product from the catalog, without inferring unknown categories."""
    return _catalog(_read("catalog.json")).get(product)


def _documents(source: Literal["policy", "bulletin"]) -> list[_Document]:
    filename = "policies.md" if source == "policy" else "bulletins.md"
    blocks = re.split(r"\n(?=## )", _read(filename).decode("utf-8"))[1:]
    documents = []
    identifiers = set()
    for block in blocks:
        parsed = _DOCUMENT_PARTS.fullmatch(block.strip())
        if parsed is None:
            raise ValueError("Malformed policy lab document")
        metadata = json.loads(parsed["metadata"])
        if not isinstance(metadata, dict) or "quote" in metadata or metadata.get("title") != parsed["heading"]:
            raise ValueError("Malformed policy lab index")
        record = json.dumps({**metadata, "quote": parsed["excerpt"].strip()})
        document = _Document.model_validate_json(record)
        if document.source != source or document.id in identifiers:
            raise ValueError("Invalid document identity")
        documents.append(document)
        identifiers.add(document.id)
    return documents


def _retrieved(documents: list[_Document], query: str) -> list[_Document]:
    """Find normalized textual matches. Exact applicability is checked separately."""
    query_terms = _terms(query)
    matches = []
    for document in documents:
        relevance = len(query_terms & _terms(" ".join(
            (document.title, document.category, document.region, document.quote)
        )))
        if relevance:
            matches.append((relevance, document))
    return [document for _, document in sorted(matches, key=lambda match: (-match[0], match[1].id))]


def _applies(document: _Document, product: str, category: str, region: str, start: date, end: date) -> bool:
    return (
        document.authority == "official"
        and document.category == category
        and document.region == region
        and (document.product is None or _normalize(document.product) == _normalize(product))
        and document.start <= start < end <= document.end_exclusive
    )


def _select(documents: list[_Document], *, category_fallback: bool = False) -> dict | None:
    if not documents:
        return None

    def priority(document: _Document) -> tuple[bool, date]:
        return category_fallback and document.product is not None, document.start

    winning_priority = max(priority(document) for document in documents)
    winners = [document for document in documents if priority(document) == winning_priority]
    if len(winners) != 1:
        raise AmbiguousDocument("More than one policy source has the best applicability")
    return winners[0].model_dump(mode="json")


def search_policy(product: str, category: str, region: str, start: date, end_exclusive: date) -> dict | None:
    """Return the most specific eligible authority, with no fallback to FAQs."""
    retrieved = _retrieved(_documents("policy"), f"{product} {category} {region} 退款政策")
    return _select([document for document in retrieved if _applies(
        document, product, category, region, start, end_exclusive,
    )], category_fallback=True)


def search_bulletin(policy: dict, category: str, region: str, start: date, end_exclusive: date) -> dict | None:
    """Resolve a title in the prose or choose the latest authority covering the interval."""
    kind = policy["ref_kind"]
    if kind not in ("title", "latest"):
        return None
    title = policy["ref_title"] if kind == "title" else None
    if kind == "title" and not title:
        return None
    retrieved = _retrieved(_documents("bulletin"), f"{title or ''} {category} {region} 退款公告")
    product = policy["product"]
    return _select([
        document for document in retrieved
        if _applies(document, product or "", category, region, start, end_exclusive)
        and (title is None or _normalize(document.title) == _normalize(title))
    ])


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
