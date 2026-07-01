import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import fund_tracker
import cn_equity_temperature
import model_risk
import us_equity_history as ueh


class UsEquityHistoryTests(unittest.TestCase):
    def test_month_end_uses_last_valid_observation(self):
        rows = [
            (dt.date(2026, 1, 29), 1.1),
            (dt.date(2026, 1, 30), 1.2),
            (dt.date(2026, 2, 27), 1.3),
        ]
        monthly = ueh.month_end_last_valid_value(rows, "TEST", "now")
        self.assertEqual(monthly[0]["monthly_value"], 1.2)
        self.assertEqual(monthly[0]["source_date"], "2026-01-30")
        self.assertEqual(monthly[0]["aggregation_method"], "month_end_last_valid_value")

    def test_persisted_rate_history_is_complete_and_fresh(self):
        base = Path("data/us_equity_history")
        months = []
        for filename in ("tips5y_monthly.csv", "tips10y_monthly.csv", "fed_funds_monthly.csv"):
            with (base / filename).open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreaterEqual(len(rows), 120)
            keys = [row["month"] for row in rows]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(rows[-1]["month"], "2026-06")
            months.append(set(keys))
        self.assertGreaterEqual(len(set.intersection(*months)), 120)

    def test_candidate_required_fields_and_window_labels(self):
        base = Path("data/us_equity_history")
        for filename in ("nasdaq_valuation_candidates.csv", "sp500_valuation_candidates.csv"):
            with (base / filename).open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreaterEqual(len(rows), 3)
            for row in rows:
                for field in ueh.CANDIDATE_REQUIRED_FIELDS:
                    self.assertIn(field, row)
                    self.assertNotEqual(row[field], "")
                for field in ("5y_percentile", "10y_percentile", "15y_percentile",
                              "20y_percentile", "full_history_percentile"):
                    self.assertIn(field, row)

    def test_revision_log_records_changed_values_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "revisions.csv"
            self.assertTrue(ueh.record_revision_if_changed(
                path, "source", "metric", "2026-01", 1.0, 1.2
            ))
            self.assertFalse(ueh.record_revision_if_changed(
                path, "source", "metric", "2026-02", 2.0, 2.0
            ))
            with path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["revision_reason"], "UNKNOWN")

    def test_attempt_log_appends_success_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.csv"
            ueh.append_attempt(path, source_name="a", indicator="x", success="true")
            ueh.append_attempt(path, source_name="b", indicator="y", success="false",
                               error_type="HTTP_403")
            with path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["success"] for row in rows], ["true", "false"])

    def test_legacy_sources_pending_and_freeze_retained(self):
        for filename in ("nasdaq_valuation_candidates.csv", "sp500_valuation_candidates.csv"):
            with Path("data/us_equity_history", filename).open(encoding="utf-8") as handle:
                legacy = [row for row in csv.DictReader(handle) if row["legacy_source"] == "true"]
            self.assertEqual(len(legacy), 1)
            self.assertEqual(legacy[0]["recommended_role"], "DISPLAY_ONLY")
            self.assertEqual(legacy[0]["approval_status"], "PENDING_PROXY_REVIEW")
        approved = Path("data/approved-sources.json").read_text(encoding="utf-8")
        self.assertNotIn("APPROVED_PROXY_PASS", approved)

    def test_protected_models_and_targets_unchanged(self):
        config = fund_tracker.load_config()
        self.assertEqual(config["copilot_v7"]["strategic_allocation"],
                         {"a_share": 0.4, "us_equity": 0.4, "gold": 0.1, "cash": 0.1})
        fixed = next(item for item in config["funds"] if item["code"] == "270023")
        self.assertEqual(fixed["weekly_auto_invest"], 100.0)
        self.assertTrue(cn_equity_temperature.LIVE_SCORING_ENABLED)
        self.assertEqual(model_risk.calculate_gold_score(1.96, 2.23, 2.25, 3.63)["final_gold_score"], 39.8)


if __name__ == "__main__":
    unittest.main()
