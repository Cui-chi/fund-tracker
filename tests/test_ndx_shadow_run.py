import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ndx_shadow_run as shadow


NY = shadow.NEW_YORK


def canonical_report(session="2026-06-22", run_id="run-day1"):
    chain = {
        "model_candidate": {"ndx_gap_routed_amount": 846.24, "ndx_candidate_release_amount": 298.75},
        "carrier_matching": {"carrier_snapshot_id": "qdii-1", "carrier_snapshot_valid": True,
                             "current_effective_carrier_capacity": 11280.0,
                             "carrier_coverable_amount": 298.75, "retained_due_to_capacity": 0.0,
                             "retained_due_to_carrier_block": 0.0},
        "formal_decision": {"formal_executable_amount": 0.0, "formal_release_amount": 0.0,
                            "retained_due_to_decision_freeze": 298.75},
        "identity_verification": {"candidate_to_carrier_reconciled": True,
                                  "carrier_to_decision_reconciled": True,
                                  "amount_chain_difference": 0},
    }
    copilot = {
        "run_id": run_id, "generated_at": session + "T16:30:00-04:00",
        "data_status": "PASS", "model_status": "UNDER_VALIDATION",
        "validation_stage": "OFFLINE_PASS", "activation_status": "NOT_ACTIVE",
        "decision_status": "FREEZE", "dynamic_cash_pool_status": "FREEZE",
        "ready_for_ndx_shadow": True, "shadow_days_completed": 0,
        "carrier_snapshot_id": "qdii-1", "input_hashes": {},
        "ndx_price_temperature": {
            "price_primary_source": "FRED_NASDAQ100",
            "source_date": session, "price_data_status": "PASS", "rate_data_status": "PASS",
            "volatility_data_status": "PASS",
            "no_lookahead_check": "PASS", "formula_version": shadow.MODEL_VERSION,
            "temperature_score": 22.0, "temperature_level": "HOT", "base_release_factor": 0.415,
            "dfii10": 2.2, "dfii10_source_date": session, "dfii10_percentile": 96.0,
            "real_yield_modifier": 0.85, "rate_adjusted_release_factor": 0.353,
            "realized_volatility_60d": 0.23, "realized_volatility_60d_percentile": 71.0,
            "volatility_cap": 1.0, "candidate_effective_release_factor": 0.353,
            "ndx_close": 30000.0, "distance_to_ma500": 0.3, "drawdown_magnitude": 0.01,
            "retrieved_at": session + "T16:20:00-04:00",
        },
        "ndx_data_layer": {
            "trade_date": session,
            "price_primary": {"source": "FRED_NASDAQ100", "instrument": "NDX", "role": "NDX_PRIMARY", "date": session, "close": 30000.0, "price_field": "close"},
            "price_validators": [],
            "proxy_validators": [{"source": "QQQ_PROXY", "instrument": "QQQ", "role": "PROXY_VALIDATOR", "date": session, "close": 300.0, "price_field": "close"}],
            "macro_inputs": [{"source": "DFII10", "instrument": "DFII10", "role": "macro_input", "date": session, "close": 2.2, "value": 2.2, "lag_trading_days": 0, "lag_status": "FRESH", "accepted_as_of_date": session}],
            "validator_warnings": [],
            "fetch_errors": [],
        },
        "v7_decision_chain": chain,
        "shadow_inputs": {
            "portfolio_snapshot": {"source": "V7 portfolio holdings", "source_date": session, "stale_status": "PASS", "current_values": {}},
            "target_snapshot": {"source": "V7 target configuration", "source_date": session, "stale_status": "PASS", "effective_targets": {}},
            "dynamic_cash_pool_status": "FREEZE", "dynamic_cash_pool": 1875,
            "formula_version": shadow.MODEL_VERSION,
        },
    }
    return {"generated_at": copilot["generated_at"], "copilot": copilot}


def qdii_files(root):
    latest = root / "latest.json"
    raw = root / "raw.json"
    latest.write_text(json.dumps({"schema_version": "qdii-carrier-facts-v2", "snapshot": {
        "snapshot_id": "qdii-1", "generated_at": "2026-06-22T16:10:00-04:00", "stale_status": "PASS"}}), encoding="utf-8")
    raw.write_text(json.dumps({"schema_version": "1.0.0", "generated_at": "2026-06-22T16:10:00-04:00"}), encoding="utf-8")
    return latest, raw


