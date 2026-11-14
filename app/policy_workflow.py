"""Fixed document → SQL workflow for the versioned synthetic refund policy."""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
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
PolicyMode = Literal["fixed", "hybrid", "react"]
PolicySelector = Callable[[list[dict[str, str]], float], dict]
_TOOL_MESSAGES = {
    "policy_conflict": "这段时间存在生效日期重叠的政策，请先确认适用版本。",
    "policy_gap": "退款政策未覆盖完整查询区间，无法计算该区间的净营收。",
    "source_unavailable": "政策或订单数据暂时不可用，请稍后重试。",
    "timeout": "执行时间已达上限，未生成可核对的答案。",
}


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


class PolicyTools:
    """Keep document and SQL evidence separate from the model's observations."""

    def __init__(self, database: Path, start: date, end: date, region: Region | None):
        self.database = database
        self.start, self.end, self.region = start, end, region
        self.policies: list[Policy] = []
        self.segments: dict[str, tuple[int, Segment]] = {}
        self.queries: dict[str, dict] = {}
        self.error: str | None = None
        self.corpus_loaded = False
        self.search_performed = False
        self.sql_queries = 0

    def load_corpus(self) -> None:
        if self.corpus_loaded:
            return
        self.corpus_loaded = True
        try:
            self.policies = _load_policies()
        except (OSError, ValueError) as exc:
            _logger.warning("Policy corpus could not be loaded (%s)", type(exc).__name__)
            self.error = "source_unavailable"

    def single_policy_scope(self) -> bool:
        self.load_corpus()
        applicable = [
            policy.metadata
            for policy in self.policies
            if policy.metadata.effective_from < self.end and policy.metadata.effective_until > self.start
        ]
        return (
            len(applicable) == 1
            and applicable[0].effective_from <= self.start < self.end <= applicable[0].effective_until
        )

    def search(self) -> dict:
        self.search_performed = True
        self.load_corpus()
        if self.error:
            return {"ok": False, "reason": self.error}
        try:
            segments = _plan_segments(self.start, self.end, self.policies)
        except PolicyConflict:
            self.error = "policy_conflict"
        else:
            if not segments:
                self.error = "policy_gap"
            self.segments = {f"segment:{number}": (number, segment) for number, segment in enumerate(segments, 1)}
        if self.error:
            return {"ok": False, "reason": self.error}
        return {
            "ok": True,
            "segments": [{
                "id": segment_id,
                "start": segment.start.isoformat(),
                "end_exclusive": segment.end_exclusive.isoformat(),
                "policy_id": segment.policy.metadata.id,
                "max_refund_cents_per_order": segment.policy.metadata.max_refund_cents_per_order,
            } for segment_id, (_, segment) in self.segments.items()],
        }

    def invoke(self, tool: str, segment_id: str | None, remaining_seconds: float) -> dict:
        if remaining_seconds <= 0:
            self.error = "timeout"
            return {"ok": False, "reason": self.error}
        if self.error:
            return {"ok": False, "reason": self.error}
        if tool == "search_policies":
            return self.search()
        if tool != "run_segment":
            return {"ok": False, "reason": "unknown_tool"}
        if not self.search_performed:
            return {"ok": False, "reason": "search_required"}
        if segment_id not in self.segments:
            return {"ok": False, "reason": "unknown_segment"}
        if segment_id in self.queries:
            return {"ok": False, "reason": "duplicate_segment"}
        number, segment = self.segments[segment_id]
        self.sql_queries += 1
        try:
            with ReadOnlyDB(self.database, timeout=min(5.0, remaining_seconds)) as db:
                query = _execute_segment(db, segment, self.region, number)
        except (sqlite3.Error, TimeoutError) as exc:
            _logger.warning("Policy query could not run (%s)", type(exc).__name__)
            self.error = "timeout" if isinstance(exc, TimeoutError) else "source_unavailable"
            return {"ok": False, "reason": self.error}
        self.queries[segment_id] = query
        return {"ok": True, "segment_id": segment_id, "sql_evidence_id": query["evidence_id"]}


