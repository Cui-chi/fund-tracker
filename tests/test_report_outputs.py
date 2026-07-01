import tempfile
import unittest
from pathlib import Path

import model_risk
import fund_tracker
from utils import output_paths
from test_decision_snapshot import decision_payload


class ReportOutputTests(unittest.TestCase):
    def test_disabled_a_share_price_model_does_not_gate_hs300_environment(self):
        inputs = fund_tracker.build_data_quality_inputs(
            {}, {}, {"modelEnabled": False}, {}, {}, {}, {}, {},
        )
        hs300 = next(
            item for item in inputs
            if item["indicator"] == "hs300_price_environment"
        )
        self.assertFalse(hs300["used_in_score"])

    def test_all_required_reports_are_generated(self):
        base = Path(model_risk.__file__).resolve().parent
        run_dirs = sorted((base / "reports" / "runs").glob("*"), reverse=True)
        self.assertTrue(any(model_risk.reports_exist(path) for path in run_dirs))

    def test_freeze_dashboard_separates_current_and_historical_amounts(self):
        dashboard = output_paths.get_dist_path("Asset Allocation Copilot V7.html")
        text = dashboard.read_text(encoding="utf-8")
        self.assertIn("Current Decision: 0 元", text)
        self.assertIn("<tr><td>Release Amount</td><td>0 元</td></tr>", text)
        self.assertIn("Current Recommended Flow: 0 元", text)
        # 「Historical Executed Amount」页头是当月状态，跨月会归零；已执行月份的
        # 历史事实按「执行流水不可变」锁定在月度执行历史表里，断言应指向那条记录。
        self.assertRegex(text, r"<td>2026-06</td>\s*<td>[^<]*</td>\s*<td>625</td>")
        self.assertIn("执行已禁用（FREEZE）", text)
        # A500 model is now ACTIVE (not blocked); PE/PB are display-only
        self.assertIn("估值数据当前仅供参考", text)
        self.assertIn("不参与当前自动评分", text)
        self.assertIn("CN_EQUITY_PRICE_TEMP_V1", text)
        self.assertIn("Display Only", text)
        self.assertIn("PENDING_PROXY_REVIEW", text)
        self.assertIn("Approval Status", text)
        self.assertIn("Coverage", text)
        self.assertIn('disabled aria-disabled="true"', text)
        self.assertIn("strategic_target", text)
        self.assertIn("target_reason", text)
        # A500 score rendered from cn_equity_temperature model
        self.assertIn('ac-score', text)
        self.assertNotIn("hs300_price_environment — 代理源待审批 · 参与 Score", text)
        for forbidden in (
            "Current Decision: 625",
            "Release Amount: 625",
            "Current Recommended Flow: 625",
            "资产层建议 625",
            "本月已执行 625",
            "Manual Review",
            "Manual Override",
            "MANUAL_REVIEW",
            "A500价格源尚未通过稳定性门",
            "BLOCKED_BY_A500_PRICE_DATA",
        ):
            self.assertNotIn(forbidden, text)
