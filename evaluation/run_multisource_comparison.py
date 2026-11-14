"""Freeze-check and compare deterministic and controlled ReAct multi-source runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urlsplit

from langsmith import tracing_context

from app.config import PROJECT_ROOT, _load_dotenv, load_settings
from app.telemetry import Trace
from evaluation.multisource_reference import CORPUS, expected_result, read_fixture
from evaluation.run_policy_comparison import _known_total, _p95

SUITE_PATH = Path(__file__).with_name("multisource_cases.json")
RESERVE_PATH = Path(__file__).with_name("multisource_reserve.json")
MODES = ("fixed", "react")
SCOPE_FIELDS = ("start", "end_exclusive", "region")
GROUP_FIELDS = ("id", "product", "region", "order_count", "gross_cents", "deducted_cents", "net_cents", "evidence_ids")
QUANTITY_FIELDS = ("order_count", "gross_cents", "deducted_cents", "net_cents")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
TOOL_NAMES = ("discover_groups", "lookup_policies", "lookup_bulletins", "aggregate_groups", "finish")
MODEL_ERRORS = ("model_error", "model_unconfigured", "timeout", "workflow_error")
ORDER_SEED = 78124


def _scope_key(case: dict) -> tuple:
    return tuple(case["request"].get(field) for field in SCOPE_FIELDS)


def _load_suite(split: str) -> dict:
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    fixture = read_fixture()
    cases = suite["development"] + suite["holdout"]
    if split == "reserve":
        reserve = json.loads(RESERVE_PATH.read_text(encoding="utf-8"))
        cases += reserve["holdout"]
    if len({_scope_key(case) for case in cases}) != len(cases) or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Every development and holdout case must have a unique scope and ID")
    for case in cases:
        if expected_result(case["request"], fixture) != case["expected"]:
            raise ValueError(f"Frozen reference path changed: {case['id']}")
    return {"suite": reserve["suite"], "cases": reserve["holdout"]} if split == "reserve" else {
        "suite": suite["suite"], "cases": suite[split],
    }


def _fingerprints(split: str) -> dict[str, str]:
    app_paths = set((PROJECT_ROOT / "app").glob("multisource*.py"))
    paths = app_paths | set(CORPUS.glob("*.json")) | {
        Path(__file__), SUITE_PATH, Path(__file__).with_name("multisource_reference.py"),
        PROJECT_ROOT / "app/llm.py", PROJECT_ROOT / "app/tools/db.py",
    }
    if split == "reserve":
        paths.add(RESERVE_PATH)
    return {path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def _by_group_id(rows: list[dict], field: str = "id") -> dict[str, dict]:
    return {row[field]: row for row in rows}


def _exact_fields(found: dict, expected: dict, fields: tuple[str, ...]) -> bool:
    return all(type(found.get(key)) is type(expected[key]) and found[key] == expected[key] for key in fields)


def _trace_counts(trace: list[dict]) -> dict[str, int]:
    return dict(Counter(row["tool"] for row in trace if row.get("tool") in TOOL_NAMES and row.get("status") == "ok"))


def _score(result: dict, case: dict, mode: str) -> dict[str, bool]:
    expected = case["expected"]
    evidence = result["evidence"]
    counts = _trace_counts(result["tool_trace"])
    metrics = result["metrics"]
    want_model = mode == "react" and expected["kind"] != "clarification"
    checks = {
        "outcome": result["kind"] == expected["kind"],
        "scope": result["scope"] == dict(zip(SCOPE_FIELDS, _scope_key(case), strict=True)),
        "treatment": result["mode"] == mode and (metrics["model_calls"] > 0) == want_model,
    }
    if expected["kind"] != "answer":
        checks.update(
            reason=result["reason"] in expected["reasons"],
            no_partial_amount=result["total_cents"] is None and not result["groups"] and not evidence["queries"],
        )
        if expected["kind"] == "clarification":
            checks["input_guard"] = not counts and metrics["sql_queries"] == 0
        else:
            expected_status = "error" if "scope_too_broad" in expected["reasons"] else "ok"
            checks["discovery"] = sum(
                item.get("tool") == "discover_groups" and item.get("status") == expected_status
                for item in result["tool_trace"]
            ) == 1
        return checks

    groups, queries, documents, discovery = (
        result["groups"], evidence["queries"], evidence["documents"], evidence["discovery"],
    )
    wanted, actual = _by_group_id(expected["groups"]), _by_group_id(groups)
    cited_queries, found_groups = _by_group_id(queries, "group_id"), _by_group_id(discovery)
    document_ids = [item["id"] for item in documents]
    wanted_documents = {doc for group in wanted.values() for doc in group["evidence_ids"]}
    checks.update(
        total=_exact_fields(result, expected, ("total_cents",)),
        groups=(len(groups) == len(wanted) and actual.keys() == wanted.keys()
                and all(_exact_fields(actual[key], row, GROUP_FIELDS) for key, row in wanted.items())),
        discovery=(counts.get("discover_groups") == 1 and len(discovery) == len(wanted)
                   and found_groups.keys() == wanted.keys() and all(
                       found_groups[key]["product"] == group["product"] and found_groups[key]["region"] == group["region"]
                       and found_groups[key]["order_count"] == group["discovered_order_count"]
                       for key, group in wanted.items()
                   )),
        cited_policies=set(document_ids) == wanted_documents and len(document_ids) == len(wanted_documents),
        cited_sql=(len(queries) == len(wanted) and cited_queries.keys() == wanted.keys() and all(
            _exact_fields(cited_queries[key], row, QUANTITY_FIELDS)
            and isinstance(cited_queries[key]["sql"], str) and bool(cited_queries[key]["sql"].strip())
            and isinstance(cited_queries[key]["parameters"], (list, tuple))
            for key, row in wanted.items()
        )),
        referral_evidence=(counts.get("lookup_bulletins", 0) >= expected["max_referrals"]
                           if expected["max_referrals"] else not counts.get("lookup_bulletins")),
        sql_queries=metrics["sql_queries"] == 1 + len(wanted),
    )
    if wanted:
        checks["policy_tool"] = bool(counts.get("lookup_policies"))
        checks["aggregate_tool"] = bool(counts.get("aggregate_groups"))
    return checks


def _evaluate(database: Path, case: dict, number: int, mode: str, selector) -> dict:
    from app.multisource_workflow import MultiSourceRequest, run_multisource_workflow

    trace = Trace("multisource_comparison", lambda _: None)
    started = perf_counter()
    result: dict = {}
    error = None
    try:
        with trace.active(), tracing_context(enabled=False):
            request = MultiSourceRequest.model_validate(case["request"])
            result = run_multisource_workflow(database, request, mode=mode, choose=selector)
        checks = _score(result, case, mode)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError, IndexError) as failure:
        checks = {"valid_response": False}
        error = type(failure).__name__
    elapsed_ms = round((perf_counter() - started) * 1000, 3)
    usage_report = trace.finish("ok" if error is None else "error", error)
    metrics = result.get("metrics", {})
    model_calls = metrics.get("model_calls", usage_report["model_calls"])
    metered = usage_report["model_calls_with_usage"]
    usage = {key: usage_report["usage"][key] if model_calls == metered else None for key in TOKEN_FIELDS}
    return {
        "round": number, "id": case["id"], "group": case["group"], "mode": mode,
        "request": case["request"], "expected": case["expected"], "passed": all(checks.values()), "checks": checks,
        "observed": {k: result.get(k) for k in ("kind", "reason", "total_cents", "groups")},
        "evidence_counts": {key: len(value) for key, value in result.get("evidence", {}).items()},
        "latency_ms": elapsed_ms, "model_calls": model_calls, "model_calls_with_usage": metered,
        "usage": usage, "sql_queries": metrics.get("sql_queries", 0),
        "invalid_actions": metrics.get("invalid_actions", 0), "duplicate_actions": metrics.get("duplicate_actions", 0),
        "tools": [{key: entry[key] for key in ("step", "tool", "ids", "status", "reason") if key in entry}
                  for entry in result.get("tool_trace", [])],
        "error_type": error,
    }


def _scores(rows: list[dict]) -> dict:
    rounds = sorted({row["round"] for row in rows})
    latencies = [row["latency_ms"] for row in rows]
    return {
        "attempts": len(rows), "passed": sum(row["passed"] for row in rows),
        "correct_by_round": [sum(row["passed"] and row["round"] == number for row in rows) for number in rounds],
        "median_ms": median(latencies), "p95_ms": _p95(latencies),
        "p95_ms_by_round": [_p95([row["latency_ms"] for row in rows if row["round"] == n]) for n in rounds],
        "model_calls": sum(row["model_calls"] for row in rows),
        "model_calls_with_usage": sum(row["model_calls_with_usage"] for row in rows),
        "usage": {key: _known_total(row["usage"][key] for row in rows) for key in TOKEN_FIELDS},
        "sql_queries": sum(row["sql_queries"] for row in rows),
        "invalid_actions": sum(row["invalid_actions"] for row in rows),
        "duplicate_actions": sum(row["duplicate_actions"] for row in rows),
        "tools_ok": dict(sum((Counter(_trace_counts(row["tools"])) for row in rows), Counter())),
        "evidence_counts": dict(sum((Counter(row["evidence_counts"]) for row in rows), Counter())),
        "failures": [{"id": row["id"], "round": row["round"], "reason": row["observed"]["reason"],
                      "checks": [name for name, passed in row["checks"].items() if not passed],
                      "error_type": row["error_type"]} for row in rows if not row["passed"]],
    }


def _summarize(rows: list[dict], modes: list[str]) -> dict:
    methods = {}
    for mode in modes:
        selected = [row for row in rows if row["mode"] == mode]
        methods[mode] = {
            "overall": _scores(selected),
            "by_group": {key: _scores([row for row in selected if row["group"] == key])
                         for key in sorted({row["group"] for row in selected})},
            "by_expected_outcome": {kind: _scores([row for row in selected if row["expected"]["kind"] == kind])
                                   for kind in sorted({row["expected"]["kind"] for row in selected})},
        }
    paired = {}
    if set(modes) == set(MODES):
        matched = {(row["round"], row["id"], row["mode"]): row["passed"] for row in rows}
        for group in sorted({row["group"] for row in rows}):
            comparison = Counter((matched[row["round"], row["id"], "fixed"], row["passed"])
                                 for row in rows if row["group"] == group and row["mode"] == "react")
            paired[group] = {"gains": comparison[False, True], "losses": comparison[True, False],
                             "both_pass": comparison[True, True], "both_fail": comparison[False, False]}
    return {"methods": methods, "paired_react_vs_fixed": paired}


def main() -> int:
    from app.multisource_data import prepare_multisource_database
    from app.multisource_workflow import multisource_selector

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("development", "holdout", "reserve"), default="holdout")
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.workers <= 8 or len(set(args.modes)) != len(args.modes):
        parser.error("Use 1–10 repeats, 1–8 workers and distinct modes")
    settings = selector = None
    if "react" in args.modes:
        if args.env_file:
            if not args.env_file.is_file():
                parser.error("--env-file must be an existing configuration file")
            _load_dotenv(args.env_file)
        settings = replace(load_settings(), llm_model=args.model, llm_temperature=0)
        endpoint = urlsplit(settings.llm_base_url or "")
        if not settings.llm_configured or endpoint.scheme != "https" or not endpoint.hostname or any(
            (endpoint.username, endpoint.password, endpoint.query, endpoint.fragment),
        ):
            parser.error("Live ReAct requires an HTTPS model configuration")
        selector = multisource_selector(settings)
    suite = _load_suite(args.split)
    cases = suite["cases"]
    metadata = {
        "suite": suite["suite"], "split": args.split, "case_count": len(cases), "modes": args.modes,
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "order_seed": ORDER_SEED,
        "model": settings.llm_model if settings else None, "temperature": 0 if settings else None,
        "provider_host": urlsplit(settings.llm_base_url).hostname if settings else None,
        "workers": args.workers, "repeats": args.repeats, "source_sha256": _fingerprints(args.split),
        "latency_method": "per application call excluding queue time; P95 nearest rank",
    }
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    rows = []
    with TemporaryDirectory() as directory, ThreadPoolExecutor(args.workers) as pool, (
        args.output_dir / "cases.jsonl"
    ).open("x", encoding="utf-8") as log:
        database = prepare_multisource_database(Path(directory))
        for number in range(1, args.repeats + 1):
            tasks = [(case, mode) for case in cases for mode in args.modes]
            random.Random(ORDER_SEED + number).shuffle(tasks)
            for future in as_completed([pool.submit(_evaluate, database, case, number, mode, selector) for case, mode in tasks]):
                row = future.result()
                rows.append(row)
                log.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                log.flush()
                verdict = "PASS" if row["passed"] else "FAIL"
                print(f"{number} {row['mode']} {row['id']} {verdict} {row['latency_ms']:.1f} ms", flush=True)
    summary = _summarize(rows, args.modes)
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for mode, result in summary["methods"].items():
        overall = result["overall"]
        print(f"{mode}: {overall['passed']}/{overall['attempts']}, P95 {overall['p95_ms']:.1f} ms")
    return int(any(row["error_type"] or row["observed"]["reason"] in MODEL_ERRORS for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
