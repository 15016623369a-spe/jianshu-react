"""Fixed document → SQL workflow for the versioned synthetic refund policy."""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.commerce_demo import Region
from app.tools.db import ReadOnlyDB

_POLICY_DIR = Path(__file__).with_name("policy_corpus")
_SECTION = "净营收口径"
_COLUMNS = ["order_count", "gross_cents", "deducted_cents", "revenue_cents"]
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_logger = logging.getLogger(__name__)


class PolicyRevenueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date | None = None
    end_exclusive: date | None = None
    region: Region | None = None

    @field_validator("start", "end_exclusive", mode="before")
    @classmethod
    def calendar_dates(cls, value):
        if value is None or type(value) is date or (isinstance(value, str) and _ISO_DATE.fullmatch(value)):
            return value
        raise ValueError("日期必须使用 YYYY-MM-DD 格式")


class PolicyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^refund-[0-9]{4}-[0-9]{2}-[a-z]+$")]
    effective_from: date
    effective_until: date
    max_refund_cents_per_order: Annotated[int, Field(ge=0, le=100_000_000)] | None


@dataclass(frozen=True)
class Policy:
    metadata: PolicyMetadata
    title: str
    quote: str

    @property
    def evidence_id(self) -> str:
        return f"doc:{self.metadata.id}#net-revenue"

    def evidence(self) -> dict:
        return {
            "id": self.metadata.id,
            "evidence_id": self.evidence_id,
            "title": self.title,
            "section": _SECTION,
            "quote": self.quote,
            "start": self.metadata.effective_from.isoformat(),
            "end_exclusive": self.metadata.effective_until.isoformat(),
            "max_refund_cents_per_order": self.metadata.max_refund_cents_per_order,
            "source": f"app/policy_corpus/{self.metadata.id}.md",
        }


@dataclass(frozen=True)
class Segment:
    start: date
    end_exclusive: date
    policy: Policy


class PolicyConflict(ValueError):
    """More than one policy applies on the same order date."""


def _load_policies() -> list[Policy]:
    policies = []
    for file in sorted(_POLICY_DIR.glob("*.md")):
        opener, frontmatter, body = file.read_text(encoding="utf-8").split("---\n", 2)
        if opener:
            raise ValueError("Expected a policy metadata header")
        metadata = PolicyMetadata.model_validate_json(frontmatter)
        heading, quote = body.strip().split(f"\n\n## {_SECTION}\n\n", 1)
        if (file.stem != metadata.id or metadata.effective_from >= metadata.effective_until
                or not heading.startswith("# ") or not quote.strip()):
            raise ValueError("Invalid policy document")
        policies.append(Policy(metadata, heading[2:].strip(), quote.strip()))
    return sorted(policies, key=lambda policy: policy.metadata.effective_from)


def _plan_segments(start: date, end: date, policies: list[Policy]) -> list[Segment]:
    segments = [
        Segment(max(start, metadata.effective_from), min(end, metadata.effective_until), policy)
        for policy in policies
        if (metadata := policy.metadata).effective_from < end and metadata.effective_until > start
    ]
    if any(left.end_exclusive > right.start for left, right in zip(segments, segments[1:])):
        raise PolicyConflict
    return segments if _covers(start, end, segments) else []


def _covers(start: date, end: date, segments: list[Segment]) -> bool:
    cursor = start
    for segment in segments:
        if segment.start != cursor or not segment.start < segment.end_exclusive <= end:
            return False
        cursor = segment.end_exclusive
    return bool(segments) and cursor == end


def _statement(segment: Segment, region: Region | None) -> tuple[str, tuple]:
    cap = segment.policy.metadata.max_refund_cents_per_order
    refund = "refund_cents" if cap is None else "MIN(refund_cents, ?)"
    region_filter = " AND region = ?" if region is not None else ""
    sql = f"""WITH matched_orders AS (
    SELECT amount_cents, {refund} AS applicable_refund_cents
    FROM orders
    WHERE ordered_on >= ? AND ordered_on < ?{region_filter}
)
SELECT COUNT(*) AS order_count,
       COALESCE(SUM(amount_cents), 0) AS gross_cents,
       COALESCE(SUM(applicable_refund_cents), 0) AS deducted_cents,
       COALESCE(SUM(amount_cents - applicable_refund_cents), 0) AS revenue_cents
FROM matched_orders"""
    dates = (segment.start.isoformat(), segment.end_exclusive.isoformat())
    parameters = (() if cap is None else (cap,)) + dates
    return sql, parameters if region is None else (*parameters, region)


def _execute_segment(db: ReadOnlyDB, segment: Segment, region: Region | None, number: int) -> dict:
    sql, parameters = _statement(segment, region)
    db.dry_run(sql, parameters=parameters)
    columns, rows = db.query(sql, limit=2, parameters=parameters)
    return {
        "evidence_id": f"sql:{number}",
        "sql": sql,
        "parameters": list(parameters),
        "columns": columns,
        "rows": [list(row) for row in rows],
        "policy_evidence_id": segment.policy.evidence_id,
    }


