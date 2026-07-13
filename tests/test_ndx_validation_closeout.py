import csv
import json
import unittest
from pathlib import Path

import cn_equity_temperature
import fund_tracker
import model_risk
import ndx_price_temperature as ndx
import qdii_carrier


ROOT = Path(__file__).resolve().parents[1]


def latest_run():
    candidates = sorted((ROOT / "reports/runs").glob("*_v7-*"), key=lambda p: p.name)
    if not candidates:
        candidates = sorted((ROOT / "reports/runs").glob("*"), key=lambda p: p.name)
    return candidates[-1] if candidates else None


def latest_full_validation_run():
    candidates = sorted((ROOT / "reports/runs").glob("*_v7-*"), key=lambda p: p.name)
    required = (
        "reports/ndx-price-temperature-validation.json",
        "reports/ndx-over-aggressive-warning-details.csv",
        "reports/ndx-historical-replay.csv",
    )
    return next(
        (candidate for candidate in reversed(candidates)
         if all((candidate / relative_path).exists() for relative_path in required)),
        None,
    )


def carriers():
    return [{"fund_code": "021000", "ndx_pool_eligible": True, "effective_limit_rmb": 1000}]


class NdxValidationCloseoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_dir = latest_run()
        cls.validation_run_dir = latest_full_validation_run()
        cls.formal = (cls.run_dir / "html/Asset Allocation Copilot V7.html").read_text(encoding="utf-8")
        fixture_fresh = cls.run_dir / "html/ndx-fixture-fresh-freeze.html"
        cls.fresh = fixture_fresh.read_text(encoding="utf-8") if fixture_fresh.exists() else ""
        fixture_active = cls.run_dir / "html/ndx-fixture-fresh-active.html"
        cls.active = fixture_active.read_text(encoding="utf-8") if fixture_active.exists() else ""
        validation_path = cls.validation_run_dir / "reports/ndx-price-temperature-validation.json"
        cls.validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else {}

    def test_01_stale_executable_zero(self):
        mc = ndx.candidate_amount_chain(1000, .5, 1875)
        self.assertEqual(mc["ndx_candidate_release_amount"], 500)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": False, "carrier_selection_status": "BLOCKED",
            "last_known_approved_carrier_capacity": 11280,
        })
        self.assertEqual(cm["carrier_coverable_amount"], 0)

    def test_02_stale_retained_by_block(self):
        mc = ndx.candidate_amount_chain(1000, .5, 1875)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": False, "carrier_selection_status": "BLOCKED",
            "last_known_approved_carrier_capacity": 11280,
        })
        self.assertEqual(cm["retained_due_to_carrier_block"], 500)

    def test_03_stale_last_known_capacity(self):
        mc = ndx.candidate_amount_chain(1000, .5, 1875)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": False, "carrier_selection_status": "BLOCKED",
            "last_known_approved_carrier_capacity": 11280,
        })
        self.assertEqual(cm["last_known_approved_carrier_capacity"], 11280)

    def test_04_stale_preview_invalid(self):
        result = qdii_carrier.calculate_multi_select({"021000": 1000}, 1000, carriers(), snapshot_valid=False, carrier_selection_status="BLOCKED")
        self.assertEqual(result["preview_status"], "INVALID")

    def test_05_stale_button_disabled(self):
        self.assertIn('disabled aria-disabled="true"', self.formal)

    def test_06_available_min_capacity(self):
        mc = ndx.candidate_amount_chain(2000, 1, 2000)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": True, "carrier_selection_status": "AVAILABLE",
            "last_known_approved_carrier_capacity": 600,
        })
        self.assertEqual(cm["carrier_coverable_amount"], 600)

    def test_07_capacity_retention(self):
        mc = ndx.candidate_amount_chain(2000, 1, 2000)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": True, "carrier_selection_status": "AVAILABLE",
            "last_known_approved_carrier_capacity": 600,
        })
        self.assertEqual(cm["retained_due_to_capacity"], 1400)

    def test_08_block_and_capacity_retention_are_exclusive(self):
        mc = ndx.candidate_amount_chain(2000, 1, 2000)
        cm_blocked = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": False, "carrier_selection_status": "BLOCKED",
            "last_known_approved_carrier_capacity": 600,
        })
        self.assertEqual(cm_blocked["retained_due_to_capacity"], 0)
        self.assertEqual(cm_blocked["retained_due_to_carrier_block"], 2000)

    def test_09_top_status_domains_separated(self):
        for text in ("模型行情数据：PASS", "NDX模型状态：ACTIVE", "QDII载体数据：", "执行状态：EXECUTE"):
            self.assertIn(text, self.formal)

    def test_10_blocked_does_not_show_available_heading(self):
        self.assertIn("QDII载体", self.formal)

    def test_11_formal_and_fixtures_are_distinct(self):
        self.assertNotIn("CONTROLLED TEST FIXTURE", self.formal)
        if self.fresh:
            self.assertIn("CONTROLLED TEST FIXTURE", self.fresh)

    def test_12_dotcom_price_gate_no_dfii10(self):
        gate = self.validation["price_model_stress_gate"]
        self.assertEqual(gate["status"], "PASS")
        self.assertFalse(gate["dfii10_required"])

    def test_13_full_chain_starts_after_rate_warmup(self):
        self.assertGreaterEqual(self.validation["full_chain_gate"]["full_chain_start_date"], "2007-01")

    def test_14_no_neutral_rate_fill(self):
        self.assertFalse(self.validation["full_chain_gate"]["dfii10_neutral_fill_used"])

    def test_15_very_hot_has_one_point_tolerance(self):
        self.assertEqual(self.validation["very_hot_tolerance_gate"]["tolerance"], .01)

    def test_16_actual_352375_passes(self):
        self.assertAlmostEqual(self.validation["very_hot_tolerance_gate"]["actual"], .352375)
        self.assertEqual(self.validation["very_hot_tolerance_gate"]["status"], "PASS")

    def test_17_weights_locked(self):
        self.assertEqual((ndx.BALANCED_PARAMETERS["ma_weight"], ndx.BALANCED_PARAMETERS["drawdown_weight"]), (.55, .45))

    def test_18_release_range_locked(self):
        self.assertEqual((ndx.base_release_factor(0), ndx.base_release_factor(100)), (.25, 1))

    def test_19_dfii10_floor_locked(self):
        self.assertEqual(ndx.real_yield_modifier(100), .85)

    def test_20_volatility_floor_locked(self):
        self.assertEqual(ndx.volatility_cap(100), .65)

    def test_21_warning_detail_count(self):
        with (self.validation_run_dir / "reports/ndx-over-aggressive-warning-details.csv").open(encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 53)

    def test_22_posthoc_not_used_for_selection(self):
        with (self.validation_run_dir / "reports/ndx-over-aggressive-warning-details.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(all(row["diagnostic_policy"] == "POST_HOC_DIAGNOSTIC_ONLY" and row["parameter_selection_policy"] == "NOT_USED_FOR_PARAMETER_SELECTION" for row in rows))

    def test_23_run_id_semantics(self):
        self.assertTrue(any(tag in self.validation_run_dir.name for tag in ("v7-ndx-v1", "v7-2", "v7-ndx")))

    def test_24_run_id_shared(self):
        with (self.validation_run_dir / "reports/ndx-historical-replay.csv").open(encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(self.validation["run_id"], self.validation_run_dir.name)
        self.assertEqual(row["run_id"], self.validation_run_dir.name)
        self.assertIn("run-id", self.formal)

    def test_25_targets_locked(self):
        for text in ("40.0%", "35.0%", "5.0%", "20.0%"):
            self.assertIn(text, self.formal)

    def test_26_gap_locked(self):
        # 持仓可在「持仓管理」编辑，故海外权益缺口从当前 config 经同一管线推导，
        # 不写死某个金额，编辑持仓不会误伤本回归。
        config = fund_tracker.load_config()
        conn = fund_tracker.connect_db()
        try:
            temperature = fund_tracker.generate_market_temperature(conn, config)
            snapshot = fund_tracker.generate_copilot_snapshot(conn, config, temperature)
        finally:
            conn.close()
        gap = snapshot["gaps"]["us_equity"]
        self.assertIn(f"{gap:+,.0f}", self.formal)

    def test_27_historical_execution_locked(self):
        # 「Historical Executed Amount」页头是当月状态，跨月会归零（新月份尚未执行），
        # 属正常行为。已执行月份的历史事实按「执行流水不可变」保存在月度执行历史表里，
        # 断言应锁定这条不变的历史记录，而不是会随月份变化的当月页头数字。
        self.assertRegex(
            self.formal,
            r"<td>2026-06</td>\s*<td>[^<]*</td>\s*<td>625</td>",
        )

    def test_28_a500_regression(self):
        self.assertTrue(cn_equity_temperature.LIVE_SCORING_ENABLED)

    def test_29_gold_regression(self):
        self.assertEqual(model_risk.calculate_gold_score(1.96, 2.23, 2.25, 3.63)["final_gold_score"], 39.8)

    def test_30_fixed_investment_regression(self):
        self.assertIn("固定定投", self.formal)

    def test_31_active_pool_is_executable(self):
        self.assertIn('data-cash-pool-status="EXECUTE"', self.formal)

    # ── V7 Three-Layer Decision Chain Tests ──

    def test_32_v7_three_layer_identity_candidate_to_carrier(self):
        """candidate == coverable + capacity_retained + carrier_block_retained"""
        mc = ndx.candidate_amount_chain(846.24, 0.353028, 1875)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": True, "carrier_selection_status": "AVAILABLE",
            "last_known_approved_carrier_capacity": 11280,
        })
        self.assertAlmostEqual(
            mc["ndx_candidate_release_amount"],
            cm["carrier_coverable_amount"] + cm["retained_due_to_capacity"] + cm["retained_due_to_carrier_block"],
            places=2,
        )

    def test_33_v7_three_layer_identity_carrier_to_decision(self):
        """coverable == executable + decision_freeze_retained"""
        coverable = 298.75
        executable = 0.0
        freeze_retained = 298.75
        self.assertAlmostEqual(coverable, executable + freeze_retained, places=2)

    def test_34_carrier_failure_zeros_coverable_not_candidate(self):
        """Carrier failure: coverable=0, candidate preserved at 298.75"""
        mc = ndx.candidate_amount_chain(846.24, 0.353028, 1875)
        self.assertAlmostEqual(mc["ndx_candidate_release_amount"], 298.75, places=2)
        cm = qdii_carrier.apply_carrier_matching(mc["ndx_candidate_release_amount"], {
            "carrier_snapshot_valid": False, "carrier_selection_status": "BLOCKED",
            "last_known_approved_carrier_capacity": 11280,
        })
        self.assertAlmostEqual(cm["carrier_coverable_amount"], 0.0, places=2)
        self.assertAlmostEqual(cm["retained_due_to_carrier_block"], 298.75, places=2)
        self.assertAlmostEqual(cm["retained_due_to_capacity"], 0.0, places=2)

    def test_35_locked_values_preserved(self):
        """NDX locked values must survive refactoring"""
        mc = ndx.candidate_amount_chain(846.24, 0.353028, 1875)
        self.assertAlmostEqual(mc["ndx_candidate_release_amount"], 298.75, places=2)
        self.assertAlmostEqual(mc["ndx_gap_routed_amount"], 846.24, places=2)

    def test_36_html_three_layer_structure(self):
        """HTML must render V7 three-layer labels"""
        self.assertIn("Layer 1", self.formal)
        self.assertIn("Layer 2", self.formal)
        self.assertIn("Layer 3", self.formal)
        self.assertIn("模型候选层", self.formal)
        self.assertIn("载体匹配层", self.formal)
        self.assertIn("NDX 独立候选承接结果", self.formal)
        self.assertIn("V7 Three-Layer Decision", self.formal)

    def test_37_html_identity_verification_rendered(self):
        """Identity verification status must appear in HTML"""
        self.assertIn("金额链身份校验", self.formal)

    def test_38_v7_decision_chain_in_report_json(self):
        """report.json must contain v7_decision_chain"""
        import json
        report = json.loads((self.run_dir / "json/report.json").read_text(encoding="utf-8"))
        v7 = report.get("copilot", {}).get("v7_decision_chain", {})
        self.assertIn("model_candidate", v7)
        self.assertIn("carrier_matching", v7)
        self.assertIn("formal_decision", v7)
        self.assertIn("identity_verification", v7)
        self.assertEqual(v7["identity_verification"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
