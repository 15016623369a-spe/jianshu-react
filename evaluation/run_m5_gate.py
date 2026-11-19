"""M5 multi-domain gate for the cases executable directly in this repository."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from unittest.mock import patch

from app.config import load_settings
from app.hard_policy_data import prepare_hard_policy_database
from app.investigation_v3 import ManualInvestigationV3, RetailInvestigationV3, run_manual_investigation
from app.policy_investigation import InvestigationRequest, run_policy_investigation
from app.policy_investigation_retrieval import load_investigation_snapshot
from app.multisource_workflow import multisource_selector
from app.retail_investigation import run_retail_investigation

ROOT = Path(__file__).with_name("m5")
MANUAL = ROOT / "manual_cases.json"
RETAIL = ROOT / "retail_cases.json"
POLICY_EXTRA = ROOT / "policy_extra_cases.json"
POLICY_SUITES = (
    Path(__file__).with_name("policy_grounding_holdout"),
    Path(__file__).with_name("policy_investigation_holdout"),
)
_AMOUNTS = ("order_count", "gross_cents", "deducted_cents", "net_cents")
_DELTAS = ("actual_delta_cents", "counterfactual_delta_cents", "policy_effect_cents")


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
                     "kind": result["kind"], "expected_kind": case["kind"], "latency_ms": 0.0, "model_calls": 0})
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
                         "citation_ok": bool(result.get("evidence")), "expected": target, "actual": actual,
                         "expected_kind": "answer", "numeric_output": True, "amount_ok": actual == target,
                         "latency_ms": 0.0, "model_calls": 0})
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
                         "sql_queries": result["metrics"]["sql_queries"], "expected_kind": case["kind"],
                         "numeric_output": False, "amount_ok": True, "doc_recall": 1.0, "doc_precision": 1.0,
                         "latency_ms": result["metrics"]["latency_ms"], "model_calls": result["metrics"]["model_calls"]})
    return rows


def _policy_base_rows(*, use_model: bool = False) -> list[dict]:
    settings = load_settings() if use_model else None
    choose = multisource_selector(settings) if settings is not None else None
    mode = "auto" if choose is not None else "lexical"
    rows = []
    for directory in POLICY_SUITES:
        cases = json.loads((directory / "cases.json").read_text())["cases"]
        snapshot = load_investigation_snapshot(directory=directory)
        with TemporaryDirectory() as temporary, patch("app.hard_policy_data._CORPUS", directory):
            database = prepare_hard_policy_database(Path(temporary))
            for case in cases:
                expected = case["expected"]
                result = run_policy_investigation(
                    database, InvestigationRequest(question=case["question"]), mode=mode,
                    choose=choose, snapshot=snapshot,
                )
                disposition = result["kind"] == expected["kind"]
                if "reason" in expected:
                    disposition = disposition and result.get("reason") == expected["reason"]
                numeric = bool(result.get("actual") or result.get("counterfactual") or
                               any(result.get(key) is not None for key in _DELTAS))
                if expected["kind"] != "answer":
                    amount_ok = not numeric and result["metrics"]["sql_queries"] == 0
                    expected_docs, predicted_docs = set(), set()
                else:
                    expected_groups = expected["actual"]
                    actual_groups = result.get("actual") or []
                    amount_ok = len(expected_groups) == len(actual_groups)
                    for expected_group, actual_group in zip(expected_groups, actual_groups, strict=False):
                        amount_ok = amount_ok and all(
                            type(actual_group.get(key)) is int and actual_group.get(key) ==
                            sum(segment[key] for segment in expected_group) for key in _AMOUNTS
                        )
                    amount_ok = amount_ok and all(
                        type(result.get(key)) is type(expected[key]) and result.get(key) == expected[key]
                        for key in _DELTAS
                    )
                    expected_docs = {
                        document_id for groups in (expected.get("actual", ()), expected.get("counterfactual", ()))
                        for group in groups for segment in group for document_id in segment["document_ids"]
                    }
                    predicted_docs = {
                        document["id"] for groups in (result.get("actual", ()), result.get("counterfactual", ()))
                        for group in groups for segment in group["segments"] for document in segment["documents"]
                    }
                doc_recall = (len(expected_docs & predicted_docs) / len(expected_docs)) if expected_docs else 1.0
                citation_precision = (len(expected_docs & predicted_docs) / len(predicted_docs)) if predicted_docs else 1.0
                citation_support = 1.0 if predicted_docs <= expected_docs else 0.0
                rows.append({
                    "id": f"{directory.name}-{case['id']}", "domain": "policy", "ok": disposition and amount_ok,
                    "citation_ok": citation_support, "doc_recall": doc_recall,
                    "doc_precision": citation_precision, "kind": result["kind"],
                    "expected_kind": expected["kind"], "sql_queries": result["metrics"]["sql_queries"],
                    "latency_ms": result["metrics"]["latency_ms"], "amount_ok": amount_ok,
                    "numeric_output": numeric, "model_calls": result["metrics"]["model_calls"],
                })
    return rows


def evaluate(*, use_model: bool = False) -> dict:
    rows = _manual() + _retail() + _policy_extra() + _policy_base_rows(use_model=use_model)
    wrong_numeric = sum(
        1 for row in rows
        if (row["domain"] == "retail" and row["expected_kind"] == "answer" and not row["ok"])
        or (row["domain"] == "policy" and row["expected_kind"] == "answer"
            and row.get("numeric_output") and not row.get("amount_ok", True))
    )
    doc_recalls = [row.get("doc_recall", 1.0) for row in rows if row["domain"] == "policy"]
    doc_precisions = [row.get("doc_precision", 1.0) for row in rows if row["domain"] == "policy"]
    abstain_cases = [row for row in rows if row.get("expected_kind") in {"clarification", "abstained"}]
    latencies = sorted(row.get("latency_ms", 0.0) for row in rows)
    p95 = latencies[max(0, int(len(latencies) * .95) - 1)] if latencies else 0.0
    return {
        "suite": "m5-multidomain-v1",
        "executed": len(rows),
        "passed": sum(row["ok"] for row in rows),
        "wrong_numeric": wrong_numeric,
        "citation_precision": sum(row["citation_ok"] for row in rows) / len(rows),
        "doc_recall_at_5": sum(doc_recalls) / len(doc_recalls) if doc_recalls else 1.0,
        "doc_precision": sum(doc_precisions) / len(doc_precisions) if doc_precisions else 1.0,
        "abstain_pass": sum(row["ok"] for row in abstain_cases) / len(abstain_cases) if abstain_cases else 1.0,
        "model_calls": sum(row.get("model_calls", 0) for row in rows),
        "median_ms": round(median(latencies), 3) if latencies else 0.0,
        "p95_ms": round(p95, 3),
        "failed": [row for row in rows if not row["ok"]],
        "rows": rows,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="store_true", help="Use the configured model fallback for policy cases.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(use_model=args.model)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for row in report["failed"][:10]:
        print("FAIL", row)
    print(json.dumps({key: value for key, value in report.items() if key not in {"rows", "failed"}}, ensure_ascii=False))
    raise SystemExit(0 if not report["failed"] else 1)
