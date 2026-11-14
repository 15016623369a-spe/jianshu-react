"""Evidence adapters for three sources in the synthetic multi-source experiment."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from app.commerce_demo import Region
from app.tools.db import ReadOnlyDB

_CORPUS = Path(__file__).with_name("multisource_corpus")
_APPLICATION_ID = 0x5241474D
_MAX_SOURCE_BYTES = 32_768
_MAX_ORDERS = 128
_MAX_DOCUMENTS = 32

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9:_-]{0,47}$")]
Product = Annotated[str, StringConstraints(min_length=1, max_length=16)]
Cents = Annotated[int, Field(ge=0, le=100_000_000)]


class FixtureOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    ordered_on: date
    product: Product
    region: Region
    amount_cents: Cents
    refund_cents: Cents

    @model_validator(mode="after")
    def validate_refund(self):
        if self.refund_cents > self.amount_cents:
            raise ValueError("Fixture refund exceeds the order amount")
        return self


class EvidenceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: Identifier
    source: Literal["policy", "bulletin"]
    product: Product
    region: Region
    start: date
    end_exclusive: date
    cap_cents: Cents | None
    ref: Identifier | None
    quote: Annotated[str, StringConstraints(min_length=1, max_length=250)]

    @model_validator(mode="after")
    def validate_scope(self):
        if self.end_exclusive <= self.start or not self.id.startswith(self.source + "-"):
            raise ValueError("Invalid document scope or identifier")
        if self.ref is not None and (self.cap_cents is not None or not self.ref.startswith("bulletin-")):
            raise ValueError("A referral cannot also specify an amount")
        return self


_ORDERS = TypeAdapter(Annotated[list[FixtureOrder], Field(max_length=_MAX_ORDERS)])
_DOCUMENTS = TypeAdapter(Annotated[list[EvidenceDocument], Field(max_length=_MAX_DOCUMENTS)])
_DML = "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)"
_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    ordered_on TEXT NOT NULL,
    product TEXT NOT NULL,
    region TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    refund_cents INTEGER NOT NULL CHECK (refund_cents BETWEEN 0 AND amount_cents)
);
CREATE INDEX orders_date ON orders (ordered_on, product, region);
"""


def _read_corpus(name: str) -> bytes:
    with (_CORPUS / f"{name}.json").open("rb") as file:
        content = file.read(_MAX_SOURCE_BYTES + 1)
    if len(content) > _MAX_SOURCE_BYTES:
        raise ValueError("Synthetic evidence source exceeds its size limit")
    return content


def prepare_multisource_database(root: Path) -> Path:
    """Build the isolated orders fixture once per source fingerprint, atomically."""
    content = _read_corpus("orders")
    orders = _ORDERS.validate_json(content)
    fingerprint = sha256(content).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True)
    database = root / f"multi-source-orders-{fingerprint}.sqlite"
    if not database.exists():
        with TemporaryDirectory(dir=root) as staging:
            candidate = Path(staging) / database.name
            with closing(sqlite3.connect(candidate)) as connection, connection:
                connection.executescript(_SCHEMA)
                connection.executemany(_DML, [(
                    order.id, order.ordered_on.isoformat(), order.product, order.region,
                    order.amount_cents, order.refund_cents,
                ) for order in orders])
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            try:
                os.link(candidate, database)
            except FileExistsError:
                pass  # A concurrent startup published this same fixture first.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,):
            raise ValueError("Expected the multi-source experiment order database")
    return database


def discovery_statement(start: date, end_exclusive: date, region: str | None) -> tuple[str, tuple]:
    dates = start.isoformat(), end_exclusive.isoformat()
    region_filter = " AND region = ?" if region is not None else ""
    sql = f"""SELECT product, region, COUNT(*) AS refunded_order_count
FROM orders
WHERE ordered_on >= ? AND ordered_on < ? AND refund_cents > 0{region_filter}
GROUP BY product, region
ORDER BY product, region"""
    return sql, dates if region is None else (*dates, region)


def discover_groups(
    database: Path, start: date, end_exclusive: date, region: str | None, *, timeout: float = 5,
) -> list[dict]:
    """Discover product/region groups with positive refunds in the requested window."""
    sql, parameters = discovery_statement(start, end_exclusive, region)
    with ReadOnlyDB(database, timeout=timeout) as db:
        db.dry_run(sql, parameters=parameters)
        _, rows = db.query(sql, limit=None, max_columns=3, max_result_bytes=32_768, parameters=parameters)
    return [{
        "id": f"group:{index}", "product": product, "region": area, "order_count": refunded_count,
    } for index, (product, area, refunded_count) in enumerate(rows, start=1)]


def _documents(source: Literal["policy", "bulletin"]) -> list[EvidenceDocument]:
    name = "policies" if source == "policy" else "bulletins"
    documents = _DOCUMENTS.validate_json(_read_corpus(name))
    if any(document.source != source for document in documents):
        raise ValueError("Unexpected policy document source")
    if len({document.id for document in documents}) != len(documents):
        raise ValueError("Duplicate policy document ID")
    return documents


def lookup_policy(group: dict, start: date, end_exclusive: date) -> dict | None:
    matches = [document for document in _documents("policy") if (
        document.product == group["product"] and document.region == group["region"]
        and document.start <= start < end_exclusive <= document.end_exclusive
    )]
    if len(matches) > 1:
        raise ValueError("Conflicting policies for the requested dates and group")
    return matches[0].model_dump(mode="json") if matches else None


def lookup_bulletin(id: str) -> dict | None:
    document = next((document for document in _documents("bulletin") if document.id == id), None)
    return document.model_dump(mode="json") if document is not None else None


def group_statement(group: dict, start: date, end_exclusive: date, cap_cents: int | None) -> tuple[str, tuple]:
    dates_and_group = (start.isoformat(), end_exclusive.isoformat(), group["product"], group["region"])
    refund = "refund_cents" if cap_cents is None else "MIN(refund_cents, ?)"
    sql = f"""WITH group_orders AS (
    SELECT amount_cents, {refund} AS allowed_refund_cents
    FROM orders
    WHERE ordered_on >= ? AND ordered_on < ? AND product = ? AND region = ?
)
SELECT COUNT(*) AS order_count,
       COALESCE(SUM(amount_cents), 0) AS gross_cents,
       COALESCE(SUM(allowed_refund_cents), 0) AS deducted_cents,
       COALESCE(SUM(amount_cents - allowed_refund_cents), 0) AS net_cents
FROM group_orders"""
    return sql, dates_and_group if cap_cents is None else (cap_cents, *dates_and_group)


def aggregate_group(
    database: Path, group: dict, start: date, end_exclusive: date, cap_cents: int | None, *, timeout: float = 5,
) -> dict:
    """Apply a validated per-order refund cap, then sum orders in the discovered group."""
    sql, parameters = group_statement(group, start, end_exclusive, cap_cents)
    with ReadOnlyDB(database, timeout=timeout) as db:
        db.dry_run(sql, parameters=parameters)
        _, rows = db.query(sql, limit=2, max_columns=4, parameters=parameters)
    order_count, gross, deducted, net = rows[0]
    return {"order_count": order_count, "gross_cents": gross, "deducted_cents": deducted,
            "net_cents": net, "sql": sql, "parameters": list(parameters)}
