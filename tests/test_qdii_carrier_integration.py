import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import cn_equity_temperature
import fund_tracker
import model_risk
import qdii_carrier


def snapshot_payload(generated_at="2026-06-19 12:00:00"):
    source = {"name": "channel", "type": "SECONDARY_CHANNEL_OBSERVATION",
              "confidence": "SECONDARY", "observed_at": generated_at}
    return {"schema_version": "1.0.0", "generated_at": generated_at,
            "producer": "test-monitor", "contract": {"not_investment_signal": True},
            "funds": [
                {"code": "539001", "name": "NDX Existing A", "pool": "NDX_INDEX_QDII_POOL",
                 "benchmark": "NASDAQ_100", "observed_channel_limit_rmb": 100,
                 "tracking_error_pct": 2.15, "purchase_fee_display": "0.12%", "source": source},
                {"code": "040046", "name": "NDX Lowest A", "pool": "NDX_INDEX_QDII_POOL",
                 "benchmark": "NASDAQ_100", "observed_channel_limit_rmb": 10,
                 "tracking_error_pct": 1.05, "purchase_fee_display": "0.12%", "source": source},
                {"code": "019441", "name": "Volatile A", "pool": "NDX_INDEX_QDII_POOL",
                 "benchmark": "NASDAQ_100", "observed_channel_limit_rmb": 10000,
                 "tracking_error_pct": 1.58, "purchase_fee_display": "0.10%", "source": source},
                {"code": "021000", "name": "NDX I", "pool": "NDX_INDEX_QDII_POOL",
                 "benchmark": "NASDAQ_100", "observed_channel_limit_rmb": 1000,
                 "tracking_error_pct": 1.46, "purchase_fee_display": "--", "source": source},
                {"code": "270023", "name": "Global Active", "pool": "GLOBAL_ACTIVE_EQUITY_POOL",
                 "benchmark": "MSCI_WORLD", "observed_channel_limit_rmb": 100, "source": source},
            ],
            "recent_changes": [
                {"code": "019441", "old_limit": "50", "new_limit": "10000"},
                {"code": "019441", "old_limit": "10000", "new_limit": "50"},
            ]}


class QdiiCarrierIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config = {"funds": [
            {"code": "539001", "holding_amount": 14624},
            {"code": "270023", "holding_amount": 7565},
        ]}

    def write_snapshot(self, directory, payload):
        path = Path(directory) / "snapshot.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_json_schema_validation_blocks_invalid_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = snapshot_payload(); payload["contract"]["not_investment_signal"] = False
            result = qdii_carrier.read_snapshot(self.write_snapshot(directory, payload))
            self.assertEqual(result["carrier_selection_status"], "BLOCKED")

    def test_hard_stale_snapshot_blocks_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            result = qdii_carrier.read_snapshot(self.write_snapshot(directory, snapshot_payload()),
                                                now=dt.datetime(2026, 6, 19, 13, 1))
            self.assertEqual(result["carrier_selection_status"], "BLOCKED")

    def test_all_json_funds_are_approved(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        self.assertTrue(all(row["approved"] for row in rows))
        self.assertTrue(all(row["approved_by"] == "manual_review" for row in rows))

    def test_guangfa_never_enters_ndx_pool(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        item = next(row for row in rows if row["fund_code"] == "270023")
        self.assertFalse(item["ndx_pool_eligible"])
        self.assertFalse(item["dynamic_release_eligible"])

    def test_i_class_personal_purchase_is_supported_when_channel_available(self):
        item = next(row for row in qdii_carrier.whitelist_carriers(snapshot_payload()) if row["fund_code"] == "021000")
        self.assertEqual(item["share_class"], "I")
        self.assertTrue(item["personal_purchase_supported"])
        self.assertTrue(item["channel_available"])

    def test_existing_holding_ranks_first(self):
        result = qdii_carrier.select_carriers(40, snapshot_payload(), self.config)
        self.assertEqual(result["recommended_carrier"]["fund_code"], "539001")

    def test_auto_plan_excludes_unconfigured_i_class_and_retains_remainder(self):
        payload = snapshot_payload()
        payload["carrier_data_status"] = "ACTIVE"
        payload["carrier_selection_status"] = "AVAILABLE"
        result = qdii_carrier.select_carriers(400, payload, self.config)
        self.assertEqual([row["fund_code"] for row in result["carrier_plan"]], ["539001"])
        self.assertEqual(result["allocated_amount"], 100)
        self.assertEqual(result["remaining_unallocated_amount"], 300)
        self.assertEqual(result["carrier_capacity_status"], "PARTIAL_CAPACITY")
        self.assertTrue(any("未获批准的I类基金不参与" in row for row in result["warnings"]))

    def test_explicitly_approved_i_class_is_preferred_for_current_plan(self):
        payload = snapshot_payload()
        payload["carrier_data_status"] = "ACTIVE"
        payload["carrier_selection_status"] = "AVAILABLE"
        approved = {"copilot_v7": {
            "approved_i_class_carriers": ["021000"],
            "execution_funds": {"us_equity": "021000"},
        }, "funds": self.config["funds"] + [
            {"code": "021000", "holding_amount": 0},
        ]}
        result = qdii_carrier.select_carriers(400, payload, approved)
        self.assertEqual(result["carrier_plan"][0]["fund_code"], "021000")
        self.assertEqual(result["carrier_plan"][0]["planned_amount"], 400)
        self.assertEqual(result["remaining_unallocated_amount"], 0)

    def test_partial_capacity_is_coverable_without_becoming_a_carrier_block(self):
        result = qdii_carrier.apply_carrier_matching(400, {
            "carrier_snapshot_valid": True,
            "carrier_selection_status": "PARTIAL_CAPACITY",
            "last_known_approved_carrier_capacity": 100,
        })
        self.assertEqual(result["carrier_coverable_amount"], 100)
        self.assertEqual(result["retained_due_to_capacity"], 300)
        self.assertEqual(result["retained_due_to_carrier_block"], 0)

    def test_single_cover_tag(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        tags = qdii_carrier.transparent_tags(rows, 625)
        self.assertIn("单只可覆盖", tags["019441"]["advantages"])

    def test_lowest_tracking_error_tag(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        tags = qdii_carrier.transparent_tags(rows, 100)
        self.assertIn("跟踪误差最低", tags["040046"]["advantages"])

    def test_missing_fee_and_size_are_explicit_risks(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        tags = qdii_carrier.transparent_tags(rows, 100)
        # 021000 now has fee data from lookup: shows comprehensive rate
        self.assertTrue(any("综合费率" in a for a in tags["021000"]["advantages"]))
        self.assertIn("规模待补齐", tags["021000"]["risks"])

    def test_wanjia_limit_volatility_is_flagged(self):
        item = next(row for row in qdii_carrier.whitelist_carriers(snapshot_payload()) if row["fund_code"] == "019441")
        self.assertTrue(item["limit_volatility_flag"])

    def test_multi_select_capacity_calculation(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        result = qdii_carrier.calculate_multi_select({"539001": 100, "021000": 200}, 400, rows)
        self.assertEqual(result["selected_total_capacity"], 1100)
        self.assertEqual(result["allocated_amount"], 300)
        self.assertEqual(result["remaining_uncovered_amount"], 100)

    def test_over_selection_and_complexity_are_not_blocked(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        selected = {row["fund_code"]: 100 for row in rows if row["ndx_pool_eligible"]}
        result = qdii_carrier.calculate_multi_select(selected, 100, rows)
        self.assertGreater(result["over_selected_amount"], 0)
        self.assertIn("复杂度", result["complexity_warning"])

    def test_overseas_split_uses_actual_holdings(self):
        rows = qdii_carrier.whitelist_carriers(snapshot_payload(), self.config)
        result = qdii_carrier.overseas_equity_split(self.config, rows)
        self.assertEqual(result["ndx_qdii_amount"], 14624)
        self.assertEqual(result["global_active_amount"], 7565)
        self.assertEqual(result["overseas_equity_total"], 22189)

    def test_selection_is_read_only(self):
        before = Path("config.json").read_bytes()
        qdii_carrier.select_carriers(100, snapshot_payload(), self.config)
        self.assertEqual(Path("config.json").read_bytes(), before)

    def test_protected_models_and_targets_unchanged(self):
        config = fund_tracker.load_config()
        self.assertTrue(cn_equity_temperature.LIVE_SCORING_ENABLED)
        self.assertEqual(model_risk.calculate_gold_score(1.96, 2.23, 2.25, 3.63)["final_gold_score"], 39.8)
        self.assertEqual(config["copilot_v7"]["strategic_allocation"], {"a_share": .4, "us_equity": .4, "gold": .1, "cash": .1})
        self.assertEqual(next(item for item in config["funds"] if item["code"] == "270023")["weekly_auto_invest"], 100.0)

    def test_snapshot_generation_does_not_change_dynamic_cash_pool(self):
        config = fund_tracker.load_config(); conn = fund_tracker.connect_db()
        try:
            before = fund_tracker.get_state(conn, "dynamic_cash_pool", 0)
            snapshot = fund_tracker.generate_copilot_snapshot(conn, config, fund_tracker.generate_market_temperature(conn, config))
            self.assertEqual(snapshot["legacy_us_equity_score_status"], "RETIRED")
            self.assertIn(snapshot["decision_status"], ("FREEZE", "EXECUTE"))
            self.assertEqual(fund_tracker.get_state(conn, "dynamic_cash_pool", 0), before)
            conn.rollback()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
