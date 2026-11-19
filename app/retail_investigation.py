"""Deterministic retail metrics over the demo order ledger."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date
from time import perf_counter
from pathlib import Path
from typing import Literal

from app.policy_investigation_retrieval import load_investigation_snapshot
from app.investigation_v3 import InvestigationEvidence, RetailInvestigationV3, investigation_result

_REGIONS = ("华南", "华东", "华北")
_MONTH = re.compile(r"(?:(20\d{2})\s*年\s*)?(1[0-2]|0?[1-9]|[一二三四五六七八九十]{1,3})\s*月")
_CN = {name: index for index, name in enumerate("一二三四五六七八九十", 1)}
Metric = Literal["orders", "gross", "refunds", "net", "aov"]


class RetailInput(ValueError):
    def __init__(self, reason: str, message: str):
        self.reason, self.message = reason, message


def _month_number(value: str) -> int:
    return int(value) if value.isdigit() else _CN[value]


def _parse(question: str, catalog: tuple[tuple[str, str], ...]) -> tuple[str, str, date, date, Metric, int | None]:
    products = sorted({product for product, _ in catalog if product in question}, key=len, reverse=True)
    if len(products) != 1:
        raise RetailInput("ambiguous_product" if products else "missing_product", "请明确一个商品。")
    regions = [region for region in _REGIONS if region in question]
    if len(regions) != 1:
        raise RetailInput("ambiguous_region" if regions else "missing_region", "请明确一个地区。")
    months = list(_MONTH.finditer(question))
    if len(months) != 1:
        raise RetailInput("ambiguous_period" if months else "missing_period", "零售 v1 一次查询一个月份。")
    month = months[0]
    year = int(month.group(1)) if month.group(1) else 2026
    month_number = _month_number(month.group(2))
    start = date(year, month_number, 1)
    end = date(year + (month_number == 12), month_number % 12 + 1, 1)
    if re.search(r"订单数|订单量", question):
        metric: Metric = "orders"
    elif re.search(r"退款额|退款金额|退款", question):
        metric = "refunds"
    elif re.search(r"客单价|平均订单", question):
        metric = "aov"
    elif re.search(r"毛营收|销售额|订单金额", question):
        metric = "gross"
    elif re.search(r"净营收|营收|收入", question):
        metric = "net"
    else:
        raise RetailInput("missing_metric", "请说明要查询净营收、订单数、退款额还是客单价。")
    return products[0], regions[0], start, end, metric, None if month.group(1) else year


def run_retail_investigation(database: Path, request: RetailInvestigationV3, *, mode: str = "auto") -> dict:
    began = perf_counter()
    if mode == "model":
        raise ValueError("零售指标使用确定性 SQL，不支持模型模式。")
    snapshot = load_investigation_snapshot()
    try:
        product, region, start, end, metric, assumed_year = _parse(request.question, snapshot.catalog)
    except RetailInput as exc:
        return investigation_result("retail", {
            "kind": "clarification", "reason": exc.reason, "message": exc.message,
            "scope": None, "actual": [], "counterfactual": [],
            "actual_delta_cents": None, "counterfactual_delta_cents": None,
            "single_period_delta_cents": None, "policy_effect_cents": None,
            "document_version": None,
            "metrics": {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0, "sql_queries": 0, "retrievals": 0, "rewrites": 0,
                        "lexical_hits": 0, "model_fallbacks": 0, "usage_complete": True,
                        "latency_ms": 0},
            "tool_trace": [{"stage": "scope", "status": "error", "detail": exc.reason}],
            "retrieval_trace": [],
        })
    sql = (
        "SELECT COUNT(*) AS order_count, COALESCE(SUM(amount_cents), 0) AS gross_cents, "
        "COALESCE(SUM(refund_cents), 0) AS refund_cents FROM orders "
        "WHERE product = ? AND region = ? AND ordered_on >= ? AND ordered_on < ?"
    )
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        count, gross, refund = connection.execute(sql, (product, region, start.isoformat(), end.isoformat())).fetchone()
    net = gross - refund
    aov = round(gross / count) if count else 0
    values = {"orders": count, "gross": gross, "refunds": refund, "net": net, "aov": aov}
    labels = {"orders": "订单数", "gross": "毛营收", "refunds": "退款额", "net": "净营收", "aov": "客单价"}
    period = {"period": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
              "order_count": count, "gross_cents": gross, "refund_cents": refund, "net_cents": net,
              "segments": []}
    evidence = InvestigationEvidence(
        "retail", "orders", "demo", "演示订单库", "policy_exploration_demo/orders.sqlite", 1, 1, sql,
    )
    return investigation_result("retail", {
        "kind": "answer", "reason": None,
        "message": f"{start.year} 年 {start.month} 月{region}{product}{labels[metric]}：{values[metric]}",
        "answer": str(values[metric]) if metric in ("orders",) else f"¥{values[metric] / 100:,.2f}",
        "scope": {"product": product, "region": region,
                  "periods": [period["period"]], "metric": metric, "assumed_year": assumed_year},
        "actual": [period], "counterfactual": [],
        "actual_delta_cents": None, "counterfactual_delta_cents": None,
        "single_period_delta_cents": None, "policy_effect_cents": None,
        "document_version": "demo",
        "metrics": {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "sql_queries": 1, "retrievals": 0, "rewrites": 0,
                    "lexical_hits": 0, "model_fallbacks": 0, "usage_complete": True,
                    "latency_ms": round((perf_counter() - began) * 1000, 3)},
        "tool_trace": [
            {"stage": "scope", "status": "ok", "detail": f"{product} · {region} · {start.isoformat()}"},
            {"stage": "query", "status": "ok", "detail": f"{count} orders · {labels[metric]}"},
        ],
        "retrieval_trace": [],
    }, (evidence,))