def _verify(start: date, end: date, region: Region | None, segments: list[Segment], queries: list[dict]) -> list[dict]:
    if not _covers(start, end, segments) or len(segments) != len(queries):
        raise ValueError("Coverage and SQL evidence counts must match")
    verified = []
    for number, (segment, query) in enumerate(zip(segments, queries, strict=True), 1):
        metadata = segment.policy.metadata
        sql, params = _statement(segment, region)
        if (
            not metadata.effective_from <= segment.start < segment.end_exclusive <= metadata.effective_until
            or query["evidence_id"] != f"sql:{number}"
            or query["policy_evidence_id"] != segment.policy.evidence_id
            or query["sql"] != sql
            or query["parameters"] != list(params)
            or query["columns"] != _COLUMNS
            or len(query["rows"]) != 1
        ):
            raise ValueError("SQL or policy evidence does not match the planned segment")
        count, gross, deducted, revenue = row = query["rows"][0]
        if any(type(value) is not int for value in row) or count < 0 or gross < 0 or deducted < 0:
            raise ValueError("Expected integer amounts in cents and a nonnegative order count")
        cap = metadata.max_refund_cents_per_order
        if (revenue != gross - deducted or (count == 0 and any((gross, deducted, revenue)))
                or (cap is not None and deducted > count * cap)):
            raise ValueError("Invalid revenue arithmetic")
        verified.append({
            "start": segment.start.isoformat(),
            "end_exclusive": segment.end_exclusive.isoformat(),
            "policy_id": metadata.id,
            "policy_evidence_id": segment.policy.evidence_id,
            "sql_evidence_id": query["evidence_id"],
            "order_count": count,
            "gross_cents": gross,
            "deducted_cents": deducted,
            "revenue_cents": revenue,
        })
    return verified


def run_policy_workflow(database: Path, request: PolicyRevenueRequest) -> dict:
    """Resolve scope, load policy versions, plan, execute, verify and present."""
    started = perf_counter()
    steps = []
    sql_queries = 0

    def finish(
        kind: Literal["answer", "clarification", "abstained"], reason: str | None, message: str,
        *, segments: list[dict] | None = None, policies: list[dict] | None = None, queries: list[dict] | None = None,
    ) -> dict:
        return {
            "kind": kind,
            "reason": reason,
            "message": message,
            "scope": request.model_dump(mode="json"),
            "currency": "CNY",
            "total_cents": sum(segment["revenue_cents"] for segment in segments) if segments is not None else None,
            "segments": segments or [],
            "evidence": {"policies": policies or [], "queries": queries or []},
            "metrics": {"model_calls": 0, "sql_queries": sql_queries, "latency_ms": round((perf_counter() - started) * 1000, 3)},
            "steps": steps,
        }

    def record(stage: str, status: str = "ok") -> None:
        steps.append({"stage": stage, "status": status})

    start, end = request.start, request.end_exclusive
    if start is None or end is None:
        record("resolve_scope", "clarification")
        return finish("clarification", "missing_dates", "请提供起始日期和结束日期。结束日期当天不纳入统计。")
    if start >= end:
        record("resolve_scope", "clarification")
        return finish("clarification", "invalid_period", "结束日期必须晚于起始日期。")
    record("resolve_scope")
    try:
        policies = _load_policies()
    except (OSError, ValueError) as exc:
        _logger.warning("Policy corpus could not be loaded (%s)", type(exc).__name__)
        record("load_policies", "error")
        return finish("abstained", "source_unavailable", "退款政策暂时不可用，无法核对营收口径。")
    record("load_policies")
    try:
        segments = _plan_segments(start, end, policies)
    except PolicyConflict:
        record("plan_segments", "abstained")
        return finish("abstained", "policy_conflict", "这段时间存在生效日期重叠的政策，请先确认适用版本。")
    if not segments:
        record("plan_segments", "abstained")
        return finish("abstained", "policy_gap", "退款政策未覆盖完整查询区间，无法计算该区间的净营收。")
    record("plan_segments")
    queries = []
    try:
        with ReadOnlyDB(database) as db:
            for number, segment in enumerate(segments, 1):
                sql_queries += 1
                queries.append(_execute_segment(db, segment, request.region, number))
    except (sqlite3.Error, TimeoutError) as exc:
        _logger.warning("Policy query could not run (%s)", type(exc).__name__)
        record("execute_sql", "error")
        return finish("abstained", "source_unavailable", "订单库暂时无法完成查询，请稍后重试。")
    record("execute_sql")
    try:
        verified = _verify(start, end, request.region, segments, queries)
    except (KeyError, TypeError, ValueError) as exc:
        _logger.warning("Policy evidence could not be verified (%s)", type(exc).__name__)
        record("verify", "abstained")
        return finish("abstained", "verification_failed", "查询结果未通过政策、范围和金额核对。")
    record("verify")
    return finish(
        "answer", None, "已按下单日期匹配退款政策并汇总净营收。",
        segments=verified, policies=[segment.policy.evidence() for segment in segments], queries=queries,
    )
