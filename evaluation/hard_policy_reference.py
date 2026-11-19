"""Independent arithmetic oracle for the frozen policy exploration cases.

Golden policy/announcement IDs, ledgers, caps, outcomes, and routes are adjudicated
from the human-readable documents when freezing each case. This module reads
only raw order/catalog/ledger fixtures and independently checks the frozen
amounts. The production retriever never imports the case suite.
"""
from __future__ import annotations

import json
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "app" / "hard_policy_corpus"
SCOPE_FIELDS = ("start", "end_exclusive", "product", "region")


def read_fixture(corpus: Path = CORPUS) -> dict:
    return {
        name: json.loads((corpus / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("catalog", "orders", "refunds", "chargebacks")
    }


def expected_amount(scope: dict, dataset: dict, *, ledger: str, cap: int | None) -> dict:
    start, end, product, region = (scope.get(field) for field in SCOPE_FIELDS)
    orders = [
        order for order in dataset["orders"]
        if (order["product"], order["region"]) == (product, region) and start <= order["ordered_on"] < end
    ]
    if ledger == "orders":
        refund_by_order = {order["id"]: order["refund_cents"] for order in orders}
    else:
        refund_by_order = {}
        for event in dataset[ledger]:
            key = event["order_id"]
            refund_by_order[key] = refund_by_order.get(key, 0) + event["amount_cents"]
    gross = sum(order["amount_cents"] for order in orders)
    deducted = sum(
        refund_by_order.get(order["id"], 0) if cap is None else min(refund_by_order.get(order["id"], 0), cap)
        for order in orders
    )
    return {
        "order_count": len(orders),
        "gross_cents": gross,
        "deducted_cents": deducted,
        "total_cents": gross - deducted,
    }


def validate_expected(scope: dict, expected: dict, dataset: dict) -> bool:
    start, end, product, region = (scope.get(field) for field in SCOPE_FIELDS)
    if None in (start, end, product, region):
        return (expected["kind"], expected["reason"]) == ("clarification", "missing_scope")
    if start >= end:
        return (expected["kind"], expected["reason"]) == ("clarification", "invalid_period")
    category = next((entry["category"] for entry in dataset["catalog"] if entry["product"] == product), None)
    if category is None:
        return (expected["kind"], expected["reason"]) == ("abstained", "product_unknown")
    if expected["kind"] != "answer":
        return expected["kind"] == "abstained" and expected["reason"] in {
            "policy_conflict", "policy_missing", "bulletin_conflict", "bulletin_missing", "referral_cycle",
            "referral_limit",
        }
    return expected["category"] == category and all(
        expected[key] == value
        for key, value in expected_amount(scope, dataset, ledger=expected["ledger"], cap=expected["cap_cents"]).items()
    )
