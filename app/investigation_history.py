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
             data_version: str | None = None) -> dict:
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
        }
        scope = json.dumps(record.get("scope") or {}, ensure_ascii=False, separators=(",", ":"))
        evidence = json.dumps(record.get("evidence") or [], ensure_ascii=False, separators=(",", ":"))
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO investigations "
                "(id, owner, domain, question, status, created_at, scope_json, evidence_json, document_version, data_version, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identifier, owner, domain, question, status, created_at, scope, evidence,
                 record.get("document_version"), data_version, payload),
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
