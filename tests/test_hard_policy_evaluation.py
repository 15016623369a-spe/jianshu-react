from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.hard_policy_data import prepare_hard_policy_database
from app.hard_policy_workflow import HardPolicyRequest, run_hard_policy
from evaluation.run_hard_policy_comparison import SUITES, _load_suite, _request_key, _run_case, _score


def _suite(name):
    return _load_suite(Path(__file__).resolve().parents[1] / "evaluation" / SUITES[name])


class HardPolicyEvaluationTests(TestCase):
    def test_text_suite_has_distinct_scopes_and_three_authority_paths(self):
        legacy, text = _suite("legacy"), _suite("text")
        legacy_scopes = {_request_key(case) for case in legacy["holdout"] + legacy["development"]}
        self.assertTrue(legacy_scopes.isdisjoint(_request_key(case) for case in text["holdout"] + text["development"]))
        cases_with_money = [case for case in text["holdout"] if case["group"] == "multi_hop"]
        self.assertEqual(len({tuple(case["expected"]["bulletin_ids"]) for case in cases_with_money}), 3)
        self.assertEqual({len(case["expected"]["bulletin_ids"]) for case in cases_with_money}, {2, 3})

    def test_scorer_rejects_wrong_cents_ledger_and_reference_order(self):
        case = next(case for case in _suite("text")["holdout"] if case["id"] == "south-title-latest-full-month")
        database = prepare_hard_policy_database(Path(self.enterContext(TemporaryDirectory())))
        answer = run_hard_policy(database, HardPolicyRequest.model_validate(case["request"]), mode="uniform")
        self.assertTrue(all(_score(answer, case, "uniform").values()))

        wrong = deepcopy(answer)
        wrong["total_cents"] += 1
        wrong["evidence"]["query"]["ledger"] = "refunds"
        wrong["evidence"]["bulletins"].reverse()
        wrong["evidence"]["policy"]["quote"] = "这段没有出现在引用材料里。"
        checks = _score(wrong, case, "uniform")
        for field in ("total", "ledger", "citations", "citation_text"):
            self.assertFalse(checks[field], field)

    def test_safe_unsupported_is_reported_without_counting_as_correct(self):
        case = next(case for case in _suite("text")["holdout"] if case["id"] == "east-three-referrals-full-month")
        database = prepare_hard_policy_database(Path(self.enterContext(TemporaryDirectory())))
        row = _run_case(database, case, "fixed", 1, None, None)
        self.assertFalse(row["passed"])
        self.assertTrue(row["safe_unsupported"])
        self.assertIsNone(row["observed"]["total_cents"])
        self.assertEqual(row["sql_queries"], 0)
