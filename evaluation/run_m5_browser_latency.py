"""Measure browser end-to-end latency for the normal policy path."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import uuid
from pathlib import Path
from time import perf_counter

from playwright.sync_api import sync_playwright

QUESTION = "华南电水壶 2026 年 10 月和 11 月的净营收差了多少？如果两个月都按 10 月口径重算，差额又是多少？"


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * .95) - 1)] if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:28502")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    timings = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            for _ in range(args.repeats):
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(f"{args.url}/?workspace=policy-exploration&session={uuid.uuid4()}", wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                page.locator("textarea").first.fill(QUESTION)
                began = perf_counter()
                page.get_by_role("button", name="开始调查").click()
                page.get_by_text("实际口径差额", exact=True).first.wait_for(timeout=60000)
                timings.append((perf_counter() - began) * 1000)
                page.close()
        finally:
            browser.close()
    report = {"suite": "m5-browser-latency-v1", "repeats": args.repeats, "samples": len(timings),
              "p50_ms": round(statistics.median(timings), 3), "p95_ms": round(_p95(timings), 3),
              "timings_ms": [round(value, 3) for value in timings]}
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
