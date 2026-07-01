import unittest

import model_risk


class AllocationRoutingTests(unittest.TestCase):
    def test_gap_first_bounded_temperature_routing(self):
        result = model_risk.route_allocation(
        {"a_share": 10000, "us_equity": 10000, "gold": 10000},
        {"a_share": 30, "us_equity": 20, "gold": 80},
        600,
    )
        gold = result["assets"]["gold"]
        self.assertEqual(gold["temperature_multiplier"], 1.25)
        self.assertTrue(0 < gold["allocation_amount"] < 600)
        for asset in ("a_share", "us_equity", "gold"):
            self.assertIn("gap_weight", result["assets"][asset])
            self.assertIn("temperature_multiplier", result["assets"][asset])
            self.assertIn("final_weight", result["assets"][asset])


    def test_gold_allocation_falls_when_score_falls(self):
        high = model_risk.route_allocation(
        {"a_share": 10000, "us_equity": 10000, "gold": 10000},
        {"a_share": 30, "us_equity": 20, "gold": 80},
        600,
    )
        low = model_risk.route_allocation(
        {"a_share": 10000, "us_equity": 10000, "gold": 10000},
        {"a_share": 30, "us_equity": 20, "gold": 30},
        600,
    )
        self.assertLess(low["allocations"]["gold"], high["allocations"]["gold"])

    def test_blocked_allocation_is_retained_not_redistributed(self):
        statuses = {
            "a_share": {"data_quality_status": "FAIL", "execution_status": "BLOCKED", "reason": "bad data"},
            "us_equity": {"data_quality_status": "PASS", "execution_status": "ELIGIBLE", "reason": "pass"},
            "gold": {"data_quality_status": "WARNING", "execution_status": "BLOCKED", "reason": "warning blocks binary gate"},
        }
        result = model_risk.route_asset_level_allocation(
            {"a_share": 10000, "us_equity": 10000, "gold": 10000},
            {"a_share": 30, "us_equity": 30, "gold": 80},
            600,
            statuses,
        )
        self.assertGreater(result["assets"]["a_share"]["theoretical_allocation"], 0)
        self.assertEqual(result["assets"]["a_share"]["executable_allocation"], 0)
        self.assertGreater(result["retained_in_dynamic_cash_pool"], 0)
        self.assertLess(sum(result["allocations"].values()), 600)
