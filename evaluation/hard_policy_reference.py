"""Independent exact-answer oracle for the checked-in hard-policy fixture.

The oracle reads fixture files directly. It does no model calls, document ranking,
SQL template generation, or calls into the policy workflow.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "app" / "hard_policy_corpus"
SCOPE_FIELDS = ("start", "end_exclusive", "product", "region")
METADATA = re.compile(r"<!--\s*({[^\n]*})\s*-->")


def read_fixture(corpus: Path = CORPUS) -> dict:
    def read(name: str) -> list[dict]:
        return json.loads((corpus / f"{name}.json").read_text(encoding="utf-8"))

    def docs(name: str) -> list[dict]:
        return [json.loads(meta) for meta in METADATA.findall((corpus / f"{name}.md").read_text(encoding="utf-8"))]

    return {
        **{name: read(name) for name in ("catalog", "orders", "refunds", "chargebacks")},
        "policies": docs("policies"),
        "bulletins": docs("bulletins"),
    }


def _normalized_title(title: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", title).casefold() if char.isalnum())


def _scope_documents(
    docs: list[dict], product: str, category: str, region: str, start: str, end: str,
) -> list[dict]:
    return [
        doc for doc in docs
        if doc["authority"] == "official" and doc["product"] in (None, product)
        and (doc["category"], doc["region"]) == (category, region)
        and doc["start"] <= start < end <= doc["end_exclusive"]
    ]


def _policy_version(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    priority = max((candidate["product"] is not None, candidate["start"]) for candidate in candidates)
    return [candidate for candidate in candidates if (candidate["product"] is not None, candidate["start"]) == priority]


def _bulletin_version(candidates: list[dict], policy: dict) -> list[dict]:
    if policy["ref_kind"] == "title":
        candidates = [doc for doc in candidates
                      if _normalized_title(doc["title"]) == _normalized_title(policy["ref_title"])]
    if not candidates:
        return []
    # Bulletin effective date takes precedence when the policy says "最新公告".
    latest = max(doc["start"] for doc in candidates)
    return [doc for doc in candidates if doc["start"] == latest]


def expected_result(scope: dict, dataset: dict) -> dict:
    start, end, product, region = (scope.get(field) for field in SCOPE_FIELDS)
    if None in (start, end, product, region):
        return {"kind": "clarification", "reason": "missing_scope"}
    if start >= end:
        return {"kind": "clarification", "reason": "invalid_period"}
    categories = {entry["product"]: entry["category"] for entry in dataset["catalog"]}
    category = categories.get(product)
    if category is None:
        return {"kind": "abstained", "reason": "product_unknown"}
    policies = _policy_version(_scope_documents(dataset["policies"], product, category, region, start, end))
    if len(policies) != 1:
        return {"kind": "abstained", "reason": "policy_conflict" if policies else "policy_missing"}
    policy, = policies
    document = policy
    bulletin_id = None
    if policy["ref_kind"] is not None:
        bulletins = _bulletin_version(
            _scope_documents(dataset["bulletins"], product, category, region, start, end), policy,
        )
        if len(bulletins) != 1:
            return {
                "kind": "abstained",
                "reason": "bulletin_conflict" if bulletins else "bulletin_missing",
                "policy_id": policy["id"],
            }
        document, = bulletins
        bulletin_id = document["id"]
    ledger, cap = document["ledger"], document["cap_cents"]
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
    tool_path = [
        *(["find_latest_bulletin" if policy["ref_kind"] == "latest" else "find_titled_bulletin"]
          if bulletin_id else []),
        *(["inspect_ledger"] if ledger != "orders" else []),
        "query",
    ]
    return {
        "kind": "answer",
        "reason": None,
        "total_cents": gross - deducted,
        "category": category,
        "policy_id": policy["id"],
        "bulletin_id": bulletin_id,
        "cap_cents": cap,
        "ledger": ledger,
        "order_count": len(orders),
        "gross_cents": gross,
        "deducted_cents": deducted,
        "tool_path": tool_path,
    }
