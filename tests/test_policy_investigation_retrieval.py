from datetime import date
from unittest import TestCase
from unittest.mock import Mock

from app.hard_policy_data import DocumentSnapshot, _passages
from app.policy_investigation_retrieval import RetrievalStop, resolve_policy_window


class PolicyInvestigationRetrievalTests(TestCase):
    scope = "在华东销售的家电类商品适用本页规则，具体限于智能台灯。"
    start = date(2027, 2, 1)
    end = date(2027, 3, 1)

    @classmethod
    def snapshot(cls, policy: str, bulletins: str) -> DocumentSnapshot:
        return DocumentSnapshot(
            "test", (("智能台灯", "家电"),),
            (*_passages(policy.encode(), "policy", "policies.md"), *_passages(bulletins.encode(), "bulletin", "bulletins.md")),
        )

    @classmethod
    def passage(cls, title: str, number: str, content: str, start=1, end=28, alias=""):
        return (
            f"## {title}\n\n文号：{number}。\n\n{alias}\n\n"
            f"{cls.scope}有效期为2027年2月{start}日至2027年2月{end}日（含首尾日期）。\n\n{content}\n"
        )

    def resolve(self, policy, bulletins, rewrite=None, observe=None):
        return resolve_policy_window(
            self.snapshot(policy, bulletins), "智能台灯", "华东", self.start, self.end, rewrite=rewrite, observe=observe,
        )

    def test_recovers_cited_alias_only_after_targeted_rewrite(self):
        policy = self.passage("台灯售后规则", "lamp-policy", "结算时请查此前称为「退款再入账」的通知。")
        bulletin = self.passage(
            "直营销售补扣月度通知", "lamp-bulletin", "使用订单售后实退台账；同一订单最多抵扣人民币 7 元。",
            alias="本通知过去也称《退款再入账》。",
        )
        with self.assertRaisesRegex(RetrievalStop, "bulletin_missing"):
            self.resolve(policy, bulletin)

        observer, rewrite = Mock(), Mock(return_value="直营销售补扣月度通知")
        result = self.resolve(policy, bulletin, rewrite, observer)[0]
        self.assertEqual((result.ledger, result.cap_cents), ("refunds", 700))
        self.assertIn("结算时请查此前称为「退款再入账」", result.policy["quote"])
        self.assertIn("同一订单最多抵扣", result.bulletins[0]["quote"])
        gap = rewrite.call_args.args[0]
        self.assertEqual((gap.query, gap.candidates[0]["title"]), ("退款再入账", "直营销售补扣月度通知"))
        observer.assert_any_call("retrieve_bulletin", "miss", "退款再入账")
        observer.assert_any_call("retrieve_rewritten", "found", "直营销售补扣月度通知")
        lexical = self.resolve(policy, bulletin, lambda gap: gap.query)[0]
        self.assertEqual(lexical, result)

    def test_rewrite_cannot_select_unlinked_or_informational_sources(self):
        policy = self.passage("台灯售后规则", "lamp-policy", "按《退款再入账》执行。")
        official = self.passage(
            "直营销售补扣月度通知", "lamp-bulletin", "使用售后退款台账，逐笔扣减上限为人民币 7 元。",
            alias="本通知过去也称《退款再入账》。",
        )
        unrelated = self.passage(
            "门店账务撤销执行通知", "other-bulletin", "使用争议扣回台账，逐笔扣减上限为人民币 5 元。",
        )
        fake = self.passage(
            "退款再入账问答", "faq", "本 FAQ 不能直接用于核算，不得作为结算依据。",
            alias="本通知过去也称《退款再入账》。",
        )
        with self.assertRaisesRegex(RetrievalStop, "bulletin_missing"):
            self.resolve(policy, official + unrelated + fake, Mock(return_value="门店账务撤销执行通知"))
        rewrite = Mock(return_value="退款再入账问答")
        with self.assertRaisesRegex(RetrievalStop, "bulletin_missing"):
            self.resolve(policy, unrelated + fake, rewrite)
        rewrite.assert_not_called()

    def test_same_priority_recoverable_alias_conflict_refuses_before_rewrite(self):
        policy = self.passage("台灯售后规则", "lamp-policy", "按《退款再入账》执行。")
        bulletins = "".join(
            self.passage(title, number, "使用售后退款台账，逐笔扣减上限为人民币 7 元。", alias="本通知过去也称《退款再入账》。")
            for title, number in (("直营销售补扣月度通知", "new-ledger"), ("门店账务处理通知", "other-ledger"))
        )
        rewrite = Mock()
        with self.assertRaisesRegex(RetrievalStop, "bulletin_conflict"):
            self.resolve(policy, bulletins, rewrite)
        rewrite.assert_not_called()

    def test_effective_midmonth_switch_partitions_and_rejects_partial_coverage(self):
        policy = self.passage("台灯售后规则", "lamp-policy", "采用查询期间生效的最新公告。")
        first = self.passage("二月上旬退款公告", "lamp-bulletin-first", "使用原订单退款列，逐笔扣减上限为人民币 6 元。", end=15)
        second = self.passage("二月下旬退款公告", "lamp-bulletin-second", "使用支付撤销对账台账；同一订单最多抵扣人民币 7 元。", start=16)
        results = self.resolve(policy, first + second)
        self.assertEqual(
            [(rule.start, rule.end_exclusive, rule.ledger, rule.cap_cents) for rule in results],
            [(self.start, date(2027, 2, 16), "orders", 600), (date(2027, 2, 16), self.end, "chargebacks", 700)],
        )
        with self.assertRaisesRegex(RetrievalStop, "bulletin_missing"):
            self.resolve(policy, first)

    def test_citation_cycle_refuses(self):
        policy = self.passage("台灯售后规则", "lamp-policy", "按《二月退款公告》执行。")
        bulletin = self.passage("二月退款公告", "lamp-bulletin", "按《二月退款公告》执行。")
        with self.assertRaisesRegex(RetrievalStop, "referral_cycle"):
            self.resolve(policy, bulletin)
