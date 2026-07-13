import datetime as dt
import sqlite3
import unittest

import cn_equity_temperature
import fund_tracker
import model_risk
from scripts import audit_us_equity_sources


class UsEquitySourceAuditTests(unittest.TestCase):
    def test_metric_definitions_match_documented_sources(self):
        nasdaq = fund_tracker.US_VALUATION_SOURCE_DEFINITIONS["nasdaq100"]
        sp500 = fund_tracker.US_VALUATION_SOURCE_DEFINITIONS["sp500"]
        self.assertEqual(nasdaq["metric_type"], "trailing_pe")
        self.assertEqual(nasdaq["underlying_object"], "QQQ ETF proxy for Nasdaq-100")
        self.assertEqual(nasdaq["source_name"], "World PE Ratio Nasdaq 100")
        self.assertFalse(nasdaq["methodology_known"])
        self.assertEqual(sp500["metric_type"], "trailing_pe")
        self.assertEqual(sp500["underlying_object"], "S&P 500 index")
        self.assertEqual(sp500["source_name"], "Multpl S&P 500 PE Ratio by Month")

    def test_percentile_is_recomputed_with_inclusive_60_month_window(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE pe_history (index_code TEXT,index_name TEXT,metric_name TEXT,"
            "metric_type TEXT,value REAL,observation_date TEXT,frequency TEXT,"
            "source_name TEXT,source_url TEXT,is_estimated INTEGER,"
            "validation_status TEXT,note TEXT,fetched_at TEXT)"
        )
        start = dt.date(2021, 7, 1)
        for index in range(60):
            month = start.year * 12 + start.month - 1 + index
            date = dt.date(month // 12, month % 12 + 1, 1).isoformat()
            conn.execute(
                "INSERT INTO pe_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("NDX", "Nasdaq-100", "PE Ratio", "trailing_pe", index + 1,
                 date, "monthly", "World PE Ratio Nasdaq 100", "https://worldperatio.com/index/nasdaq-100",
                 0, "valid", "QQQ proxy", "2026-06-19T00:00:00"),
            )
        row = fund_tracker.latest_us_valuation(conn, "nasdaq100")
        conn.close()
        self.assertEqual(row["percentile"], 100.0)
        self.assertEqual(row["sample_count"], 60)
        self.assertEqual(row["window_label"], "recent_5y_percentile")
        self.assertNotEqual(row["window_label"], "long_term_percentile")

    def test_pending_proxy_and_unknown_nasdaq_methodology_block_execution(self):
        item = {
            "indicator": "nasdaq100_pe_percentile",
            "source": "World PE Ratio Nasdaq 100",
            "source_type": "third-party",
            "direct_or_proxy": "Proxy Indicator",
            "latest_date": "2026-06-01",
            "frequency": "monthly",
            "expected_release_date": "2026-07-10",
            "sample_size": 60,
            "used_in_score": True,
            "assets": ["us_equity"],
            "methodology_known": False,
            "reproducible": True,
            "approval_status": "PENDING_PROXY_REVIEW",
        }
        gate = model_risk.run_data_quality_gate([item], dt.date(2026, 6, 19))
        evaluated = gate["indicators"][0]
        self.assertEqual(evaluated["approval_status"], "PENDING_PROXY_REVIEW")
        self.assertEqual(evaluated["gate_result"], "FAIL")
        self.assertEqual(gate["asset_level_status"]["us_equity"]["execution_status"], "BLOCKED")
        self.assertFalse(gate["allow_execution"])

    def test_missing_source_is_not_neutralized(self):
        item = {
            "indicator": "sp500_pe_percentile", "source": None,
            "source_type": "third-party", "direct_or_proxy": "Proxy Indicator",
            "latest_date": None, "frequency": "monthly", "expected_release_date": None,
            "sample_size": 0, "used_in_score": True, "assets": ["us_equity"],
            "methodology_known": True, "reproducible": False,
            "approval_status": "PENDING_PROXY_REVIEW",
        }
        gate = model_risk.run_data_quality_gate([item], dt.date(2026, 6, 19))
        self.assertEqual(gate["indicators"][0]["stale_status"], "FAIL")
        self.assertEqual(gate["asset_level_status"]["us_equity"]["execution_status"], "BLOCKED")

    def test_highly_correlated_valuation_signals_are_flagged(self):
        result = {"pearson": 0.88, "spearman": 0.87, "sample_size": 60}
        self.assertEqual(
            audit_us_equity_sources.overlap_verdict(result),
            "CONFIRMED_HIGH_REDUNDANCY",
        )

    def test_a500_gold_targets_and_fixed_invest_regression(self):
        config = fund_tracker.load_config()
        targets = config["copilot_v7"]["strategic_allocation"]
        self.assertEqual(targets, {"a_share": 0.4, "us_equity": 0.4, "gold": 0.1, "cash": 0.1})
        fixed = next(item for item in config["funds"] if item["code"] == "270023")
        self.assertEqual(fixed["weekly_auto_invest"], 100.0)
        self.assertTrue(cn_equity_temperature.LIVE_SCORING_ENABLED)
        self.assertEqual(cn_equity_temperature.FORMULA_VERSION, "CN_EQUITY_PRICE_TEMP_V1")
        self.assertEqual(
            model_risk.calculate_gold_score(1.96, 2.23, 2.25, 3.63)["final_gold_score"],
            39.8,
        )


if __name__ == "__main__":
    unittest.main()
