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
        # 结构性不变量：021000 是 I 类份额（不随外部快照的申购状态漂移）。
        i_class = next(row for row in self.carriers if row["fund_code"] == "021000")
        self.assertEqual(i_class["share_class"], "I")
        # 「个人可买」是外部 qdii-monitor 快照的事实字段，会随快照变化；断言页面
        # 忠实渲染该字段的当前值，而不是写死某个 是/否。
        expected = "个人可买：%s" % ("是" if i_class["personal_purchase_supported"] else "否")
        row_start = self.html.index('data-code="021000"')
        self.assertIn(expected, self.html[row_start:row_start + 700])

    def test_multi_select_amounts_and_missing_fields_are_visible(self):
        self.assertGreater(self.html.count('class="qdii-select"'), 1)
        self.assertGreater(self.html.count('class="qdii-amount"'), 1)
        self.assertIn("有效覆盖金额", self.html)
        self.assertIn("剩余未覆盖", self.html)
        self.assertIn("超额", self.html)
        self.assertIn("待补齐", self.html)

    def test_transparent_tags_and_volatility_disclosure(self):
        # 「单只可覆盖」是否出现，取决于当月 Dynamic Cash Pool 规模（随月度新增资金变化）
        # 与外部 QDII 载体额度快照（独立于本仓库更新）两者的实时大小关系，两者都会随
        # 时间独立漂移，不适合在渲染页面里断言某个标签必然/必不出现；改由下面的
        # test_single_carrier_coverage_tag 对产出该标签的纯函数做受控输入验证。
        for label in ("已有持仓", "跟踪误差最低", "综合费率", "规模待补齐"):
            self.assertIn(label, self.html)
        self.assertIn("50元与10000元之间反复切换", self.html)
        self.assertIn("底层指数高度重合", self.html)
        self.assertNotIn("Carrier Score", self.html)

    def test_single_carrier_coverage_tag(self):
        carriers = [
            {"fund_code": "A", "ndx_pool_eligible": True, "effective_limit_rmb": 1000},
            {"fund_code": "B", "ndx_pool_eligible": True, "effective_limit_rmb": 500},
        ]
        tags = qdii_carrier.transparent_tags(carriers, asset_amount=800)
        self.assertIn("单只可覆盖", tags["A"]["advantages"])
        self.assertNotIn("单只可覆盖", tags["B"]["advantages"])

    def test_execution_controls_follow_live_cash_pool_status(self):
        if 'data-cash-pool-status="FREEZE"' in self.html:
            self.assertIn("Current Decision: 0 元", self.html)
            self.assertIn("执行已禁用", self.html)
            self.assertIn('disabled aria-disabled="true"', self.html)
        else:
            self.assertIn('data-cash-pool-status="EXECUTE"', self.html)
            self.assertIn("本月动态资金释放方向", self.html)
            self.assertIn("ACTIVE · 已进入正式决策", self.html)
            self.assertIn("执行本月方案", self.html)
            self.assertIn("确认执行并入账", self.html)
        self.assertIn("此区域仅用于载体能力预览，不执行、不入账", self.html)
        self.assertNotIn('id="qdii-execute-button"', self.html)
        # 页头「Historical Executed Amount」是当月状态，跨月会归零；已执行月份的
        # 历史事实按「执行流水不可变」锁定在月度执行历史表里，断言应指向那条记录。
        self.assertRegex(
            self.html,
            r"<td>2026-06</td>\s*<td>[^<]*</td>\s*<td>625</td>",
        )

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
        self.assertRegex(
            self.html,
            r"<td>2026-06</td>\s*<td>[^<]*</td>\s*<td>625</td>",
        )


if __name__ == "__main__":
    unittest.main()
