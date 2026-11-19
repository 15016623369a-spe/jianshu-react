"""Bounded policy retrieval, verified plan caching and evidence-driven SQL."""
from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from time import monotonic, perf_counter
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.hard_policy_data import (
    AmbiguousDocument, aggregate_hard_group, aggregate_statement, inspect_ledger, load_document_snapshot, product_category,
    search_bulletin, search_policy,
)
from app.llm import completion_text
from app.policy_workflow import PolicyRevenueRequest

HardMode = Literal["auto", "fixed", "uniform", "plan", "uncached"]
Stage = Literal["find_latest_bulletin", "find_titled_bulletin", "follow_references", "inspect_ledger", "query"]
Selector = Callable[[list[dict[str, str]], float], dict]
_VERSION = "policy-exploration-plan-2"
_MAX_REFERENCES = 4
_LEDGERS = {"orders", "refunds", "chargebacks"}
_ORDER_FIELDS = frozenset({"id", "ordered_on", "product", "region", "amount_cents", "refund_cents"})
_EVENT_FIELDS = frozenset({"order_id", "amount_cents"})
_REF_STAGES = {"latest": "find_latest_bulletin", "title": "find_titled_bulletin"}
_MAX_SECONDS = 30.0
_MAX_PROMPT_BYTES = 12_000
_CACHE_LIMIT = 128
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_FIELDS = ("category", "policy", "bulletin", "bulletins", "schema", "query", "document_version")
_MESSAGES = {
    "missing_scope": "请填写产品、地区、起始日期和截止日期。",
    "invalid_period": "截止日期必须晚于起始日期。",
    "product_unknown": "产品目录中没有找到该产品，无法确认其政策类目。",
    "policy_missing": "缺少适用该产品类目、地区与完整日期的正式政策。",
    "policy_conflict": "存在同等优先级的正式政策，无法确认口径。",
    "bulletin_missing": "没有找到覆盖完整查询区间的适用公告。",
    "bulletin_conflict": "多份公告同等适用，无法确认口径。",
    "referral_cycle": "公告引用形成循环，无法确认最终口径。",
    "referral_limit": "公告引用超过可核验层数。",
    "workflow_unsupported": "单层固定流程无法处理继续转引的公告。可切换自动模式。",
    "source_unavailable": "政策文档或订单数据暂时不可用。",
    "model_unconfigured": "当前没有匹配的计划缓存。请配置模型或使用固定流程。",
    "model_error": "规划模型暂时不可用。",
    "plan_incomplete": "计划和单次重规划均未补齐所需证据。",
    "timeout": "已达到执行时间预算。",
    "token_budget": "已达到模型用量预算。",
    "verification_failed": "查询金额或引用未通过核验。",
}
_SYSTEM = """你为当前证据计划剩余的只读步骤。仅输出 JSON 对象，字段 steps 是工具名数组。
剩余步骤必须从 state.next_required 开始。follow_references 会依次核查全部剩余引用，直至正式结算条款，引用超过上限或循环会停止。
- next_required 是 follow_references：输出 {"steps":["follow_references","inspect_ledger","query"]}。此工具同时适用于按标题和按生效日期找最新公告的引用。无需逐个写出后续公告工具。
- next_required 是 inspect_ledger：输出 {"steps":["inspect_ledger","query"]}。
- next_required 是 query：输出 {"steps":["query"]}。
如果证据已经经过首个引用，切勿重复已完成步骤。禁止指定文档 ID、SQL 或金额。"""


class HardPolicyRequest(PolicyRevenueRequest):
    product: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)] | None = None


class HardPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    steps: Annotated[list[Stage], Field(min_length=1, max_length=_MAX_REFERENCES + 2)]


