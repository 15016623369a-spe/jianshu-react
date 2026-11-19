"""Measure HTTP end-to-end latency for normal and model-fallback M5 cases."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from time import perf_counter

import httpx

ROOT = Path(__file__).with_name("policy_grounding_holdout")
CASES = [
    {"id": "flagship", "question": "华南电水壶 2026 年 10 月和 11 月的净营收差了多少？如果两个月都按 10 月口径重算，差额又是多少？"},
    {"id": "a1", "question": "2027 年 1 月华南电水壶净营收是多少？如果都按 2026 年 10 月口径重算呢？"},
    {"id": "a3", "question": "华南电水壶 2026 年 10 月比 11 月高多少？如果都按 10 月口径重算呢？"},
]


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * .95) - 1)] if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:28002")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    selected = CASES
    rows = []
    with httpx.Client(trust_env=False, timeout=120) as client:
        for _ in range(args.repeats):
            for case in selected:
                began = perf_counter()
                response = client.post(args.base_url + "/api/v3/investigations", json={
                    "domain": "policy", "question": case["question"],
                })
                elapsed = (perf_counter() - began) * 1000
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                rows.append({"id": case["id"], "fallback": False,
                             "status": response.status_code, "kind": body.get("kind"), "latency_ms": elapsed})
    normal = [row["latency_ms"] for row in rows if not row["fallback"]]
    fallback = [row["latency_ms"] for row in rows if row["fallback"]]
    report = {
        "suite": "m5-http-latency-v1", "base_url": args.base_url, "repeats": args.repeats,
        "cases": len(rows), "normal_cases": len(normal), "fallback_cases": len(fallback),
        "normal_p50_ms": round(statistics.median(normal), 3) if normal else 0.0,
        "normal_p95_ms": round(_p95(normal), 3),
        "fallback_p50_ms": round(statistics.median(fallback), 3) if fallback else 0.0,
        "fallback_p95_ms": round(_p95(fallback), 3),
        "errors": sum(1 for row in rows if row["status"] != 200 or row["kind"] != "answer"),
        "rows": rows,
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False))
    return 0 if report["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
