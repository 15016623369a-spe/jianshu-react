"""Durable investigation records and owner isolation."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.investigation_history import InvestigationHistory, InvestigationMissing


def answer(identifier="x"):
    return {
        "kind": "answer", "reason": None, "domain": "policy", "question": "Q",
        "scope": {"product": "电水壶"}, "evidence": [{"quote": "原文"}],
        "document_version": "v1", "actual": [{"net_cents": 100}],
        "metrics": {"sql_queries": 1, "model_calls": 0},
    }


class InvestigationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.history = InvestigationHistory(self.root / "investigations.sqlite")

    def test_answer_is_saved_with_id_status_and_evidence(self):
        record = self.history.save("alice", "policy", "问题", answer())
        self.assertEqual((record["status"], record["investigation_id"][0:0]), ("completed", ""))
        self.assertEqual(record["domain"], "policy")
        loaded = self.history.get("alice", record["investigation_id"])
        self.assertEqual(loaded["actual"][0]["net_cents"], 100)
        self.assertEqual(loaded["evidence"][0]["quote"], "原文")

    def test_clarification_and_insufficient_have_distinct_statuses(self):
        clarification = self.history.save("alice", "manual", "缺什么", {
            "kind": "clarification", "reason": "missing_scope", "message": "请补充", "domain": "manual",
        })
        insufficient = self.history.save("alice", "policy", "无证据", {
            "kind": "abstained", "reason": "policy_missing", "message": "没有覆盖", "domain": "policy",
        })
        self.assertEqual((clarification["status"], insufficient["status"]), ("clarification", "insufficient"))

    def test_list_is_owner_scoped_and_newest_first(self):
        first = self.history.save("alice", "policy", "第一", answer())
        second = self.history.save("alice", "policy", "第二", answer())
        self.history.save("bob", "policy", "别人的", answer())
        page = self.history.list("alice", limit=10)
        self.assertEqual([item["question"] for item in page["investigations"]], ["第二", "第一"])
        self.assertEqual(page["investigations"][0]["investigation_id"], second["investigation_id"])
        self.assertEqual(page["investigations"][1]["investigation_id"], first["investigation_id"])
        with self.assertRaises(InvestigationMissing):
            self.history.get("bob", first["investigation_id"])
        with self.assertRaises(InvestigationMissing):
            self.history.get("alice", "not-a-uuid")