class HardPlanCache:
    """In-process cache of verified stage sequences, with no saved query results."""

    def __init__(self) -> None:
        self._plans: OrderedDict[tuple, tuple[Stage, ...]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: tuple) -> tuple[Stage, ...] | None:
        with self._lock:
            plan = self._plans.get(key)
            if plan is not None:
                self._plans.move_to_end(key)
            return plan

    def has_other_version(self, key: tuple) -> bool:
        with self._lock:
            return any(entry[:-1] == key[:-1] and entry[-1] != key[-1] for entry in self._plans)

    def put(self, key: tuple, plan: tuple[Stage, ...]) -> None:
        with self._lock:
            self._plans[key] = plan
            self._plans.move_to_end(key)
            if len(self._plans) > _CACHE_LIMIT:
                self._plans.popitem(last=False)

    def discard(self, key: tuple, expected: tuple[Stage, ...]) -> None:
        with self._lock:
            if self._plans.get(key) == expected:
                self._plans.pop(key, None)


_DEFAULT_CACHE = HardPlanCache()


class StopPolicy(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _same_money(query: dict, cap: int | None) -> bool:
    count, gross, deducted, net = amounts = [
        query[k] for k in ("order_count", "gross_cents", "deducted_cents", "net_cents")
    ]
    return (
        all(type(value) is int for value in amounts)
        and count >= 0 and 0 <= deducted <= gross and net == gross - deducted
        and (count != 0 or gross == 0)
        and (cap is None or deducted <= cap * count)
    )


def _supports_query(schema: dict, ledger: str) -> bool:
    if schema["ledger"] != ledger:
        return False
    tables = {table["name"]: set(table["columns"]) for table in schema["tables"]}
    return tables.keys() == {"orders", ledger} and _ORDER_FIELDS <= tables["orders"] and _EVENT_FIELDS <= tables[ledger]


def run_hard_policy(
    database: Path, request: HardPolicyRequest, *, mode: HardMode = "auto", choose: Selector | None = None,
    cache: HardPlanCache | None = None,
) -> dict:
    """A deterministic route and two planner treatments share the same tool gate."""
    if mode not in ("auto", "fixed", "uniform", "plan", "uncached"):
        raise ValueError("Unknown policy exploration mode")
    cache = _DEFAULT_CACHE if cache is None else cache
    began, deadline = perf_counter(), monotonic() + _MAX_SECONDS
    evidence = dict.fromkeys(_FIELDS)
    evidence["bulletins"] = []
    version = None
    route = mode
    metrics = dict.fromkeys((*_TOKEN_FIELDS, "model_calls", "sql_queries", "replans", "cache_hits", "cache_invalidated", "invalid_plans"), 0)
    metrics["usage_complete"] = True
    trace = []
    scope = request.model_dump(mode="json")
    start, end, product, region = request.start, request.end_exclusive, request.product, request.region

    def record(tool: str, status: str = "ok", ids=(), reason: str | None = None) -> None:
        trace.append({"step": len(trace) + 1, "tool": tool, "status": status, "ids": list(ids), "reason": reason})

    def result(kind: str, reason: str | None = None) -> dict:
        return {
            "kind": kind, "reason": reason,
            "message": _MESSAGES.get(reason, "已按适用政策与真实账本核对净营收。"),
            "mode": mode, "route": route, "scope": scope, "currency": "CNY",
            "document_version": version,
            "total_cents": evidence["query"]["net_cents"] if kind == "answer" else None,
            "evidence": evidence if kind == "answer" else dict.fromkeys(_FIELDS),
            "tool_trace": trace,
            "metrics": {**metrics, "latency_ms": round((perf_counter() - began) * 1000, 3)},
        }

    def remaining() -> float:
        budget = deadline - monotonic()
        if budget <= 0:
            raise StopPolicy("timeout")
        return budget

    if None in (start, end, product, region):
        return result("clarification", "missing_scope")
    if start >= end:
        return result("clarification", "invalid_period")

    stage = "identify_product"
    try:
        snapshot = load_document_snapshot()
        version = snapshot.version
        evidence["document_version"] = version
        category = product_category(product, snapshot=snapshot)
        if category is None:
            raise StopPolicy("product_unknown")
        evidence["category"] = category
        record(stage, ids=(product, category))
        stage = "find_policy"
        policy = search_policy(product, category, region, start, end, snapshot=snapshot)
        if policy is None:
            raise StopPolicy("policy_missing")
        evidence["policy"] = policy
        record(stage, ids=(policy["id"],))
    except AmbiguousDocument:
        record(stage, "error", reason="policy_conflict")
        return result("abstained", "policy_conflict")
    except StopPolicy as exc:
        record(stage, "error", reason=exc.reason)
        return result("abstained", exc.reason)
    except (OSError, ValueError):
        record(stage, "error", reason="source_unavailable")
        return result("abstained", "source_unavailable")

    completed: list[Stage] = []
    key = (_VERSION, database.resolve(), product, region, policy["id"], version)
    prompt_bytes = 0

    def authority() -> dict:
        return evidence["bulletin"] or policy

    def next_step() -> Stage | None:
        if evidence["query"] is not None:
            return None
        current = authority()
        if ref := current["ref_kind"]:
            return _REF_STAGES[ref]
        if "inspect_ledger" not in completed and (current["ledger"] != "orders" or evidence["bulletins"]):
            return "inspect_ledger"
        return "query"

    def advance(stage: Stage) -> None:
        try:
            timeout = min(5.0, remaining())
            current = authority()
            if stage in _REF_STAGES.values():
                path = evidence["bulletins"]
                if len(path) >= _MAX_REFERENCES:
                    raise StopPolicy("referral_limit")
                item = search_bulletin(current, category, region, start, end, product=product, snapshot=snapshot)
                if item is None:
                    raise StopPolicy("bulletin_missing")
                if item["id"] in {policy["id"], *(doc["id"] for doc in path)}:
                    raise StopPolicy("referral_cycle")
                path.append(item)
                evidence["bulletin"] = item
                record(stage, ids=(item["id"],))
            elif current["ledger"] not in _LEDGERS:
                raise StopPolicy("verification_failed")
            elif stage == "inspect_ledger":
                ledger = current["ledger"]
                if ledger != "orders":
                    schema = inspect_ledger(database, ledger, timeout=timeout)
                    if not _supports_query(schema, ledger):
                        raise StopPolicy("verification_failed")
                    evidence["schema"] = schema
                    record(stage, ids=(ledger,))
                else:
                    record(stage, "skipped", (ledger,))
            else:
                metrics["sql_queries"] += 1
                evidence["query"] = aggregate_hard_group(
                    database, product, region, start, end, current["ledger"], current["cap_cents"], timeout=timeout,
                )
                record(stage, ids=(current["ledger"],))
            completed.append(stage)
        except AmbiguousDocument as exc:
            record(stage, "error", reason="bulletin_conflict")
            raise StopPolicy("bulletin_conflict") from exc
        except StopPolicy as exc:
            record(stage, "error", reason=exc.reason)
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            reason = "timeout" if isinstance(exc, TimeoutError) else "source_unavailable"
            record(stage, "error", reason=reason)
            raise StopPolicy(reason) from exc

    def execute(stages: tuple[Stage, ...]) -> bool:
        for stage in stages:
            expected = next_step()
            if stage == "follow_references":
                if expected in _REF_STAGES.values():
                    while (expected := next_step()) in _REF_STAGES.values():
                        advance(expected)
                elif not evidence["bulletins"] or expected not in ("inspect_ledger", "query"):
                    return False
                # The preceding referral may already have resolved the terminal rule.
            elif stage == expected:
                advance(stage)
            else:
                return False
        return evidence["query"] is not None

    def walk() -> None:
        while (stage := next_step()) is not None:
            advance(stage)

    def load_plan(event: str, previous_failure: str | None) -> tuple[Stage, ...]:
        nonlocal prompt_bytes
        if choose is None:
            raise StopPolicy("model_unconfigured")
        required = next_step()
        context = {
            "scope": scope,
            "policy": {field: policy[field] for field in ("ref_kind", "ledger")},
            "state": {
                "completed": completed,
                "reference_depth": len(evidence["bulletins"]),
                "next_required": "follow_references" if required in _REF_STAGES.values() else required,
                "ref_kind": authority()["ref_kind"],
                "resolved_ledger": authority()["ledger"],
            },
            "previous_failure": previous_failure,
        }
        messages = [{"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]
        prompt_bytes += sum(len(message["content"].encode("utf-8")) for message in messages)
        if prompt_bytes > _MAX_PROMPT_BYTES:
            raise StopPolicy("token_budget")
        budget = remaining()
        metrics["model_calls"] += 1
        try:
            response = choose(messages, budget)
        except (OSError, ValueError, RuntimeError) as exc:
            metrics["usage_complete"] = False
            reason = "timeout" if isinstance(exc, TimeoutError) or (
                isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)
            ) else "model_error"
            record(event, "error", reason=f"http_{exc.code}" if isinstance(exc, HTTPError) else reason)
            raise StopPolicy(reason) from exc
        remaining()
        usage = response.get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        counts = {k: usage[k] for k in _TOKEN_FIELDS if type(usage.get(k)) is int and usage[k] >= 0}
        if not all(k in counts for k in _TOKEN_FIELDS[:2]):
            metrics["usage_complete"] = False
        else:
            counts.setdefault("total_tokens", counts["prompt_tokens"] + counts["completion_tokens"])
        for name, value in counts.items():
            metrics[name] += value
        if metrics["prompt_tokens"] > _MAX_PROMPT_BYTES:
            raise StopPolicy("token_budget")
        try:
            plan = tuple(HardPlan.model_validate_json(completion_text(response)).steps)
        except (KeyError, TypeError, ValueError, IndexError):
            metrics["invalid_plans"] += 1
            record(event, "invalid", reason="invalid_plan")
            return ()
        if plan[0] != required and not (plan[0] == "follow_references" and required in _REF_STAGES.values()):
            metrics["invalid_plans"] += 1
            record(event, "invalid", plan, "invalid_dependency")
            return ()
        record(event, ids=plan)
        return plan

    try:
        cached = cache.get(key) if mode in ("auto", "plan") else None
        if cached is None and mode in ("auto", "plan") and cache.has_other_version(key):
            metrics["cache_invalidated"] += 1
            record("cache_invalidated", "skipped", reason="document_version_changed")
        if mode == "auto":
            # One reference is the deterministic probe. Consult plans only for a deeper chain.
            if next_step() in _REF_STAGES.values():
                advance(next_step())
            if next_step() not in _REF_STAGES.values():
                route = "fixed_fast"
                record("fixed_route")
                walk()
                cached = None

        if cached is not None:
            route = "cache"
            metrics["cache_hits"] += 1
            record("cache_hit", ids=cached)
            prefix = tuple(completed)
            if cached[:len(prefix)] != prefix or not execute(cached[len(prefix):]):
                cache.discard(key, cached)
                metrics["cache_invalidated"] += 1
                record("cache_invalidated", "skipped", reason="evidence_path_changed")

        if next_step() is not None and mode in ("fixed", "uniform"):
            if mode == "fixed" and next_step() in _REF_STAGES.values():
                advance(next_step())
                if next_step() in _REF_STAGES.values():
                    raise StopPolicy("workflow_unsupported")
            walk()

        if next_step() is not None:
            for attempt in range(1 if cached is not None else 2):
                event = "replan" if attempt or cached is not None else "plan"
                route = "replan" if event == "replan" else "planned"
                if event == "replan":
                    metrics["replans"] += 1
                plan = load_plan(event, "evidence_path_changed" if event == "replan" else None)
                if execute(plan):
                    break
        if next_step() is not None:
            raise StopPolicy("plan_incomplete")
        remaining()
        applied = authority()
        query, schema = evidence["query"], evidence["schema"]
        cap, ledger = applied["cap_cents"], applied["ledger"]
        expected_sql, params = aggregate_statement(product, region, start, end, ledger, cap)
        if (query["ledger"] != ledger or query["sql"] != expected_sql or query["parameters"] != list(params)
                or not _same_money(query, cap)
                or (ledger != "orders" and (not schema or not _supports_query(schema, ledger)))
                or (policy["ref_kind"] == "title" and applied.get("title") is None)):
            raise StopPolicy("verification_failed")
        record("verify", ids=(applied["id"],))
        if mode == "plan" or (mode == "auto" and route != "fixed_fast"):
            cache.put(key, tuple(completed))
        return result("answer")
    except StopPolicy as exc:
        if not trace or trace[-1]["status"] != "error":
            record("verify", "error", reason=exc.reason)
        return result("abstained", exc.reason)
    except (TypeError, ValueError, KeyError):
        record("verify", "error", reason="verification_failed")
        return result("abstained", "verification_failed")
