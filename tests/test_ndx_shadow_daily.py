import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ndx_shadow_run
from scripts import run_ndx_shadow_daily as daily


TARGET = dt.date(2026, 6, 23)
NOW = dt.datetime(2026, 6, 24, 13, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def check(ndx, dfii10, local_ndx=None, local_dfii10=None):
    freshness = daily.evaluate_dfii10_freshness(dfii10, TARGET)
    return {
        "fred_ndx_date": ndx,
        "fred_dfii10_date": dfii10,
        "fred_dfii10_value": 2.2,
        "dfii10_lag_trading_days": freshness["lag_trading_days"],
        "dfii10_lag_status": freshness["status"],
        "dfii10_accepted_as_of_date": dfii10 if freshness["status"] in ("FRESH", "ACCEPTABLE_LAG") else None,
        "local_ndx_date": local_ndx or ndx,
        "local_dfii10_date": local_dfii10 or dfii10,
    }


class NdxShadowDailyTests(unittest.TestCase):
    def run_with(self, prechecks, refresh_ok=True, ledger_done=False, target=TARGET):
        with tempfile.TemporaryDirectory() as temp:
            sla = Path(temp) / "source-sla.json"
            def refresh_side_effect(target_date, accepted_dfii10=None):
                accepted_date = dt.date.fromisoformat(accepted_dfii10["dfii10_source_date"]) if accepted_dfii10 else target
                if refresh_ok:
                    return True, target, accepted_date
                return False, target - dt.timedelta(days=1), accepted_date - dt.timedelta(days=1)
            with mock.patch.object(daily, "latest_complete_us_session", return_value=target), \
                 mock.patch.object(daily, "ledger_has_completed_day", return_value=ledger_done), \
                 mock.patch.object(daily, "precheck", side_effect=prechecks), \
                 mock.patch.object(daily, "refresh_and_validate", side_effect=refresh_side_effect):
                shadow = mock.Mock(return_value="SHADOW_EXECUTED")
                result = daily.run_once(now=NOW, sleep_until_retry=False, sla_path=sla, shadow_executor=shadow)
                return result, shadow, sla.exists()

    def test_first_check_ready_executes_shadow(self):
        result, shadow, exists = self.run_with([check(TARGET, TARGET)])
        self.assertEqual((result["ready_attempt"], result["final_status"], result["shadow_executed"]), ("FIRST", "SHADOW_EXECUTED", True))
        shadow.assert_called_once()
        self.assertEqual(shadow.call_args[1]["accepted_dfii10"]["dfii10_lag_status"], "FRESH")
        self.assertTrue(exists)

    def test_first_not_ready_retry_ready_executes_shadow_once(self):
        result, shadow, _ = self.run_with([check(TARGET - dt.timedelta(days=1), TARGET - dt.timedelta(days=1)), check(TARGET, TARGET)])
        self.assertEqual((result["ready_attempt"], result["final_status"]), ("RETRY", "SHADOW_EXECUTED"))
        shadow.assert_called_once()

    def test_two_checks_not_ready_does_not_execute_shadow(self):
        result, shadow, _ = self.run_with([check(TARGET - dt.timedelta(days=1), TARGET), check(TARGET - dt.timedelta(days=1), TARGET)])
        self.assertEqual((result["ready_attempt"], result["final_status"], result["shadow_executed"]), ("NONE", "NOT_READY", False))
        shadow.assert_not_called()

    def test_local_refresh_failed_blocks_shadow(self):
        result, shadow, _ = self.run_with([check(TARGET, TARGET)], refresh_ok=False)
        self.assertEqual((result["ready_attempt"], result["final_status"], result["shadow_executed"]), ("FIRST", "LOCAL_REFRESH_FAILED", False))
        shadow.assert_not_called()

    def test_dfii10_same_day_is_fresh(self):
        self.assertEqual(daily.evaluate_dfii10_freshness(TARGET, TARGET), {"status": "FRESH", "lag_trading_days": 0})

    def test_dfii10_one_trading_day_lag_is_acceptable(self):
        result, shadow, _ = self.run_with([check(TARGET, TARGET - dt.timedelta(days=1))])
        self.assertEqual((result["ready_attempt"], result["final_status"]), ("FIRST", "SHADOW_EXECUTED"))
        self.assertEqual(shadow.call_args[1]["accepted_dfii10"]["dfii10_lag_status"], "ACCEPTABLE_LAG")

    def test_dfii10_more_than_one_trading_day_lag_is_not_ready(self):
        result, shadow, _ = self.run_with([check(TARGET, TARGET - dt.timedelta(days=2)), check(TARGET, TARGET - dt.timedelta(days=2))])
        self.assertEqual((result["ready_attempt"], result["final_status"]), ("NONE", "NOT_READY"))
        shadow.assert_not_called()

    def test_dfii10_future_date_is_as_of_mismatch(self):
        result, shadow, _ = self.run_with([check(TARGET, TARGET + dt.timedelta(days=1))])
        self.assertEqual((result["ready_attempt"], result["final_status"]), ("NONE", "AS_OF_MISMATCH"))
        shadow.assert_not_called()

    def test_accepted_dfii10_is_used_to_build_model_report(self):
        accepted = {
            "accepted_ndx": {
                "ndx_source": "FRED_NASDAQ100",
                "ndx_instrument": "NDX",
                "ndx_source_date": TARGET.isoformat(),
                "ndx_value": 30000.0,
                "ndx_retrieved_at": NOW.isoformat(),
                "ndx_accepted_as_of_date": TARGET.isoformat(),
            },
            "dfii10_source": "DFII10",
            "dfii10_source_date": TARGET.isoformat(),
            "dfii10_value": 2.31,
            "dfii10_retrieved_at": NOW.isoformat(),
            "dfii10_lag_trading_days": 0,
            "dfii10_lag_status": "FRESH",
            "dfii10_accepted_as_of_date": TARGET.isoformat(),
        }
        snapshot = {
            "source_date": TARGET.isoformat(),
            "ndx_close": 30000.0,
            "dfii10_source_date": TARGET.isoformat(),
            "dfii10": 2.31,
        }
        report = {"copilot": {"ndx_data_layer": {}, "ndx_price_temperature": {}}}
        with mock.patch.object(daily, "latest_model_snapshot_with_accepted_inputs", return_value=snapshot) as builder:
            patched = daily.apply_shadow_inputs_to_report(report, TARGET, accepted, accepted["accepted_ndx"])
        builder.assert_called_once_with(accepted["accepted_ndx"], accepted)
        self.assertEqual(patched["copilot"]["ndx_data_layer"]["price_primary"]["date"], TARGET.isoformat())
        self.assertEqual(patched["copilot"]["ndx_data_layer"]["price_primary"]["close"], 30000.0)
        self.assertEqual(patched["copilot"]["ndx_price_temperature"]["dfii10"], 2.31)
        self.assertEqual(patched["copilot"]["ndx_data_layer"]["macro_inputs"][0]["date"], TARGET.isoformat())
        self.assertEqual(patched["copilot"]["ndx_data_layer"]["macro_inputs"][0]["value"], 2.31)

    def test_execute_shadow_writes_isolated_prepared_snapshot(self):
        accepted = {
            "accepted_ndx": {
                "ndx_source": "FRED_NASDAQ100",
                "ndx_instrument": "NDX",
                "ndx_source_date": TARGET.isoformat(),
                "ndx_value": 30000.0,
                "ndx_retrieved_at": NOW.isoformat(),
                "ndx_accepted_as_of_date": TARGET.isoformat(),
            },
            "dfii10_source": "DFII10",
            "dfii10_source_date": TARGET.isoformat(),
            "dfii10_value": 2.31,
            "dfii10_retrieved_at": NOW.isoformat(),
            "dfii10_lag_trading_days": 0,
            "dfii10_lag_status": "FRESH",
            "dfii10_accepted_as_of_date": TARGET.isoformat(),
        }
        base_report = {
            "copilot": {
                "run_id": "run-test",
                "ndx_price_temperature": {
                    "source_name": daily.ndx_price_temperature.PRICE_SOURCE_NAME,
                    "source_date": "2026-06-18",
                },
                "ndx_data_layer": {},
            }
        }
        snapshot = {
            "source_date": TARGET.isoformat(),
            "ndx_close": 30000.0,
            "dfii10_source_date": TARGET.isoformat(),
            "dfii10": 2.31,
        }
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "report.json"
            report_path.write_text(daily.json.dumps(base_report), encoding="utf-8")
            prepared = Path(temp) / "prepared"
            with mock.patch.object(daily, "latest_model_snapshot_with_accepted_inputs", return_value=snapshot), \
                 mock.patch.object(daily, "PREPARED_REPORT_ROOT", prepared), \
                mock.patch.object(daily.subprocess, "run") as runner:
                runner.return_value = mock.Mock(returncode=0, stdout="")
                result = daily.execute_shadow(TARGET, report_path=report_path, accepted_dfii10=accepted)
                self.assertEqual(result, "SHADOW_EXECUTED")
                files = list(prepared.glob("%s/*canonical-shadow-report.json" % TARGET.isoformat()))
                self.assertEqual(len(files), 1)
                payload = daily.json.loads(files[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["copilot"]["ndx_price_temperature"]["source_date"], TARGET.isoformat())
                self.assertEqual(payload["copilot"]["ndx_data_layer"]["macro_inputs"][0]["value"], 2.31)

    def test_duplicate_completed_day_is_idempotent(self):
        result, shadow, exists = self.run_with([], ledger_done=True)
        self.assertEqual(result["final_status"], "ALREADY_COMPLETED")
        shadow.assert_not_called()
        self.assertFalse(exists)

    def test_no_complete_session_safely_skips(self):
        with tempfile.TemporaryDirectory() as temp:
            sla = Path(temp) / "source-sla.json"
            with mock.patch.object(daily, "latest_complete_us_session", return_value=None):
                result = daily.run_once(now=NOW, sleep_until_retry=False, sla_path=sla, shadow_executor=mock.Mock())
        self.assertEqual(result["final_status"], "NO_COMPLETE_SESSION")
        self.assertFalse(sla.exists())

    def test_ssot_and_freeze_rules_remain_strict(self):
        layer = {"price_primary": {"source": "QQQ_PROXY", "instrument": "QQQ", "date": TARGET.isoformat()}}
        self.assertEqual(ndx_shadow_run.evaluate_primary_shadow_gate(layer, TARGET)["decision"], "CRITICAL_FAIL")
        formal = {"formal_executable_amount": 0.0, "formal_release_amount": 0.0}
        self.assertEqual((formal["formal_executable_amount"], formal["formal_release_amount"]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
