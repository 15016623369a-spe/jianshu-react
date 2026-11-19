"""Investigation telemetry is structured and contains no content."""
from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import patch

from app.observability import log_investigation
from app.investigation_history import InvestigationHistory
from pathlib import Path
from tempfile import TemporaryDirectory


class ObservabilityTests(unittest.TestCase):
    def test_structured_log_omits_question_and_quotes(self):
        record = {
            "investigation_id": "id-1", "domain": "policy", "status": "completed", "reason": None,
            "parent_investigation_id": None, "evidence": [{"quote": "secret quote"}],
            "metrics": {"sql_queries": 2, "model_calls": 0, "total_tokens": 0, "retrievals": 2,
                        "latency_ms": 1.5}, "document_version": "v1", "data_version": "d1",
        }
        with patch("app.observability._logger") as logger:
            log_investigation(record)
        payload = json.loads(logger.info.call_args.args[0])
        self.assertEqual(payload["event"], "investigation")
        self.assertEqual(payload["sql_queries"], 2)
        self.assertNotIn("question", payload)
        self.assertNotIn("secret quote", logger.info.call_args.args[0])

    def test_history_summary_counts_statuses(self):
        with TemporaryDirectory() as directory:
            history = InvestigationHistory(Path(directory) / "i.sqlite")
            history.save("alice", "policy", "q", {"kind": "answer", "domain": "policy"})
            history.save("alice", "manual", "q", {"kind": "clarification", "domain": "manual"})
            summary = history.summary("alice")
            self.assertEqual(summary["total"], 2)
            self.assertEqual({(row["domain"], row["status"]): row["total"] for row in summary["domains"]},
                             {("policy", "completed"): 1, ("manual", "clarification"): 1})
