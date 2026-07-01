import datetime as dt
import unittest

import model_risk


AS_OF = dt.date(2026, 6, 18)


def indicator(**overrides):
    data = {
        "indicator": "tips10y",
        "source": "FRED",
        "source_type": "official",
        "direct_or_proxy": "Direct Indicator",
        "latest_date": "2026-06-15",
        "frequency": "daily",
        "sample_size": 88,
        "used_in_score": True,
        "methodology_known": True,
        "reproducible": True,
    }
    data.update(overrides)
    return data


class DataQualityGateTests(unittest.TestCase):
    def test_daily_lag_three_days_passes(self):
        result = model_risk.evaluate_indicator_quality(indicator(), AS_OF)
        self.assertEqual(result["stale_status"], "PASS")

    def test_daily_lag_twelve_days_fails_and_freezes(self):
        gate = model_risk.run_data_quality_gate([indicator(latest_date="2026-06-06")], AS_OF)
        self.assertEqual(gate["indicators"][0]["gate_result"], "FAIL")
        self.assertFalse(gate["allow_execution"])
        self.assertEqual(gate["dynamic_cash_pool_status"], "FREEZE")

    def test_monthly_indicator_more_than_thirty_days_after_release_fails(self):
        item = indicator(
        indicator="m2_yoy",
        latest_date="2026-03-31",
        expected_release_date="2026-05-15",
        frequency="monthly",
    )
        result = model_risk.evaluate_indicator_quality(item, AS_OF)
        self.assertEqual(result["gate_result"], "FAIL")


    def test_non_reproducible_proxy_confidence_is_low(self):
        item = indicator(
        indicator="a500_pe_percentile",
        direct_or_proxy="Proxy Indicator",
        source_type="third-party",
        methodology_known=False,
        reproducible=False,
    )
        result = model_risk.evaluate_indicator_quality(item, AS_OF)
        self.assertEqual(result["confidence"], "Low")


    def test_low_confidence_score_input_blocks_execution(self):
        item = indicator(
        direct_or_proxy="Proxy Indicator",
        methodology_known=False,
        reproducible=False,
    )
        gate = model_risk.run_data_quality_gate([item], AS_OF)
        self.assertFalse(gate["allow_execution"])

    def test_pending_proxy_review_blocks_execution_with_explicit_status(self):
        item = indicator(
            indicator="hs300_pe_percentile", direct_or_proxy="Proxy Indicator",
            source_type="third-party", confidence="Medium",
            approval_status="PENDING_PROXY_REVIEW",
        )
        gate = model_risk.run_data_quality_gate([item], AS_OF)
        self.assertEqual(gate["indicators"][0]["approval_status"], "PENDING_PROXY_REVIEW")
        self.assertEqual(gate["indicators"][0]["gate_result"], "WARNING")
        self.assertFalse(gate["allow_execution"])

    def test_explicitly_approved_medium_proxy_can_pass(self):
        item = indicator(
            indicator="hs300_pe_percentile", direct_or_proxy="Proxy Indicator",
            source_type="third-party", confidence="Medium",
            approval_status="APPROVED_PROXY_PASS",
        )
        gate = model_risk.run_data_quality_gate([item], AS_OF)
        self.assertEqual(gate["indicators"][0]["gate_result"], "PASS")
        self.assertTrue(gate["allow_execution"])

    def test_low_confidence_cannot_be_upgraded_by_approval_flag(self):
        item = indicator(
            indicator="a500_pe_percentile", direct_or_proxy="Proxy Indicator",
            source_type="third-party", confidence="Low",
            approval_status="APPROVED_PROXY_PASS",
        )
        gate = model_risk.run_data_quality_gate([item], AS_OF)
        self.assertEqual(gate["indicators"][0]["approval_status"], "REJECTED")
        self.assertFalse(gate["allow_execution"])

    def test_asset_level_statuses_are_independent(self):
        items = [
            indicator(
                indicator="a500_pe_percentile", assets=["a_share"],
                direct_or_proxy="Proxy Indicator", methodology_known=False,
                reproducible=False,
            ),
            indicator(
                indicator="nasdaq100_pe_percentile", assets=["us_equity"],
                direct_or_proxy="Proxy Indicator", methodology_known=True,
                reproducible=True, source_type="third-party",
            ),
            indicator(indicator="tips5y", assets=["gold"]),
        ]
        gate = model_risk.run_data_quality_gate(items, AS_OF)
        status = gate["asset_level_status"]
        self.assertEqual(status["a_share"]["execution_status"], "BLOCKED")
        self.assertEqual(status["us_equity"]["execution_status"], "BLOCKED")
        self.assertEqual(status["gold"]["execution_status"], "ELIGIBLE")

    def test_pool_freezes_when_any_positive_gap_asset_is_blocked(self):
        status = {
            "a_share": {"execution_status": "BLOCKED"},
            "us_equity": {"execution_status": "BLOCKED"},
            "gold": {"execution_status": "ELIGIBLE"},
        }
        result = model_risk.apply_pool_status(
            status,
            {"a_share": 10000, "us_equity": 12000, "gold": 0},
        )
        self.assertEqual(result["dynamic_cash_pool_status"], "FREEZE")
        self.assertFalse(result["allow_auto_execution"])
        self.assertNotIn("allow_manual_review", result)

    def test_pool_freezes_when_largest_positive_gap_is_blocked(self):
        status = {
            "a_share": {"execution_status": "BLOCKED"},
            "us_equity": {"execution_status": "ELIGIBLE"},
            "gold": {"execution_status": "ELIGIBLE"},
        }
        result = model_risk.apply_pool_status(
            status,
            {"a_share": 15000, "us_equity": 12000, "gold": 0},
        )
        self.assertEqual(result["dynamic_cash_pool_status"], "FREEZE")

    def test_manual_override_is_disabled(self):
        with self.assertRaises(ValueError):
            model_risk.validate_manual_override_request(100, "任何理由", 1875)
