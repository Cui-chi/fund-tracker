import copy
import unittest

import local_server


NOW = "2026-06-30T15:21:00+08:00"

BASE_CONFIG = {
    "cash_available": 50000.0,
    "copilot_v7": {"monthly_contribution": 2500.0},
    "funds": [
        {
            "code": "022459", "name": "易方达中证A500ETF联接A", "type": "宽基",
            "asset_class": "a_share", "holding_amount": 12097.68, "profit_pct": 0.3,
            "strategy": "每周定投500", "daily_auto_invest": 0.0, "weekly_auto_invest": 500.0,
            "max_holding_amount": 25000.0, "drawdown_20_buy_amount": 5000.0,
            "drawdown_30_buy_amount": 8000.0,
        },
        {
            "code": "014661", "name": "天弘上海金ETF联接A", "type": "黄金",
            "asset_class": "gold", "holding_amount": 8002.3, "profit_pct": -6.9,
            "strategy": "无", "daily_auto_invest": 0.0, "weekly_auto_invest": 0.0,
            "max_holding_amount": 12000.0, "drawdown_20_buy_amount": 0.0,
            "drawdown_30_buy_amount": 0.0,
        },
    ],
}


def fund_by_code(config, code):
    return {fund["code"]: fund for fund in config["funds"]}[code]


class ApplyPortfolioUpdateTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(BASE_CONFIG)

    def test_updates_amount_and_stamps_time(self):
        updated, last = local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "022459", "holding_amount": 13000}]}, now_iso=NOW)
        fund = fund_by_code(updated, "022459")
        self.assertEqual(fund["holding_amount"], 13000.0)
        self.assertEqual(fund["holding_updated_at"], NOW)
        self.assertEqual(last, NOW)

    def test_two_decimal_rounding(self):
        updated, _ = local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "022459", "holding_amount": 12345.678}]}, now_iso=NOW)
        self.assertEqual(fund_by_code(updated, "022459")["holding_amount"], 12345.68)

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(
                self.config, {"holdings": [{"code": "022459", "holding_amount": -1}]}, now_iso=NOW)

    def test_rejects_empty_and_none(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                local_server.apply_portfolio_update(
                    self.config, {"holdings": [{"code": "022459", "holding_amount": bad}]}, now_iso=NOW)

    def test_rejects_non_number(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(
                self.config, {"holdings": [{"code": "022459", "holding_amount": "abc"}]}, now_iso=NOW)

    def test_rejects_bool(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(
                self.config, {"holdings": [{"code": "022459", "holding_amount": True}]}, now_iso=NOW)

    def test_rejects_above_max_holding(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(
                self.config, {"holdings": [{"code": "022459", "holding_amount": 26000}]}, now_iso=NOW)

    def test_unknown_code(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(
                self.config, {"holdings": [{"code": "999999", "holding_amount": 100}]}, now_iso=NOW)

    def test_cash_update_stamps_time(self):
        updated, last = local_server.apply_portfolio_update(
            self.config, {"cash_available": 40000}, now_iso=NOW)
        self.assertEqual(updated["cash_available"], 40000.0)
        self.assertEqual(updated["cash_updated_at"], NOW)
        self.assertEqual(last, NOW)

    def test_cash_rejects_negative(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(self.config, {"cash_available": -5}, now_iso=NOW)

    def test_no_amount_raises(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(self.config, {}, now_iso=NOW)
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(self.config, {"holdings": []}, now_iso=NOW)

    def test_only_submitted_fund_changes(self):
        updated, _ = local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "022459", "holding_amount": 13000}]}, now_iso=NOW)
        gold = fund_by_code(updated, "014661")
        self.assertEqual(gold["holding_amount"], 8002.3)
        self.assertNotIn("holding_updated_at", gold)
        self.assertNotIn("cash_updated_at", updated)

    def test_preserves_other_fund_fields(self):
        updated, _ = local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "022459", "holding_amount": 13000}]}, now_iso=NOW)
        fund = fund_by_code(updated, "022459")
        self.assertEqual(fund["profit_pct"], 0.3)
        self.assertEqual(fund["max_holding_amount"], 25000.0)
        self.assertEqual(fund["strategy"], "每周定投500")

    def test_does_not_mutate_input(self):
        original = copy.deepcopy(self.config)
        local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "022459", "holding_amount": 13000}],
                          "cash_available": 1}, now_iso=NOW)
        self.assertEqual(self.config, original)

    def test_profit_pct_editable(self):
        updated, _ = local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "022459", "holding_amount": 13000, "profit_pct": 5.25}]}, now_iso=NOW)
        self.assertEqual(fund_by_code(updated, "022459")["profit_pct"], 5.25)

    def test_profit_pct_allows_negative(self):
        updated, _ = local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "014661", "holding_amount": 8000, "profit_pct": -6.9}]}, now_iso=NOW)
        self.assertEqual(fund_by_code(updated, "014661")["profit_pct"], -6.9)

    def test_profit_pct_blank_becomes_none(self):
        for blank in ("", "   ", None):
            cfg = copy.deepcopy(BASE_CONFIG)
            updated, _ = local_server.apply_portfolio_update(
                cfg, {"holdings": [{"code": "022459", "holding_amount": 13000, "profit_pct": blank}]}, now_iso=NOW)
            self.assertIsNone(fund_by_code(updated, "022459")["profit_pct"])

    def test_profit_pct_rejects_below_minus_100(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(
                self.config, {"holdings": [{"code": "022459", "holding_amount": 13000, "profit_pct": -150}]}, now_iso=NOW)

    def test_profit_pct_rejects_non_number(self):
        with self.assertRaises(ValueError):
            local_server.apply_portfolio_update(
                self.config, {"holdings": [{"code": "022459", "holding_amount": 13000, "profit_pct": "x"}]}, now_iso=NOW)

    def test_profit_pct_unchanged_when_omitted(self):
        updated, _ = local_server.apply_portfolio_update(
            self.config, {"holdings": [{"code": "022459", "holding_amount": 13000}]}, now_iso=NOW)
        self.assertEqual(fund_by_code(updated, "022459")["profit_pct"], 0.3)

    def test_last_updated_is_latest_stamp(self):
        # Pre-existing older stamp on gold should be superseded by the newer cash stamp.
        self.config["funds"][1]["holding_updated_at"] = "2026-06-01T09:00:00+08:00"
        updated, last = local_server.apply_portfolio_update(
            self.config, {"cash_available": 40000}, now_iso=NOW)
        self.assertEqual(last, NOW)


class ValidatePayloadOptionalHoldingTests(unittest.TestCase):
    def _settings_payload(self, current):
        # Mimics settings.html after the amount fields were removed: every numeric
        # field except holding_amount is still sent.
        funds = []
        for fund in current["funds"]:
            funds.append({
                "code": fund["code"], "name": fund["name"], "type": fund["type"],
                "asset_class": fund["asset_class"], "strategy": fund["strategy"],
                "profit_pct": fund["profit_pct"], "daily_auto_invest": fund["daily_auto_invest"],
                "weekly_auto_invest": fund["weekly_auto_invest"],
                "max_holding_amount": fund["max_holding_amount"],
                "drawdown_20_buy_amount": fund["drawdown_20_buy_amount"],
                "drawdown_30_buy_amount": fund["drawdown_30_buy_amount"],
            })
        return {"funds": funds}

    def test_omitted_holding_amount_is_preserved(self):
        current = copy.deepcopy(BASE_CONFIG)
        current["funds"][0]["holding_updated_at"] = NOW
        updated = local_server.validate_payload(self._settings_payload(current), current)
        fund = fund_by_code(updated, "022459")
        self.assertEqual(fund["holding_amount"], 12097.68)
        self.assertEqual(fund["holding_updated_at"], NOW)

    def test_omitted_cash_is_preserved(self):
        current = copy.deepcopy(BASE_CONFIG)
        updated = local_server.validate_payload(self._settings_payload(current), current)
        self.assertEqual(updated["cash_available"], 50000.0)

    def test_submitted_non_amount_field_still_updates(self):
        current = copy.deepcopy(BASE_CONFIG)
        payload = self._settings_payload(current)
        payload["funds"][0]["max_holding_amount"] = 30000.0
        updated = local_server.validate_payload(payload, current)
        self.assertEqual(fund_by_code(updated, "022459")["max_holding_amount"], 30000.0)


if __name__ == "__main__":
    unittest.main()
