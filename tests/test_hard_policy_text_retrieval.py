"""Verify that authoritative Markdown survives retrieval and is interpreted conservatively."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from app.hard_policy_data import AmbiguousDocument, load_document_snapshot, search_policy

OCTOBER = (date(2026, 10, 1), date(2026, 11, 1))


def with_rewritten_text(snapshot, title, original, replacement):
    passages = tuple(
        replace(passage, quote=passage.quote.replace(original, replacement))
        if passage.title == title else passage
        for passage in snapshot.passages
    )
    return replace(snapshot, passages=passages)


class HardPolicyTextRetrievalTests(unittest.TestCase):
    def test_malformed_category_rule_does_not_shadow_product_rule(self):
        snapshot = with_rewritten_text(
            load_document_snapshot(),
            "华东家电直营退款政策",
            "华东家电直营网售按《家电直营网售退款细则》执行。",
            "扣减依据另行公布。",
        )
        winner = search_policy("空气炸锅", "家电", "华东", *OCTOBER, snapshot=snapshot)
        self.assertEqual(winner["id"], "policy-east-airfryer")
        with self.assertRaises(AmbiguousDocument):
            search_policy("电水壶", "家电", "华东", *OCTOBER, snapshot=snapshot)

    def test_unrecognized_cap_cannot_become_unlimited_refund(self):
        snapshot = with_rewritten_text(
            load_document_snapshot(),
            "华南户外原单退款政策",
            "全部退款。",
            "全部退款，但超过五元的部分暂缓核销。",
        )
        with self.assertRaises(AmbiguousDocument):
            search_policy("帐篷", "户外", "华南", *OCTOBER, snapshot=snapshot)
