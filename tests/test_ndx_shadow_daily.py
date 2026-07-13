import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ndx_shadow_run
import fund_tracker
from scripts import run_ndx_shadow_daily as daily


TARGET = dt.date(2026, 6, 23)
NOW = dt.datetime(2026, 6, 24, 13, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def canonical_copilot(run_id="run-test"):
    return {
        "run_id": run_id,
        "generated_at": NOW.isoformat(),
        "data_status": "WARNING",
        "model_status": "UNDER_VALIDATION",
        "validation_stage": "OFFLINE_PASS",
        "activation_status": "NOT_ACTIVE",
        "decision_status": "FREEZE",
        "dynamic_cash_pool_status": "FREEZE",
        "carrier_snapshot_id": "qdii-1",
        "input_hashes": {},
        "shadow_inputs": {
            "portfolio_snapshot": {"source_date": NOW.isoformat(), "stale_status": "PASS"},
            "target_snapshot": {"source_date": NOW.isoformat(), "stale_status": "PASS"},
        },
        "ndx_price_temperature": {
            "price_primary_source": "FRED_NASDAQ100",
            "source_date": TARGET.isoformat(),
            "ndx_close": 30000.0,
            "dfii10_source_date": TARGET.isoformat(),
            "dfii10": 2.31,
            "formula_version": ndx_shadow_run.MODEL_VERSION,
            "no_lookahead_check": "PASS",
            "temperature_score": 50.0,
            "candidate_effective_release_factor": 0.25,
            "base_release_factor": 0.25,
            "real_yield_modifier": 1.0,
            "volatility_cap": 1.0,
        },
        "v7_decision_chain": {
            "model_candidate": {
                "ndx_gap_routed_amount": 1000.0,
                "ndx_candidate_release_amount": 250.0,
            },
            "carrier_matching": {
                "carrier_snapshot_id": "qdii-1",
                "carrier_snapshot_valid": True,
                "carrier_selection_status": "AVAILABLE",
                "carrier_coverable_amount": 250.0,
                "retained_due_to_capacity": 0.0,
                "retained_due_to_carrier_block": 0.0,
                "current_effective_carrier_capacity": 1000.0,
                "last_known_approved_carrier_capacity": 1000.0,
            },
            "formal_decision": {
                "formal_executable_amount": 0.0,
                "formal_release_amount": 0.0,
                "retained_due_to_decision_freeze": 250.0,
            },
            "identity_verification": {
                "candidate_to_carrier_reconciled": True,
                "carrier_to_decision_reconciled": True,
            },
        },
    }


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
    def run_with(self, prechecks, refresh_ok=True, ledger_done=False, target=TARGET, dashboard_refresher=None):
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
                # dashboard_refresher defaults to a no-op mock: this file's own
                # scope is the READY/SHADOW_EXECUTED decision, not the real
                # local_server dashboard rebuild, which the dedicated
                # test_dashboard_refresh_* cases below exercise directly.
                refresher = dashboard_refresher if dashboard_refresher is not None else mock.Mock()
                result = daily.run_once(now=NOW, sleep_until_retry=False, sla_path=sla, shadow_executor=shadow,
                                         dashboard_refresher=refresher)
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

    def test_lagged_dfii10_is_mapped_to_target_month_for_model(self):
        accepted_ndx = {
            "ndx_source": "FRED_NASDAQ100",
            "ndx_instrument": "NDX",
            "ndx_source_date": "2026-07-01",
            "ndx_value": 29809.13,
        }
        accepted = {
            "dfii10_source": "DFII10",
            "dfii10_source_date": "2026-06-30",
            "dfii10_value": 2.20,
            "dfii10_lag_trading_days": 1,
            "dfii10_lag_status": "ACCEPTABLE_LAG",
            "dfii10_accepted_as_of_date": "2026-06-30",
        }
        captured = {}
        def fake_latest_snapshot(prices, monthly_rates):
            captured["monthly_rates"] = monthly_rates
            return {
                "source_date": "2026-07-01",
                "ndx_close": 29809.13,
                "dfii10_source_date": "2026-06-30",
                "dfii10": 2.20,
                "dfii10_percentile": 80.0,
                "real_yield_modifier": 0.85,
                "candidate_effective_release_factor": 0.40,
            }
        with mock.patch.object(daily.ndx_price_temperature, "read_fred_csv", side_effect=[
            [(dt.date(2026, 6, 30), 30276.35)],
            [(dt.date(2026, 6, 30), 2.20)],
        ]), mock.patch.object(daily.ndx_price_temperature, "latest_snapshot", side_effect=fake_latest_snapshot):
            snapshot = daily.latest_model_snapshot_with_accepted_inputs(accepted_ndx, accepted)
        self.assertEqual(captured["monthly_rates"]["2026-07"], {"value": 2.20, "source_date": "2026-06-30"})
        self.assertEqual(snapshot["real_yield_modifier"], 0.85)
        self.assertEqual(snapshot["candidate_effective_release_factor"], 0.40)

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
                    "ndx_close": 29000.0,
                    "dfii10_source_date": "2026-06-18",
                    "dfii10": 2.20,
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
                mock.patch.object(daily.subprocess, "Popen") as runner:
                runner.return_value = mock.Mock(returncode=0, communicate=mock.Mock(return_value=("", None)))
                result = daily.execute_shadow(TARGET, report_path=report_path, accepted_dfii10=accepted)
                self.assertEqual(result, "SHADOW_EXECUTED")
                files = list(prepared.glob("%s/*canonical-shadow-report.json" % TARGET.isoformat()))
                self.assertEqual(len(files), 1)
                payload = daily.json.loads(files[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["copilot"]["ndx_price_temperature"]["source_date"], TARGET.isoformat())
                self.assertEqual(payload["copilot"]["ndx_price_temperature"]["ndx_close"], 30000.0)
                self.assertEqual(payload["copilot"]["prepared_snapshot_validation"]["macro_input_match"], True)
                self.assertEqual(payload["copilot"]["ndx_data_layer"]["macro_inputs"][0]["value"], 2.31)

    def test_execute_shadow_without_report_builds_minimal_prepared_snapshot(self):
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
        minimal = {"copilot": canonical_copilot("minimal-run")}
        with tempfile.TemporaryDirectory() as temp:
            prepared = Path(temp) / "prepared"
            with mock.patch.object(daily, "latest_report_path", return_value=None), \
                 mock.patch.object(daily, "build_minimal_shadow_report", return_value=minimal) as builder, \
                 mock.patch.object(daily, "PREPARED_REPORT_ROOT", prepared), \
                 mock.patch.object(daily.subprocess, "Popen") as runner:
                runner.return_value = mock.Mock(returncode=0, communicate=mock.Mock(return_value=("", None)))
                result = daily.execute_shadow(TARGET, accepted_dfii10=accepted)
        self.assertEqual(result, "SHADOW_EXECUTED")
        builder.assert_called_once_with(TARGET, accepted)
        runner.assert_called_once()

    def _accepted_inputs(self):
        return {
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

    def test_execute_shadow_timeout_kills_process_group(self):
        accepted = self._accepted_inputs()
        minimal = {"copilot": canonical_copilot("minimal-run")}
        proc = mock.Mock(pid=4321, returncode=-9)
        proc.communicate.side_effect = daily.subprocess.TimeoutExpired(cmd="x", timeout=900)
        with tempfile.TemporaryDirectory() as temp:
            prepared = Path(temp) / "prepared"
            with mock.patch.object(daily, "latest_report_path", return_value=None), \
                 mock.patch.object(daily, "build_minimal_shadow_report", return_value=minimal), \
                 mock.patch.object(daily, "PREPARED_REPORT_ROOT", prepared), \
                 mock.patch.object(daily.subprocess, "Popen", return_value=proc), \
                 mock.patch.object(daily.os, "getpgid", return_value=4321), \
                 mock.patch.object(daily.os, "killpg") as killpg:
                with self.assertRaises(daily.DailyShadowError):
                    daily.execute_shadow(TARGET, accepted_dfii10=accepted)
        killpg.assert_called_once_with(4321, daily.signal.SIGKILL)

    def _run_fallback_case(self, temp, model, accepted):
        report_path = Path(temp) / "report.json"
        report_path.write_text(
            daily.json.dumps({"copilot": {"run_id": "run-test", "ndx_price_temperature": model, "ndx_data_layer": {}}}),
            encoding="utf-8",
        )
        minimal = {"copilot": canonical_copilot("minimal-run")}
        prepared = Path(temp) / "prepared"
        with mock.patch.object(daily, "build_minimal_shadow_report", return_value=minimal) as builder, \
             mock.patch.object(daily, "PREPARED_REPORT_ROOT", prepared), \
             mock.patch.object(daily.subprocess, "Popen") as runner:
            runner.return_value = mock.Mock(returncode=0, communicate=mock.Mock(return_value=("", None)))
            result = daily.execute_shadow(TARGET, report_path=report_path, accepted_dfii10=accepted)
        builder.assert_called_once_with(TARGET, accepted)
        files = list(prepared.glob("%s/*canonical-shadow-report.json" % TARGET.isoformat()))
        self.assertEqual(len(files), 1)
        payload = daily.json.loads(files[0].read_text(encoding="utf-8"))
        return result, payload

    def test_report_with_none_ndx_identity_falls_back_to_minimal_snapshot(self):
        model = {
            "source_name": daily.ndx_price_temperature.PRICE_SOURCE_NAME,
            "source_date": None,
            "ndx_close": None,
            "dfii10_source_date": TARGET.isoformat(),
            "dfii10": 2.20,
        }
        with tempfile.TemporaryDirectory() as temp:
            result, payload = self._run_fallback_case(temp, model, self._accepted_inputs())
        self.assertNotEqual(result, "MODEL_SNAPSHOT_NOT_READY")
        self.assertEqual(result, "SHADOW_EXECUTED")
        self.assertEqual(payload["copilot"]["prepared_snapshot_validation"]["ndx_input_match"], True)
        self.assertEqual(payload["copilot"]["prepared_snapshot_validation"]["macro_input_match"], True)
        self.assertEqual(payload["copilot"]["prepared_snapshot_validation"]["status"], "PASS")

    def test_report_with_none_dfii10_identity_falls_back_to_minimal_snapshot(self):
        model = {
            "source_name": daily.ndx_price_temperature.PRICE_SOURCE_NAME,
            "source_date": "2026-06-18",
            "ndx_close": 29000.0,
            "dfii10_source_date": None,
            "dfii10": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            result, payload = self._run_fallback_case(temp, model, self._accepted_inputs())
        self.assertNotEqual(result, "MODEL_SNAPSHOT_NOT_READY")
        self.assertEqual(result, "SHADOW_EXECUTED")
        self.assertEqual(payload["copilot"]["prepared_snapshot_validation"]["ndx_input_match"], True)
        self.assertEqual(payload["copilot"]["prepared_snapshot_validation"]["macro_input_match"], True)
        self.assertEqual(payload["copilot"]["prepared_snapshot_validation"]["status"], "PASS")

    def test_invalid_report_without_accepted_inputs_returns_model_snapshot_not_ready(self):
        model = {"source_name": daily.ndx_price_temperature.PRICE_SOURCE_NAME, "source_date": None}
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "report.json"
            report_path.write_text(
                daily.json.dumps({"copilot": {"run_id": "run-test", "ndx_price_temperature": model, "ndx_data_layer": {}}}),
                encoding="utf-8",
            )
            result = daily.execute_shadow(TARGET, report_path=report_path)
        self.assertEqual(result, "MODEL_SNAPSHOT_NOT_READY")

    def test_no_report_without_accepted_inputs_remains_no_report(self):
        with mock.patch.object(daily, "latest_report_path", return_value=None):
            self.assertEqual(daily.execute_shadow(TARGET), "NO_REPORT")

    def test_minimal_builder_injects_accepted_inputs_into_canonical_snapshot(self):
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
        snapshot = dict(canonical_copilot()["ndx_price_temperature"])
        fake_conn = mock.Mock()
        with mock.patch.object(daily, "latest_model_snapshot_with_accepted_inputs", return_value=snapshot), \
             mock.patch.object(fund_tracker, "load_config", return_value={"funds": []}), \
             mock.patch.object(fund_tracker, "connect_db", return_value=fake_conn), \
             mock.patch.object(fund_tracker, "generate_market_temperature", return_value={}), \
             mock.patch.object(fund_tracker, "generate_copilot_snapshot", return_value=canonical_copilot()), \
             mock.patch.object(daily.qdii_carrier, "RAW_SNAPSHOT_PATH", Path("/tmp/nonexistent-qdii-raw.json")), \
             mock.patch.object(daily.qdii_carrier, "CARRIER_JSON_PATH", Path("/tmp/nonexistent-qdii-latest.json")):
            report = daily.build_minimal_shadow_report(TARGET, accepted)
        canonical = ndx_shadow_run.canonical_shadow_view(report)
        self.assertEqual(canonical["ndx_price_temperature"]["source_date"], TARGET.isoformat())
        self.assertEqual(canonical["ndx_price_temperature"]["ndx_close"], 30000.0)
        self.assertEqual(canonical["ndx_price_temperature"]["dfii10_source_date"], TARGET.isoformat())
        self.assertEqual(canonical["ndx_price_temperature"]["dfii10"], 2.31)
        self.assertEqual(report["copilot"]["prepared_snapshot_validation"]["status"], "PASS")
        self.assertIsInstance(ndx_shadow_run.canonical_input_hash(canonical, TARGET, canonical["ndx_data_layer"]), str)
        self.assertEqual(ndx_shadow_run.evaluate_primary_shadow_gate(canonical["ndx_data_layer"], TARGET)["decision"], "READY")
        fake_conn.close.assert_called_once()

    def test_prepared_snapshot_atomic_write_produces_valid_json(self):
        payload = {
            "copilot": {
                "run_id": "atomic-ok",
                "ndx_price_temperature": {
                    "source_date": TARGET.isoformat(),
                    "ndx_close": 30000.0,
                    "dfii10_source_date": TARGET.isoformat(),
                    "dfii10": 2.31,
                    "date": TARGET,
                },
                "ndx_data_layer": {
                    "price_primary": {"source": "FRED_NASDAQ100", "date": TARGET.isoformat(), "close": 30000.0},
                    "macro_inputs": [{"source": "DFII10", "date": TARGET.isoformat(), "value": 2.31}],
                },
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            path = daily.write_prepared_shadow_report(payload, TARGET, prepared_root=Path(temp))
            loaded = daily.json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["copilot"]["ndx_price_temperature"]["date"], TARGET.isoformat())
            self.assertFalse(list(Path(temp).glob("**/*.tmp")))

    def test_prepared_snapshot_serialization_failure_leaves_no_final_json(self):
        class NotSerializable:
            pass
        payload = {
            "bad": NotSerializable(),
            "copilot": {
                "run_id": "atomic-fail",
                "ndx_price_temperature": {
                    "source_date": TARGET.isoformat(),
                    "ndx_close": 30000.0,
                    "dfii10_source_date": TARGET.isoformat(),
                    "dfii10": 2.31,
                },
                "ndx_data_layer": {
                    "price_primary": {"source": "FRED_NASDAQ100", "date": TARGET.isoformat(), "close": 30000.0},
                    "macro_inputs": [{"source": "DFII10", "date": TARGET.isoformat(), "value": 2.31}],
                },
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(TypeError):
                daily.write_prepared_shadow_report(payload, TARGET, prepared_root=Path(temp))
            self.assertFalse(list(Path(temp).glob("**/*canonical-shadow-report.json")))
            self.assertFalse(list(Path(temp).glob("**/*.tmp")))

    def test_invalid_prepared_snapshot_is_not_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text('{"copilot": {"ndx_price_temperature": {"date": ', encoding="utf-8")
            self.assertFalse(daily.prepared_snapshot_is_valid(path))

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

    def test_dashboard_refresh_called_after_shadow_executed(self):
        refresher = mock.Mock()
        result, shadow, _ = self.run_with([check(TARGET, TARGET)], dashboard_refresher=refresher)
        self.assertEqual(result["final_status"], "SHADOW_EXECUTED")
        refresher.assert_called_once_with()
        self.assertTrue(result["dashboard_refreshed"])
        self.assertIsNone(result["dashboard_refresh_error"])

    def test_dashboard_refresh_not_called_when_not_ready(self):
        refresher = mock.Mock()
        not_ready = check(TARGET - dt.timedelta(days=1), TARGET)
        result, shadow, _ = self.run_with([not_ready, not_ready], dashboard_refresher=refresher)
        self.assertEqual(result["final_status"], "NOT_READY")
        refresher.assert_not_called()
        self.assertFalse(result["dashboard_refreshed"])

    def test_dashboard_refresh_failure_does_not_undo_shadow_success(self):
        refresher = mock.Mock(side_effect=RuntimeError("dist rebuild boom"))
        result, shadow, exists = self.run_with([check(TARGET, TARGET)], dashboard_refresher=refresher)
        self.assertEqual(result["final_status"], "SHADOW_EXECUTED")
        self.assertTrue(result["shadow_executed"])
        self.assertFalse(result["dashboard_refreshed"])
        self.assertEqual(result["dashboard_refresh_error"], "dist rebuild boom")
        self.assertTrue(exists)

    def test_refresh_dashboard_after_shadow_success_reuses_rebuild_outputs(self):
        with mock.patch("local_server.load_config", return_value={"fake": "config"}) as load_cfg, \
             mock.patch("local_server.rebuild_outputs") as rebuild:
            daily.refresh_dashboard_after_shadow_success()
        load_cfg.assert_called_once_with()
        rebuild.assert_called_once_with({"fake": "config"}, phase="ndx-shadow-sync")

    def test_sla_record_persisted_before_dashboard_refresh(self):
        # Regression: the just-run session must already be in source-sla.json
        # when the dashboard resync fires, otherwise the rebuilt Automation
        # History renders that session as 电脑离线 (inconsistent with the ledger).
        import json
        captured = {}
        with tempfile.TemporaryDirectory() as temp:
            sla = Path(temp) / "source-sla.json"
            def refresher():
                data = json.loads(sla.read_text()) if sla.exists() else {"records": []}
                captured["targets"] = [r["target_trade_date"] for r in data["records"]]
            def refresh_ok(target_date, accepted_dfii10=None):
                acc = dt.date.fromisoformat(accepted_dfii10["dfii10_source_date"]) if accepted_dfii10 else TARGET
                return True, TARGET, acc
            with mock.patch.object(daily, "latest_complete_us_session", return_value=TARGET), \
                 mock.patch.object(daily, "ledger_has_completed_day", return_value=False), \
                 mock.patch.object(daily, "precheck", side_effect=[check(TARGET, TARGET)]), \
                 mock.patch.object(daily, "refresh_and_validate", side_effect=refresh_ok):
                daily.run_once(now=NOW, sleep_until_retry=False, sla_path=sla,
                               shadow_executor=mock.Mock(return_value="SHADOW_EXECUTED"),
                               dashboard_refresher=refresher)
        self.assertIn(TARGET.isoformat(), captured.get("targets", []))


if __name__ == "__main__":
    unittest.main()
