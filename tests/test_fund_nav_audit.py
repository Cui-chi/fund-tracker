import datetime as dt
import sqlite3
import tempfile
import unittest

import data_layer_audit
import fund_tracker


class FundNavAuditTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = fund_tracker.DB_PATH
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite")
        fund_tracker.DB_PATH = self.tmp.name
        self.conn = fund_tracker.connect_db()
        self.config = {
            "funds": [{
                "code": "000001", "name": "Test Fund", "type": "QDII",
                "holding_amount": 0, "max_holding_amount": 1,
                "drawdown_20_buy_amount": 0, "drawdown_30_buy_amount": 0,
            }]
        }

    def tearDown(self):
        self.conn.close()
        self.tmp.close()
        fund_tracker.DB_PATH = self.original_db_path

    def _insert(self, date, nav):
        self.conn.execute(
            """INSERT INTO nav_history
            (code, nav_date, nav, accumulated_nav, pct_change, source,
             source_url, fetch_time, is_qdii, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("000001", date, nav, nav, None, "source", "url",
             "2026-06-18T18:00:00", 1, "2026-06-18T18:00:00"),
        )

    def test_drawdown_is_locally_reproducible(self):
        current = dt.date(2025, 6, 18)
        while current <= dt.date(2026, 6, 16):
            if current.weekday() < 5:
                self._insert(current.isoformat(), 2.0 if current == dt.date(2025, 6, 18) else 1.5)
            current += dt.timedelta(days=1)
        result = data_layer_audit.audit_fund_nav(
            self.conn, self.config, as_of=dt.date(2026, 6, 18)
        )
        row = result["funds"][0]["drawdowns"]["12m"]
        self.assertAlmostEqual(row["drawdown"], -0.25)
        self.assertEqual(row["coverage_status"], "PASS")
        fund = result["funds"][0]
        for key in (
            "latest_nav", "qdii_lag_status", "6m_window_start", "6m_sample_size",
            "6m_coverage_ratio", "12m_max_nav", "12m_drawdown", "12m_coverage_status",
        ):
            self.assertIn(key, fund)

    def test_insufficient_coverage_is_disclosed(self):
        self._insert("2026-04-13", 1.0)
        self._insert("2026-06-16", 0.9)
        result = data_layer_audit.audit_fund_nav(
            self.conn, self.config, as_of=dt.date(2026, 6, 18)
        )
        row = result["funds"][0]["drawdowns"]["6m"]
        self.assertEqual(row["coverage_status"], "INSUFFICIENT")
        self.assertEqual(row["display_note"], "样本不足，仅供参考")

    def test_qdii_lag_is_explicit(self):
        self._insert("2026-06-12", 1.0)
        result = data_layer_audit.audit_fund_nav(
            self.conn, self.config, as_of=dt.date(2026, 6, 18)
        )
        fund = result["funds"][0]
        self.assertEqual(fund["data_lag_days"], 6)
        self.assertEqual(fund["lag_status"], "WARNING")

    def test_zero_holding_execution_only_carrier_does_not_block_nav_audit(self):
        self.config["funds"][0]["execution_only"] = True
        result = data_layer_audit.audit_fund_nav(
            self.conn, self.config, as_of=dt.date(2026, 6, 18)
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["funds"][0]["status"], "NOT_APPLICABLE")
        self.assertEqual(result["blocking_issues"], [])
        with tempfile.TemporaryDirectory() as directory:
            data_layer_audit.write_phase1_reports(directory, result)

    def test_execution_only_carrier_blocks_once_it_has_a_holding(self):
        self.config["funds"][0].update({
            "execution_only": True,
            "holding_amount": 100,
        })
        result = data_layer_audit.audit_fund_nav(
            self.conn, self.config, as_of=dt.date(2026, 6, 18)
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("000001:missing_raw_nav_series", result["blocking_issues"])


if __name__ == "__main__":
    unittest.main()
