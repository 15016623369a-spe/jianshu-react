"""Structured, content-free investigation telemetry."""
from __future__ import annotations

import json
import logging

_logger = logging.getLogger("jianshu.investigations")


def log_investigation(record: dict) -> None:
    """Log routing/outcome metrics without questions, quotes, or credentials."""
    metrics = record.get("metrics") or {}
    payload = {
        "event": "investigation",
        "investigation_id": record.get("investigation_id"),
        "domain": record.get("domain"),
        "status": record.get("status"),
        "reason": record.get("reason"),
        "parent_id": record.get("parent_investigation_id"),
        "evidence_count": len(record.get("evidence") or ()),
        "sql_queries": metrics.get("sql_queries", 0),
        "model_calls": metrics.get("model_calls", 0),
        "total_tokens": metrics.get("total_tokens", 0),
        "retrievals": metrics.get("retrievals", 0),
        "latency_ms": metrics.get("latency_ms", 0),
        "document_version": (record.get("document_version") or "")[:64],
        "data_version": (record.get("data_version") or "")[:64],
    }
    _logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