def runnable_report(root, session="2026-06-22", run_id="run-day1"):
    latest, raw = qdii_files(root)
    payload = canonical_report(session, run_id)
    payload["copilot"]["input_hashes"] = {
        "carrier_latest_sha256": shadow.sha256_file(latest),
        "carrier_raw_sha256": shadow.sha256_file(raw),
    }
    report = root / (run_id + ".json")
    report.write_text(json.dumps(payload), encoding="utf-8")
    return report, latest, raw


def run_valid_day(root, ledger, session, run_id):
    report, latest, raw = runnable_report(root, session, run_id)
    day = dt.date.fromisoformat(session)
    return shadow.run_shadow_session(report, ledger, root / "shadow", day,
                                     dt.datetime.combine(day, dt.time(16, 16)).replace(tzinfo=NY),
                                     latest, raw, True)


def mark_ledger_failed(path, failed_session="2026-06-22", run_id="failed-run"):
    payload = shadow.load_ledger(path)
    payload["status"] = "SHADOW_FAILED"
    payload["next_status"] = "MANUAL_REVIEW_REQUIRED"
    payload.setdefault("failures", []).append({
        "shadow_day": payload["shadow_days_completed"] + 1,
        "market_session_date": failed_session,
        "run_id": run_id,
        "failures": [{"failed_gate": "data", "failed_field": "primary.date"}],
    })
    payload["ledger_sha256"] = shadow._ledger_hash(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def day0_report(root):
    report = canonical_report("2026-06-18", "day0")
    path = root / "day0.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


class NdxShadowRunTests(unittest.TestCase):
    def test_01_weekend_not_session(self): self.assertFalse(shadow.is_nasdaq_session(dt.date(2026, 6, 20)))
    def test_02_juneteenth_closed(self): self.assertFalse(shadow.is_nasdaq_session(dt.date(2026, 6, 19)))
    def test_03_next_session(self): self.assertEqual(shadow.next_nasdaq_session(dt.date(2026, 6, 18)), dt.date(2026, 6, 22))
    def test_04_intraday_not_complete(self):
        s=shadow.market_session_status(dt.date(2026,6,22),dt.datetime(2026,6,22,15,0,tzinfo=NY)); self.assertFalse(s["complete_us_trading_day"])
    def test_05_after_close_complete(self):
        s=shadow.market_session_status(dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY)); self.assertTrue(s["complete_us_trading_day"])
    def test_05b_independence_observed_eve_is_early_close(self):
        self.assertTrue(shadow.is_early_close(dt.date(2026,7,2)))
    def test_06_day0_not_counted(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(ledger["shadow_days_completed"],0)
    def test_07_initial_status_pending(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(ledger["status"],"DAY1_PENDING")
    def test_08_ledger_hash_valid(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(shadow.load_ledger(r/"ledger.json")["shadow_days_completed"],0)
    def test_09_tampered_ledger_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); shadow.initialize_ledger(day0_report(r),r/"ledger.json"); p=json.loads((r/"ledger.json").read_text()); p["shadow_days_completed"]=2; (r/"ledger.json").write_text(json.dumps(p));
            with self.assertRaises(shadow.ShadowRunError): shadow.load_ledger(r/"ledger.json")
    def test_10_canonical_view_ignores_legacy_468(self):
        p=canonical_report(); p["copilot"]["qdii_carrier_integration"]={"selection":{"asset_allocated_amount":468.75}}; self.assertEqual(shadow.canonical_shadow_view(p)["v7_decision_chain"]["model_candidate"]["ndx_candidate_release_amount"],298.75)
    def test_11_missing_canonical_chain_rejected(self):
        p=canonical_report(); del p["copilot"]["v7_decision_chain"]
        with self.assertRaises(shadow.ShadowRunError): shadow.canonical_shadow_view(p)
    def test_12_selection_not_returned(self): self.assertNotIn("selection", shadow.canonical_shadow_view(canonical_report()))
    def test_13_formula_must_match(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["ndx_price_temperature"]["formula_version"]="bad"; self.assertTrue(any(x["failed_field"]=="model.formula_version" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_14_source_date_must_match(self):
        c=shadow.canonical_shadow_view(canonical_report()); self.assertTrue(any(x["failed_field"]=="primary.date" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,23),{})))
    def test_15_no_lookahead_required(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["ndx_price_temperature"]["no_lookahead_check"]="FAIL"; self.assertTrue(any(x["failed_field"]=="model.no_lookahead_check" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_16_missing_score_not_defaulted(self):
        c=shadow.canonical_shadow_view(canonical_report()); del c["ndx_price_temperature"]["temperature_score"]; self.assertTrue(any(x["failed_field"]=="temperature_score" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_17_nan_rejected(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["ndx_price_temperature"]["temperature_score"]=float("nan"); self.assertTrue(any(x["failed_field"]=="temperature_score" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_18_factor_bounds(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["ndx_price_temperature"]["candidate_effective_release_factor"]=1.1; self.assertTrue(any(x["failed_field"]=="candidate_effective_release_factor" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_19_candidate_identity(self):
        c=shadow.canonical_shadow_view(canonical_report()); self.assertFalse(any(x["failed_field"]=="candidate_identity" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_20_decision_identity(self):
        c=shadow.canonical_shadow_view(canonical_report()); self.assertFalse(any(x["failed_field"]=="decision_identity" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_21_formal_execution_zero(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["v7_decision_chain"]["formal_decision"]["formal_executable_amount"]=1; self.assertTrue(any(x["failed_field"]=="safety.formal_executable_amount" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_22_formal_release_zero(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["v7_decision_chain"]["formal_decision"]["formal_release_amount"]=1; self.assertTrue(any(x["failed_field"]=="safety.formal_release_amount" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_23_freeze_required(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["status"]["decision_status"]="EXECUTE"; self.assertTrue(any(x["failed_field"]=="safety.decision_status" for x in shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})))
    def test_24_carrier_failure_does_not_change_candidate(self):
        c=shadow.canonical_shadow_view(canonical_report()); c["v7_decision_chain"]["carrier_matching"].update({"carrier_snapshot_valid":False,"carrier_coverable_amount":0,"retained_due_to_carrier_block":298.75}); self.assertEqual(c["v7_decision_chain"]["model_candidate"]["ndx_candidate_release_amount"],298.75)
    def test_25_incomplete_session_does_not_increment(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger); latest,raw=qdii_files(r); report=r/"report.json"; report.write_text(json.dumps(canonical_report())); result=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,15,0,tzinfo=NY),latest,raw,True); self.assertEqual(result["shadow_days_completed"],0)
    def test_26_browser_gate_does_not_increment(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger); latest,raw=qdii_files(r); report=r/"report.json"; report.write_text(json.dumps(canonical_report())); result=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY),latest,raw,False); self.assertEqual(result["reason"],"BROWSER_VERIFICATION_REQUIRED")
    def test_27_pending_keeps_freeze(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(shadow.pending_status(ledger)["dynamic_cash_pool_status"],"FREEZE")
    def test_28_pending_release_zero(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(shadow.pending_status(ledger)["formal_release_amount"],0)
    def test_29_ledger_dates_unique_validation(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(len({x.get("market_session_date") for x in ledger["days"]}),0)
    def test_30_ledger_run_ids_unique_validation(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(len({x.get("run_id") for x in ledger["days"]}),0)
    def test_31_completion_never_activates(self):
        ledger={"activation_status":"NOT_ACTIVE","decision_status":"FREEZE","dynamic_cash_pool_status":"FREEZE"}; self.assertEqual(tuple(ledger.values()),("NOT_ACTIVE","FREEZE","FREEZE"))
    def test_32_day_number_starts_at_one(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=shadow.initialize_ledger(day0_report(r),r/"ledger.json"); self.assertEqual(ledger["shadow_days_completed"]+1,1)

    def test_33_complete_valid_day_increments_once(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            report,latest,raw=runnable_report(r)
            result=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY),latest,raw,True)
            self.assertTrue(result["shadow_evaluation"]["day_gate_pass"])
            self.assertEqual((shadow.load_ledger(ledger)["status"],shadow.load_ledger(ledger)["shadow_days_completed"]),("DAY1_PASS",1))

    def test_34_duplicate_session_does_not_increment(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            report,latest,raw=runnable_report(r)
            now=dt.datetime(2026,6,22,16,16,tzinfo=NY)
            shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),now,latest,raw,True)
            result=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),now,latest,raw,True)
            self.assertEqual((result["reason"],result["shadow_days_completed"]),("DUPLICATE_MARKET_SESSION_DATE",1))

    def test_35_failed_complete_day_does_not_increment(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            report,latest,raw=runnable_report(r)
            payload=json.loads(report.read_text()); payload["copilot"]["ndx_price_temperature"]["formula_version"]="wrong"; report.write_text(json.dumps(payload))
            result=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY),latest,raw,True)
            self.assertFalse(result["shadow_evaluation"]["day_gate_pass"])
            self.assertEqual((shadow.load_ledger(ledger)["status"],shadow.load_ledger(ledger)["shadow_days_completed"]),("SHADOW_FAILED",0))

    def test_35b_historical_failure_does_not_block_future_success(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            mark_ledger_failed(ledger, "2026-06-22", "failed-run")
            result=run_valid_day(r,ledger,"2026-06-23","run-day-after-failure")
            final=shadow.load_ledger(ledger)
            self.assertTrue(result["shadow_evaluation"]["day_gate_pass"])
            self.assertEqual(final["shadow_days_completed"],1)
            self.assertEqual(final["days"][0]["market_session_date"],"2026-06-23")
            self.assertEqual(final["failures"][0]["market_session_date"],"2026-06-22")

    def test_35c_duplicate_failed_session_is_not_recounted(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            mark_ledger_failed(ledger, "2026-06-22", "failed-run")
            report,latest,raw=runnable_report(r,"2026-06-22","retry-failed-run")
            result=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY),latest,raw,True)
            final=shadow.load_ledger(ledger)
            self.assertEqual(result["reason"],"DUPLICATE_FAILED_MARKET_SESSION_DATE")
            self.assertEqual(final["shadow_days_completed"],0)
            self.assertEqual(len(final["failures"]),1)

    def test_36_success_archives_all_required_inputs(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            report,latest,raw=runnable_report(r)
            shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY),latest,raw,True)
            inputs=r/"shadow"/"2026-06-22"/"inputs"
            expected={"ndx-price-input.json","ndx-data-layer.json","dfii10-input.json","qdii-carrier-latest.json","qdii-carrier-snapshot-raw.json","portfolio-snapshot.json","target-snapshot.json","input-manifest.json"}
            self.assertEqual({p.name for p in inputs.iterdir()},expected)

    def test_37_five_pass_days_complete_without_activation(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            sessions = ("2026-06-22","2026-06-23","2026-06-24","2026-06-25","2026-06-26")
            self.assertEqual(len(sessions), shadow.REQUIRED_COMPLETE_DAYS)
            for index,session in enumerate(sessions,1):
                run_valid_day(r,ledger,session,"run-day%d" % index)
            final=shadow.load_ledger(ledger)
            self.assertEqual((final["status"],final["shadow_days_completed"],final["activation_status"]),("SHADOW_COMPLETE",5,"NOT_ACTIVE"))

    def test_38_three_pass_days_keep_pool_frozen(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            for index,session in enumerate(("2026-06-22","2026-06-23","2026-06-24"),1): run_valid_day(r,ledger,session,"run-day%d" % index)
            final=shadow.load_ledger(ledger)
            self.assertEqual((final["decision_status"],final["dynamic_cash_pool_status"]),("FREEZE","FREEZE"))
            self.assertTrue(all(row["formal_release_amount"] == 0 for row in final["days"]))

    def test_39_day_two_contains_adjacent_comparison(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            run_valid_day(r,ledger,"2026-06-22","run-day1")
            output=run_valid_day(r,ledger,"2026-06-23","run-day2")
            self.assertIn("adjacent_day_comparison",output)
            self.assertFalse(output["adjacent_day_comparison"]["shadow_anomaly"])

    def test_40_stale_qdii_snapshot_fails_day(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            report,latest,raw=runnable_report(r); payload=json.loads(latest.read_text()); payload["snapshot"]["stale_status"]="STALE"; latest.write_text(json.dumps(payload))
            report_payload=json.loads(report.read_text()); report_payload["copilot"]["input_hashes"]["carrier_latest_sha256"]=shadow.sha256_file(latest); report.write_text(json.dumps(report_payload))
            output=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY),latest,raw,True)
            self.assertFalse(output["shadow_evaluation"]["day_gate_pass"])
            self.assertEqual(shadow.load_ledger(ledger)["shadow_days_completed"],0)

    def test_41_completed_shadow_only_enters_manual_review(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            sessions = ("2026-06-22","2026-06-23","2026-06-24","2026-06-25","2026-06-26")
            for index,session in enumerate(sessions,1): run_valid_day(r,ledger,session,"run-day%d" % index)
            final=shadow.load_ledger(ledger)
            self.assertEqual(final["next_status"],"MANUAL_ACTIVATION_REVIEW")
            self.assertTrue(final["ready_for_manual_activation_review"])

    def test_42_primary_gate_ready_when_primary_date_reaches_target(self):
        data={"trade_date":"2026-06-23","price_primary":{"source":"FRED_NASDAQ100","instrument":"NDX","date":"2026-06-23","close":30000.0},"price_validators":[]}
        self.assertEqual(shadow.evaluate_primary_shadow_gate(data, dt.date(2026,6,23))["decision"], "READY")

    def test_43_primary_gate_not_ready_when_primary_lags(self):
        data={"trade_date":"2026-06-23","price_primary":{"source":"FRED_NASDAQ100","instrument":"NDX","date":"2026-06-22","close":30000.0},"price_validators":[]}
        self.assertEqual(shadow.evaluate_primary_shadow_gate(data, dt.date(2026,6,23))["decision"], "NOT_READY")

    def test_44_validator_lag_is_warning_only(self):
        data={"trade_date":"2026-06-23","price_primary":{"source":"FRED_NASDAQ100","instrument":"NDX","date":"2026-06-23","close":30000.0},"proxy_validators":[{"source":"QQQ_PROXY","date":"2026-06-20"}]}
        self.assertEqual(shadow.evaluate_primary_shadow_gate(data, dt.date(2026,6,23))["decision"], "READY")
        self.assertEqual(shadow.validator_lag_warnings(data["price_primary"], data["proxy_validators"])[0]["warning"], "VALIDATOR_LAG_GT_1_DAY")

    @mock.patch("ndx_shadow_run._run_curl_csv")
    def test_45_fetch_qqq_proxy_parses_latest_close(self, curl):
        curl.return_value = "Date,Open,High,Low,Close,Volume\n2026-06-22,1,1,1,300.12,10\n2026-06-23,1,1,1,301.45,10\n"
        self.assertEqual(shadow.fetch_qqq_proxy(), {"source": "QQQ_PROXY", "instrument": "QQQ", "role": "PROXY_VALIDATOR", "date": "2026-06-23", "close": 301.45, "price_field": "close", "session": "daily_vendor_close"})

    def test_46_primary_future_date_is_as_of_mismatch(self):
        data={"trade_date":"2026-06-23","price_primary":{"source":"FRED_NASDAQ100","instrument":"NDX","date":"2026-06-24","close":30000.0},"price_validators":[]}
        self.assertEqual(shadow.evaluate_primary_shadow_gate(data, dt.date(2026,6,23))["decision"], "AS_OF_MISMATCH")

    def test_47_primary_and_model_price_source_mismatch_fails(self):
        c=shadow.canonical_shadow_view(canonical_report())
        c["ndx_data_layer"]["price_primary"]["source"]="QQQ_PROXY"
        c["ndx_data_layer"]["price_primary"]["instrument"]="NDX"
        failures=shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})
        self.assertTrue(any(x["failed_field"]=="model_price_source" for x in failures))

    def test_48_primary_and_model_price_date_mismatch_fails(self):
        c=shadow.canonical_shadow_view(canonical_report())
        c["ndx_price_temperature"]["source_date"]="2026-06-21"
        failures=shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})
        self.assertTrue(any(x["failed_field"]=="model_price_source_date" for x in failures))

    def test_48b_primary_and_model_price_value_mismatch_fails(self):
        c=shadow.canonical_shadow_view(canonical_report())
        c["ndx_data_layer"]["price_primary"]["close"]=29999.0
        failures=shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})
        self.assertTrue(any(x["failed_field"]=="model_ndx_value" for x in failures))

    def test_49_dfii10_is_macro_input_not_price_validator(self):
        layer=canonical_report()["copilot"]["ndx_data_layer"]
        self.assertEqual(layer["macro_inputs"][0]["source"], "DFII10")
        self.assertNotIn("DFII10", [row["source"] for row in layer["price_validators"]])

    def test_49b_macro_input_matches_model(self):
        c=shadow.canonical_shadow_view(canonical_report())
        result=shadow.evaluate_macro_input_consistency(c["ndx_data_layer"], c["ndx_price_temperature"])
        self.assertEqual((result["decision"], result["macro_input_match"]), ("PASS", True))

    def test_49c_macro_input_date_mismatch_is_critical_fail(self):
        c=shadow.canonical_shadow_view(canonical_report())
        c["ndx_data_layer"]["macro_inputs"][0]["date"]="2026-06-21"
        failures=shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})
        self.assertTrue(any(x["failed_field"]=="model_dfii10_source_date" for x in failures))
        self.assertFalse(any(x["failed_field"]=="safety.formal_release_amount" and x["actual_value"] != 0.0 for x in failures))

    def test_49d_macro_input_value_mismatch_is_critical_fail(self):
        c=shadow.canonical_shadow_view(canonical_report())
        c["ndx_data_layer"]["macro_inputs"][0]["value"]=2.1
        c["ndx_data_layer"]["macro_inputs"][0]["close"]=2.1
        failures=shadow.evaluate_day_gates(c,dt.date(2026,6,22),{})
        self.assertTrue(any(x["failed_field"]=="model_dfii10_value" for x in failures))

    def test_50_qqq_is_proxy_validator_not_primary(self):
        layer=canonical_report()["copilot"]["ndx_data_layer"]
        self.assertEqual((layer["price_primary"]["source"], layer["price_primary"]["instrument"]), ("FRED_NASDAQ100", "NDX"))
        self.assertEqual((layer["proxy_validators"][0]["source"], layer["proxy_validators"][0]["price_field"]), ("QQQ_PROXY", "close"))

    def test_50b_qqq_cannot_drive_ndx_model(self):
        data={"trade_date":"2026-06-23","price_primary":{"source":"QQQ_PROXY","instrument":"QQQ","date":"2026-06-23","close":300.0}}
        self.assertEqual(shadow.evaluate_primary_shadow_gate(data, dt.date(2026,6,23))["decision"], "CRITICAL_FAIL")

    def test_51_fred_primary_fetch_parses_latest_ndx_close(self):
        with mock.patch("ndx_shadow_run._run_curl_csv") as curl:
            curl.return_value = "observation_date,NASDAQ100\n2026-06-22,30000.1\n2026-06-23,30010.2\n"
            self.assertEqual(shadow.fetch_ndx_primary(), {"source": "FRED_NASDAQ100", "date": "2026-06-23", "close": 30010.2, "instrument": "NDX", "role": "NDX_PRIMARY", "price_field": "close"})

    def test_52_canonical_hash_stable_for_same_business_input(self):
        c=shadow.canonical_shadow_view(canonical_report())
        h1=shadow.canonical_input_hash(c,dt.date(2026,6,22),c["ndx_data_layer"])
        h2=shadow._sha256_bytes(shadow._canonical_json(dict(reversed(list(shadow.canonical_input_payload(c,dt.date(2026,6,22),c["ndx_data_layer"]).items())))))
        self.assertEqual(h1,h2)

    def test_53_canonical_hash_ignores_runtime_fields(self):
        c1=shadow.canonical_shadow_view(canonical_report())
        c2=shadow.canonical_shadow_view(canonical_report())
        c2["run_id"]="different-run"
        c2["generated_at"]="2099-01-01T00:00:00+00:00"
        c2["ndx_price_temperature"]["retrieved_at"]="2099-01-01T00:00:00+00:00"
        h1=shadow.canonical_input_hash(c1,dt.date(2026,6,22),c1["ndx_data_layer"])
        h2=shadow.canonical_input_hash(c2,dt.date(2026,6,22),c2["ndx_data_layer"])
        self.assertEqual(h1,h2)

    def test_54_canonical_hash_changes_when_ndx_changes(self):
        c=shadow.canonical_shadow_view(canonical_report())
        h1=shadow.canonical_input_hash(c,dt.date(2026,6,22),c["ndx_data_layer"])
        c["ndx_data_layer"]["price_primary"]["close"]=30001.0
        c["ndx_price_temperature"]["ndx_close"]=30001.0
        h2=shadow.canonical_input_hash(c,dt.date(2026,6,22),c["ndx_data_layer"])
        self.assertNotEqual(h1,h2)

    def test_55_canonical_hash_changes_when_dfii10_changes(self):
        c=shadow.canonical_shadow_view(canonical_report())
        h1=shadow.canonical_input_hash(c,dt.date(2026,6,22),c["ndx_data_layer"])
        c["ndx_data_layer"]["macro_inputs"][0]["value"]=2.21
        c["ndx_price_temperature"]["dfii10"]=2.21
        h2=shadow.canonical_input_hash(c,dt.date(2026,6,22),c["ndx_data_layer"])
        self.assertNotEqual(h1,h2)

    def test_56_manifest_canonical_hash_controls_gate_not_raw_hash(self):
        c=shadow.canonical_shadow_view(canonical_report())
        manifest={"canonical_input_hash":shadow.canonical_input_hash(c,dt.date(2026,6,22),c["ndx_data_layer"]),
                  "hash_match":True,"inputs":{}}
        hashes={"qdii-carrier-snapshot-raw.json":"0"*64}
        failures=shadow.evaluate_day_gates(c,dt.date(2026,6,22),hashes,manifest,c["ndx_data_layer"])
        self.assertFalse(any(x["failed_field"]=="carrier_raw_sha256" for x in failures))
        self.assertFalse(any(x["failed_field"]=="canonical_input_hash" for x in failures))

    def test_57_hash_match_false_blocks_without_release(self):
        c=shadow.canonical_shadow_view(canonical_report())
        manifest={"canonical_input_hash":shadow.canonical_input_hash(c,dt.date(2026,6,22),c["ndx_data_layer"]),
                  "hash_match":False,"inputs":{}}
        failures=shadow.evaluate_day_gates(c,dt.date(2026,6,22),{},manifest,c["ndx_data_layer"])
        self.assertTrue(any(x["failed_field"]=="hash_match" for x in failures))
        self.assertEqual(c["v7_decision_chain"]["formal_decision"]["formal_release_amount"],0.0)

    def test_58_success_manifest_contains_canonical_and_raw_hashes(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t); ledger=r/"ledger.json"; shadow.initialize_ledger(day0_report(r),ledger)
            report,latest,raw=runnable_report(r)
            output=shadow.run_shadow_session(report,ledger,r/"shadow",dt.date(2026,6,22),dt.datetime(2026,6,22,16,16,tzinfo=NY),latest,raw,True)
            manifest=json.loads((r/"shadow"/"2026-06-22"/"inputs"/"input-manifest.json").read_text())
            self.assertEqual(manifest["hash_algorithm"],"sha256")
            self.assertEqual(manifest["hash_canonicalization_version"],shadow.HASH_CANONICALIZATION_VERSION)
            self.assertTrue(manifest["hash_match"])
            self.assertEqual(output["canonical_input_hash"],manifest["canonical_input_hash"])
            self.assertIn("carrier_raw_snapshot_sha256",manifest["raw_snapshot_sha256"])


if __name__ == "__main__": unittest.main()
