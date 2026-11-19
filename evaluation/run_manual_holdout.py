"""Run the frozen manual-domain holdout against the deterministic service."""
from __future__ import annotations

import json
from pathlib import Path

from app.investigation_v3 import ManualInvestigationV3, run_manual_investigation

CASES = Path(__file__).with_name("manual_holdout_cases.json")


def evaluate() -> dict:
    suite = json.loads(CASES.read_text())
    results, failures = [], []
    for case in suite["cases"]:
        result = run_manual_investigation(ManualInvestigationV3(
            domain="manual", question=case["question"], version=case["version"],
        ), None)
        ok = result["kind"] == case["kind"]
        if case.get("reason"):
            ok = ok and result.get("reason") == case["reason"]
        if case.get("contains"):
            ok = ok and case["contains"] in (result.get("answer") or "")
        row = {"id": case["id"], "ok": ok, "kind": result["kind"], "reason": result.get("reason"),
               "answer": result.get("answer")}
        results.append(row)
        if not ok:
            failures.append(row)
    return {"suite": suite["suite"], "total": len(results), "passed": len(results) - len(failures),
            "failed": failures, "results": results}


def main() -> int:
    report = evaluate()
    print(f"{report['suite']}: {report['passed']}/{report['total']} passed")
    for failure in report["failed"]:
        print("FAIL", failure)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
