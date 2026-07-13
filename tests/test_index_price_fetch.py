import json
import unittest
from unittest import mock

import fund_tracker


class IndexPriceFetchTests(unittest.TestCase):
    @mock.patch("fund_tracker.subprocess.run")
    def test_eastmoney_fetch_uses_stable_http_options(self, run):
        payload = {
            "data": {
                "klines": [
                    "2026-06-18,6166.62,6219.79,6253.78,6166.62,1,1,1,1,1,1"
                ]
            }
        }
        run.return_value.stdout = json.dumps(payload).encode("utf-8")

        rows = fund_tracker.fetch_index_price_history("000510", "中证A500")

        command = run.call_args[0][0]
        self.assertIn("--http1.1", command)
        self.assertIn("--compressed", command)
        self.assertIn("--retry", command)
        self.assertIn("Referer: https://quote.eastmoney.com/", command)
        self.assertEqual(rows[0]["tradeDate"], "2026-06-18")
        self.assertEqual(rows[0]["close"], 6219.79)


if __name__ == "__main__":
    unittest.main()
