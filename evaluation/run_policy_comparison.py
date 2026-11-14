"""Compare fixed, hybrid, and pure ReAct on the frozen synthetic policy holdout."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urlsplit

from langsmith import tracing_context

from app.commerce_demo import prepare_database
from app.config import PROJECT_ROOT, Settings, _load_dotenv, load_settings
from app.policy_workflow import PolicyRevenueRequest, run_policy_workflow
from app.telemetry import Trace
from evaluation.run_policy_workflow import _inspect

_MODES = ("fixed", "hybrid", "react")
_GROUPS = ("simple", "mixed", "guard")
_SUITES = {
    "original": Path(__file__).with_name("react_holdout.json"),
    "expanded": Path(__file__).with_name("react_holdout_expanded.json"),
}
_DEVELOPMENT = Path(__file__).with_name("policy_workflow_cases.json")
_ORDER_SEED = 918274
_BUDGET_STOPS = {
    "max_steps", "max_tools", "tool_budget", "max_sql", "max_retrieval", "max_tokens",
    "token_budget", "timeout", "deadline", "no_progress", "observation_limit",
}
_MEASURED_NAMES = ("model_calls", "sql_queries", "invalid_actions", "duplicate_actions")


def _load_suite(name: str = "original") -> dict:
    release = json.loads(_SUITES[name].read_text(encoding="utf-8"))
    prior_files = [_DEVELOPMENT]
    if name == "expanded":
        prior_files.append(_SUITES["original"])

    def request_key(case: dict) -> tuple:
        request = PolicyRevenueRequest.model_validate(case["request"])
        return request.start, request.end_exclusive, request.region

    cases = release["cases"]
    keys = {request_key(case) for case in cases}
    prior_keys = {
        request_key(case)
        for file in prior_files
        for case in json.loads(file.read_text(encoding="utf-8"))["cases"]
    }
    if len(keys) != len(cases) or keys & prior_keys:
        raise ValueError("Holdout cases overlap with each other or a prior suite")
    if len({case["id"] for case in cases}) != len(cases) or set(case["group"] for case in cases) != set(_GROUPS):
        raise ValueError("Holdout IDs or group labels are invalid")
    return release


def _fingerprint(suite: Path) -> dict[str, str]:
    files = [
        "app/commerce_demo.py", "app/llm.py", "app/policy_workflow.py", "app/policy_react.py",
        "app/tools/db.py", suite.relative_to(PROJECT_ROOT).as_posix(),
        Path(__file__).relative_to(PROJECT_ROOT).as_posix(),
    ]
    files.extend(path.relative_to(PROJECT_ROOT).as_posix() for path in sorted((PROJECT_ROOT / "app/policy_corpus").glob("*.md")))
    return {
        name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest()
        for name in files if (PROJECT_ROOT / name).is_file()
    }


def _settings(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Settings | None:
    if not any(mode != "fixed" for mode in args.modes):
        return None
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("--env-file must be an existing file")
        _load_dotenv(args.env_file)
    settings = replace(load_settings(), llm_model=args.model, llm_temperature=args.temperature)
    if not settings.llm_configured or not settings.llm_base_url:
        parser.error("Set LLM_API_KEY and LLM_BASE_URL or pass --env-file for live ReAct")
    endpoint = urlsplit(settings.llm_base_url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:
        parser.error("The live model endpoint must be HTTPS with no user information, query, or fragment")
    return settings


def _token_usage(result: dict, trace: dict, model_calls: int) -> tuple[dict[str, int | None], int]:
    """Retain unknown usage as unknown, including any unmetered call."""
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    metered = trace["model_calls_with_usage"]
    metrics = result.get("metrics", {})
    if model_calls == 0:
        return dict.fromkeys(keys, 0), 0
    metered = metrics.get("model_calls_with_usage", metered)
    if metered != model_calls:
        return dict.fromkeys(keys), metered
    return {
        key: trace["usage"][key] if trace["model_calls"] == model_calls else metrics.get(key)
        for key in keys
    }, metered


def _evaluate(database: Path, case: dict, round_number: int, mode: str, choose) -> dict:
    trace = Trace("policy_comparison", lambda _: None)
    started = perf_counter()
    try:
        with trace.active(), tracing_context(enabled=False):
            request = PolicyRevenueRequest.model_validate(case["request"])
            result = run_policy_workflow(database, request, mode=mode, choose=choose)
        observed, checks = _inspect(result, case["expected"], case["request"], expect_no_model=(mode == "fixed"))
        if case["expected"]["kind"] == "clarification":
            checks["experiment_route"] = observed["model_calls"] == 0
        else:
            expected_strategy = "fixed" if mode == "fixed" or (mode == "hybrid" and case["group"] == "simple") else "react"
            checks["experiment_route"] = result.get("strategy") == expected_strategy
            checks["model_calls_match_route"] = (
                isinstance(observed["model_calls"], int)
                and (observed["model_calls"] > 0) == (expected_strategy == "react")
            )
        error = None
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError) as failure:
        error = type(failure).__name__
        observed, checks, result = {"error_type": error}, {"workflow_response": False}, {}
    elapsed = round((perf_counter() - started) * 1000, 3)
    report = trace.finish("ok" if error is None else "error", error)
    metrics = result.get("metrics", {})
    model_calls = metrics.get("model_calls", report["model_calls"])
    usage, metered = _token_usage(result, report, model_calls)
    price = report["estimated_cost_usd"] if report["model_calls"] == model_calls else None
    return {
        "id": case["id"], "group": case["group"], "round": round_number, "mode": mode,
        "strategy": result.get("strategy"), "routing_reason": result.get("routing_reason"),
        "passed": all(checks.values()), "checks": checks, "request": case["request"],
        "expected": case["expected"], "observed": observed, "latency_ms": elapsed,
        "model_calls": model_calls, "model_calls_with_usage": metered, "usage": usage,
        "estimated_cost_usd": price, "cost_basis": report["cost_basis"] if price is not None else None,
        **{key: metrics.get(key, 0) for key in _MEASURED_NAMES if key != "model_calls"},
        "stop_reason": result.get("stop_reason") or result.get("reason"),
        "tools": [
            {key: action[key] for key in ("step", "tool", "status", "reason", "segment_id") if key in action}
            for action in result.get("tool_trace", [])
        ],
    }


def _p95(values: list[float]) -> float:
    return sorted(values)[math.ceil(.95 * len(values)) - 1]


def _known_total(values) -> int | float | None:
    known = tuple(values)
    return None if any(value is None for value in known) else sum(known)


def _scores(rows: list[dict]) -> dict:
    rounds = sorted({row["round"] for row in rows})
    count = len(rows)
    return {
        "attempts": count, "passed": sum(row["passed"] for row in rows),
        "correct_by_round": [sum(row["passed"] for row in rows if row["round"] == number) for number in rounds],
        "questions_per_round": count // len(rounds),
        "median_ms": median([row["latency_ms"] for row in rows]),
        "p95_ms": _p95([row["latency_ms"] for row in rows]),
        "p95_ms_by_round": [_p95([row["latency_ms"] for row in rows if row["round"] == number]) for number in rounds],
        "stats_per_case": {key: round(mean(row[key] for row in rows), 3) for key in _MEASURED_NAMES},
        "totals": {key: sum(row[key] for row in rows) for key in _MEASURED_NAMES},
        "model_calls_with_usage": sum(row["model_calls_with_usage"] for row in rows),
        "usage": {key: _known_total(row["usage"][key] for row in rows) for key in rows[0]["usage"]},
        "estimated_cost_usd": _known_total(row["estimated_cost_usd"] for row in rows),
        "cost_bases": dict(Counter(row["cost_basis"] for row in rows if row["cost_basis"] is not None)),
        "routes": dict(Counter(row["strategy"] or "unknown" for row in rows)),
        "tool_status_counts": dict(Counter(
            tool.get("status", "unknown") for row in rows for tool in row["tools"]
        )),
        "stop_reasons": dict(Counter(row["stop_reason"] for row in rows if row["stop_reason"])),
        "budget_stops": sum(row["stop_reason"] in _BUDGET_STOPS for row in rows),
        "failed_cases": sorted({row["id"] for row in rows if not row["passed"]}),
        "error_types": dict(Counter(row["observed"]["error_type"] for row in rows if "error_type" in row["observed"])),
    }


def _summarize(records: list[dict], modes: list[str]) -> dict:
    variants = {}
    paired = {}
    lookup = {(row["mode"], row["round"], row["id"]): row["passed"] for row in records}
    for mode in modes:
        rows = [row for row in records if row["mode"] == mode]
        variants[mode] = {
            "overall": _scores(rows),
            "by_group": {group: _scores([row for row in rows if row["group"] == group]) for group in _GROUPS},
        }
        if mode != "fixed" and "fixed" in modes:
            paired[mode] = {}
            for group in _GROUPS:
                group_rows = [row for row in rows if row["group"] == group]
                outcomes = [(row["passed"], lookup["fixed", row["round"], row["id"]]) for row in group_rows]
                paired[mode][group] = {
                    "gains": outcomes.count((True, False)), "regressions": outcomes.count((False, True)),
                    "both_correct": outcomes.count((True, True)), "both_wrong": outcomes.count((False, False)),
                }
    return {"methods": variants, "paired_against_fixed": paired}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=tuple(_SUITES), default="original")
    parser.add_argument("--modes", nargs="+", choices=_MODES, default=list(_MODES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--env-file", type=Path, help="Read existing model configuration without copying credentials")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--output-dir", type=Path, help="New directory for run metadata, case log and summary")
    args = parser.parse_args()
    if len(args.modes) != len(set(args.modes)):
        parser.error("Each method can appear only once in --modes")
    if not 1 <= args.repeats <= 10 or not 1 <= args.workers <= 8 or not 0 <= args.temperature <= 2:
        parser.error("Expected repeats 1–10, workers 1–8, and temperature 0–2")

    settings = _settings(args, parser)
    choose = None
    if settings is not None:
        from app.policy_react import deepseek_policy_selector

        choose = deepseek_policy_selector(settings)
    suite = _load_suite(args.suite)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir or Path(__file__).with_name("results") / f"policy-comparison-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    metadata = {
        "suite": suite["suite"], "started_at_utc": stamp, "model": settings.llm_model if settings else None,
        "provider_host": urlsplit(settings.llm_base_url).hostname if settings else None,
        "temperature": args.temperature if settings else None, "workers": args.workers, "modes": args.modes,
        "repeats": args.repeats, "order_seed": _ORDER_SEED, "source_sha256": _fingerprint(_SUITES[args.suite]),
        "case_count": len(suite["cases"]),
        "score_definition": "exact expected outcome, scope, cited evidence, SQL query counts, and actual treatment route",
        "latency_method": "measured around local application call, excluding queue time; P95 nearest rank",
    }
    (output / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    records = []
    with TemporaryDirectory() as directory, ThreadPoolExecutor(args.workers) as pool, (output / "cases.jsonl").open(
        "x", encoding="utf-8",
    ) as log:
        database = prepare_database(Path(directory))
        for index in range(1, args.repeats + 1):
            tasks = [(case, mode) for case in suite["cases"] for mode in args.modes]
            random.Random(_ORDER_SEED + index).shuffle(tasks)
            for future in as_completed([pool.submit(_evaluate, database, case, index, mode, choose) for case, mode in tasks]):
                row = future.result()
                records.append(row)
                log.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                log.flush()
                status = "PASS" if row["passed"] else "FAIL"
                print(f"run {index} {row['mode']} {row['id']} {status} · {row['latency_ms']:.1f} ms", flush=True)

    summary = _summarize(records, args.modes)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for mode, data in summary["methods"].items():
        score = data["overall"]
        print(f"{mode} · {score['passed']}/{score['attempts']} · P95 {score['p95_ms']:.1f} ms")
    print(f"Results: {output.resolve()}")
    if any("error_type" in row["observed"] or row["stop_reason"] in {"model_error", "model_unconfigured"} for row in records):
        print("Infrastructure failures are present. See case log before comparing modes.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
