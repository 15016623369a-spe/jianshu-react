"""Measure evidence retrieval separately from grounded SQL answers on fresh documents."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from langsmith import tracing_context

from app.config import PROJECT_ROOT, _load_dotenv, load_settings
from app.hard_policy_data import prepare_hard_policy_database
from app.multisource_workflow import multisource_selector
from evaluation.run_policy_investigation import (
    _AMOUNTS, _DELTAS, _MEASURES, _comparators, _compare_scenario,
    _exact_citation, _expected_scope, _expected_sums, _fixture_amounts, _summarize,
)

HOLDOUT = Path(__file__).with_name("policy_grounding_holdout")
MODES = ("fixed", "lexical", "auto", "model")
_K = (1, 3, 5)
_ORDER_SEED = 280716
_ROUTE_METRICS = ("lexical_hits", "model_fallbacks")


def _read(name: str):
    return json.loads((HOLDOUT / f"{name}.json").read_text(encoding="utf-8"))


def _documents() -> dict[str, list[str]]:
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): path.read_text(encoding="utf-8").splitlines()
        for path in sorted(HOLDOUT.glob("*.md"))
    }


def _load_cases(documents: dict[str, list[str]]) -> list[dict]:
    cases = _read("cases")["cases"]
    for field in ("id", "question"):
        if len({case[field] for case in cases}) != len(cases):
            raise ValueError(f"Expanded holdout contains duplicate {field}")
    document_ids = Counter(
        line.removeprefix("文号：").removesuffix("。")
        for source in documents.values() for line in source if line.startswith("文号：")
    )
    ledgers = {name: defaultdict(int) for name in ("refunds", "chargebacks")}
    for name, ledger in ledgers.items():
        for row in _read(name):
            ledger[row["order_id"]] += row["amount_cents"]

    orders = _read("orders")
    for case in cases:
        expected = case["expected"]
        if expected["kind"] != "answer":
            continue
        for scenario in ("actual", "counterfactual"):
            for segments in expected[scenario]:
                for segment in segments:
                    raw = _fixture_amounts(segment, expected, orders, ledgers)
                    if raw != {key: segment[key] for key in _AMOUNTS}:
                        raise ValueError(f"Golden amount does not match ledger rows for {case['id']}")
                    if any(document_ids[identifier] != 1 for identifier in segment["document_ids"]):
                        raise ValueError(f"Golden citations must identify unique documents for {case['id']}")
        actual, uniform = (
            [_expected_sums(segments)["net_cents"] for segments in expected[scenario]]
            for scenario in ("actual", "counterfactual")
        )
        delta = actual[1] - actual[0] if len(actual) == 2 else None
        uniform_delta = uniform[1] - uniform[0] if uniform else None
        effect = delta - uniform_delta if uniform_delta is not None else None
        if (delta, uniform_delta, effect) != tuple(expected[key] for key in _DELTAS):
            raise ValueError(f"Golden comparison does not match ledger rows for {case['id']}")
    return cases


def _gold_retrieval(expected: dict) -> set[tuple[str, str | None, str, str, str]]:
    """Each actual span contributes a policy unit and its ordered reference units."""
    return {
        ("policy" if index == 0 else "bulletin", ids[index - 1] if index else None,
         segment["period"]["start"], segment["period"]["end_exclusive"], identifier)
        for group in expected.get("actual", []) for segment in group
        for ids in (segment["document_ids"],) for index, identifier in enumerate(ids)
    }


def _covers(start: str, end: str, spans: list[tuple[str, str]]) -> bool:
    """A golden span may be split by other document boundaries at run time."""
    cursor = start
    for first, last in sorted(spans):
        if first > cursor:
            break
        cursor = max(cursor, last)
        if cursor >= end:
            return True
    return False


def _retrieval_counts(expected: dict, trace: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys(
        ("expected", "selected", *(f"{stage}_hit_at_{k}" for stage in ("first", "any") for k in _K)), 0
    )
    gold = _gold_retrieval(expected)
    counts["expected"] = len(gold)
    for source, referrer, start, end, identifier in gold:
        events = [
            event for event in trace if event["source"] == source and event.get("referrer_id") == referrer
            and event["scope"]["start"] < end and event["scope"]["end_exclusive"] > start
        ]
        selected, ranks = [], defaultdict(list)
        for event in events:
            span = (event["scope"]["start"], event["scope"]["end_exclusive"])
            if (event.get("selected") or {}).get("id") == identifier:
                selected.append(span)
            for k in _K:
                if identifier in {item.get("id") for item in event["candidates"][:k]}:
                    ranks[f"any_hit_at_{k}"].append(span)
                    if event["stage"] == "exact":
                        ranks[f"first_hit_at_{k}"].append(span)
        counts["selected"] += _covers(start, end, selected)
        for key, spans in ranks.items():
            counts[key] += _covers(start, end, spans)
    return counts


def _citation_counts(expected: dict, result: dict, document_text: dict[str, list[str]]) -> dict[str, int]:
    gold, verified = set(), set()
    predicted = 0
    for scenario in ("actual", "counterfactual"):
        for group in expected.get(scenario, []):
            for segment in group:
                period = segment["period"]
                gold.update((scenario, period["start"], period["end_exclusive"], identifier)
                            for identifier in segment["document_ids"])
        for group in result.get(scenario) or []:
            for segment in group.get("segments") or []:
                period = segment["period"]
                for document in segment.get("documents") or []:
                    predicted += 1
                    if _exact_citation(document, document_text):
                        verified.add((scenario, period["start"], period["end_exclusive"], document["id"]))
    return {"expected": len(gold), "predicted": predicted, "correct": len(gold & verified)}


def _score(case: dict, result: dict, mode: str, documents: dict[str, list[str]]) -> dict:
    expected = case["expected"]
    checks = {
        "mode": result.get("mode") == mode and (mode in ("auto", "model") or not result["metrics"]["model_calls"]),
        "disposition": result.get("kind") == expected["kind"],
    }
    if "reason" in expected:
        checks["disposition"] &= result.get("reason") == expected["reason"]
    numeric = bool(result.get("actual") or result.get("counterfactual")
                   or any(result.get(key) is not None for key in _DELTAS))
    if expected["kind"] != "answer" or result.get("kind") != "answer":
        checks["safe_no_amount"] = not numeric
        checks["no_query_before_stop"] = result["metrics"]["sql_queries"] == 0
        incorrect = numeric
    else:
        checks["scope"] = all(result["scope"].get(key) == value for key, value in _expected_scope(expected).items())
        checks["snapshot_version"] = bool(result.get("document_version"))
        outcomes = [_compare_scenario(case, result, documents, scenario) for scenario in ("actual", "counterfactual")]
        checks.update({field: all(outcome[index] for outcome in outcomes)
                       for index, field in enumerate(("amount", "evidence", "sql"))})
        checks["deltas"] = all(type(result.get(key)) is type(expected[key]) and result.get(key) == expected[key]
                              for key in _DELTAS)
        incorrect = numeric and not all(checks[key] for key in ("scope", "amount", "evidence", "sql", "deltas"))
    trace = result.get("retrieval_trace")
    if not isinstance(trace, list):
        raise ValueError("Runtime did not return a retrieval candidate trace")
    counts = _retrieval_counts(expected, trace)
    citations = _citation_counts(expected, result, documents)
    return {
        "passed": all(checks.values()), "checks": checks, "numeric_output": numeric,
        "incorrect_numeric_output": incorrect, "retrieval": counts, "citations": citations,
    }


def _proportion(num: int, denominator: int) -> float | None:
    return round(num / denominator, 6) if denominator else None


def _grounding_summary(rows: list[dict]) -> dict:
    retrieval = Counter()
    citations = Counter()
    for row in rows:
        retrieval.update(row["retrieval"])
        citations.update(row["citations"])
    summary = _summarize(rows)
    summary["retrieval_units"] = retrieval["expected"]
    summary["first_pass_candidate_recall_at_k"] = {str(k): _proportion(retrieval[f"first_hit_at_{k}"], retrieval["expected"])
                                                   for k in _K}
    summary["all_stage_candidate_recall_at_k"] = {str(k): _proportion(retrieval[f"any_hit_at_{k}"], retrieval["expected"])
                                                  for k in _K}
    summary["verified_selection_recall"] = _proportion(retrieval["selected"], retrieval["expected"])
    summary["citation_counts"] = dict(citations)
    summary["citation_precision"] = _proportion(citations["correct"], citations["predicted"])
    summary["citation_recall"] = _proportion(citations["correct"], citations["expected"])
    summary.update({key: sum(row[key] for row in rows) for key in _ROUTE_METRICS})
    return summary


def _score_all(database: Path, snapshot, cases: list[dict], documents: dict, args, choose):
    from app.policy_investigation import InvestigationRequest, run_policy_investigation

    for repeat in range(1, args.repeats + 1):
        tasks = [(case, mode) for case in cases for mode in args.modes]
        random.Random(_ORDER_SEED + repeat).shuffle(tasks)
        for case, mode in tasks:
            start = perf_counter()
            with tracing_context(enabled=False):
                result = run_policy_investigation(
                    database, InvestigationRequest(question=case["question"]), mode=mode,
                    snapshot=snapshot, choose=choose if mode in ("auto", "model") else None,
                )
            latency = (perf_counter() - start) * 1000
            metrics = result["metrics"]
            yield {
                "case": case["id"], "family": case["family"], "group": case["group"],
                "expected_path": case.get("expected_path"), "expected_kind": case["expected"]["kind"],
                "mode": mode, "round": repeat, "kind": result["kind"], "reason": result["reason"],
                "latency_ms": round(latency, 3),
                "total_tokens": metrics.get("total_tokens") if metrics.get("usage_complete", True) else None,
                **{key: metrics.get(key, 0) for key in (*_MEASURES, *_ROUTE_METRICS)},
                **_score(case, result, mode, documents),
            }


def _fingerprint() -> dict[str, str]:
    modules = (
        "app/hard_policy_data.py", "app/policy_investigation.py", "app/policy_investigation_retrieval.py",
        "app/multisource_workflow.py", "app/llm.py", "app/tools/db.py", "evaluation/run_policy_investigation.py",
    )
    sources = (*HOLDOUT.iterdir(), *(PROJECT_ROOT / name for name in modules), Path(__file__))
    return {path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(sources) if path.is_file()}


def main() -> int:
    from app.policy_investigation_retrieval import load_investigation_snapshot

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=["fixed", "lexical", "auto"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--offline", action="store_true", help="Exercise the selected modes without an external model.")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or len(set(args.modes)) != len(args.modes):
        parser.error("Use distinct modes and 1–10 repeats.")
    settings = choose = None
    if set(args.modes) & {"auto", "model"} and not args.offline:
        if args.env_file:
            if not args.env_file.is_file():
                parser.error("--env-file must be an existing file.")
            _load_dotenv(args.env_file)
        settings = replace(load_settings(), llm_model=args.model, llm_temperature=0)
        endpoint = urlsplit(settings.llm_base_url or "")
        if not settings.llm_configured or endpoint.scheme != "https" or not endpoint.hostname or any(
            (endpoint.username, endpoint.password, endpoint.query, endpoint.fragment),
        ):
            parser.error("Live model modes require configured credentials and an HTTPS endpoint.")
        choose = multisource_selector(settings)

    documents = _documents()
    cases = _load_cases(documents)
    snapshot = load_investigation_snapshot(directory=HOLDOUT)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    run = {
        "suite": "policy-grounding-frozen-expansion", "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "modes": args.modes, "repeats": args.repeats, "case_count": len(cases),
        "document_families": sorted({case["family"] for case in cases} - {"request"}),
        "passage_count": len(snapshot.passages), "document_version": snapshot.version,
        "source_sha256": _fingerprint(), "model": settings.llm_model if settings else None,
        "temperature": 0 if settings else None, "order_seed": _ORDER_SEED,
        "latency_method": "sequential application wall time before scoring; nearest-rank P95",
        "retrieval_method": (
            "Micro-average per actual authority link (start, end, source, referrer, document). Candidate hits require "
            "whole-span coverage by intersecting queries from the same authority link. First pass uses only exact events. "
            "All-stage recall accepts exact, lexical and model events. Selected authority is counted separately."
        ),
        "citation_method": (
            "Micro-average per scenario, calculation span and document. A predicted claim counts as correct only when "
            "it agrees with the labeled authority and the entire Markdown title, line range and body match."
        ),
    }
    (args.output_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with TemporaryDirectory() as temporary:
        with patch("app.hard_policy_data._CORPUS", HOLDOUT):
            database = prepare_hard_policy_database(Path(temporary))
        rows = []
        with (args.output_dir / "cases.jsonl").open("x", encoding="utf-8") as log:
            for row in _score_all(database, snapshot, cases, documents, args, choose):
                rows.append(row)
                log.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                log.flush()
                failed = ",".join(check for check, passed in row["checks"].items() if not passed)
                disposition = "PASS" if row["passed"] else f"FAIL {row['reason'] or failed}"
                print(f"{row['round']} {row['mode']} {row['case']} {disposition}", flush=True)
    summary = {
        "modes": {
            mode: {
                "overall": _grounding_summary([row for row in rows if row["mode"] == mode]),
                "by_expected_path": {
                    path: _grounding_summary([row for row in rows if row["mode"] == mode and row["expected_path"] == path])
                    for path in ("none", "lexical", "model", None)
                },
            }
            for mode in args.modes
        },
        "paired": _comparators(rows, args.modes),
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(any(row["reason"] in {"model_error", "timeout", "source_unavailable"} for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
