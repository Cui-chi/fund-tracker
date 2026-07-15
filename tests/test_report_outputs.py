import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import model_risk
import fund_tracker
from utils import output_paths
from test_decision_snapshot import decision_payload


class ReportOutputTests(unittest.TestCase):
    def test_executed_dashboard_labels_plan_actual_and_difference_precisely(self):
        base = Path(model_risk.__file__).resolve().parent
        report_paths = sorted(
            (base / "reports" / "runs").glob("*/json/report.json"),
            reverse=True,
        )
        payload = json.loads(report_paths[0].read_text(encoding="utf-8"))
        copilot = copy.deepcopy(payload["copilot"])
        copilot.update({
            "status": "executed",
            "user_decision": "execute",
            "allow_auto_execution": True,
            "dynamic_cash_pool_status": "EXECUTE",
            "plan_amount": 674.75,
            "executed_amount": 673.0,
            "unexecuted_amount": 1.75,
            "remaining_dynamic_cash_pool": 3702.0,
            "allocation_plan": {
                "a_share": 250.54, "us_equity": 424.21, "gold": 0.0,
            },
            "executed_allocations": {
                "a_share": 249.0, "us_equity": 424.0, "gold": 0.0,
            },
        })
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch.object(
                output_paths,
                "get_html_snapshot_path",
                side_effect=lambda name: tmp_path / name,
            ), mock.patch.object(
                output_paths,
                "get_dist_path",
                side_effect=lambda name: tmp_path / ("dist-" + name),
            ):
                fund_tracker.write_copilot_dashboard(
                    payload["rows"],
                    payload["macro"],
                    payload["marketTemperature"],
                    copilot,
                    payload.get("allocationHistory", []),
                    config=fund_tracker.load_config(),
                )
            html = (tmp_path / "Asset Allocation Copilot V7.html").read_text(
                encoding="utf-8"
            )
        self.assertIn(
            "本月原计划 674.75 元 · 实际执行 673.00 元，未执行差额 1.75 元",
            html,
        )
        self.assertIn("原资产层计划 674.75 元", html)
        self.assertIn("基金层实际执行 673.00 元", html)
        self.assertIn("未执行差额 1.75 元", html)
        self.assertIn("本月实际执行: 673.00 元", html)

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

    def test_dashboard_separates_current_and_historical_amounts(self):
        dashboard = output_paths.get_dist_path("Asset Allocation Copilot V7.html")
        text = dashboard.read_text(encoding="utf-8")
        if 'data-cash-pool-status="FREEZE"' in text:
            self.assertIn("Current Decision: 0 元", text)
            self.assertIn("<tr><td>Release Amount</td><td>0 元</td></tr>", text)
            self.assertIn("Current Recommended Flow: 0 元", text)
            self.assertIn("执行已禁用（FREEZE）", text)
            self.assertIn('disabled aria-disabled="true"', text)
        else:
            self.assertIn('data-cash-pool-status="EXECUTE"', text)
            self.assertIn("本月动态资金释放方向", text)
            self.assertIn("ACTIVE · 已进入正式决策", text)
            self.assertIn("独立候选承接上限", text)
            self.assertNotIn("Validation · 待影子运行", text)
        # 「Historical Executed Amount」页头是当月状态，跨月会归零；已执行月份的
        # 历史事实按「执行流水不可变」锁定在月度执行历史表里，断言应指向那条记录。
        self.assertRegex(text, r"<td>2026-06</td>\s*<td>[^<]*</td>\s*<td>625</td>")
        # A500 model is now ACTIVE (not blocked); PE/PB are display-only
        self.assertIn("估值数据当前仅供参考", text)
        self.assertIn("不参与当前自动评分", text)
        self.assertIn("CN_EQUITY_PRICE_TEMP_V1", text)
        self.assertIn("Display Only", text)
        self.assertIn("PENDING_PROXY_REVIEW", text)
        self.assertIn("Approval Status", text)
        self.assertIn("Coverage", text)
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
