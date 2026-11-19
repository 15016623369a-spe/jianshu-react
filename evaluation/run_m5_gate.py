"""M5 multi-domain gate for the cases executable directly in this repository."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.hard_policy_data import prepare_hard_policy_database
from app.investigation_v3 import ManualInvestigationV3, RetailInvestigationV3, run_manual_investigation
from app.policy_investigation import InvestigationRequest, run_policy_investigation
from app.retail_investigation import run_retail_investigation

ROOT = Path(__file__).with_name("m5")
MANUAL = ROOT / "manual_cases.json"
RETAIL = ROOT / "retail_cases.json"
POLICY_EXTRA = ROOT / "policy_extra_cases.json"


def _manual() -> list[dict]:
    cases = json.loads(MANUAL.read_text())["cases"]
    rows = []
    for case in cases:
        result = run_manual_investigation(ManualInvestigationV3(
            domain="manual", question=case["question"], version=case["version"],
        ), None)
        ok = result["kind"] == case["kind"]
        if case.get("reason"):
            ok = ok and result.get("reason") == case["reason"]
        if case.get("contains"):
            ok = ok and case["contains"] in (result.get("answer") or "")
        if case.get("section"):
            ok = ok and bool(result.get("evidence")) and result["evidence"][0]["title"] == case["section"]
        citation_ok = (not case.get("section")) or (bool(result.get("evidence")) and
                                                   result["evidence"][0]["title"] == case["section"])
        rows.append({"id": case["id"], "domain": "manual", "ok": ok, "citation_ok": citation_ok,
                     "kind": result["kind"], "expected_kind": case["kind"]})
    return rows


def _retail() -> list[dict]:
    cases = json.loads(RETAIL.read_text())["cases"]
    rows = []
    with TemporaryDirectory() as directory:
        database = prepare_hard_policy_database(Path(directory))
        for case in cases:
            result = run_retail_investigation(database, RetailInvestigationV3(domain="retail", question=case["question"]))
            expected = case["expected"]
            actual = {
                "net": result["actual"][0]["net_cents"] if result.get("actual") else None,
                "orders": result["actual"][0]["order_count"] if result.get("actual") else None,
                "aov": (round(result["actual"][0]["gross_cents"] / result["actual"][0]["order_count"])
                        if result.get("actual") and result["actual"][0]["order_count"] else None),
            }[case["metric"]]
            target = {"net": expected["net_cents"], "orders": expected["order_count"], "aov": expected["aov_cents"]}[case["metric"]]
            rows.append({"id": case["id"], "domain": "retail", "ok": result["kind"] == "answer" and actual == target,
                         "citation_ok": bool(result.get("evidence")), "expected": target, "actual": actual})
    return rows


def _policy_extra() -> list[dict]:
    cases = json.loads(POLICY_EXTRA.read_text())["cases"]
    rows = []
    with TemporaryDirectory() as directory:
        database = prepare_hard_policy_database(Path(directory))
        for case in cases:
            result = run_policy_investigation(database, InvestigationRequest(question=case["question"]))
            rows.append({"id": case["id"], "domain": "policy", "ok": (result["kind"], result.get("reason")) ==
                         (case["kind"], case["reason"]), "citation_ok": True,
                         "sql_queries": result["metrics"]["sql_queries"]})
    return rows


def evaluate() -> dict:
    rows = _manual() + _retail() + _policy_extra()
    wrong_numeric = sum(1 for row in rows if row["domain"] == "retail" and not row["ok"])
    abstain_cases = [row for row in rows if row.get("expected_kind") in {"clarification", "abstained"}]
    return {
        "suite": "m5-multidomain-v1",
        "executed": len(rows),
        "passed": sum(row["ok"] for row in rows),
        "wrong_numeric": wrong_numeric,
        "citation_precision": sum(row["citation_ok"] for row in rows) / len(rows),
        "abstain_pass": sum(row["ok"] for row in abstain_cases) / len(abstain_cases) if abstain_cases else 1.0,
        "failed": [row for row in rows if not row["ok"]],
        "rows": rows,
    }


if __name__ == "__main__":
    report = evaluate()
    for row in report["failed"][:10]:
        print("FAIL", row)
    print(json.dumps({key: value for key, value in report.items() if key not in {"rows", "failed"}}, ensure_ascii=False))
    raise SystemExit(0 if not report["failed"] else 1)
