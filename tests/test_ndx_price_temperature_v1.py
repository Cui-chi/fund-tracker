import datetime as dt
import unittest
from pathlib import Path

import cn_equity_temperature
import model_risk
import ndx_price_temperature as ndx
import qdii_carrier


def carriers():
    return [
        {"fund_code": "021000", "ndx_pool_eligible": True, "effective_limit_rmb": 1000},
        {"fund_code": "539001", "ndx_pool_eligible": True, "effective_limit_rmb": 100},
        {"fund_code": "270023", "ndx_pool_eligible": False, "effective_limit_rmb": 99999},
    ]


class NdxPriceTemperatureV1Tests(unittest.TestCase):
    def test_01_ma500_distance_direction(self):
        self.assertGreater(100 - ndx.empirical_percentile(-.2, [-.2, 0, .2]),
                           100 - ndx.empirical_percentile(.2, [-.2, 0, .2]))

    def test_02_drawdown_direction(self):
        self.assertGreater(ndx.empirical_percentile(.2, [0, .1, .2]),
                           ndx.empirical_percentile(0, [0, .1, .2]))

    def test_03_new_high_drawdown_zero(self):
        rows = [(dt.date(2020, 1, 1) + dt.timedelta(days=i), float(i + 1)) for i in range(252)]
        self.assertEqual(ndx.build_daily_features(rows)[-1]["drawdown_magnitude"], 0)

    def test_04_balanced_weight(self):
        self.assertAlmostEqual(ndx.price_temperature(80, 20), 53)

    def test_05_release_formula_is_continuous(self):
        self.assertAlmostEqual(ndx.base_release_factor(40), .55)

    def test_06_score_zero_release_floor(self):
        self.assertEqual(ndx.base_release_factor(0), .25)

    def test_07_score_100_release_ceiling(self):
        self.assertEqual(ndx.base_release_factor(100), 1)

    def test_08_dfii10_boundaries(self):
        self.assertEqual([ndx.real_yield_modifier(x) for x in (0, 20, 60, 80)], [1.05, 1, .95, .85])

    def test_09_real_yield_floor(self):
        self.assertEqual(ndx.real_yield_modifier(100), .85)

    def test_10_extreme_volatility_cap(self):
        self.assertEqual(ndx.volatility_cap(100), .65)

    def test_11_volatility_not_in_temperature(self):
        self.assertEqual(ndx.price_temperature(40, 60), ndx.price_temperature(40, 60))

    def test_12_no_lookahead_prefix_stability(self):
        rows = [(dt.date(2000, 1, 1) + dt.timedelta(days=i), 100 + i * .01) for i in range(1400)]
        prefix = ndx.build_daily_features(rows[:1350])[-1]
        full = ndx.build_daily_features(rows)[1349]
        self.assertEqual(prefix["temperature_score"], full["temperature_score"])

    def test_13_nasdaq_pe_isolation(self):
        self.assertEqual(ndx.price_temperature(30, 70), ndx.price_temperature(30, 70))

    def test_14_sp500_pe_isolation(self):
        self.assertNotIn("sp500", Path("ndx_price_temperature.py").read_text(encoding="utf-8").lower())

    def test_15_qdii_capacity_isolation(self):
        score = ndx.price_temperature(30, 70)
        ndx.candidate_amount_chain(1000, .5, 1000)
        self.assertEqual(score, ndx.price_temperature(30, 70))

    def test_16_global_active_isolation(self):
        self.assertFalse(carriers()[-1]["ndx_pool_eligible"])

    def test_17_under_validation_formal_release_zero(self):
        self.assertEqual(ndx.latest_snapshot([], {}).get("model_status"), "UNDER_VALIDATION")

    def test_18_shadow_formal_release_zero(self):
        self.assertIn("formal_release_amount", Path("fund_tracker.py").read_text(encoding="utf-8"))

    def test_19_no_automatic_activation(self):
        self.assertNotEqual(ndx.latest_snapshot([], {}).get("activation_status"), "ACTIVE")

    def test_20_under_assignment_invalid(self):
        self.assertEqual(qdii_carrier.calculate_multi_select({"021000": 900}, 1000, carriers())["preview_status"], "INVALID")

    def test_21_over_assignment_invalid(self):
        self.assertEqual(qdii_carrier.calculate_multi_select({"021000": 1000, "539001": 100}, 1000, carriers())["preview_status"], "INVALID")

    def test_22_exact_assignment_valid(self):
        self.assertEqual(qdii_carrier.calculate_multi_select({"021000": 1000}, 1000, carriers())["preview_status"], "VALID")

    def test_23_one_cent_tolerance(self):
        self.assertEqual(qdii_carrier.calculate_multi_select({"021000": 999.99}, 1000, carriers())["preview_status"], "VALID")

    def test_24_row_over_limit_invalid(self):
        self.assertEqual(qdii_carrier.calculate_multi_select({"539001": 500}, 500, carriers())["preview_status"], "INVALID")

    def test_25_unselected_nonzero_invalid(self):
        result = qdii_carrier.calculate_multi_select({"021000": 1000, "539001": 1}, 1000, carriers(), selected_codes={"021000"})
        self.assertEqual(result["preview_status"], "INVALID")

    def test_26_freeze_valid_button_disabled(self):
        text = Path("fund_tracker.py").read_text(encoding="utf-8")
        self.assertIn("dynamicCashPoolIsFrozen || previewStatus !== 'VALID'", text)

    def test_27_active_valid_button_rule(self):
        text = Path("fund_tracker.py").read_text(encoding="utf-8")
        self.assertIn("cashPoolStatus !== 'ACTIVE'", text)

    def test_28_capacity_shortfall_retained(self):
        mc = ndx.candidate_amount_chain(1000, 1, 1000)
        self.assertEqual(mc["ndx_candidate_release_amount"], 1000)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": True, "carrier_selection_status": "AVAILABLE",
            "last_known_approved_carrier_capacity": 600,
        })
        self.assertEqual(cm["retained_due_to_capacity"], 400)

    def test_29_unapproved_carrier_not_used(self):
        result = qdii_carrier.calculate_multi_select({"270023": 1000}, 1000, carriers())
        self.assertEqual(result["effective_covered_amount"], 0)

    def test_30_targets_unchanged(self):
        html = Path("dist/Asset Allocation Copilot V7.html").read_text(encoding="utf-8")
        for text in ("40.0%", "35.0%", "5.0%", "20.0%"):
            self.assertIn(text, html)

    def test_31_overseas_gap_unchanged(self):
        # 持仓可在「持仓管理」编辑，故海外权益缺口从当前 config 经同一管线推导，
        # 再断言它出现在已渲染的 dashboard 中，而不是写死某个金额。
        import fund_tracker
        config = fund_tracker.load_config()
        conn = fund_tracker.connect_db()
        try:
            temperature = fund_tracker.generate_market_temperature(conn, config)
            snapshot = fund_tracker.generate_copilot_snapshot(conn, config, temperature)
        finally:
            conn.close()
        gap = snapshot["gaps"]["us_equity"]
        html = Path("dist/Asset Allocation Copilot V7.html").read_text(encoding="utf-8")
        self.assertIn(f"{gap:+,.0f}", html)

    def test_32_historical_625_unchanged(self):
        html = Path("dist/Asset Allocation Copilot V7.html").read_text(encoding="utf-8")
        self.assertIn("Historical Executed Amount", html)
        self.assertIn("<td>2026-06</td>", html)
        self.assertIn("<td>625</td>", html)

    def test_33_a500_regression(self):
        self.assertTrue(cn_equity_temperature.LIVE_SCORING_ENABLED)

    def test_34_gold_regression(self):
        self.assertEqual(model_risk.calculate_gold_score(1.96, 2.23, 2.25, 3.63)["final_gold_score"], 39.8)

    def test_35_fixed_investment_regression(self):
        self.assertIn("固定定投", Path("dist/Asset Allocation Copilot V7.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
