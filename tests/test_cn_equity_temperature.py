import datetime as dt
import math
import unittest

import cn_equity_temperature as cn
import model_risk


AS_OF = dt.date(2026, 6, 19)


def records(count, start=100.0, step=0.1, code=cn.A500_CODE):
    first = AS_OF - dt.timedelta(days=count - 1)
    return [
        {
            "indexCode": code, "indexName": "test",
            "tradeDate": (first + dt.timedelta(days=i)).isoformat(),
            "close": start + step * i,
        }
        for i in range(count)
    ]


class PriceMetricTests(unittest.TestCase):
    def test_500_rows_use_ma500(self):
        result = cn.calculate_metrics(records(500), cn.A500_CODE, "A500", AS_OF)
        self.assertEqual(result["movingAverageWindow"], 500)
        self.assertEqual(result["confidence"], "HIGH")

    def test_300_rows_use_ma250(self):
        result = cn.calculate_metrics(records(300), cn.A500_CODE, "A500", AS_OF)
        self.assertEqual(result["movingAverageWindow"], 250)
        self.assertEqual(result["confidence"], "MEDIUM")

    def test_150_rows_use_ma120_low(self):
        result = cn.calculate_metrics(records(150), cn.A500_CODE, "A500", AS_OF)
        self.assertEqual(result["movingAverageWindow"], 120)
        self.assertEqual(result["confidence"], "LOW")

    def test_100_rows_unavailable(self):
        result = cn.calculate_metrics(records(100), cn.A500_CODE, "A500", AS_OF)
        self.assertIsNone(result["movingAverageWindow"])
        self.assertEqual(result["confidence"], "UNAVAILABLE")

    def test_duplicate_null_and_nonpositive_do_not_count(self):
        rows = records(250)
        rows += [dict(rows[-1]), {"tradeDate": "2026-06-19", "close": None},
                 {"tradeDate": "2026-06-18", "close": 0}]
        result = cn.calculate_metrics(rows, cn.A500_CODE, "A500", AS_OF)
        self.assertEqual(result["sampleCount"], 250)
        self.assertTrue(any("DUPLICATE_DATE" in item for item in result["warnings"]))

    def test_current_at_one_year_high_has_zero_drawdown(self):
        result = cn.calculate_metrics(records(250), cn.A500_CODE, "A500", AS_OF)
        self.assertAlmostEqual(result["oneYearDrawdown"], 0.0)

    def test_twenty_percent_below_high(self):
        rows = records(249, start=100, step=0)
        rows.append({"tradeDate": AS_OF.isoformat(), "close": 80})
        result = cn.calculate_metrics(rows, cn.A500_CODE, "A500", AS_OF)
        self.assertAlmostEqual(result["oneYearDrawdown"], -0.20)

    def test_short_year_window_is_low_confidence(self):
        result = cn.calculate_metrics(records(150), cn.A500_CODE, "A500", AS_OF)
        self.assertIsNotNone(result["oneYearDrawdown"])
        self.assertEqual(result["confidence"], "LOW")

    def test_extreme_price_does_not_silently_score(self):
        rows = records(250, start=100, step=0)
        rows[-1]["close"] = 200
        result = cn.calculate_metrics(rows, cn.A500_CODE, "A500", AS_OF)
        self.assertEqual(result["confidence"], "UNAVAILABLE")
        self.assertTrue(any("EXTREME_DAILY_MOVE" in item for item in result["warnings"]))

    def test_fixed_price_volatility_is_zero(self):
        result = cn.calculate_metrics(records(250, step=0), cn.A500_CODE, "A500", AS_OF)
        self.assertAlmostEqual(result["annualizedVolatility"], 0.0)

    def test_sample_standard_deviation_annualization(self):
        values = [100.0]
        returns = ([0.01, -0.01] * 30)
        for value in returns:
            values.append(values[-1] * (1 + value))
        rows = [
            {"tradeDate": (AS_OF - dt.timedelta(days=60-i)).isoformat(), "close": close}
            for i, close in enumerate(values)
        ]
        result = cn.calculate_metrics(rows, cn.A500_CODE, "A500", AS_OF)
        mean = sum(returns) / 60
        expected = math.sqrt(sum((x-mean)**2 for x in returns) / 59) * math.sqrt(252)
        self.assertAlmostEqual(result["annualizedVolatility"], expected)

    def test_fewer_than_61_prices_has_no_volatility(self):
        result = cn.calculate_metrics(records(60), cn.A500_CODE, "A500", AS_OF)
        self.assertIsNone(result["annualizedVolatility"])