def run_policy_workflow(
    database: Path, request: PolicyRevenueRequest, *, mode: PolicyMode = "fixed", choose: PolicySelector | None = None,
) -> dict:
    """Run the selected strategy, then finalize only with shared verified evidence."""
    if mode not in ("fixed", "hybrid", "react"):
        raise ValueError("Unknown policy workflow mode")
    started = perf_counter()
    steps, tool_trace = [], []
    strategy = "fixed"
    routing_reason = "explicit_fixed"
    stop_reason = None
    tools = None
    metrics = dict.fromkeys((
        "model_calls", "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "invalid_actions", "duplicate_actions",
    ), 0)
    metrics["usage_complete"] = True

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
            "metrics": {
                **metrics, "sql_queries": tools.sql_queries if tools else 0,
                "latency_ms": round((perf_counter() - started) * 1000, 3),
            },
            "steps": steps,
            "requested_mode": mode,
            "strategy": strategy,
            "routing_reason": routing_reason,
            "stop_reason": stop_reason or reason or "finish",
            "tool_trace": tool_trace,
        }

    def record(stage: str, status: str = "ok") -> None:
        steps.append({"stage": stage, "status": status})

    start, end = request.start, request.end_exclusive
    if start is None or end is None:
        routing_reason = "scope_validation"
        record("resolve_scope", "clarification")
        return finish("clarification", "missing_dates", "请提供起始日期和结束日期。结束日期当天不纳入统计。")
    if start >= end:
        routing_reason = "scope_validation"
        record("resolve_scope", "clarification")
        return finish("clarification", "invalid_period", "结束日期必须晚于起始日期。")
    record("resolve_scope")
    tools = PolicyTools(database, start, end, request.region)
    if mode == "react":
        strategy, routing_reason = "react", "explicit_react"
    elif mode == "hybrid":
        if tools.single_policy_scope():
            routing_reason = "single_policy"
        else:
            strategy, routing_reason = "react", "multi_version_or_uncovered"

    if strategy == "react":
        if choose is None:
            record("react", "abstained")
            return finish("abstained", "model_unconfigured", "请先配置模型，再运行 ReAct。")
        from app.policy_react import run_policy_react

        react = run_policy_react(request.model_dump(mode="json"), tools.invoke, choose)
        tool_trace.extend(react.trace)
        metrics.update({name: getattr(react, name) for name in metrics})
        stop_reason = react.stop_reason
        terminal = tools.error or stop_reason
        record("react", "ok" if terminal == "finish" else "abstained")
        if terminal != "finish":
            message = _TOOL_MESSAGES.get(terminal, {
                "model_error": "模型调用未完成，请稍后重试。",
                "max_steps": "已达到工具决策次数上限，证据尚不完整。",
                "token_budget": "已达到模型用量上限，证据尚不完整。",
                "no_progress": "连续尝试未取得新证据，已停止。",
            }.get(terminal, "未取得足够证据，无法核对答案。"))
            return finish("abstained", terminal, message)
    else:
        def invoke_fixed(tool: str, segment_id: str | None = None) -> dict:
            observation = tools.invoke(tool, segment_id, 5.0)
            tool_trace.append({
                "step": len(tool_trace) + 1, "tool": tool, "segment_id": segment_id,
                "status": "ok" if observation["ok"] else "error", "reason": observation.get("reason"),
            })
            return observation

        decision = invoke_fixed("search_policies")
        record("load_policies", "ok" if tools.policies or tools.error != "source_unavailable" else "error")
        record("plan_segments", "ok" if decision["ok"] else "abstained")
        if not decision["ok"]:
            return finish("abstained", tools.error, _TOOL_MESSAGES[tools.error])
        for segment_id in tools.segments:
            if not invoke_fixed("run_segment", segment_id)["ok"]:
                record("execute_sql", "error")
                stop_reason = tools.error
                return finish("abstained", "source_unavailable", _TOOL_MESSAGES[tools.error])
        record("execute_sql")

    segments = [segment for _, segment in tools.segments.values()]
    queries = [tools.queries[key] for key in tools.segments if key in tools.queries]
    try:
        verified = _verify(start, end, request.region, segments, queries)
    except (KeyError, TypeError, ValueError) as exc:
        _logger.warning("Policy evidence could not be verified (%s)", type(exc).__name__)
        record("verify", "abstained")
        stop_reason = "verification_failed"
        return finish("abstained", "verification_failed", "查询结果未通过政策、范围和金额核对。")
    record("verify")
    return finish(
        "answer", None, "已按下单日期匹配退款政策并汇总净营收。",
        segments=verified, policies=[segment.policy.evidence() for segment in segments], queries=queries,
    )
