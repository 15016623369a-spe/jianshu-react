"""Durable investigation records for the policy/manual workbench."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

_MAX_LIMIT = 100
_SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    domain TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    document_version TEXT,
    data_version TEXT,
    parent_id TEXT,
    result_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS investigations_owner_created
ON investigations(owner, created_at DESC);
"""


class InvestigationMissing(LookupError):
    """Missing and unauthorized records intentionally share one result."""


_STATUS = {
    "answer": "completed",
    "clarification": "clarification",
    "abstained": "insufficient",
}


class InvestigationHistory:
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, owner: str, domain: str, question: str, result: dict, *,
             data_version: str | None = None, parent_id: str | None = None) -> dict:
        identifier = str(uuid4())
        created_at = datetime.now(UTC).isoformat()
        status = _STATUS.get(result.get("kind"), "failed")
        record = {
            **result,
            "investigation_id": identifier,
            "status": status,
            "created_at": created_at,
            "domain": domain,
            "question": question,
            "data_version": data_version,
            "parent_investigation_id": parent_id,
        }
        scope = json.dumps(record.get("scope") or {}, ensure_ascii=False, separators=(",", ":"))
        evidence = json.dumps(record.get("evidence") or [], ensure_ascii=False, separators=(",", ":"))
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO investigations "
                "(id, owner, domain, question, status, created_at, scope_json, evidence_json, document_version, data_version, parent_id, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identifier, owner, domain, question, status, created_at, scope, evidence,
                 record.get("document_version"), data_version, parent_id, payload),
            )
        return record

    def get(self, owner: str, identifier: str) -> dict:
        try:
            UUID(identifier)
        except ValueError:
            raise InvestigationMissing from None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT result_json FROM investigations WHERE id = ? AND owner = ?",
                (identifier, owner),
            ).fetchone()
        if row is None:
            raise InvestigationMissing
        return json.loads(row["result_json"])

    def list(self, owner: str, *, limit: int = 20, before: str | None = None) -> dict:
        limit = max(1, min(limit, _MAX_LIMIT))
        query = "SELECT result_json FROM investigations WHERE owner = ?"
        args: list[object] = [owner]
        if before:
            query += " AND created_at < ?"
            args.append(before)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit + 1)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, args).fetchall()
        items = [json.loads(row["result_json"]) for row in rows[:limit]]
        return {
            "investigations": items,
            "next_before": items[-1]["created_at"] if len(rows) > limit and items else None,
        }


def _money(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "−" if cents < 0 else ""
    return f"{sign}¥{abs(cents) / 100:,.2f}"


def _entry_net(entry: dict) -> int:
    if "net_cents" in entry:
        return int(entry["net_cents"])
    return sum(segment["query"]["net_cents"] for segment in entry.get("segments", ()))


def markdown_report(record: dict) -> str:
    lines = [
        "# 见数调查记录", "",
        f"- 调查 ID：`{record['investigation_id']}`",
        f"- 状态：{record['status']}",
        f"- 领域：{record['domain']}",
        f"- 问题：{record['question']}",
        f"- 创建时间：{record['created_at']}",
        f"- 文档版本：{record.get('document_version') or '—'}",
        f"- 数据版本：{record.get('data_version') or '—'}",
        "", "## 结论", "",
    ]
    if record.get("kind") == "answer":
        if record.get("single_period_delta_cents") is not None:
            actual = _entry_net(record["actual"][0])
            unified = _entry_net(record["counterfactual"][0])
            lines.extend([
                f"- 实际净营收：{_money(actual)}",
                f"- 统一口径净营收：{_money(unified)}",
                f"- 实际减统一：{_money(record['single_period_delta_cents'])}",
            ])
        else:
            lines.extend([
                f"- 实际差额：{_money(record.get('actual_delta_cents'))}",
                f"- 统一口径差额：{_money(record.get('counterfactual_delta_cents'))}",
                f"- 口径影响：{_money(record.get('policy_effect_cents'))}",
            ])
        lines.extend(["", record.get("message") or ""])
    else:
        lines.extend([f"{record.get('message') or '未完成'}", "", f"原因：`{record.get('reason') or '—'}`"])
    scope = record.get("scope") or {}
    if scope:
        lines.extend(["", "## 范围", "",
                      f"- 商品：{scope.get('product') or '—'}",
                      f"- 地区：{scope.get('region') or '—'}",
                      f"- 期间数：{len(scope.get('periods') or [])}"])
    if record.get("actual"):
        lines.extend(["", "## 金额明细", "",
                      "| 场景 | 期间 | 账本 | 上限 | 订单 | 毛营收 | 扣减 | 净营收 |",
                      "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |"])
        for scenario, entries in (("实际", record.get("actual", [])), ("统一", record.get("counterfactual", []))):
            for entry in entries:
                for segment in entry.get("segments", []):
                    query, rule = segment["query"], segment["rule"]
                    lines.append(
                        f"| {scenario} | {entry['period']['start']} 至 {entry['period']['end_exclusive']} "
                        f"| {rule['ledger']} | {_money(rule.get('cap_cents'))} | {query['order_count']} "
                        f"| {_money(query['gross_cents'])} | {_money(query['deducted_cents'])} | {_money(query['net_cents'])} |"
                    )
    if record.get("evidence"):
        lines.extend(["", "## 证据", ""])
        for item in record["evidence"]:
            lines.append(f"- [{item['source']}] {item['title']} · `{item['file']}:{item['line_start']}`  "
                         f"第 {item['version']} 版")
            lines.append(f"  > {item['quote']}")
    metrics = record.get("metrics") or {}
    lines.extend(["", "## 执行摘要", "",
                  f"- SQL：{metrics.get('sql_queries', 0)} 次",
                  f"- 检索：{metrics.get('retrievals', 0)} 次",
                  f"- 模型调用：{metrics.get('model_calls', 0)} 次",
                  f"- 耗时：{metrics.get('latency_ms', 0)} ms"])
    return "\n".join(lines).rstrip() + "\n"


def csv_report(record: dict) -> str:
    import csv
    import io

    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["investigation_id", "status", "domain", "question", "scenario", "period_start",
                     "period_end_exclusive", "ledger", "cap_cents", "order_count", "gross_cents",
                     "deducted_cents", "net_cents"])
    for scenario, entries in (("actual", record.get("actual", [])), ("counterfactual", record.get("counterfactual", []))):
        for entry in entries:
            for segment in entry.get("segments", []):
                query, rule = segment["query"], segment["rule"]
                writer.writerow([record["investigation_id"], record["status"], record["domain"], record["question"],
                                 scenario, entry["period"]["start"], entry["period"]["end_exclusive"],
                                 rule["ledger"], rule.get("cap_cents"), query["order_count"],
                                 query["gross_cents"], query["deducted_cents"], query["net_cents"]])
    return stream.getvalue()