class PriceScoreTests(unittest.TestCase):
    def metrics(self, distance, drawdown, volatility, confidence="HIGH"):
        return {
            "confidence": confidence, "movingAverageDistance": distance,
            "oneYearDrawdown": drawdown, "annualizedVolatility": volatility,
            "warnings": [],
        }

    def test_hot_position_scores_lower_than_cool_position(self):
        market = self.metrics(0, 0, 0.15)
        hot = cn.calculate_temperature(self.metrics(0.20, 0, 0.15), market)
        cool = cn.calculate_temperature(self.metrics(-0.20, -0.20, 0.15), market)
        self.assertLess(hot["finalScore"], cool["finalScore"])

    def test_high_volatility_is_penalty(self):
        market = self.metrics(0, 0, 0.15)
        low = cn.calculate_temperature(self.metrics(-0.10, -0.15, 0.15), market)
        high = cn.calculate_temperature(self.metrics(-0.10, -0.15, 0.45), market)
        self.assertLess(high["finalScore"], low["finalScore"])

    def test_extreme_drawdown_is_capped(self):
        self.assertEqual(cn.drawdown_score(-0.40), cn.drawdown_score(-0.80))

    def test_market_adjustment_is_bounded(self):
        carrier = self.metrics(-0.10, -0.15, 0.20)
        for market in (self.metrics(0.50, 0, 0.15), self.metrics(-0.50, -0.5, 0.15)):
            result = cn.calculate_temperature(carrier, market)
            self.assertLessEqual(abs(result["marketAdjustment"]), 5)
            self.assertGreaterEqual(result["finalScore"], 0)
            self.assertLessEqual(result["finalScore"], 100)

    def test_unavailable_a500_falls_back_to_one(self):
        result = cn.calculate_temperature(self.metrics(None, None, None, "UNAVAILABLE"), {})
        self.assertEqual(result["releaseFactor"], 1.0)
        self.assertEqual(result["level"], "UNAVAILABLE")

    def test_low_confidence_cannot_increase_release(self):
        result = cn.calculate_temperature(self.metrics(-0.2, -0.2, 0.15, "LOW"), {})
        self.assertEqual(result["releaseFactor"], 1.0)

    def test_missing_hs300_means_zero_adjustment(self):
        result = cn.calculate_temperature(self.metrics(-0.1, -0.1, 0.2), {})
        self.assertEqual(result["marketAdjustment"], 0)

    def test_pe_failure_does_not_block_price_model(self):
        gate = model_risk.run_data_quality_gate([
            {
                "indicator": "a500_price_temperature", "source": "price",
                "source_type": "official-distributor", "direct_or_proxy": "Direct Indicator",
                "latest_date": AS_OF.isoformat(), "frequency": "daily",
                "sample_size": 500, "used_in_score": True, "assets": ["a_share"],
                "methodology_known": True, "reproducible": True,
            },
            {
                "indicator": "hs300_pe_percentile", "source": "broken",
                "source_type": "third-party", "direct_or_proxy": "Proxy Indicator",
                "latest_date": None, "frequency": "daily", "sample_size": 0,
                "used_in_score": False, "assets": ["a_share"],
                "methodology_known": False, "reproducible": False,
            },
        ], AS_OF)
        self.assertEqual(gate["asset_level_status"]["a_share"]["execution_status"], "ELIGIBLE")

    def test_price_failure_only_disables_temperature_adjustment(self):
        gate = model_risk.run_data_quality_gate([{
            "indicator": "a500_price_temperature", "source": "price",
            "source_type": "official-distributor", "direct_or_proxy": "Direct Indicator",
            "latest_date": None, "frequency": "daily", "sample_size": 0,
            "used_in_score": True, "non_blocking_fallback": True,
            "assets": ["a_share"], "methodology_known": True,
            "reproducible": True,
        }], AS_OF)
        status = gate["asset_level_status"]["a_share"]
        self.assertEqual(status["data_quality_status"], "WARNING")
        self.assertEqual(status["execution_status"], "ELIGIBLE")
        self.assertTrue(gate["allow_execution"])

    def test_release_factor_retains_unreleased_cash(self):
        status = {
            key: {"execution_status": "ELIGIBLE", "data_quality_status": "PASS", "reason": "ok"}
            for key in ("a_share", "us_equity", "gold")
        }
        routed = model_risk.route_asset_level_allocation(
            {"a_share": 100, "us_equity": 0, "gold": 0},
            {"a_share": 50, "us_equity": 50, "gold": 50}, 100, status,
            release_factors={"a_share": 0.5},
            temperature_multiplier_overrides={"a_share": 1.0},
        )
        self.assertEqual(routed["allocations"]["a_share"], 50)
        self.assertEqual(routed["retained_in_dynamic_cash_pool"], 50)


if __name__ == "__main__":
    unittest.main()
