import unittest

import model_risk


def gold(t5=1.5, t10=1.8, be=2.2, fed=3.0):
    return model_risk.calculate_gold_score(t5, t10, be, fed)


class GoldDirectionTests(unittest.TestCase):
    def test_5y_tips_increase_does_not_raise_gold_score(self):
        self.assertLessEqual(gold(t5=2.0)["final_gold_score"], gold(t5=1.0)["final_gold_score"])

    def test_10y_tips_increase_does_not_raise_gold_score(self):
        self.assertLessEqual(gold(t10=2.5)["final_gold_score"], gold(t10=1.5)["final_gold_score"])

    def test_breakeven_increase_can_raise_gold_score(self):
        self.assertGreaterEqual(gold(be=2.8)["final_gold_score"], gold(be=2.0)["final_gold_score"])

    def test_fed_funds_increase_does_not_raise_gold_score(self):
        low = gold(fed=2.0)
        high = gold(fed=4.0)
        self.assertIsNone(low["explicit_exclusion_reason"])
        self.assertLessEqual(high["final_gold_score"], low["final_gold_score"])

    def test_both_real_yields_increase_do_not_raise_gold_score(self):
        self.assertLessEqual(gold(t5=2.5, t10=2.5)["final_gold_score"], gold(t5=1.0, t10=1.0)["final_gold_score"])
