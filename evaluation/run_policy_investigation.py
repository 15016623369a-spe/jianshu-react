"""Compare policy investigation on a frozen, separately authored document corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from langsmith import tracing_context

from app.config import PROJECT_ROOT, _load_dotenv, load_settings
from app.hard_policy_data import prepare_hard_policy_database
from app.multisource_workflow import multisource_selector
from evaluation.run_policy_comparison import _p95

HOLDOUT = Path(__file__).with_name("policy_investigation_holdout")
MODES = ("fixed", "auto", "lexical")
_ORDER_SEED = 710318
_AMOUNTS = ("order_count", "gross_cents", "deducted_cents", "net_cents")
_DELTAS = ("actual_delta_cents", "counterfactual_delta_cents", "policy_effect_cents")
_MEASURES = ("model_calls", "sql_queries", "retrievals", "rewrites")


def _read(name: str):
    return json.loads((HOLDOUT / f"{name}.json").read_text(encoding="utf-8"))


def _expected_scope(expected: dict) -> dict:
    periods = [{"start": group[0]["period"]["start"], "end_exclusive": group[-1]["period"]["end_exclusive"]}
               for group in expected["actual"]]
    return {key: expected[key] for key in ("product", "region", "baseline_period")} | {"periods": periods}


def _expected_sums(expected_segments: list[dict]) -> dict:
    return {key: sum(segment[key] for segment in expected_segments) for key in _AMOUNTS}


def _fixture_amounts(segment: dict, expected: dict, orders: Iterable[dict], ledgers: dict) -> dict:
    """Independent cent arithmetic from the raw rows, using human-labeled rules."""
    values = dict.fromkeys(_AMOUNTS, 0)
    period = segment["period"]
    cap = segment["cap_cents"]
    for order in orders:
        if (order["product"] != expected["product"] or order["region"] != expected["region"]
                or not period["start"] <= order["ordered_on"] < period["end_exclusive"]):
            continue
        ledger = segment["ledger"]
        deduction = order["refund_cents"] if ledger == "orders" else ledgers[ledger].get(order["id"], 0)
        if cap is not None:
            deduction = min(cap, deduction)
        values["order_count"] += 1
        values["gross_cents"] += order["amount_cents"]
        values["deducted_cents"] += deduction
        values["net_cents"] += order["amount_cents"] - deduction
    return values


def _load_cases() -> list[dict]:
    cases = _read("cases")["cases"]
    if len({case["id"] for case in cases}) != len(cases) or len({case["question"] for case in cases}) != len(cases):
        raise ValueError("Holdout cases must have unique IDs and question text")
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
                        raise ValueError(f"Frozen monetary labels do not match independent calculation: {case['id']}")
        actual = [_expected_sums(segments)["net_cents"] for segments in expected["actual"]]
        uniform = [_expected_sums(segments)["net_cents"] for segments in expected["counterfactual"]]
        delta = actual[1] - actual[0]
        uniform_delta = uniform[1] - uniform[0] if uniform else None
        effect = delta - uniform_delta if uniform_delta is not None else None
        if (delta, uniform_delta, effect) != tuple(expected[key] for key in _DELTAS):
            raise ValueError(f"Frozen delta labels do not match independent calculation: {case['id']}")
    return cases


def _raw_documents() -> dict[str, list[str]]:
    return {path.relative_to(PROJECT_ROOT).as_posix(): path.read_text(encoding="utf-8").splitlines()
            for path in sorted(HOLDOUT.glob("*.md"))}


def _exact_citation(doc: dict, documents: dict[str, list[str]]) -> bool:
    source = documents.get(doc.get("file"))
    start, end = doc.get("line_start"), doc.get("line_end")
    if source is None or type(start) is not int or type(end) is not int or not 1 <= start < end <= len(source):
        return False
    title = source[start - 1]
    body = source[start:end]
    next_line = next((line for line in source[end:] if line.strip()), "## ")
    return (
        title == f"## {doc.get('title')}" and bool(body[-1].strip())
        and f"文号：{doc.get('id')}。" in body and not any(line.startswith("## ") for line in body)
        and "\n".join(body).lstrip("\n") == doc.get("quote") and next_line.startswith("## ")
    )


def _compare_scenario(
    case: dict, result: dict, document_text: dict[str, list[str]], scenario: str,
) -> tuple[bool, bool, bool]:
    expected = case["expected"]
    groups, got = expected[scenario], result.get(scenario)
    if not isinstance(got, list) or len(got) != len(groups):
        return False, False, False
    amount_ok = evidence_ok = sql_ok = True
    for segments, actual_group in zip(groups, got, strict=True):
        aggregate = _expected_sums(segments)
        period = {"start": segments[0]["period"]["start"], "end_exclusive": segments[-1]["period"]["end_exclusive"]}
        actual_segments = actual_group.get("segments")
        if actual_group.get("period") != period or not isinstance(actual_segments, list) or len(actual_segments) != len(segments):
            return False, False, False
        amount_ok &= all(type(actual_group.get(key)) is int and actual_group[key] == value
                         for key, value in aggregate.items())
        for expected_segment, actual in zip(segments, actual_segments, strict=True):
            rule, documents, query = (actual.get("rule") or {}, actual.get("documents") or [], actual.get("query") or {})
            ledger, cap = expected_segment["ledger"], expected_segment["cap_cents"]
            bindings = [expected["product"], expected["region"], *expected_segment["period"].values()]
            if cap is not None:
                bindings.append(cap)
            used_tables = set(re.findall(r'\b(?:FROM|JOIN)\s+"?([a-z_]+)"?', query.get("sql", ""), flags=re.I))
            required_tables = {"orders", ledger}
            evidence_ok &= (
                actual.get("period") == expected_segment["period"]
                and [document.get("id") for document in documents] == expected_segment["document_ids"]
                and all(_exact_citation(document, document_text) for document in documents)
                and rule.get("ledger") == ledger and rule.get("cap_cents") == cap
            )
            sql_ok &= (
                query.get("parameters") == bindings and query.get("ledger") == ledger
                and (used_tables & {"orders", "refunds", "chargebacks"}) == required_tables
                and all(type(query.get(key)) is int and query[key] == expected_segment[key] for key in _AMOUNTS)
            )
    return amount_ok, evidence_ok, sql_ok


def _score(case: dict, result: dict, mode: str, document_text: dict[str, list[str]]) -> dict:
    expected = case["expected"]
    metrics = result.get("metrics") or {}
    answer = result.get("kind") == "answer"
    checks = {
        "mode": result.get("mode") == mode and (mode == "auto" or not metrics.get("model_calls")),
        "disposition": result.get("kind") == expected["kind"],
    }
    if "reason" in expected:
        checks["disposition"] &= result.get("reason") == expected["reason"]
    numeric_output = bool(result.get("actual") or result.get("counterfactual")
                          or any(result.get(key) is not None for key in _DELTAS))
    if expected["kind"] != "answer" or not answer:
        checks["safe_no_amount"] = not numeric_output
        checks["no_query_before_stop"] = metrics.get("sql_queries") == 0
        return {"passed": all(checks.values()), "checks": checks,
                "numeric_output": numeric_output, "incorrect_numeric_output": numeric_output}
    checks["scope"] = isinstance(result.get("scope"), dict) and all(
        result["scope"].get(key) == value for key, value in _expected_scope(expected).items()
    )
    checks["snapshot_version"] = bool(result.get("document_version"))
    outcomes = [_compare_scenario(case, result, document_text, name) for name in ("actual", "counterfactual")]
    checks["amount"] = all(amount for amount, _, _ in outcomes)
    checks["evidence"] = all(evidence for _, evidence, _ in outcomes)
    checks["sql"] = all(sql for _, _, sql in outcomes)
    checks["deltas"] = all(type(result.get(key)) is type(expected[key]) and result.get(key) == expected[key]
                          for key in _DELTAS)
    if case.get("needs_recovery") and mode == "auto":
        checks["recovery_observed"] = bool(metrics.get("rewrites"))
    return {"passed": all(checks.values()), "checks": checks, "numeric_output": numeric_output,
            "incorrect_numeric_output": numeric_output and not all(checks[key] for key in ("scope", "amount", "evidence", "sql", "deltas"))}


def _evaluate(database, snapshot, cases, modes, repeats, choose):
    from app.policy_investigation import InvestigationRequest, run_policy_investigation

    document_text = _raw_documents()
    for repeat in range(1, repeats + 1):
        jobs = [(case, mode) for case in cases for mode in modes]
        random.Random(_ORDER_SEED + repeat).shuffle(jobs)
        for case, mode in jobs:
            started = perf_counter()
            with tracing_context(enabled=False):
                result = run_policy_investigation(
                    database, InvestigationRequest(question=case["question"]), mode=mode,
                    choose=choose if mode == "auto" else None, snapshot=snapshot,
                )
            elapsed_ms = (perf_counter() - started) * 1000
            metrics = result.get("metrics") or {}
            tokens = metrics.get("total_tokens") if metrics.get("usage_complete", True) else None
            yield {
                "case": case["id"], "family": case["family"], "group": case["group"],
                "calibration": case.get("calibration", False),
                "mode": mode, "round": repeat, "expected_kind": case["expected"]["kind"],
                "kind": result.get("kind"), "reason": result.get("reason"),
                "latency_ms": round(elapsed_ms, 3),
                "total_tokens": tokens if metrics.get("model_calls") else 0,
                **{key: metrics.get(key) for key in _MEASURES},
                **_score(case, result, mode, document_text),
            }


def _sum_known(values) -> int | None:
    values = list(values)
    return sum(values) if all(type(value) is int for value in values) else None


def _summarize(rows: list[dict]) -> dict:
    expected_answers = [row for row in rows if row["expected_kind"] == "answer"]
    answered = [row for row in rows if row["numeric_output"]]
    bad = sum(row["incorrect_numeric_output"] for row in rows)
    elapsed = [row["latency_ms"] for row in rows]
    return {
        "evaluated": len(rows), "passed": sum(row["passed"] for row in rows),
        "amount_cases": len(expected_answers), "amount_passed": sum(row["passed"] for row in expected_answers),
        "amount_correct": sum(bool(row["checks"].get("amount") and row["checks"].get("deltas")) for row in expected_answers),
        "citations_correct": sum(bool(row["checks"].get("evidence")) for row in expected_answers),
        "sql_bound_correct": sum(bool(row["checks"].get("sql")) for row in expected_answers),
        "safe_expected_stops": sum(row["passed"] for row in rows if row["expected_kind"] != "answer"),
        "numeric_outputs": len(answered), "incorrect_numeric_outputs": bad,
        "incorrect_numeric_output_rate": bad / len(answered) if answered else None,
        "median_ms": round(median(elapsed), 3), "p95_ms": round(_p95(elapsed), 3),
        "total_tokens": _sum_known(row["total_tokens"] for row in rows),
        **{key: _sum_known(row[key] for row in rows) for key in _MEASURES},
        "stops_by_reason": dict(sorted(Counter(row["reason"] or "unspecified" for row in rows if row["kind"] != "answer").items())),
        "failed_cases": sorted({row["case"] for row in rows if not row["passed"]}),
    }


def _comparators(rows: list[dict], modes: list[str]) -> dict:
    by_mode = {mode: {(row["case"], row["round"]): row["passed"] for row in rows if row["mode"] == mode}
               for mode in modes}
    return {
        f"auto_vs_{other}": {
            "improved": sum(by_mode["auto"][key] and not scored for key, scored in by_mode[other].items()),
            "regressed": sum(scored and not by_mode["auto"][key] for key, scored in by_mode[other].items()),
        }
        for other in modes if other != "auto" and "auto" in modes
    }


def _fingerprint() -> dict[str, str]:
    sources = [
        *HOLDOUT.iterdir(), *Path("app").glob("policy_investigation*.py"), Path(__file__),
        Path("app/hard_policy_data.py"), Path("app/multisource_workflow.py"), Path("app/llm.py"), Path("app/tools/db.py"),
    ]
    return {path.resolve().relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(sources, key=lambda path: path.name) if path.is_file()}


def main() -> int:
    from app.policy_investigation_retrieval import load_investigation_snapshot

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=["fixed", "lexical", "auto"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--offline-auto", action="store_true", help="Exercise auto without recovery model.")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or len(set(args.modes)) != len(args.modes):
        parser.error("Use 1–10 repeats and distinct modes.")
    choose = settings = None
    if "auto" in args.modes and not args.offline_auto:
        if args.env_file:
            if not args.env_file.is_file():
                parser.error("--env-file must be an existing file.")
            _load_dotenv(args.env_file)
        settings = replace(load_settings(), llm_model=args.model, llm_temperature=0)
        endpoint = urlsplit(settings.llm_base_url or "")
        if not settings.llm_configured or endpoint.scheme != "https" or not endpoint.hostname or any(
            (endpoint.username, endpoint.password, endpoint.query, endpoint.fragment),
        ):
            parser.error("Live auto requires model credentials and a safe HTTPS endpoint.")
        choose = multisource_selector(settings)
    cases = _load_cases()
    snapshot = load_investigation_snapshot(directory=HOLDOUT)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    run = {
        "suite": "policy-investigation-independent-documents", "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode_order": args.modes, "case_count": len(cases), "repeats": args.repeats,
        "calibration_cases": [case["id"] for case in cases if case.get("calibration")],
        "document_families": sorted({case["family"] for case in cases} - {"request"}),
        "order_seed": _ORDER_SEED, "sequential_execution": True,
        "source_sha256": _fingerprint(), "document_version": snapshot.version,
        "model": settings.llm_model if settings else None, "temperature": 0 if settings else None,
        "latency_method": "application wall clock before independent scoring; nearest-rank P95",
    }
    (args.output_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with TemporaryDirectory() as temporary:
        with patch("app.hard_policy_data._CORPUS", HOLDOUT):
            database = prepare_hard_policy_database(Path(temporary))
        rows = []
        with (args.output_dir / "cases.jsonl").open("x", encoding="utf-8") as log:
            for row in _evaluate(database, snapshot, cases, args.modes, args.repeats, choose):
                rows.append(row)
                log.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                log.flush()
                failed_checks = ",".join(check for check, ok in row["checks"].items() if not ok)
                disposition = "PASS" if row["passed"] else f"FAIL {row['reason'] or failed_checks}"
                print(f"{row['round']} {row['mode']} {row['case']} {disposition}", flush=True)
    primary = [row for row in rows if not row["calibration"]]
    summary = {
        "modes": {
            mode: {
                "primary": _summarize([row for row in primary if row["mode"] == mode]),
                "overall": _summarize([row for row in rows if row["mode"] == mode]),
                "by_group": {group: _summarize([row for row in rows if (row["mode"], row["group"]) == (mode, group)])
                             for group in sorted({case["group"] for case in cases})},
            }
            for mode in args.modes
        },
        "paired_primary": _comparators(primary, args.modes),
        "paired_overall": _comparators(rows, args.modes),
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(any(row["reason"] in {"model_error", "timeout", "source_unavailable"} for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
