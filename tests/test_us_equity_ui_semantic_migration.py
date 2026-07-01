import unittest
from pathlib import Path

import cn_equity_temperature
import fund_tracker
import model_risk
import qdii_carrier


class UsEquityUiSemanticMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("dist/Asset Allocation Copilot V7.html").read_text(encoding="utf-8")
        cls.config = fund_tracker.load_config()
        cls.snapshot = qdii_carrier.read_snapshot()
        cls.carriers = qdii_carrier.whitelist_carriers(cls.snapshot, cls.config)

    def test_homepage_has_four_semantic_asset_cards_without_legacy_score(self):
        for label in ("A股", "纳指指数型QDII", "全球主动权益", "黄金"):
            self.assertIn(label, self.html)
        self.assertNotIn("美股Score", self.html)
        self.assertNotIn(">22.6<", self.html)

    def test_pe_is_display_only_and_non_blocking(self):
        self.assertIn("nasdaq100_pe.used_in_score</td><td>false", self.html)
        self.assertIn("sp500_pe.used_in_score</td><td>false", self.html)
        self.assertIn("blocking</td><td>false · DISPLAY_ONLY", self.html)
        inputs = fund_tracker.build_data_quality_inputs({}, {}, {"modelEnabled": True}, {}, {}, {}, {}, {})
        pe = [row for row in inputs if row["indicator"] in ("nasdaq100_pe_percentile", "sp500_pe_percentile")]
        self.assertTrue(all(not row["used_in_score"] and not row["blocking"] for row in pe))

    def test_global_active_never_appears_in_ndx_selector(self):
        selector_start = self.html.index("JSON已批准白名单 · 多选与金额预览")
        selector_end = self.html.index("Global Active Equity Pool", selector_start)
        self.assertNotIn("270023", self.html[selector_start:selector_end])

    def test_overseas_split_matches_actual_holdings(self):
        split = qdii_carrier.overseas_equity_split(self.config, self.carriers)
        # 持仓金额可在「持仓管理」编辑，期望值从当前 config 推导而非写死，
        # 这样编辑持仓不会误伤本回归（拆分=NDX指数QDII vs 全球主动）。
        self.assertGreater(split["ndx_qdii_amount"], 0)
        self.assertGreater(split["global_active_amount"], 0)
        # Overview 不再重复「海外权益结构」概览块；拆分的金额与占比现在只在
        # 「配置与资金流」Tab 的海外权益拆分表中呈现（分列展示）。
        self.assertIn(f"{split['ndx_qdii_amount']:,.0f} 元", self.html)
        self.assertIn(f"{split['ndx_qdii_ratio'] * 100:.1f}%", self.html)
        self.assertIn(f"{split['global_active_amount']:,.0f} 元", self.html)
        self.assertIn(f"{split['global_active_ratio'] * 100:.1f}%", self.html)

    def test_json_whitelist_and_i_class_semantics(self):
        self.assertTrue(all(row["approved"] for row in self.carriers))
        i_class = next(row for row in self.carriers if row["fund_code"] == "021000")
        self.assertTrue(i_class["personal_purchase_supported"])
        self.assertIn("个人可买：是", self.html)

    def test_multi_select_amounts_and_missing_fields_are_visible(self):
        self.assertGreater(self.html.count('class="qdii-select"'), 1)
        self.assertGreater(self.html.count('class="qdii-amount"'), 1)
        self.assertIn("有效覆盖金额", self.html)
        self.assertIn("剩余未覆盖", self.html)
        self.assertIn("超额", self.html)
        self.assertIn("待补齐", self.html)

    def test_transparent_tags_and_volatility_disclosure(self):
        for label in ("已有持仓", "单只可覆盖", "跟踪误差最低", "综合费率", "规模待补齐"):
            self.assertIn(label, self.html)
        self.assertIn("50元与10000元之间反复切换", self.html)
        self.assertIn("底层指数高度重合", self.html)
        self.assertNotIn("Carrier Score", self.html)

    def test_freeze_and_execution_controls(self):
        self.assertIn("Current Decision: 0 元", self.html)
        self.assertIn("当前FREEZE，执行确认不可用", self.html)
        self.assertIn('disabled aria-disabled="true"', self.html)
        self.assertIn("Historical Executed Amount: 625 元", self.html)

    def test_manual_carrier_and_add_holding_features_are_removed(self):
        for forbidden in ("手动添加载体", "加入观察名单", "/api/qdii/carriers/manual",
                          "/api/qdii/carriers/status", "添加持仓"):
            self.assertNotIn(forbidden, self.html)
        server = Path("local_server.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/qdii/carriers/manual", server)
        self.assertNotIn("/api/qdii/carriers/status", server)

    def test_protected_models_targets_fixed_investment_and_history(self):
        self.assertTrue(cn_equity_temperature.LIVE_SCORING_ENABLED)
        self.assertEqual(model_risk.calculate_gold_score(1.96, 2.23, 2.25, 3.63)["final_gold_score"], 39.8)
        self.assertEqual(self.config["copilot_v7"]["strategic_allocation"],
                         {"a_share": .4, "us_equity": .4, "gold": .1, "cash": .1})
        self.assertEqual(next(row for row in self.config["funds"] if row["code"] == "270023")["weekly_auto_invest"], 100.0)
        self.assertIn("Historical Executed Amount: 625 元", self.html)


if __name__ == "__main__":
    unittest.main()
