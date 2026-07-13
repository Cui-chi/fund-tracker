import sqlite3
import unittest

import fund_tracker


class AShareValuationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE a_share_valuation_observations (
                index_id TEXT, observation_date TEXT, pe_ttm REAL, pb REAL,
                source TEXT, fetch_time TEXT, role TEXT, reproducible INTEGER,
                confidence TEXT, used_in_score INTEGER,
                PRIMARY KEY (index_id, observation_date)
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def add_rows(self, count, index_id="a500"):
        self.conn.executemany(
            """
            INSERT INTO a_share_valuation_observations
            VALUES (?, ?, ?, ?, 'local daily snapshot', '2026-06-18T18:00:00',
                    'Display Only', 0, 'Low', 0)
            """,
            [
                (index_id, "day-%04d" % i, 10.0 + i / 100, 1.0 + i / 1000)
                for i in range(count)
            ],
        )

    def test_below_250_does_not_calculate_percentile(self):
        self.add_rows(249)
        result = fund_tracker.local_a_share_valuation(self.conn, "a500")
        self.assertEqual(result["percentile_status"], "NOT_CALCULATED")
        self.assertIsNone(result["percentile"])
        self.assertFalse(result["used_in_score"])

    def test_250_to_749_is_reference_only(self):
        self.add_rows(250)
        result = fund_tracker.local_a_share_valuation(self.conn, "a500")
        self.assertEqual(result["percentile_status"], "REFERENCE_ONLY")
        self.assertIsNotNone(result["percentile"])
        self.assertFalse(result["used_in_score"])

    def test_750_is_candidate_but_not_automatically_enabled(self):
        self.add_rows(750)
        result = fund_tracker.local_a_share_valuation(self.conn, "a500")
        self.assertEqual(result["percentile_status"], "CANDIDATE_MODEL_INPUT")
        self.assertFalse(result["used_in_score"])

