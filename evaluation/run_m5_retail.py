"""Run the M5 retail slice against the deterministic retail service."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.hard_policy_data import prepare_hard_policy_database
from app.investigation_v3 import RetailInvestigationV3
from app.retail_investigation import run_retail_investigation

CASES = Path(__file__).with_name("m5") / "retail_cases.json"


def evaluate() -> dict:
    cases = json.loads(CASES.read_text())["cases"]
    rows, failures = [], []
    with TemporaryDirectory() as directory:
        db = prepare_hard_policy_database(Path(directory))
        for case in cases:
            result = run_retail_investigation(db, RetailInvestigationV3(domain="retail", question=case["question"]))
            expected = case["expected"]
            value = {
                "net": result["actual"][0]["net_cents"] if result.get("actual") else None,
                "orders": result["actual"][0]["order_count"] if result.get("actual") else None,
                "aov": (round(result["actual"][0]["gross_cents"] / result["actual"][0]["order_count"])
                        if result.get("actual") and result["actual"][0]["order_count"] else None),
            }[case["metric"]]
            target = {"net": expected["net_cents"], "orders": expected["order_count"], "aov": expected["aov_cents"]}[case["metric"]]
            ok = result["kind"] == expected["kind"] and value == target
            row = {"id": case["id"], "ok": ok, "metric": case["metric"], "expected": target, "actual": value,
                   "kind": result["kind"]}
            rows.append(row)
            if not ok:
                failures.append(row)
    return {"suite": "m5-retail-v1", "total": len(rows), "passed": len(rows) - len(failures),
            "failed": failures, "results": rows}


if __name__ == "__main__":
    report = evaluate()
    print(f"{report['suite']}: {report['passed']}/{report['total']} passed")
    for failure in report["failed"][:10]:
        print("FAIL", failure)
    raise SystemExit(0 if not report["failed"] else 1)
