"""State backup/restore keeps investigation and document SQLite state."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.hard_policy_data import prepare_hard_policy_database
from app.investigation_history import InvestigationHistory
from app.policy_documents import PolicyDocumentStore
from scripts.state_backup import backup, restore


class StateBackupTests(unittest.TestCase):
    def test_backup_restore_preserves_history_and_documents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "state"
            source.mkdir()
            history = InvestigationHistory(source / "investigations.sqlite")
            record = history.save("alice", "policy", "q", {"kind": "answer", "domain": "policy"})
            documents = PolicyDocumentStore(source / "policy_documents.sqlite")
            documents.create("alice", "policy.md", "policy", (
                "# 政策\n\n## 十月\n\n文号：backup-policy。\n\n华南地区的家电类商品按本页口径办理，具体限于电水壶。"
                "从2026年10月1日起执行，到2026年10月31日为止（含当天）。"
                "按售后退款台账结算，每笔扣减上限 5 元。"
            ).encode())
            archive = root / "state.tar.gz"
            backup(source, archive)
            restored = root / "restored"
            restore(archive, restored)
            self.assertEqual(InvestigationHistory(restored / "investigations.sqlite").get(
                "alice", record["investigation_id"])["question"], "q")
            self.assertEqual(len(PolicyDocumentStore(restored / "policy_documents.sqlite").list_visible(
                "alice", limit=10, offset=0)), 1)
