"""Compare fixed retrieval, live planning, and validated-plan reuse."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
from evaluation.hard_policy_reference import CORPUS, SCOPE_FIELDS, expected_result, read_fixture
from evaluation.run_policy_comparison import _known_total, _p95

SUITE_PATH = Path(__file__).with_name("hard_policy_cases.json")
MODES = ("fixed", "uncached", "plan")
MODEL_STOP = {"model_error", "model_unconfigured", "timeout", "workflow_error"}
OBSERVED_TOOLS = {"find_latest_bulletin", "find_titled_bulletin", "inspect_ledger", "query"}
PLANNER_EVENTS = {"plan", "replan", "cache_hit"}
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
ORDER_SEED = 102317


def _request_key(case: dict) -> tuple:
    return tuple(case["request"].get(field) for field in SCOPE_FIELDS)


def _load_suite() -> dict:
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    dataset = read_fixture()
    cases = suite["development"] + suite["holdout"]
    if len({_request_key(case) for case in cases}) != len(cases) or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Every development and holdout case must have a distinct scope and ID")
    for case in cases:
        if expected_result(case["request"], dataset) != case["expected"]:
            raise ValueError(f"Independent reference changed for {case['id']}")
    return suite


def _fingerprints() -> dict[str, str]:
    files = [
        *sorted(CORPUS.iterdir()), *sorted((PROJECT_ROOT / "app").glob("hard_policy*.py")), SUITE_PATH,
        Path(__file__), Path(__file__).with_name("hard_policy_reference.py"),
        PROJECT_ROOT / "app/llm.py", PROJECT_ROOT / "app/tools/db.py", PROJECT_ROOT / "app/multisource_workflow.py",
    ]
    return {file.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in files if file.is_file()}


def _doc_id(document) -> str | None:
    return document.get("id") if isinstance(document, dict) else None


def _observation_signature(trace: list[dict]) -> list[str]:
    return [event["tool"] for event in trace if event.get("tool") in OBSERVED_TOOLS and event.get("status") == "ok"]


def _plans(trace: list[dict]) -> list[dict]:
    return [
        {"event": event["tool"], "sequence": event.get("ids", []), "status": event.get("status")}
        for event in trace if event.get("tool") in PLANNER_EVENTS
    ]


def _score(result: dict, case: dict, mode: str) -> dict[str, bool]:
    expected, evidence, metrics = case["expected"], result["evidence"], result["metrics"]
    trace = result["tool_trace"]
    model_calls = metrics["model_calls"]
    requires_tools = expected["kind"] == "answer" or expected.get("policy_id") is not None
    checks = {
        "outcome": (result["kind"], result["reason"]) == (expected["kind"], expected["reason"]),
        "scope": all(result["scope"].get(field) == value for field, value in zip(
            SCOPE_FIELDS, _request_key(case), strict=True,
        )),
        "treatment": result["mode"] == mode,
    }
    if mode == "fixed" or not requires_tools:
        checks["treatment"] &= model_calls == 0
    elif mode == "uncached":
        checks["treatment"] &= model_calls >= 1 and not metrics.get("cache_hits")
    else:
        checks["treatment"] &= bool(model_calls or metrics.get("cache_hits"))
    if expected["kind"] != "answer":
        checks["no_partial_amount"] = result["total_cents"] is None and not evidence.get("query")
        checks["no_query_execution"] = "query" not in _observation_signature(trace)
        return checks

    category = evidence.get("category")
    category = category.get("category") if isinstance(category, dict) else category
    query = evidence.get("query") or {}
    schema = evidence.get("schema")
    bindings = tuple(query.get("parameters") or ())
    request = case["request"]
    sql = query.get("sql", "")
    required_tables = {"orders"} if expected["ledger"] == "orders" else {"orders", expected["ledger"]}
    quoted_tables = set(re.findall(r'\b(?:FROM|JOIN)\s+"?([a-z_]+)"?', sql, flags=re.I))
    expected_bindings = (request["product"], request["region"], request["start"], request["end_exclusive"])
    cap = expected["cap_cents"]
    if cap is not None:
        expected_bindings += (cap,)
    checks.update(
        total=type(result["total_cents"]) is int and result["total_cents"] == expected["total_cents"],
        category=category == expected["category"],
        citations=(_doc_id(evidence.get("policy")), _doc_id(evidence.get("bulletin")))
        == (expected["policy_id"], expected["bulletin_id"]),
        ledger=query.get("ledger") == expected["ledger"] and required_tables <= quoted_tables
        and not (quoted_tables & {"orders", "refunds", "chargebacks"}) - required_tables,
        sql_bindings=bindings == expected_bindings,
        query_result=(type(query.get("net_cents")) is int and query["net_cents"] == expected["total_cents"]
                      and all(type(query.get(key)) is int and query[key] == expected[key]
                              for key in ("order_count", "gross_cents", "deducted_cents"))),
        tool_choices=_observation_signature(trace) == expected["tool_path"],
        one_read_only_query=metrics.get("sql_queries") == 1,
        has_expected_schema=(not schema if expected["ledger"] == "orders"
                             else isinstance(schema, dict) and schema.get("ledger") == expected["ledger"]),
    )
    return checks


def _run_case(database: Path, case: dict, mode: str, number: int, choose, cache) -> dict:
    from app.hard_policy_workflow import HardPolicyRequest, run_hard_policy

    response = {}
    trace = Trace("hard_policy_comparison", lambda _: None)
    started = perf_counter()
    error = None
    try:
        with trace.active(), tracing_context(enabled=False):
            response = run_hard_policy(database, HardPolicyRequest.model_validate(case["request"]),
                                       mode=mode, choose=choose, cache=cache)
        elapsed = round((perf_counter() - started) * 1000, 3)
        checks = _score(response, case, mode)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        elapsed = round((perf_counter() - started) * 1000, 3)
        checks = {"valid_response": False}
        error = type(exc).__name__
    usage = trace.finish("error" if error else "ok", error)
    metrics = response.get("metrics", {})
    calls = metrics.get("model_calls", usage["model_calls"])
    events = response.get("tool_trace", [])
    return {
        "id": case["id"], "group": case["group"], "round": number, "mode": mode, "request": case["request"],
        "expected": case["expected"], "passed": all(checks.values()), "checks": checks,
        "observed": {key: response.get(key) for key in ("kind", "reason", "total_cents", "evidence")},
        "model_calls": calls, "metered_model_calls": usage["model_calls_with_usage"],
        "tokens": {key: usage["usage"][key] if usage["model_calls_with_usage"] == calls else None
                   for key in TOKEN_FIELDS},
        "sql_queries": metrics.get("sql_queries", 0), "replans": metrics.get("replans", 0),
        "cache_hits": metrics.get("cache_hits", 0), "latency_ms": elapsed, "error_type": error,
        "observed_tool_path": _observation_signature(events), "plans": _plans(events),
        "tool_trace": events,
    }


def _score_summary(rows: list[dict]) -> dict:
    elapsed = [row["latency_ms"] for row in rows]
    tool_checks = [row["checks"]["tool_choices"] for row in rows if "tool_choices" in row["checks"]]
    plans = Counter(
        " → ".join(map(str, plan["sequence"])) for row in rows for plan in row["plans"]
        if plan["event"] in ("plan", "replan") and plan["status"] == "ok"
    )
    paths = Counter(" → ".join(row["observed_tool_path"]) for row in rows
                    if row["passed"] and row["expected"]["kind"] == "answer")
    return {
        "attempts": len(rows), "passed": sum(row["passed"] for row in rows),
        "median_ms": median(elapsed), "p95_ms": _p95(elapsed),
        "model_calls": sum(row["model_calls"] for row in rows),
        "metered_model_calls": sum(row["metered_model_calls"] for row in rows),
        "tokens": {key: _known_total(row["tokens"][key] for row in rows) for key in TOKEN_FIELDS},
        "sql_queries": sum(row["sql_queries"] for row in rows), "cache_hits": sum(row["cache_hits"] for row in rows),
        "replans": sum(row["replans"] for row in rows), "validated_model_plan_sequences": dict(plans),
        "validated_model_plan_diversity": len(plans), "successful_observed_tool_sequences": dict(paths),
        "successful_observed_tool_diversity": len(paths),
        "tool_sequence_correct": sum(tool_checks), "tool_sequence_applicable": len(tool_checks),
        "amount_attempts": sum(row["expected"]["kind"] == "answer" for row in rows),
        "verified_amount_answers": sum(row["passed"] and row["expected"]["kind"] == "answer" for row in rows),
        "failed_cases": [row["id"] for row in rows if not row["passed"]],
    }


def _summarize(rows: list[dict], modes: list[str]) -> dict:
    return {
        mode: {
            "overall": _score_summary([row for row in rows if row["mode"] == mode]),
            "by_group": {
                group: _score_summary([row for row in rows if row["mode"] == mode and row["group"] == group])
                for group in sorted({row["group"] for row in rows})
            },
        }
        for mode in modes
    }


def main() -> int:
    from app.hard_policy_data import prepare_hard_policy_database
    from app.hard_policy_workflow import HardPlanCache
    from app.multisource_workflow import multisource_selector

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.workers <= 8 or len(set(args.modes)) != len(args.modes):
        parser.error("Use 1–10 repeats, 1–8 workers and distinct modes")
    selector = settings = None
    if any(mode != "fixed" for mode in args.modes):
        if args.env_file:
            if not args.env_file.is_file():
                parser.error("--env-file must be an existing file")
            _load_dotenv(args.env_file)
        settings = replace(load_settings(), llm_model=args.model, llm_temperature=0)
        endpoint = urlsplit(settings.llm_base_url or "")
        if not settings.llm_configured or endpoint.scheme != "https" or not endpoint.hostname or any(
            (endpoint.username, endpoint.password, endpoint.query, endpoint.fragment),
        ):
            parser.error("Live model modes require an HTTPS model configuration")
        selector = multisource_selector(settings)
    suite = _load_suite()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "suite": suite["suite"], "holdout_cases": len(suite["holdout"]), "development_cases": len(suite["development"]),
        "modes": args.modes, "started_at_utc": datetime.now(timezone.utc).isoformat(), "order_seed": ORDER_SEED,
        "model": settings.llm_model if settings else None, "temperature": 0 if settings else None,
        "provider_host": urlsplit(settings.llm_base_url).hostname if settings else None,
        "workers": args.workers, "repeats": args.repeats, "source_sha256": _fingerprints(),
        "latency_method": "application call excluding verifier scoring and executor queue; P95 nearest rank",
        "cache_method": "new cache per round; prime only on distinct development scopes; report warm-up separately",
    }
    (args.output_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows, warmup = [], []
    with TemporaryDirectory() as temporary, ThreadPoolExecutor(max_workers=args.workers) as pool, (
        args.output_dir / "cases.jsonl"
    ).open("x", encoding="utf-8") as log:
        database = prepare_hard_policy_database(Path(temporary))
        for number in range(1, args.repeats + 1):
            cache = HardPlanCache()
            if "plan" in args.modes:
                for case in suite["development"]:
                    row = _run_case(database, case, "plan", number, selector, cache)
                    warmup.append(row)
                    print(f"{number} warmup {case['id']} {'PASS' if row['passed'] else 'FAIL'}", flush=True)
            tasks = [(case, mode) for case in suite["holdout"] for mode in args.modes]
            random.Random(ORDER_SEED + number).shuffle(tasks)
            # Sequential cached requests avoid nondeterministic first-fill races for new plan keys.
            track = []
            for case, mode in tasks:
                if mode == "plan":
                    track.append(_run_case(database, case, mode, number, selector, cache))
                else:
                    track.append(pool.submit(_run_case, database, case, mode, number, selector, None))
            for entry in track:
                row = entry if isinstance(entry, dict) else entry.result()
                rows.append(row)
                log.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                log.flush()
                print(f"{number} {row['mode']} {row['id']} {'PASS' if row['passed'] else 'FAIL'}", flush=True)
    summary = {"methods": _summarize(rows, args.modes), "cache_warmup": _score_summary(warmup) if warmup else None}
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if warmup:
        (args.output_dir / "warmup.jsonl").write_text("".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in warmup
        ), encoding="utf-8")
    for mode, stats in summary["methods"].items():
        score = stats["overall"]
        print(f"{mode}: {score['passed']}/{score['attempts']}, P95 {score['p95_ms']:.1f} ms", flush=True)
    return int(any(row["error_type"] or row["observed"]["reason"] in MODEL_STOP for row in rows + warmup))


if __name__ == "__main__":
    raise SystemExit(main())
