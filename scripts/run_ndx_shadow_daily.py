#!/usr/bin/env python3
"""Daily 13:10 NDX V1 shadow-run orchestrator.

This script does not change the NDX formula or V7 reports. It only checks
FRED freshness, refreshes governed local CSV inputs when ready, records a
single SLA ledger, and then delegates to the existing shadow runner.
"""

import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ndx_price_temperature
import ndx_shadow_run


SLA_PATH = ROOT / "reports/shadow/ndx-v1/source-sla.json"
PREPARED_REPORT_ROOT = ROOT / "reports/shadow/ndx-v1/prepared"
NDX_CSV = ROOT / "data/ndx_history/ndx_daily.csv"
DFII10_CSV = ROOT / "data/ndx_history/dfii10_daily.csv"
LEDGER_PATH = ROOT / "reports/shadow/ndx-v1/shadow-ledger.json"


class DailyShadowError(RuntimeError):
    pass


def now_sgt():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).astimezone()


def latest_complete_us_session(now=None):
    now = now or now_sgt()
    ny_date = now.astimezone(ndx_shadow_run.NEW_YORK).date()
    for offset in range(10):
        candidate = ny_date - dt.timedelta(days=offset)
        status = ndx_shadow_run.market_session_status(candidate, evaluated_at=now)
        if status.get("complete_us_trading_day"):
            return candidate
    return None


def fetch_fred_date(series_id):
    row = ndx_shadow_run.fetch_fred_latest(series_id, series_id)
    return ndx_shadow_run._parse_date(row["date"])


def fetch_fred_observation(series_id):
    row = ndx_shadow_run.fetch_fred_latest(series_id, series_id)
    return {
        "source": series_id,
        "date": ndx_shadow_run._parse_date(row["date"]),
        "value": row.get("close"),
    }


def local_csv_max_date(path, series_id):
    rows = ndx_price_temperature.read_fred_csv(path, series_id)
    return rows[-1][0] if rows else None


def refresh_fred_csv(series_id, path):
    text = ndx_shadow_run._run_curl_csv(ndx_shadow_run.FRED_CSV_URL % series_id)
    rows = list(csv.DictReader(text.splitlines()))
    if not rows or series_id not in rows[0]:
        raise DailyShadowError("invalid FRED CSV for %s" % series_id)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=str(path.parent), delete=False) as handle:
        writer = csv.DictWriter(handle, fieldnames=["observation_date", series_id])
        writer.writeheader()
        for row in rows:
            writer.writerow({"observation_date": row.get("observation_date"), series_id: row.get(series_id)})
        temporary = Path(handle.name)
    temporary.replace(path)


