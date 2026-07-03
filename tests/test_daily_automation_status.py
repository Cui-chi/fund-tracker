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

    def test_root_cause_explains_shadow_failed_is_gate_rejection(self):
        layers = das.root_cause_layers(
            {"final_status": "SHADOW_EXECUTED", "target_trade_date": "2026-07-02"},
            {"status": "SHADOW_FAILED",
             "failures": [{"failures": [{"root_cause": "archived QDII input hash differs"}]}]})
        self.assertEqual(layers["surface"], "SHADOW_FAILED")
        self.assertIn("不会倒退", layers["root"])


if __name__ == "__main__":
    unittest.main()
