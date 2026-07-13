import datetime as dt
import unittest

import daily_automation_status as das


REQUIRED_KEYS = {"label", "color", "trigger", "is_anomaly", "needs_manual",
                 "affects_graduation", "affects_dcp"}


class DailyAutomationStatusTests(unittest.TestCase):
    def test_every_state_has_full_semantic_contract(self):
        for key, spec in das.STATES.items():
            self.assertTrue(REQUIRED_KEYS.issubset(spec), "%s missing keys" % key)
            self.assertIn(spec["color"], (das.GREEN, das.BLUE, das.YELLOW, das.ORANGE, das.RED, das.GRAY))
            self.assertIsInstance(spec["label"], str)

    def test_every_final_status_maps_to_a_defined_state(self):
        for raw, key in das.FINAL_STATUS_MAP.items():
            self.assertIn(key, das.STATES)
            self.assertEqual(das.classify_final_status(raw)["key"], key)

    def test_success_is_green_and_advances_graduation(self):
        s = das.classify_final_status("SHADOW_EXECUTED")
        self.assertEqual((s["label"], s["color"]), ("执行成功", das.GREEN))
        self.assertTrue(s["affects_graduation"])
        self.assertFalse(s["affects_dcp"])   # 仍保持冻结
        self.assertFalse(s["is_anomaly"])

    def test_waiting_states_are_not_anomalies_and_need_no_manual(self):
        for raw in ("NOT_READY", "AS_OF_MISMATCH", "NO_COMPLETE_SESSION"):
            s = das.classify_final_status(raw)
            self.assertFalse(s["is_anomaly"], raw)
            self.assertFalse(s["needs_manual"], raw)
            self.assertIn(s["color"], (das.YELLOW, das.GRAY), raw)

    def test_data_and_snapshot_problems_are_orange_not_red(self):
        for raw in ("LOCAL_REFRESH_FAILED", "MODEL_SNAPSHOT_NOT_READY", "NO_REPORT"):
            self.assertEqual(das.classify_final_status(raw)["color"], das.ORANGE, raw)

    def test_unknown_status_is_flagged_system_error(self):
        s = das.classify_final_status("SOME_BRAND_NEW_STATUS")
        self.assertEqual(s["key"], "SYSTEM_ERROR")
        self.assertEqual(s["color"], das.RED)
        self.assertTrue(s["needs_manual"])

    def test_empty_status_is_unknown_not_system_error(self):
        self.assertEqual(das.classify_final_status(None)["key"], "UNKNOWN")

    def test_ledger_shadow_failed_is_not_system_crash(self):
        s = das.classify_ledger_status("SHADOW_FAILED")
        self.assertEqual(s["label"], "上次尝试未计入")
        self.assertFalse(s["is_anomaly"])   # 闸门正常拒绝，非崩溃
        self.assertEqual(s["color"], das.ORANGE)

    def test_ledger_pending_and_pass_are_validating(self):
        self.assertEqual(das.classify_ledger_status("DAY2_PENDING")["key"], "VALIDATING")
        self.assertEqual(das.classify_ledger_status("DAY3_PASS")["key"], "VALIDATING")
        self.assertEqual(das.classify_ledger_status("SHADOW_COMPLETE")["key"], "GRAD_COMPLETE")

    def test_dcp_freeze_is_design_not_anomaly(self):
        s = das.classify_dcp_status("FREEZE")
        self.assertEqual(s["label"], "策略冻结")
        self.assertFalse(s["is_anomaly"])
        self.assertTrue(s["affects_dcp"])

    def test_carrier_display_translates_bool_to_chinese(self):
        c = das.carrier_display({
            "fund_code": "539001", "fund_name": "建信纳指100",
            "personal_purchase_supported": False, "channel_available": False,
            "effective_limit_rmb": 100.0, "current_holding": True,
            "purchase_status": "暂停申购",
        })
        self.assertEqual(c["personal_text"], "不可买")
        self.assertEqual(c["channel_text"], "不可买")
        self.assertNotIn("False", str(c.values()))
        self.assertEqual(c["result"], "个人不可买")   # personal 先决

    def test_carrier_executable_when_all_open(self):
        c = das.carrier_display({
            "fund_code": "021000", "fund_name": "南方纳指100 I",
            "personal_purchase_supported": True, "channel_available": True,
            "effective_limit_rmb": 1000.0, "purchase_status": "开放申购",
        })
        self.assertEqual((c["result"], c["result_color"]), ("可执行", das.GREEN))

    def test_carrier_gate_partial_capacity_is_wait_not_fail(self):
        self.assertEqual(das.carrier_gate("ACTIVE", "AVAILABLE")[:2], ("成功", das.GREEN))
        self.assertEqual(das.carrier_gate("ACTIVE", "PARTIAL_CAPACITY")[:2], ("等待", das.YELLOW))
        self.assertEqual(das.carrier_gate("ACTIVE", "BLOCKED")[:2], ("失败", das.RED))
        self.assertEqual(das.carrier_gate("STALE", "AVAILABLE")[:2], ("失败", das.RED))

    def test_execution_flow_no_record_is_all_waiting(self):
        steps = das.execution_flow(None)
        self.assertTrue(all(s["status"] == "等待" for s in steps))
        self.assertEqual(steps[0]["name"], "定时触发")

    def test_execution_flow_success_run(self):
        record = {"target_trade_date": "2026-07-02", "fred_ndx_date": "2026-07-02",
                  "local_ndx_date": "2026-07-02", "dfii10_lag_status": "ACCEPTABLE_LAG",
                  "final_status": "SHADOW_EXECUTED"}
        steps = das.execution_flow(record, ledger_counted_today=True, prepared_status="PASS")
        by_name = {s["name"]: s for s in steps}
        self.assertEqual(by_name["影子运行"]["status"], "成功")
        self.assertEqual(by_name["账本记录"]["status"], "成功")
        self.assertEqual(by_name["宏观利率输入"]["status"], "成功")

    def test_execution_flow_executed_but_not_counted_does_not_greenlight_graduation(self):
        # SHADOW_EXECUTED（Runner 成功）但账本未计入当日：毕业进度不得标绿。
        record = {"target_trade_date": "2026-07-02", "fred_ndx_date": "2026-07-02",
                  "local_ndx_date": "2026-07-02", "dfii10_lag_status": "ACCEPTABLE_LAG",
                  "final_status": "SHADOW_EXECUTED"}
        by_name = {s["name"]: s for s in das.execution_flow(record, ledger_counted_today=False)}
        self.assertEqual(by_name["账本记录"]["status"], "阻断")
        self.assertEqual(by_name["毕业进度"]["status"], "未计入")
        self.assertNotEqual(by_name["毕业进度"]["status"], "成功")
        # 而真正计入时才标绿
        counted = {s["name"]: s for s in das.execution_flow(record, ledger_counted_today=True)}
        self.assertEqual(counted["毕业进度"]["status"], "成功")

    def test_execution_flow_not_ready_waits_not_fails(self):
        record = {"target_trade_date": "2026-07-02", "fred_ndx_date": "2026-07-01",
                  "local_ndx_date": "2026-07-01", "dfii10_lag_status": "NOT_READY",
                  "final_status": "NOT_READY"}
        steps = das.execution_flow(record)
        by_name = {s["name"]: s for s in steps}
        self.assertEqual(by_name["NDX价格输入"]["status"], "等待")
        self.assertNotEqual(by_name["影子运行"]["status"], "失败")   # 未到达≠失败

    def test_graduation_cells_classify_failures_not_all_system_error(self):
        ledger = {
            "days": [{"market_session_date": "2026-06-30", "shadow_day": 1, "result": "PASS",
                      "temperature_score": 26.0, "temperature_level": "HOT"}],
            "failures": [{"market_session_date": "2026-06-29",
                          "failures": [{"failed_field": "carrier_raw_sha256",
                                        "root_cause": "archived QDII input hash differs"}]}],
        }
        cells = das.graduation_cells(ledger)
        self.assertEqual(cells[0]["date"], "2026-06-29")   # sorted by date
        self.assertEqual(cells[0]["label"], "数据异常")     # hash mismatch → 数据异常, not 系统异常
        self.assertEqual(cells[1]["label"], "成功")

    def test_root_cause_distinguishes_market_limit_from_broken(self):
        layers = das.root_cause_layers(
            {"final_status": "NOT_READY", "target_trade_date": "2026-07-03"},
            {"status": "DAY1_PASS"})
        self.assertIn("并非系统故障", layers["root"])

    def test_root_cause_executed_but_not_counted_is_not_contradictory(self):
        # 07-02: Runner 成功但未计入（不在 days，账本 SHADOW_FAILED）。
        layers = das.root_cause_layers(
            {"final_status": "SHADOW_EXECUTED", "target_trade_date": "2026-07-02"},
            {"status": "SHADOW_FAILED", "days": [{"market_session_date": "2026-06-30"}],
             "failures": [{"failures": [{"root_cause": "archived QDII input hash differs"}]}]})
        self.assertEqual(layers["surface"], "SHADOW_FAILED")
        self.assertIn("不会倒退", layers["root"])
        self.assertIn("未推进", layers["root"])
        # 不得再声称「已成功计入」——那是与 SHADOW_FAILED 自相矛盾的旧 bug。
        self.assertNotIn("已成功计入", layers["root"])

    def test_root_cause_executed_and_counted_says_counted(self):
        layers = das.root_cause_layers(
            {"final_status": "SHADOW_EXECUTED", "target_trade_date": "2026-06-30"},
            {"status": "DAY1_PASS", "days": [{"market_session_date": "2026-06-30", "shadow_day": 1}]})
        self.assertIn("已成功计入", layers["root"])


    # ── Automation History + Execution Coverage ──
    def _history(self, **kw):
        sla = {"records": [
            {"target_trade_date": "2026-06-24", "final_status": "NOT_READY"},
            {"target_trade_date": "2026-06-25", "final_status": "MODEL_SNAPSHOT_NOT_READY"},
            {"target_trade_date": "2026-06-26", "final_status": "MODEL_SNAPSHOT_NOT_READY"},
            {"target_trade_date": "2026-06-29", "final_status": "SHADOW_EXECUTED"},
            {"target_trade_date": "2026-06-30", "final_status": "SHADOW_EXECUTED"},
            {"target_trade_date": "2026-07-02", "final_status": "SHADOW_EXECUTED"},
        ]}
        ledger = {"days": [{"market_session_date": "2026-06-30", "shadow_day": 1}],
                  "failures": [{"market_session_date": "2026-07-02"}]}
        params = dict(latest_complete_session=dt.date(2026, 7, 2),
                      today=dt.date(2026, 7, 3), window_days=16)
        params.update(kw)
        return das.build_automation_history(sla["records"], ledger, **params)

    def _by_date(self, rows):
        return {r["date"]: r for r in rows}

    def test_history_weekend_is_non_trading_not_failure(self):
        rows = self._by_date(self._history())
        self.assertEqual(rows["2026-06-27"]["category"], "non_trading")  # Saturday
        self.assertEqual(rows["2026-06-27"]["state"]["label"], "非交易日")
        self.assertFalse(rows["2026-06-27"]["state"]["is_anomaly"])

    def test_history_july4_holiday_is_non_trading(self):
        # 2026-07-03 (Fri) is the observed US Independence Day holiday.
        row = self._by_date(self._history())["2026-07-03"]
        self.assertEqual(row["category"], "non_trading")
        self.assertEqual(row["root_cause"], "美股节假日")

    def test_history_trading_day_without_record_is_offline_not_system_error(self):
        row = self._by_date(self._history())["2026-07-01"]   # Wed, in window, no record
        self.assertEqual(row["category"], "offline")
        self.assertEqual(row["state"]["label"], "电脑离线")
        self.assertFalse(row["state"]["is_anomaly"])          # 环境问题，非程序错误

    def test_history_before_deploy_is_not_deployed(self):
        row = self._by_date(self._history())["2026-06-22"]    # Mon, before first SLA
        self.assertEqual(row["category"], "not_deployed")
        self.assertEqual(row["state"]["label"], "未部署")

    def test_history_future_trading_day_is_pending(self):
        rows = self._by_date(self._history(today=dt.date(2026, 7, 6),
                                           latest_complete_session=dt.date(2026, 7, 2)))
        # 2026-07-06 (Mon) is a trading day after latest complete session → 待运行
        self.assertEqual(rows["2026-07-06"]["category"], "pending")

    def test_history_ran_day_maps_final_status_and_graduation(self):
        rows = self._by_date(self._history())
        self.assertEqual(rows["2026-06-30"]["state"]["label"], "执行成功")
        self.assertEqual(rows["2026-06-30"]["graduation"], "计入 Day 1")
        self.assertEqual(rows["2026-07-02"]["graduation"], "未计入")   # in ledger failures

    def test_coverage_counts_ran_over_due_excluding_predeploy(self):
        cov = das.execution_coverage(self._history())
        self.assertEqual(cov["should"], 7)      # trading days 06-24..07-02
        self.assertEqual(cov["actual"], 6)      # all but 07-01
        self.assertEqual(cov["rate"], 85.7)
        self.assertEqual(cov["missing_days"], ["2026-07-01"])

    def test_coverage_ignores_non_trading_and_not_deployed(self):
        cov = das.execution_coverage(self._history())
        # denominator excludes weekends/holidays/pre-deploy; only trading days in window
        self.assertLessEqual(cov["actual"], cov["should"])
        self.assertNotIn("2026-06-27", cov["missing_days"])   # Saturday never "missing"


if __name__ == "__main__":
    unittest.main()
