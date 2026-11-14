"""Independent reference calculation for the synthetic multi-source fixture."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "app" / "multisource_corpus"


def read_fixture(path: Path = CORPUS) -> tuple[list[dict], list[dict], dict[str, dict]]:
    def read(name: str) -> list[dict]:
        return json.loads((path / f"{name}.json").read_text(encoding="utf-8"))

    return read("orders"), read("policies"), {row["id"]: row for row in read("bulletins")}


def expected_result(request: dict, fixture: tuple[list[dict], list[dict], dict[str, dict]]) -> dict:
    """Resolve referrals and sum the raw order rows without the application tools."""
    start, end, region = (request.get(key) for key in ("start", "end_exclusive", "region"))
    if start is None or end is None:
        return {"kind": "clarification", "reasons": ["missing_dates"]}
    if start >= end:
        return {"kind": "clarification", "reasons": ["invalid_period"]}

    orders, policies, bulletins = fixture
    matched = defaultdict(list)
    for order in orders:
        if start <= order["ordered_on"] < end and (region is None or order["region"] == region):
            matched[order["product"], order["region"]].append(order)
    actual_groups = sorted(key for key, rows in matched.items() if any(row["refund_cents"] > 0 for row in rows))
    if len(actual_groups) > 6:
        return {"kind": "abstained", "reasons": ["scope_too_broad"], "discovered_groups": len(actual_groups)}
    groups = []
    refusals = set()
    source_shape = set()
    for number, (product, group_region) in enumerate(actual_groups, 1):
        rules = [
            doc for doc in policies
            if applies(doc, product, group_region, start, end)
        ]
        if len(rules) != 1:
            refusals.add("policy_conflict" if rules else "policy_missing")
            continue
        rule, = rules
        documents = [rule["id"]]
        while reference := rule["ref"]:
            if reference in documents:
                refusals.add("referral_limit")
                break
            if reference not in bulletins:
                refusals.add("bulletin_missing")
                break
            rule = bulletins[reference]
            if not applies(rule, product, group_region, start, end):
                refusals.add("bulletin_outside_scope")
                break
            documents.append(reference)
        else:
            source_shape.add(len(documents) - 1)
            rows = matched[product, group_region]
            cap = rule["cap_cents"]
            gross = sum(row["amount_cents"] for row in rows)
            deducted = sum(row["refund_cents"] if cap is None else min(row["refund_cents"], cap) for row in rows)
            groups.append({
                "id": f"group:{number}", "product": product, "region": group_region,
                "order_count": len(rows), "discovered_order_count": sum(row["refund_cents"] > 0 for row in rows),
                "gross_cents": gross, "deducted_cents": deducted, "net_cents": gross - deducted,
                "evidence_ids": documents,
            })
    if refusals:
        return {"kind": "abstained", "reasons": sorted(refusals), "discovered_groups": len(actual_groups)}
    return {
        "kind": "answer", "total_cents": sum(group["net_cents"] for group in groups), "groups": groups,
        "max_referrals": max(source_shape, default=0),
    }


def applies(rule: dict, product: str, region: str, start: str, end: str) -> bool:
    return (
        rule["product"] == product and rule["region"] == region
        and rule["start"] <= start and end <= rule["end_exclusive"]
    )