def load_sla(path=SLA_PATH):
    path = Path(path)
    if not path.exists():
        return {"schema_version": "ndx-source-sla-v1", "records": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_sla(payload, path=SLA_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def upsert_sla_record(record, path=SLA_PATH):
    payload = load_sla(path)
    records = [row for row in payload.get("records", []) if row.get("target_trade_date") != record["target_trade_date"]]
    records.append(record)
    records.sort(key=lambda row: row["target_trade_date"])
    payload["records"] = records
    write_sla(payload, path)


def ledger_has_completed_day(target_date, ledger_path=LEDGER_PATH):
    path = Path(ledger_path)
    if not path.exists():
        return False
    try:
        ledger = ndx_shadow_run.load_ledger(path)
    except Exception:
        return False
    return any(row.get("market_session_date") == target_date.isoformat() and row.get("result") == "PASS" for row in ledger.get("days", []))


def latest_report_path():
    candidates = sorted((ROOT / "reports/runs").glob("*/json/report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def report_model_identity(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    model = report.get("copilot", {}).get("ndx_price_temperature", {})
    source = model.get("price_primary_source") or model.get("price_source") or model.get("source_name")
    if source == ndx_price_temperature.PRICE_SOURCE_NAME:
        source = ndx_shadow_run.NDX_PRIMARY_SOURCE
    return source, ndx_shadow_run._parse_date(model.get("source_date"))


def trading_day_lag(source_date, target_date):
    source_date = ndx_shadow_run._parse_date(source_date)
    target_date = ndx_shadow_run._parse_date(target_date)
    if not source_date or not target_date:
        return None
    if source_date >= target_date:
        return 0
    lag = 0
    cursor = source_date
    while cursor < target_date:
        cursor += dt.timedelta(days=1)
        if ndx_shadow_run.is_nasdaq_session(cursor):
            lag += 1
    return lag


def evaluate_dfii10_freshness(dfii10_date, target_date):
    dfii10_date = ndx_shadow_run._parse_date(dfii10_date)
    target_date = ndx_shadow_run._parse_date(target_date)
    if not dfii10_date:
        return {"status": "NOT_READY", "lag_trading_days": None}
    if dfii10_date > target_date:
        return {"status": "AS_OF_MISMATCH", "lag_trading_days": 0}
    lag = trading_day_lag(dfii10_date, target_date)
    if dfii10_date == target_date:
        return {"status": "FRESH", "lag_trading_days": lag}
    if lag == 1:
        return {"status": "ACCEPTABLE_LAG", "lag_trading_days": lag}
    return {"status": "NOT_READY", "lag_trading_days": lag}


def precheck(target_date):
    ndx = fetch_fred_observation("NASDAQ100")
    dfii10 = fetch_fred_observation("DFII10")
    dfii10_freshness = evaluate_dfii10_freshness(dfii10["date"], target_date)
    return {
        "fred_ndx_date": ndx["date"],
        "fred_ndx_value": ndx["value"],
        "fred_dfii10_date": dfii10["date"],
        "fred_dfii10_value": dfii10["value"],
        "dfii10_lag_trading_days": dfii10_freshness["lag_trading_days"],
        "dfii10_lag_status": dfii10_freshness["status"],
        "dfii10_accepted_as_of_date": dfii10["date"] if dfii10_freshness["status"] in ("FRESH", "ACCEPTABLE_LAG") else None,
        "local_ndx_date": local_csv_max_date(NDX_CSV, "NASDAQ100"),
        "local_dfii10_date": local_csv_max_date(DFII10_CSV, "DFII10"),
    }


def ready_from_fred(check, target_date):
    if check["fred_ndx_date"] > target_date:
        return "AS_OF_MISMATCH"
    if check["fred_ndx_date"] < target_date:
        return "NOT_READY"
    dfii10_status = check.get("dfii10_lag_status") or evaluate_dfii10_freshness(check.get("fred_dfii10_date"), target_date)["status"]
    if dfii10_status == "AS_OF_MISMATCH":
        return "AS_OF_MISMATCH"
    if dfii10_status in ("FRESH", "ACCEPTABLE_LAG"):
        return "READY"
    return "NOT_READY"


def refresh_and_validate(target_date, accepted_dfii10=None):
    refresh_fred_csv("NASDAQ100", NDX_CSV)
    refresh_fred_csv("DFII10", DFII10_CSV)
    local_ndx = local_csv_max_date(NDX_CSV, "NASDAQ100")
    local_dfii10 = local_csv_max_date(DFII10_CSV, "DFII10")
    accepted_dfii10_date = ndx_shadow_run._parse_date((accepted_dfii10 or {}).get("dfii10_source_date")) or target_date
    return local_ndx == target_date and local_dfii10 == accepted_dfii10_date, local_ndx, local_dfii10


def accepted_ndx_from_check(check, target_date, retrieved_at):
    ndx_date = ndx_shadow_run._parse_date(check.get("fred_ndx_date"))
    if ndx_date != target_date:
        return None
    return {
        "ndx_source": ndx_shadow_run.NDX_PRIMARY_SOURCE,
        "ndx_instrument": ndx_shadow_run.NDX_PRIMARY_INSTRUMENT,
        "ndx_source_date": ndx_date.isoformat(),
        "ndx_value": check.get("fred_ndx_value"),
        "ndx_retrieved_at": retrieved_at,
        "ndx_accepted_as_of_date": ndx_date.isoformat(),
    }


def accepted_dfii10_from_check(check, target_date, retrieved_at):
    freshness = evaluate_dfii10_freshness(check.get("fred_dfii10_date"), target_date)
    if freshness["status"] not in ("FRESH", "ACCEPTABLE_LAG"):
        return None
    return {
        "dfii10_source": "DFII10",
        "dfii10_source_date": check["fred_dfii10_date"].isoformat(),
        "dfii10_value": check.get("fred_dfii10_value"),
        "dfii10_retrieved_at": retrieved_at,
        "dfii10_lag_trading_days": freshness["lag_trading_days"],
        "dfii10_lag_status": freshness["status"],
        "dfii10_accepted_as_of_date": check["fred_dfii10_date"].isoformat(),
    }


def latest_model_snapshot_with_accepted_inputs(accepted_ndx, accepted_dfii10):
    prices = ndx_price_temperature.read_fred_csv(NDX_CSV, "NASDAQ100")
    rate_daily = ndx_price_temperature.read_fred_csv(DFII10_CSV, "DFII10")
    accepted_ndx_date = ndx_shadow_run._parse_date(accepted_ndx.get("ndx_source_date"))
    accepted_ndx_value = accepted_ndx.get("ndx_value")
    if accepted_ndx_date and accepted_ndx_value is not None:
        prices = [(date, value) for date, value in prices if date < accepted_ndx_date]
        prices.append((accepted_ndx_date, float(accepted_ndx_value)))
        prices.sort(key=lambda item: item[0])
    accepted_date = ndx_shadow_run._parse_date(accepted_dfii10.get("dfii10_source_date"))
    accepted_value = accepted_dfii10.get("dfii10_value")
    if accepted_date and accepted_value is not None:
        rate_daily = [(date, value) for date, value in rate_daily if date != accepted_date]
        rate_daily.append((accepted_date, float(accepted_value)))
        rate_daily.sort(key=lambda item: item[0])
    rates = ndx_price_temperature.daily_rates_to_monthly(rate_daily)
    snapshot = ndx_price_temperature.latest_snapshot(prices, rates)
    snapshot["price_primary_source"] = ndx_shadow_run.NDX_PRIMARY_SOURCE
    return snapshot


def latest_model_snapshot_with_accepted_dfii10(accepted_dfii10):
    accepted_ndx = {
        "ndx_source": ndx_shadow_run.NDX_PRIMARY_SOURCE,
        "ndx_instrument": ndx_shadow_run.NDX_PRIMARY_INSTRUMENT,
        "ndx_source_date": local_csv_max_date(NDX_CSV, "NASDAQ100").isoformat(),
        "ndx_value": ndx_price_temperature.read_fred_csv(NDX_CSV, "NASDAQ100")[-1][1],
    }
    return latest_model_snapshot_with_accepted_inputs(accepted_ndx, accepted_dfii10)


def apply_shadow_inputs_to_report(report, target_date, accepted_dfii10, accepted_ndx=None):
    report = dict(report)
    copilot = dict(report.get("copilot", {}))
    report["copilot"] = copilot
    accepted_ndx = accepted_ndx or {
        "ndx_source": ndx_shadow_run.NDX_PRIMARY_SOURCE,
        "ndx_instrument": ndx_shadow_run.NDX_PRIMARY_INSTRUMENT,
        "ndx_source_date": target_date.isoformat(),
        "ndx_value": None,
    }
    model = _json_safe(latest_model_snapshot_with_accepted_inputs(accepted_ndx, accepted_dfii10))
    model["source_date"] = accepted_ndx.get("ndx_source_date")
    model["ndx_close"] = accepted_ndx.get("ndx_value", model.get("ndx_close"))
    model["dfii10_source_date"] = accepted_dfii10.get("dfii10_source_date")
    model["dfii10"] = accepted_dfii10.get("dfii10_value")
    copilot["ndx_price_temperature"] = model
    ndx_data_layer = dict(copilot.get("ndx_data_layer") or {})
    ndx_data_layer.update({
        "trade_date": target_date.isoformat(),
        "price_primary": {
            "source": ndx_shadow_run.NDX_PRIMARY_SOURCE,
            "instrument": ndx_shadow_run.NDX_PRIMARY_INSTRUMENT,
            "role": "NDX_PRIMARY",
            "date": accepted_ndx.get("ndx_source_date"),
            "close": accepted_ndx.get("ndx_value", model.get("ndx_close")),
            "price_field": "close",
            "retrieved_at": accepted_ndx.get("ndx_retrieved_at"),
            "accepted_as_of_date": accepted_ndx.get("ndx_accepted_as_of_date"),
        },
        "macro_inputs": [{
            "source": "DFII10",
            "instrument": "DFII10",
            "role": "macro_input",
            "date": accepted_dfii10.get("dfii10_source_date"),
            "close": accepted_dfii10.get("dfii10_value"),
            "value": accepted_dfii10.get("dfii10_value"),
            "retrieved_at": accepted_dfii10.get("dfii10_retrieved_at"),
            "lag_trading_days": accepted_dfii10.get("dfii10_lag_trading_days"),
            "lag_status": accepted_dfii10.get("dfii10_lag_status"),
            "accepted_as_of_date": accepted_dfii10.get("dfii10_accepted_as_of_date"),
        }],
    })
    ndx_data_layer.setdefault("price_validators", [])
    ndx_data_layer.setdefault("proxy_validators", [])
    ndx_data_layer.setdefault("validator_warnings", [])
    ndx_data_layer.setdefault("fetch_errors", [])
    copilot["ndx_data_layer"] = ndx_data_layer
    copilot["prepared_snapshot_validation"] = validate_prepared_snapshot_fields(report, target_date)
    return report


def _json_safe(value):
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def validate_prepared_snapshot_fields(report, target_date):
    copilot = report.get("copilot", {})
    model = copilot.get("ndx_price_temperature", {})
    data_layer = copilot.get("ndx_data_layer", {})
    primary = data_layer.get("price_primary", {})
    macro = (data_layer.get("macro_inputs") or [{}])[0]
    try:
        ndx_value_match = float(primary.get("close")) == float(model.get("ndx_close"))
    except (TypeError, ValueError):
        ndx_value_match = False
    try:
        dfii10_value_match = float(macro.get("value")) == float(model.get("dfii10"))
    except (TypeError, ValueError):
        dfii10_value_match = False
    result = {
        "target_trade_date": target_date.isoformat(),
        "accepted_ndx_source": primary.get("source"),
        "accepted_ndx_source_date": primary.get("date"),
        "model_ndx_source_date": model.get("source_date"),
        "accepted_ndx_close": primary.get("close"),
        "model_ndx_close": model.get("ndx_close"),
        "accepted_dfii10_source_date": macro.get("date"),
        "model_dfii10_source_date": model.get("dfii10_source_date"),
        "accepted_dfii10_value": macro.get("value"),
        "model_dfii10_value": model.get("dfii10"),
        "ndx_input_match": primary.get("date") == model.get("source_date") and ndx_value_match,
        "macro_input_match": macro.get("date") == model.get("dfii10_source_date") and dfii10_value_match,
    }
    result["status"] = "PASS" if result["ndx_input_match"] and result["macro_input_match"] else "CRITICAL_FAIL"
    return result


def prepared_snapshot_is_valid(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    validation = payload.get("copilot", {}).get("prepared_snapshot_validation", {})
    return validation.get("status") == "PASS"


def write_prepared_shadow_report(report, target_date, prepared_root=None):
    target_dir = Path(prepared_root or PREPARED_REPORT_ROOT) / target_date.isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    run_id = report.get("copilot", {}).get("run_id") or "shadow"
    generated = now_sgt().strftime("%Y%m%dT%H%M%S%z")
    path = target_dir / ("%s-%s-canonical-shadow-report.json" % (generated, run_id))
    payload = _json_safe(report)
    validation = validate_prepared_snapshot_fields(payload, target_date)
    payload.setdefault("copilot", {})["prepared_snapshot_validation"] = validation
    if validation.get("status") != "PASS":
        raise DailyShadowError("prepared snapshot field mismatch")
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with temporary.open(encoding="utf-8") as handle:
            json.load(handle)
        os.replace(str(temporary), str(path))
        with path.open(encoding="utf-8") as handle:
            json.load(handle)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def execute_shadow(target_date, report_path=None, accepted_dfii10=None):
    report_path = Path(report_path) if report_path else latest_report_path()
    if not report_path:
        return "NO_REPORT"
    source, model_date = report_model_identity(report_path)
    if source != ndx_shadow_run.NDX_PRIMARY_SOURCE:
        return "MODEL_SNAPSHOT_NOT_READY"
    run_report = report_path
    accepted_ndx = None
    if accepted_dfii10:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        accepted_ndx = accepted_dfii10.get("accepted_ndx")
        report = apply_shadow_inputs_to_report(report, target_date, accepted_dfii10, accepted_ndx)
        model_date = ndx_shadow_run._parse_date(report.get("copilot", {}).get("ndx_price_temperature", {}).get("source_date"))
        if model_date != target_date:
            return "MODEL_SNAPSHOT_NOT_READY"
        try:
            run_report = write_prepared_shadow_report(report, target_date)
        except Exception:
            return "MODEL_SNAPSHOT_NOT_READY"
        if not prepared_snapshot_is_valid(run_report):
            return "MODEL_SNAPSHOT_NOT_READY"
    elif model_date != target_date:
        return "MODEL_SNAPSHOT_NOT_READY"
    command = [
        sys.executable,
        str(ROOT / "scripts/run_ndx_shadow.py"),
        "--run-session", target_date.isoformat(),
        "--report", str(run_report),
        "--browser-verified",
    ]
    completed = subprocess.run(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=900)
    if completed.returncode:
        raise DailyShadowError(completed.stdout.strip() or "shadow runner failed")
    return "SHADOW_EXECUTED"


def run_once(now=None, sleep_until_retry=True, sla_path=SLA_PATH, shadow_executor=execute_shadow):
    now = now or now_sgt()
    target = latest_complete_us_session(now)
    if not target:
        return {"final_status": "NO_COMPLETE_SESSION", "shadow_executed": False}
    if ledger_has_completed_day(target):
        return {"target_trade_date": target.isoformat(), "final_status": "ALREADY_COMPLETED", "shadow_executed": False}
    first_at = now.isoformat(timespec="seconds")
    retry_at = None
    first = precheck(target)
    status = ready_from_fred(first, target)
    ready_attempt = "FIRST" if status == "READY" else "NONE"
    final_check = dict(first)
    if status == "NOT_READY":
        retry_time = now.replace(hour=13, minute=25, second=0, microsecond=0)
        if sleep_until_retry and now < retry_time:
            time.sleep(max(0, (retry_time - now).total_seconds()))
        retry_at = now_sgt().isoformat(timespec="seconds")
        retry = precheck(target)
        final_check = dict(retry)
        status = ready_from_fred(retry, target)
        ready_attempt = "RETRY" if status == "READY" else "NONE"
    shadow_executed = False
    final_status = status
    if status == "READY":
        retrieved_at = now_sgt().isoformat(timespec="seconds")
        accepted_ndx = accepted_ndx_from_check(final_check, target, retrieved_at)
        accepted_dfii10 = accepted_dfii10_from_check(final_check, target, retrieved_at)
        if accepted_dfii10 and accepted_ndx:
            accepted_dfii10["accepted_ndx"] = accepted_ndx
        ok, local_ndx, local_dfii10 = refresh_and_validate(target, accepted_dfii10)
        final_check["local_ndx_date"] = local_ndx
        final_check["local_dfii10_date"] = local_dfii10
        if not ok:
            final_status = "LOCAL_REFRESH_FAILED"
        else:
            try:
                final_status = shadow_executor(target, accepted_dfii10=accepted_dfii10)
            except TypeError:
                final_status = shadow_executor(target)
            shadow_executed = final_status == "SHADOW_EXECUTED"
    record = {
        "target_trade_date": target.isoformat(),
        "first_check_at": first_at,
        "retry_check_at": retry_at,
        "fred_ndx_date": final_check["fred_ndx_date"].isoformat() if final_check.get("fred_ndx_date") else None,
        "ndx_value": final_check.get("fred_ndx_value"),
        "fred_dfii10_date": final_check["fred_dfii10_date"].isoformat() if final_check.get("fred_dfii10_date") else None,
        "local_ndx_date": final_check["local_ndx_date"].isoformat() if final_check.get("local_ndx_date") else None,
        "local_dfii10_date": final_check["local_dfii10_date"].isoformat() if final_check.get("local_dfii10_date") else None,
        "dfii10_lag_trading_days": final_check.get("dfii10_lag_trading_days"),
        "dfii10_lag_status": final_check.get("dfii10_lag_status"),
        "dfii10_value": final_check.get("fred_dfii10_value"),
        "dfii10_accepted_as_of_date": final_check.get("dfii10_accepted_as_of_date").isoformat() if final_check.get("dfii10_accepted_as_of_date") else None,
        "ready_attempt": ready_attempt,
        "final_status": final_status,
        "shadow_executed": shadow_executed,
    }
    upsert_sla_record(record, sla_path)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-sleep", action="store_true")
    args = parser.parse_args()
    result = run_once(sleep_until_retry=not args.no_sleep)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("final_status") in ("SHADOW_EXECUTED", "NOT_READY", "AS_OF_MISMATCH", "ALREADY_COMPLETED", "MODEL_SNAPSHOT_NOT_READY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
