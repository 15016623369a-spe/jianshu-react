"""Measure bounded concurrency behavior of the investigation API."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
from pathlib import Path
from time import perf_counter

import httpx

CASES = (
    {"domain": "policy", "question": "2027 年 1 月华南电水壶净营收是多少？如果都按 2026 年 10 月口径重算呢？"},
    {"domain": "retail", "question": "2026 年 10 月华南电水壶净营收是多少？"},
    {"domain": "manual", "question": "怎样连接 Wi-Fi？", "version": 2},
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:28002")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    def call(index: int):
        began = perf_counter()
        try:
            with httpx.Client(trust_env=False, timeout=120) as client:
                response = client.post(args.base_url + "/api/v3/investigations", json=CASES[index % len(CASES)])
            return {"status": response.status_code, "kind": (response.json() if response.status_code == 200 else None).get("kind") if response.status_code == 200 else None,
                    "latency_ms": (perf_counter() - began) * 1000}
        except Exception as exc:
            return {"status": type(exc).__name__, "kind": None, "latency_ms": (perf_counter() - began) * 1000}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(call, range(args.requests)))
    success = [row for row in rows if row["status"] == 200]
    latencies = sorted(row["latency_ms"] for row in success)
    report = {
        "suite": "m6-concurrency-v1", "workers": args.workers, "requests": args.requests,
        "success": len(success), "load_shed_503": sum(1 for row in rows if row["status"] == 503),
        "errors": [row for row in rows if row["status"] not in (200, 503)],
        "p50_ms": round(statistics.median(latencies), 3) if latencies else 0,
        "p95_ms": round(latencies[max(0, int(len(latencies) * .95) - 1)], 3) if latencies else 0,
        "rows": rows,
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
