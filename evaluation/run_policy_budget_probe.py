"""Test unchanged policy modes against a separate, three-version policy fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from langsmith import tracing_context

from app import policy_workflow
from app.commerce_demo import prepare_database
from app.config import PROJECT_ROOT, _load_dotenv, load_settings
from app.policy_react import deepseek_policy_selector

POLICIES = (
    ("refund-2026-10-a", "2026-10-01", "2026-10-15", None, "净营收等于订单金额减去该订单全部退款。"),
    ("refund-2026-10-b", "2026-10-15", "2026-10-21", 300, "每笔订单最多扣除退款 3 元。"),
    ("refund-2026-10-c", "2026-10-21", "2026-12-01", 100, "每笔订单最多扣除退款 1 元。"),
)

SCOPES = (
    ("three_policies", "2026-10-01", "2026-11-01", [35000, 5000, 7900]),
    ("two_policies", "2026-10-15", "2026-11-01", [5000, 7900]),
)

def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--env-file", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    args = cli.parse_args()
    if not args.env_file.is_file():
        cli.error("--env-file must be an existing file")
    _load_dotenv(args.env_file)
    settings = replace(load_settings(), llm_model="deepseek-v4-pro", llm_temperature=0.0)
    endpoint = urlsplit(settings.llm_base_url or "")
    if not settings.llm_configured or endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com":
        cli.error("An HTTPS DeepSeek configuration is required")
    choose = deepseek_policy_selector(settings)
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Separate three-policy budget probe. Excluded from the expanded holdout.",
        "model": settings.llm_model,
        "temperature": settings.llm_temperature,
        "source_sha256": {
            path: hashlib.sha256((PROJECT_ROOT / path).read_bytes()).hexdigest()
            for path in ["app/commerce_demo.py", "app/llm.py", "app/policy_workflow.py", "app/policy_react.py",
                         "app/tools/db.py"]
        },
        "policies": [],
        "cases": [],
    }
    with TemporaryDirectory() as temporary_dir:
        root = Path(temporary_dir)
        policies = root / "policy_example"
        policies.mkdir()
        database = prepare_database(root)
        for policy_id, start, end, cap, clause in POLICIES:
            metadata = {
                "id": policy_id, "effective_from": start, "effective_until": end,
                "max_refund_cents_per_order": cap,
            }
            report["policies"].append(metadata)
            (policies / f"{policy_id}.md").write_text(
                f"---\n{json.dumps(metadata)}\n---\n# 测试用退款政策 {policy_id}\n\n## 净营收口径\n\n{clause}\n",
                encoding="utf-8",
            )
        with patch.object(policy_workflow, "_POLICY_DIR", policies), tracing_context(enabled=False):
            for name, start, end, expected_parts in SCOPES:
                request = policy_workflow.PolicyRevenueRequest(start=start, end_exclusive=end)
                for mode in ("fixed", "hybrid", "react"):
                    result = policy_workflow.run_policy_workflow(database, request, mode=mode, choose=choose)
                    actual_parts = [item["revenue_cents"] for item in result["segments"]]
                    record = {
                        "id": name,
                        "request": request.model_dump(mode="json"),
                        "mode": mode,
                        "expected_segments_cents": expected_parts,
                        "expected_total_cents": sum(expected_parts),
                        "kind": result["kind"],
                        "strategy": result["strategy"],
                        "stop_reason": result["stop_reason"],
                        "total_cents": result["total_cents"],
                        "segments_cents": actual_parts,
                        "passed": result["kind"] == "answer" and actual_parts == expected_parts
                                  and result["total_cents"] == sum(expected_parts),
                        "metrics": result["metrics"],
                        "tool_trace": result["tool_trace"],
                    }
                    report["cases"].append(record)
                    print(f"{name} {mode} {record['kind']} {record['stop_reason']} "
                          f"total_cents={record['total_cents']} sql_queries={record['metrics']['sql_queries']} "
                          f"model_calls={record['metrics']['model_calls']}", flush=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
