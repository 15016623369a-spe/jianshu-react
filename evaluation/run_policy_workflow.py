"""Check the fixed policy workflow against hand-calculated synthetic cases."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from math import ceil
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urlsplit

import httpx

_CASES = Path(__file__).with_name("policy_workflow_cases.json")
_ROUTE = "/api/v2/policy-revenue"
_RESULT_COLUMNS = ["order_count", "gross_cents", "deducted_cents", "revenue_cents"]
_SEGMENT_FIELDS = ("start", "end_exclusive", "policy_id", *_RESULT_COLUMNS)
_CAPS = {"refund-2026-10-a": None, "refund-2026-10-b": 300}


def _same(value, expected) -> bool:
    return type(value) is type(expected) and value == expected


def _evidence_index(items: list[dict]) -> dict[str, dict]:
    return {entry["evidence_id"]: entry for entry in items if entry.get("evidence_id")}


def _query_matches_segment(query: dict, segment: dict, region: str | None) -> bool:
    cap = _CAPS[segment["policy_id"]]
    expected_params = [] if cap is None else [cap]
    expected_params.extend((segment["start"], segment["end_exclusive"]))
    if region is not None:
        expected_params.append(region)
    rows = query.get("rows", [])
    if (query.get("columns") != _RESULT_COLUMNS or query.get("parameters") != expected_params
            or len(rows) != 1):
        return False
    expected_row = [segment.get(column) for column in _RESULT_COLUMNS]
    return len(rows[0]) == len(expected_row) and all(
        type(value) is int and _same(value, expected) for value, expected in zip(rows[0], expected_row, strict=True)
    )


def _verified_citations(segments: list[dict], evidence: dict, region: str | None) -> bool:
    policy_rows = evidence.get("policies", [])
    query_rows = evidence.get("queries", [])
    policies = _evidence_index(policy_rows)
    queries = _evidence_index(query_rows)
    if len(policies) != len(policy_rows) or len(queries) != len(query_rows) or policies.keys() & queries.keys():
        return False
    linked_queries = [segment.get("sql_evidence_id") for segment in segments]
    linked_policies = [segment.get("policy_evidence_id") for segment in segments]
    return (
        len(linked_queries) == len(queries)
        and set(linked_queries) == queries.keys()
        and set(linked_policies) == policies.keys()
        and all(
            policies[policy_id].get("id") == segment["policy_id"]
            and policies[policy_id].get("start", "") <= segment["start"]
            < segment["end_exclusive"] <= policies[policy_id].get("end_exclusive", "")
            and policies[policy_id].get("quote", "").strip()
            and queries[query_id].get("policy_evidence_id") == policy_id
            and _query_matches_segment(queries[query_id], segment, region)
            for segment, policy_id, query_id in zip(segments, linked_policies, linked_queries, strict=True)
        )
    )


def _inspect(result: dict, expected: dict, request: dict) -> tuple[dict, dict[str, bool]]:
    segments = result.get("segments", [])
    evidence = result.get("evidence") or {}
    metrics = result.get("metrics") or {}
    compared = [{field: segment.get(field) for field in _SEGMENT_FIELDS} for segment in segments]
    observed = {
        "kind": result.get("kind"), "reason": result.get("reason"), "total_cents": result.get("total_cents"),
        "segments": compared, "model_calls": metrics.get("model_calls"), "sql_queries": metrics.get("sql_queries"),
    }
    checks = {
        "action": observed["kind"] == expected["kind"],
        "scope": result.get("scope") == {field: request.get(field) for field in ("start", "end_exclusive", "region")},
        "no_model_calls": _same(metrics.get("model_calls"), 0),
        "sql_query_count": _same(metrics.get("sql_queries"), len(expected.get("segments", []))),
    }
    if expected["kind"] != "answer":
        checks["reason"] = observed["reason"] == expected["reason"]
        checks["no_partial_answer"] = not segments and not evidence.get("queries") and observed["total_cents"] is None
        return observed, checks

    checks["currency"] = result.get("currency") == "CNY"
    checks["total_cents"] = _same(observed["total_cents"], expected["total_cents"])
    wanted = expected["segments"]
    checks["segments"] = len(compared) == len(wanted) and all(
        _same(actual[field], target[field]) for actual, target in zip(compared, wanted, strict=True)
        for field in _SEGMENT_FIELDS
    )
    checks["citations"] = bool(segments) and all(segment.get("policy_id") for segment in segments) and (
        _verified_citations(segments, evidence, request.get("region"))
    )
    return observed, checks


def _run(suite: dict, execute: Callable[[dict], dict], transport: str) -> dict:
    cases = []
    for case in suite["cases"]:
        started = perf_counter()
        try:
            result = execute(case["request"])
            elapsed = round((perf_counter() - started) * 1000, 3)
            observed, checks = _inspect(result, case["expected"], case["request"])
        except (httpx.HTTPError, ValueError, RuntimeError, TypeError, KeyError, AttributeError) as exc:
            elapsed = round((perf_counter() - started) * 1000, 3)
            observed, checks = {"error_type": type(exc).__name__}, {"workflow_response": False}
        cases.append({
            "id": case["id"], "passed": all(checks.values()), "latency_ms": elapsed,
            "expected": case["expected"], "observed": observed, "checks": checks,
        })
    times = sorted(case["latency_ms"] for case in cases)
    return {
        "suite": suite["suite"], "transport": transport, "passed": sum(case["passed"] for case in cases),
        "total": len(cases), "median_ms": median(times), "p95_ms": times[ceil(.95 * len(times)) - 1], "cases": cases,
    }


def _local(suite: dict) -> dict:
    from app.commerce_demo import prepare_database
    from app.policy_workflow import PolicyRevenueRequest, run_policy_workflow

    with TemporaryDirectory() as directory:
        database = prepare_database(Path(directory))

        def execute(request: dict) -> dict:
            return run_policy_workflow(database, PolicyRevenueRequest.model_validate(request))

        return _run(suite, execute, "local workflow with fresh seeded SQLite")


def _http(suite: dict, base_url: str) -> dict:
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client:
        def execute(request: dict) -> dict:
            return client.post(_ROUTE, json=request).raise_for_status().json()

        return _run(suite, execute, "HTTP API")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Test a running local API instead of calling the workflow in-process")
    parser.add_argument("--output", type=Path, help="Save the case-by-case JSON report to a new file")
    args = parser.parse_args()
    if args.base_url:
        base = urlsplit(args.base_url)
        if (base.scheme != "http" or base.hostname not in {"127.0.0.1", "localhost", "::1"}
                or base.path.rstrip("/") or base.query or base.fragment or base.username or base.password):
            parser.error("--base-url must be a local HTTP origin")
    suite = json.loads(_CASES.read_text(encoding="utf-8"))
    report = _http(suite, args.base_url) if args.base_url else _local(suite)
    for case in report["cases"]:
        detail = ", ".join(name for name, ok in case["checks"].items() if not ok)
        status = "PASS" if case["passed"] else f"FAIL ({detail})"
        print(f"{status} {case['id']} · {case['latency_ms']:.3f} ms")
    print(f"{report['passed']}/{report['total']} passed · median {report['median_ms']:.3f} ms · P95 {report['p95_ms']:.3f} ms")
    if args.output:
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write("\n")
    return int(report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())
