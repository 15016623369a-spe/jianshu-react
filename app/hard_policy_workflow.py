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
    AmbiguousDocument, aggregate_hard_group, aggregate_statement, inspect_ledger, product_category,
    search_bulletin, search_policy,
)
from app.llm import completion_text
from app.policy_workflow import PolicyRevenueRequest

HardMode = Literal["auto", "fixed", "plan", "uncached"]
Stage = Literal["find_latest_bulletin", "find_titled_bulletin", "inspect_ledger", "query"]
Selector = Callable[[list[dict[str, str]], float], dict]
_VERSION = "policy-exploration-plan-1"
_LEDGERS = {"orders", "refunds", "chargebacks"}
_ORDER_FIELDS = frozenset({"id", "ordered_on", "product", "region", "amount_cents", "refund_cents"})
_EVENT_FIELDS = frozenset({"order_id", "amount_cents"})
_REF_STAGES = {"latest": "find_latest_bulletin", "title": "find_titled_bulletin"}
_MAX_SECONDS = 30.0
_MAX_PROMPT_BYTES = 12_000
_CACHE_LIMIT = 128
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_FIELDS = ("category", "policy", "bulletin", "schema", "query")
_MESSAGES = {
    "missing_scope": "请填写产品、地区、起始日期和截止日期。",
    "invalid_period": "截止日期必须晚于起始日期。",
    "product_unknown": "产品目录中没有找到该产品，无法确认其政策类目。",
    "policy_missing": "缺少适用该产品类目、地区与完整日期的正式政策。",
    "policy_conflict": "存在同等优先级的正式政策，无法确认口径。",
    "bulletin_missing": "没有找到覆盖完整查询区间的适用公告。",
    "bulletin_conflict": "多份公告同等适用，无法确认口径。",
    "source_unavailable": "政策文档或订单数据暂时不可用。",
    "model_unconfigured": "当前没有匹配的计划缓存。请配置模型或使用固定流程。",
    "model_error": "规划模型暂时不可用。",
    "plan_incomplete": "计划和单次重规划均未补齐所需证据。",
    "timeout": "已达到执行时间预算。",
    "token_budget": "已达到模型用量预算。",
    "verification_failed": "查询金额或引用未通过核验。",
}
_SYSTEM = """从授权的只读工具生成执行计划。只输出 JSON，例如 {"steps":["find_latest_bulletin","inspect_ledger","query"]}。
根据 policy.ref_kind 选择公告步骤：latest 用 find_latest_bulletin，title 用 find_titled_bulletin，无引用时不查公告。
公告会给出实际账本，计划必须预留 inspect_ledger，再 query。执行器对主订单账本跳过 schema 探查；外部退款/冲正账本必须先检查再查。
无公告引用且 policy.ledger 为 orders 时，只需 query；无引用且已知外部账本时，先 inspect_ledger 后 query。
若 previous_failure 非空，参考 state 中已取得的证据，只补剩余步骤。不要输入文档 ID、SQL、任意工具或金额。"""


class HardPolicyRequest(PolicyRevenueRequest):
    product: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)] | None = None


class HardPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    steps: Annotated[list[Stage], Field(min_length=1, max_length=3)]


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


def _required_stages(policy: dict) -> tuple[Stage, ...]:
    if ref_kind := policy["ref_kind"]:
        return _REF_STAGES[ref_kind], "inspect_ledger", "query"
    return ("query",) if policy["ledger"] == "orders" else ("inspect_ledger", "query")


def _cache_key(policy: dict) -> tuple:
    return (
        _VERSION, policy["id"], policy["region"], policy["start"], policy["end_exclusive"],
        policy["ref_kind"], policy.get("ref_title"), policy["ledger"], policy["cap_cents"],
    )


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
    if mode not in ("auto", "fixed", "plan", "uncached"):
        raise ValueError("Unknown policy exploration mode")
    cache = _DEFAULT_CACHE if cache is None else cache
    began, deadline = perf_counter(), monotonic() + _MAX_SECONDS
    evidence = dict.fromkeys(_FIELDS)
    metrics = dict.fromkeys((*_TOKEN_FIELDS, "model_calls", "sql_queries", "replans", "cache_hits", "invalid_plans"), 0)
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
            "mode": mode, "scope": scope, "currency": "CNY",
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
        category = product_category(product)
        if category is None:
            raise StopPolicy("product_unknown")
        evidence["category"] = category
        record(stage, ids=(product, category))
        stage = "find_policy"
        policy = search_policy(product, category, region, start, end)
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

    required = _required_stages(policy)
    completed = set()
    key = _cache_key(policy)
    prompt_bytes = 0

    def authority() -> dict | None:
        return evidence["bulletin"] if policy["ref_kind"] else policy

    def execute(stages: tuple[Stage, ...]) -> bool:
        future = tuple(step for step in required if step not in completed)
        if not stages or len(set(stages)) != len(stages) or any(step not in future for step in stages):
            return False
        for stage in stages:
            if stage != next((step for step in required if step not in completed), None):
                return False
            try:
                timeout = min(5.0, remaining())
                current = authority()
                if stage in _REF_STAGES.values():
                    item = search_bulletin(policy, category, region, start, end)
                    if item is None:
                        raise StopPolicy("bulletin_missing")
                    evidence["bulletin"] = item
                    record(stage, ids=(item["id"],))
                elif current is None or current["ledger"] not in _LEDGERS:
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
                completed.add(stage)
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
        return evidence["query"] is not None

    def load_plan(event: str, previous_failure: str | None) -> tuple[Stage, ...]:
        nonlocal prompt_bytes
        if choose is None:
            raise StopPolicy("model_unconfigured")
        context = {
            "scope": scope,
            "policy": {field: policy[field] for field in ("ref_kind", "ledger")},
            "state": {
                "completed": [step for step in required if step in completed],
                "resolved_ledger": authority()["ledger"] if authority() else None,
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
        future = tuple(step for step in required if step not in completed)
        if len(set(plan)) != len(plan) or plan != future[:len(plan)]:
            metrics["invalid_plans"] += 1
            record(event, "invalid", plan, "invalid_dependency")
            return ()
        record(event, ids=plan)
        return plan

    try:
        cached = cache.get(key) if mode in ("auto", "plan") else None
        done = False
        if cached is not None:
            metrics["cache_hits"] = 1
            record("cache_hit", ids=cached)
            done = execute(cached)
            if not done:
                cache.discard(key, cached)
        if not done and (mode == "fixed" or (mode == "auto" and (choose is None or not policy["ref_kind"]))):
            done = execute(tuple(step for step in required if step not in completed))
            if done and mode == "auto":
                record("fixed_route", ids=required)
        elif not done:
            for attempt in range(2 if cached is None else 1):
                event = "plan" if attempt == 0 and cached is None else "replan"
                if event == "replan":
                    metrics["replans"] += 1
                plan = load_plan(event, "incomplete_or_invalid_plan" if event == "replan" else None)
                if execute(plan):
                    done = True
                    break
        if not done:
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
        if mode in ("auto", "plan"):
            cache.put(key, required)
        return result("answer")
    except StopPolicy as exc:
        if not trace or trace[-1]["status"] != "error":
            record("verify", "error", reason=exc.reason)
        return result("abstained", exc.reason)
    except (TypeError, ValueError, KeyError):
        record("verify", "error", reason="verification_failed")
        return result("abstained", "verification_failed")
