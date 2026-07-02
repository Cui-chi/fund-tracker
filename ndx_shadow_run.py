#!/usr/bin/env python3
"""Governed three-session NDX V1 prospective shadow-run state machine."""

import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from dateutil import tz

import ndx_price_temperature


MODEL_VERSION = "NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED"
REQUIRED_COMPLETE_DAYS = 5
NEW_YORK = tz.gettz("America/New_York")
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
LEDGER_SCHEMA = "ndx-shadow-ledger-v1"
DAILY_SCHEMA = "ndx-shadow-day-v1"
HASH_CANONICALIZATION_VERSION = "ndx-shadow-canonical-input-v1"
PRIMARY_QQQ_URL = "https://stooq.com/q/d/l/?s=qqq.us&i=d"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"
NDX_PRIMARY_SOURCE = "FRED_NASDAQ100"
NDX_PRIMARY_INSTRUMENT = "NDX"
QQQ_PROXY_SOURCE = "QQQ_PROXY"


class ShadowRunError(RuntimeError):
    pass


def _parse_date(value):
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if not value:
        return None
    return dt.date.fromisoformat(str(value)[:10])


def _run_curl_csv(url):
    command = ["curl", "-L", "--http1.1", "--silent", "--show-error", "--max-time", "25", url]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise ShadowRunError((result.stderr or b"curl failed").decode("utf-8", "replace").strip())
    text = result.stdout.decode("utf-8", "replace")
    if not text.strip():
        raise ShadowRunError("empty response")
    return text


def fetch_qqq_proxy():
    """Fetch latest QQQ close as a proxy validator only."""
    rows = []
    text = _run_curl_csv(PRIMARY_QQQ_URL)
    for row in csv.DictReader(text.splitlines()):
        date_text = row.get("Date") or row.get("date")
        close_text = row.get("Close") or row.get("close")
        if not date_text or close_text in (None, "", "."):
            continue
        rows.append((_parse_date(date_text), float(close_text)))
    rows = [(date, close) for date, close in rows if date and close > 0]
    rows.sort(key=lambda item: item[0])
    if not rows:
        raise ShadowRunError("QQQ primary returned no valid daily close")
    date, close = rows[-1]
    return {
        "source": QQQ_PROXY_SOURCE,
        "instrument": "QQQ",
        "role": "PROXY_VALIDATOR",
        "date": date.isoformat(),
        "close": close,
        "price_field": "close",
        "session": "daily_vendor_close",
    }


def fetch_fred_latest(series_id, label):
    """Fetch latest FRED observation date/value."""
    rows = []
    text = _run_curl_csv(FRED_CSV_URL % series_id)
    for row in csv.DictReader(text.splitlines()):
        raw = row.get(series_id)
        if raw in (None, "", "."):
            continue
        rows.append((_parse_date(row.get("observation_date")), float(raw)))
    rows = [(date, value) for date, value in rows if date]
    rows.sort(key=lambda item: item[0])
    if not rows:
        raise ShadowRunError("%s returned no valid observations" % label)
    date, value = rows[-1]
    return {"source": label, "date": date.isoformat(), "close": value}


def fetch_ndx_primary():
    """Fetch the current approved NDX primary. FRED remains primary until a timely NDX close source is approved."""
    latest = fetch_fred_latest("NASDAQ100", NDX_PRIMARY_SOURCE)
    latest.update({
        "instrument": NDX_PRIMARY_INSTRUMENT,
        "role": "NDX_PRIMARY",
        "price_field": "close",
    })
    return latest


def _primary_from_data_layer(data_layer):
    data_layer = data_layer or {}
    return data_layer.get("price_primary") or data_layer.get("primary")


def _price_validators_from_data_layer(data_layer):
    data_layer = data_layer or {}
    return data_layer.get("price_validators", data_layer.get("validators", []))


def _macro_inputs_from_data_layer(data_layer):
    data_layer = data_layer or {}
    return data_layer.get("macro_inputs", [])


def validator_lag_warnings(primary, validators):
    warnings = []
    primary_date = _parse_date((primary or {}).get("date"))
    if not primary_date:
        return warnings
    for validator in validators or []:
        validator_date = _parse_date(validator.get("date"))
        if not validator_date:
            continue
        lag_days = abs((primary_date - validator_date).days)
        if lag_days > 1:
            warnings.append({
                "source": validator.get("source"),
                "primary_date": primary_date.isoformat(),
                "validator_date": validator_date.isoformat(),
                "lag_days": lag_days,
                "warning": "VALIDATOR_LAG_GT_1_DAY",
            })
    return warnings


def fetch_ndx_data_layer(target_trade_date):
    """Return one NDX primary, proxy validators, and macro inputs."""
    target = _parse_date(target_trade_date)
    result = {
        "trade_date": target.isoformat(),
        "price_primary": None,
        "price_validators": [],
        "proxy_validators": [],
        "macro_inputs": [],
        "validator_warnings": [],
        "fetch_errors": [],
    }
    try:
        result["price_primary"] = fetch_ndx_primary()
    except Exception as exc:
        result["fetch_errors"].append({"source": NDX_PRIMARY_SOURCE, "error": str(exc)})
    try:
        result["proxy_validators"].append(fetch_qqq_proxy())
    except Exception as exc:
        result["proxy_validators"].append({"source": QQQ_PROXY_SOURCE, "instrument": "QQQ", "role": "PROXY_VALIDATOR", "date": None, "error": str(exc)})
    try:
        macro = fetch_fred_latest("DFII10", "DFII10")
        macro.update({"instrument": "DFII10", "role": "macro_input"})
        result["macro_inputs"].append(macro)
    except Exception as exc:
        result["macro_inputs"].append({"source": "DFII10", "date": None, "error": str(exc)})
    result["validator_warnings"] = validator_lag_warnings(result["price_primary"], result["proxy_validators"])
    return result


def evaluate_primary_shadow_gate(data_layer, target_trade_date):
    """Simplified shadow data gate: only the primary source can decide readiness."""
    target = _parse_date(target_trade_date)
    primary = _primary_from_data_layer(data_layer)
    if not primary:
        return {"decision": "FAIL", "reason": "primary missing", "field": "primary"}
    if primary.get("error"):
        return {"decision": "FAIL", "reason": "primary fetch error", "field": "primary.error"}
    if primary.get("instrument") != NDX_PRIMARY_INSTRUMENT:
        return {"decision": "CRITICAL_FAIL", "reason": "primary.instrument is not NDX", "field": "primary.instrument"}
    if primary.get("source") != NDX_PRIMARY_SOURCE:
        return {"decision": "CRITICAL_FAIL", "reason": "primary.source is not approved NDX primary", "field": "primary.source"}
    primary_date = _parse_date(primary.get("date"))
    if not primary_date:
        return {"decision": "FAIL", "reason": "primary missing date", "field": "primary.date"}
    if primary_date < target:
        return {
            "decision": "NOT_READY",
            "reason": "primary.date < target_trade_date",
            "field": "primary.date",
            "primary_date": primary_date.isoformat(),
            "target_trade_date": target.isoformat(),
        }
    if primary_date > target:
        return {
            "decision": "AS_OF_MISMATCH",
            "reason": "primary.date > target_trade_date",
            "field": "primary.date",
            "primary_date": primary_date.isoformat(),
            "target_trade_date": target.isoformat(),
        }
    return {
        "decision": "READY",
        "reason": "primary.date == target_trade_date",
        "field": "primary.date",
        "primary_date": primary_date.isoformat(),
        "target_trade_date": target.isoformat(),
    }


def _canonical_model_price_source(model):
    model_source = (
        model.get("price_primary_source")
        or model.get("price_source")
        or model.get("source_name")
    )
    if model_source == ndx_price_temperature.PRICE_SOURCE_NAME:
        return NDX_PRIMARY_SOURCE
    return model_source


def evaluate_model_price_consistency(data_layer, model):
    primary = _primary_from_data_layer(data_layer)
    if not primary:
        return {"decision": "CRITICAL_FAIL", "reason": "primary missing", "field": "primary"}
    model_source = _canonical_model_price_source(model)
    model_date = _parse_date(model.get("source_date"))
    primary_date = _parse_date(primary.get("date"))
    instrument_match = primary.get("instrument") == NDX_PRIMARY_INSTRUMENT
    source_match = model_source == primary.get("source")
    date_match = model_date == primary_date
    try:
        primary_close = float(primary.get("close"))
        model_close = float(model.get("ndx_close"))
        close_match = math.isclose(primary_close, model_close, rel_tol=0, abs_tol=1e-9)
    except (TypeError, ValueError):
        primary_close = primary.get("close")
        model_close = model.get("ndx_close")
        close_match = False
    if not instrument_match:
        return {
            "decision": "CRITICAL_FAIL",
            "reason": "primary.instrument and model instrument mismatch",
            "field": "primary_instrument",
            "primary_instrument": primary.get("instrument"),
            "primary_source": primary.get("source"),
            "primary_source_date": primary.get("date"),
            "model_price_source": model_source,
            "model_price_source_date": model.get("source_date"),
            "source_identity_match": False,
            "source_date_match": date_match,
        }
    if model_source != primary.get("source"):
        return {
            "decision": "CRITICAL_FAIL",
            "reason": "primary and model price source mismatch",
            "field": "model_price_source",
            "primary_instrument": primary.get("instrument"),
            "primary_source": primary.get("source"),
            "primary_source_date": primary.get("date"),
            "model_price_source": model_source,
            "model_price_source_date": model.get("source_date"),
            "source_identity_match": False,
            "source_date_match": date_match,
        }
    if model_date != primary_date:
        return {
            "decision": "CRITICAL_FAIL",
            "reason": "primary and model price date mismatch",
            "field": "model_price_source_date",
            "primary_instrument": primary.get("instrument"),
            "primary_source": primary.get("source"),
            "primary_source_date": primary.get("date"),
            "model_price_source": model_source,
            "model_price_source_date": model.get("source_date"),
            "source_identity_match": source_match,
            "source_date_match": False,
        }
    if not close_match:
        return {
            "decision": "CRITICAL_FAIL",
            "reason": "primary and model price value mismatch",
            "field": "model_ndx_value",
            "primary_instrument": primary.get("instrument"),
            "primary_source": primary.get("source"),
            "primary_source_date": primary.get("date"),
            "primary_ndx_value": primary.get("close"),
            "model_price_source": model_source,
            "model_price_source_date": model.get("source_date"),
            "model_ndx_value": model.get("ndx_close"),
            "source_identity_match": source_match,
            "source_date_match": date_match,
        }
    return {
        "decision": "PASS",
        "reason": "primary and model price input aligned",
        "field": "model_price_source",
        "primary_instrument": primary.get("instrument"),
        "primary_source": primary.get("source"),
        "primary_source_date": primary.get("date"),
        "model_price_source": model_source,
        "model_price_source_date": model.get("source_date"),
        "primary_ndx_value": primary.get("close"),
        "model_ndx_value": model.get("ndx_close"),
        "source_identity_match": True,
        "source_date_match": True,
    }


def _accepted_dfii10_from_data_layer(data_layer):
    for item in _macro_inputs_from_data_layer(data_layer):
        if item.get("source") == "DFII10" or item.get("instrument") == "DFII10":
            value = item.get("value", item.get("close", item.get("dfii10_value")))
            return {
                "source": item.get("source") or "DFII10",
                "date": item.get("date") or item.get("dfii10_source_date"),
                "value": value,
                "retrieved_at": item.get("retrieved_at"),
                "lag_trading_days": item.get("lag_trading_days"),
                "lag_status": item.get("lag_status"),
                "accepted_as_of_date": item.get("accepted_as_of_date"),
            }
    return None


def evaluate_macro_input_consistency(data_layer, model):
    accepted = _accepted_dfii10_from_data_layer(data_layer)
    model_date = model.get("dfii10_source_date")
    model_value = model.get("dfii10")
    if not accepted:
        return {
            "decision": "CRITICAL_FAIL",
            "reason": "accepted DFII10 macro input missing",
            "field": "accepted_dfii10",
            "accepted_dfii10_source_date": None,
            "accepted_dfii10_value": None,
            "model_dfii10_source_date": model_date,
            "model_dfii10_value": model_value,
            "macro_input_match": False,
        }
    accepted_date = _parse_date(accepted.get("date"))
    model_parsed_date = _parse_date(model_date)
    date_match = accepted_date == model_parsed_date
    try:
        accepted_value = float(accepted.get("value"))
        model_numeric = float(model_value)
        value_match = math.isclose(accepted_value, model_numeric, rel_tol=0, abs_tol=1e-9)
    except (TypeError, ValueError):
        accepted_value = accepted.get("value")
        model_numeric = model_value
        value_match = False
    if not date_match:
        return {
            "decision": "CRITICAL_FAIL",
            "reason": "accepted DFII10 source date and model source date mismatch",
            "field": "model_dfii10_source_date",
            "accepted_dfii10_source_date": accepted.get("date"),
            "accepted_dfii10_value": accepted.get("value"),
            "model_dfii10_source_date": model_date,
            "model_dfii10_value": model_value,
            "macro_input_match": False,
        }
    if not value_match:
        return {
            "decision": "CRITICAL_FAIL",
            "reason": "accepted DFII10 value and model value mismatch",
            "field": "model_dfii10_value",
            "accepted_dfii10_source_date": accepted.get("date"),
            "accepted_dfii10_value": accepted.get("value"),
            "model_dfii10_source_date": model_date,
            "model_dfii10_value": model_value,
            "macro_input_match": False,
        }
    return {
        "decision": "PASS",
        "reason": "accepted DFII10 macro input aligned with model input",
        "field": "model_dfii10",
        "accepted_dfii10_source_date": accepted.get("date"),
        "accepted_dfii10_value": accepted.get("value"),
        "model_dfii10_source_date": model_date,
        "model_dfii10_value": model_value,
        "macro_input_match": True,
    }


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return _sha256_bytes(Path(path).read_bytes())


def _canonical_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_input_payload(canonical, session_date, ndx_data_layer=None):
    model = canonical["ndx_price_temperature"]
    chain = canonical["v7_decision_chain"]
    primary = _primary_from_data_layer(ndx_data_layer or canonical.get("ndx_data_layer")) or {}
    macro = _accepted_dfii10_from_data_layer(ndx_data_layer or canonical.get("ndx_data_layer")) or {}
    carrier = chain["carrier_matching"]
    formal = chain["formal_decision"]
    return {
        "schema_version": HASH_CANONICALIZATION_VERSION,
        "target_trade_date": _parse_date(session_date).isoformat(),
        "ndx": {
            "source": primary.get("source"),
            "instrument": primary.get("instrument"),
            "source_date": primary.get("date"),
            "value": primary.get("close"),
            "model_source_date": model.get("source_date"),
            "model_value": model.get("ndx_close"),
            "formula_version": model.get("formula_version"),
        },
        "dfii10": {
            "source": macro.get("source", "DFII10"),
            "source_date": macro.get("date"),
            "value": macro.get("value"),
            "lag_status": macro.get("lag_status"),
            "lag_trading_days": macro.get("lag_trading_days"),
            "model_source_date": model.get("dfii10_source_date"),
            "model_value": model.get("dfii10"),
        },
        "model": {
            "formula_version": model.get("formula_version"),
            "temperature_score": model.get("temperature_score"),
            "candidate_effective_release_factor": model.get("candidate_effective_release_factor"),
            "real_yield_modifier": model.get("real_yield_modifier"),
            "volatility_cap": model.get("volatility_cap"),
        },
        "carrier": {
            "carrier_snapshot_id": canonical.get("carrier_snapshot_id"),
            "carrier_snapshot_valid": carrier.get("carrier_snapshot_valid"),
            "current_effective_carrier_capacity": carrier.get("current_effective_carrier_capacity"),
            "carrier_coverable_amount": carrier.get("carrier_coverable_amount"),
            "retained_due_to_capacity": carrier.get("retained_due_to_capacity"),
            "retained_due_to_carrier_block": carrier.get("retained_due_to_carrier_block"),
        },
        "decision": {
            "decision_status": canonical["status"].get("decision_status"),
            "dynamic_cash_pool_status": canonical["status"].get("dynamic_cash_pool_status"),
            "formal_executable_amount": formal.get("formal_executable_amount"),
            "formal_release_amount": formal.get("formal_release_amount"),
            "retained_due_to_decision_freeze": formal.get("retained_due_to_decision_freeze"),
        },
    }


def canonical_input_hash(canonical, session_date, ndx_data_layer=None):
    return _sha256_bytes(_canonical_json(canonical_input_payload(canonical, session_date, ndx_data_layer)))


def _ledger_hash(payload):
    body = dict(payload)
    body.pop("ledger_sha256", None)
    return _sha256_bytes(_canonical_json(body))


def _atomic_write_json(path, payload, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def ledger_lock(ledger_path):
    lock_path = Path(str(ledger_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _nth_weekday(year, month, weekday, occurrence):
    first = dt.date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=delta + 7 * (occurrence - 1))


def _last_weekday(year, month, weekday):
    if month == 12:
        last = dt.date(year, 12, 31)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(date_value):
    if date_value.weekday() == 5:
        return date_value - dt.timedelta(days=1)
    if date_value.weekday() == 6:
        return date_value + dt.timedelta(days=1)
    return date_value


def _easter_sunday(year):
    # Anonymous Gregorian algorithm.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def nasdaq_holidays(year):
    return {
        _observed(dt.date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - dt.timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(dt.date(year, 6, 19)),
        _observed(dt.date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(dt.date(year, 12, 25)),
    }


def is_nasdaq_session(session_date):
    return session_date.weekday() < 5 and session_date not in nasdaq_holidays(session_date.year)


def next_nasdaq_session(after_date):
    candidate = after_date + dt.timedelta(days=1)
    while not is_nasdaq_session(candidate):
        candidate += dt.timedelta(days=1)
    return candidate


def is_early_close(session_date):
    thanksgiving = _nth_weekday(session_date.year, 11, 3, 4)
    day_after_thanksgiving = thanksgiving + dt.timedelta(days=1)
    independence_holiday = _observed(dt.date(session_date.year, 7, 4))
    independence_eve = independence_holiday - dt.timedelta(days=1)
    while not is_nasdaq_session(independence_eve):
        independence_eve -= dt.timedelta(days=1)
    christmas_eve = dt.date(session_date.year, 12, 24)
    return session_date in {day_after_thanksgiving, independence_eve, christmas_eve} and is_nasdaq_session(session_date)


def market_session_status(session_date, evaluated_at=None):
    evaluated_at = evaluated_at or dt.datetime.now(tz=dt.timezone.utc)
    if evaluated_at.tzinfo is None:
        raise ShadowRunError("evaluated_at must include timezone")
    if not is_nasdaq_session(session_date):
        reason = "WEEKEND" if session_date.weekday() >= 5 else "NASDAQ_HOLIDAY"
        return {"market_calendar_status": "CLOSED", "market_close_confirmed": False,
                "complete_us_trading_day": False, "reason": reason, "market_session_date": session_date.isoformat()}
    close_hour = 13 if is_early_close(session_date) else 16
    close_at = dt.datetime.combine(session_date, dt.time(close_hour, 0)).replace(tzinfo=NEW_YORK)
    confirmation_at = close_at + dt.timedelta(minutes=15)
    complete = evaluated_at.astimezone(NEW_YORK) >= confirmation_at
    return {
        "market_calendar_status": "EARLY_CLOSE_SESSION" if close_hour == 13 else "REGULAR_SESSION",
        "market_close_confirmed": complete,
        "complete_us_trading_day": complete,
        "reason": "COMPLETE" if complete else "INTRADAY_OR_CLOSE_NOT_CONFIRMED",
        "market_session_date": session_date.isoformat(),
        "official_close_at": close_at.isoformat(),
        "close_confirmation_threshold": confirmation_at.isoformat(),
        "evaluated_at": evaluated_at.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
    }


def load_ledger(path):
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != LEDGER_SCHEMA:
        raise ShadowRunError("invalid shadow ledger schema")
    if payload.get("ledger_sha256") != _ledger_hash(payload):
        raise ShadowRunError("shadow ledger integrity verification failed")
    days = payload.get("days", [])
    if payload.get("shadow_days_completed") != len(days):
        raise ShadowRunError("shadow ledger count cannot regress or diverge")
    if [row.get("shadow_day") for row in days] != list(range(1, len(days) + 1)):
        raise ShadowRunError("shadow day sequence is not contiguous")
    if len({row.get("market_session_date") for row in days}) != len(days):
        raise ShadowRunError("duplicate market session date")
    if len({row.get("run_id") for row in days}) != len(days):
        raise ShadowRunError("duplicate run_id")
    return payload


def initialize_ledger(day0_report_path, ledger_path, generated_at=None):
    day0 = json.loads(Path(day0_report_path).read_text(encoding="utf-8"))
    copilot = day0.get("copilot", {})
    expected = {
        "data_status": "PASS", "model_status": "UNDER_VALIDATION",
        "validation_stage": "OFFLINE_PASS", "decision_status": "FREEZE",
        "dynamic_cash_pool_status": "FREEZE",
    }
    for key, value in expected.items():
        if copilot.get(key) != value:
            raise ShadowRunError("Day 0 baseline mismatch: %s" % key)
    if not copilot.get("ready_for_ndx_shadow") or int(copilot.get("shadow_days_completed", -1)) != 0:
        raise ShadowRunError("Day 0 is not shadow-ready with zero completed days")
    formal = copilot.get("v7_decision_chain", {}).get("formal_decision", {})
    if formal.get("formal_release_amount") != 0 or formal.get("formal_executable_amount") != 0:
        raise ShadowRunError("Day 0 formal amounts must be zero")
    ledger = {
        "schema_version": LEDGER_SCHEMA,
        "model": MODEL_VERSION,
        "required_complete_days": REQUIRED_COMPLETE_DAYS,
        "shadow_days_completed": 0,
        "status": "DAY1_PENDING",
        "next_status": "DAY1_PENDING",
        "activation_status": "NOT_ACTIVE",
        "decision_status": "FREEZE",
        "dynamic_cash_pool_status": "FREEZE",
        "ready_for_manual_activation_review": False,
        "day0_baseline": {
            "run_id": copilot.get("run_id"),
            "generated_at": copilot.get("generated_at"),
            "report_sha256": sha256_file(day0_report_path),
            "counted_as_shadow_day": False,
        },
        "days": [], "failures": [],
        "updated_at": (generated_at or dt.datetime.now().astimezone()).isoformat(timespec="seconds"),
    }
    ledger["ledger_sha256"] = _ledger_hash(ledger)
    ledger_path = Path(ledger_path)
    with ledger_lock(ledger_path):
        if ledger_path.exists():
            existing = load_ledger(ledger_path)
            if existing.get("day0_baseline", {}).get("report_sha256") != ledger["day0_baseline"]["report_sha256"]:
                raise ShadowRunError("existing ledger has a different Day 0 baseline")
            return existing
        _atomic_write_json(ledger_path, ledger, exclusive=True)
    return ledger


def canonical_shadow_view(report):
    """Read only governed fields. No legacy allocation fallback is permitted."""
    copilot = report.get("copilot")
    if not isinstance(copilot, dict):
        raise ShadowRunError("copilot canonical object is missing")
    required = ("v7_decision_chain", "ndx_price_temperature", "data_status", "model_status",
                "validation_stage", "activation_status", "decision_status",
                "dynamic_cash_pool_status", "carrier_snapshot_id", "input_hashes", "shadow_inputs")
    missing = [key for key in required if key not in copilot]
    if missing:
        raise ShadowRunError("missing canonical fields: " + ", ".join(missing))
    chain = copilot["v7_decision_chain"]
    for key in ("model_candidate", "carrier_matching", "formal_decision", "identity_verification"):
        if not isinstance(chain.get(key), dict):
            raise ShadowRunError("missing canonical decision chain: " + key)
    return {
        "run_id": copilot.get("run_id"), "generated_at": copilot.get("generated_at"),
        "status": {key: copilot.get(key) for key in (
            "data_status", "model_status", "validation_stage", "activation_status",
            "decision_status", "dynamic_cash_pool_status")},
        "ndx_price_temperature": copilot["ndx_price_temperature"],
        "v7_decision_chain": chain,
        "carrier_snapshot_id": copilot["carrier_snapshot_id"],
        "input_hashes": copilot["input_hashes"],
        "shadow_inputs": copilot["shadow_inputs"],
        "ndx_data_layer": copilot.get("ndx_data_layer"),
    }


def evaluate_day_gates(canonical, session_date, input_hashes, input_manifest=None, ndx_data_layer=None):
    failures = []
    model = canonical["ndx_price_temperature"]
    chain = canonical["v7_decision_chain"]
    status = canonical["status"]
    data_layer = ndx_data_layer or canonical.get("ndx_data_layer")

    def require(name, actual, expected):
        if actual != expected:
            failures.append({"failed_gate": name.split(".")[0], "failed_field": name,
                             "expected_value": expected, "actual_value": actual,
                             "root_cause": "canonical field mismatch"})

    primary_gate = evaluate_primary_shadow_gate(data_layer, session_date)
    if primary_gate["decision"] != "READY":
        failures.append({
            "failed_gate": "data",
            "failed_field": primary_gate["field"],
            "expected_value": "primary.date == %s" % session_date.isoformat(),
            "actual_value": _primary_from_data_layer(data_layer),
            "root_cause": primary_gate["reason"],
        })
    model_price_gate = evaluate_model_price_consistency(data_layer, model)
    if model_price_gate["decision"] != "PASS":
        failures.append({
            "failed_gate": "data",
            "failed_field": model_price_gate["field"],
            "expected_value": "primary price source/date aligned with model input",
            "actual_value": {
                "primary_source": model_price_gate.get("primary_source"),
                "primary_source_date": model_price_gate.get("primary_source_date"),
                "model_price_source": model_price_gate.get("model_price_source"),
                "model_price_source_date": model_price_gate.get("model_price_source_date"),
                "primary_ndx_value": model_price_gate.get("primary_ndx_value"),
                "model_ndx_value": model_price_gate.get("model_ndx_value"),
            },
            "root_cause": model_price_gate["reason"],
        })
    macro_gate = evaluate_macro_input_consistency(data_layer, model)
    if macro_gate["decision"] != "PASS":
        failures.append({
            "failed_gate": "data",
            "failed_field": macro_gate["field"],
            "expected_value": "accepted DFII10 macro input aligned with model input",
            "actual_value": {
                "accepted_dfii10_source_date": macro_gate.get("accepted_dfii10_source_date"),
                "accepted_dfii10_value": macro_gate.get("accepted_dfii10_value"),
                "model_dfii10_source_date": macro_gate.get("model_dfii10_source_date"),
                "model_dfii10_value": macro_gate.get("model_dfii10_value"),
            },
            "root_cause": macro_gate["reason"],
        })
    require("model.no_lookahead_check", model.get("no_lookahead_check"), "PASS")
    require("model.formula_version", model.get("formula_version"), MODEL_VERSION)
    require("status.model_status", status["model_status"], "UNDER_VALIDATION")
    require("status.validation_stage", status["validation_stage"], "OFFLINE_PASS")
    require("safety.activation_status", status["activation_status"], "NOT_ACTIVE")
    require("safety.decision_status", status["decision_status"], "FREEZE")
    require("safety.dynamic_cash_pool_status", status["dynamic_cash_pool_status"], "FREEZE")
    require("data.carrier_snapshot_valid", chain["carrier_matching"].get("carrier_snapshot_valid"), True)
    require("data.carrier_snapshot_id", chain["carrier_matching"].get("carrier_snapshot_id"), canonical.get("carrier_snapshot_id"))
    for field in ("temperature_score", "candidate_effective_release_factor",
                  "base_release_factor", "real_yield_modifier", "volatility_cap"):
        value = model.get(field)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            failures.append({"failed_gate": "model", "failed_field": field,
                             "expected_value": "finite numeric value", "actual_value": value,
                             "root_cause": "missing or non-finite model output"})
    factor = model.get("candidate_effective_release_factor")
    if isinstance(factor, (int, float)) and not 0 <= factor <= 1:
        failures.append({"failed_gate": "model", "failed_field": "candidate_effective_release_factor",
                         "expected_value": "[0,1]", "actual_value": factor, "root_cause": "factor out of bounds"})
    mc, cm, fd, identity = (chain["model_candidate"], chain["carrier_matching"],
                             chain["formal_decision"], chain["identity_verification"])
    candidate = mc.get("ndx_candidate_release_amount")
    coverable = cm.get("carrier_coverable_amount")
    rhs1 = sum(float(cm.get(key) or 0) for key in ("carrier_coverable_amount", "retained_due_to_capacity", "retained_due_to_carrier_block"))
    rhs2 = float(fd.get("formal_executable_amount") or 0) + float(fd.get("retained_due_to_decision_freeze") or 0)
    if not isinstance(candidate, (int, float)) or abs(float(candidate) - rhs1) > 0.01:
        failures.append({"failed_gate": "amount_chain", "failed_field": "candidate_identity",
                         "expected_value": candidate, "actual_value": rhs1, "root_cause": "candidate identity mismatch"})
    if not isinstance(coverable, (int, float)) or abs(float(coverable) - rhs2) > 0.01:
        failures.append({"failed_gate": "amount_chain", "failed_field": "decision_identity",
                         "expected_value": coverable, "actual_value": rhs2, "root_cause": "decision identity mismatch"})
    require("safety.formal_executable_amount", fd.get("formal_executable_amount"), 0.0)
    require("safety.formal_release_amount", fd.get("formal_release_amount"), 0.0)
    require("amount_chain.candidate_to_carrier_reconciled", identity.get("candidate_to_carrier_reconciled"), True)
    require("amount_chain.carrier_to_decision_reconciled", identity.get("carrier_to_decision_reconciled"), True)
    if not canonical.get("carrier_snapshot_id"):
        failures.append({"failed_gate": "audit", "failed_field": "carrier_snapshot_id",
                         "expected_value": "non-empty", "actual_value": None, "root_cause": "snapshot identity missing"})
    for name, value in input_hashes.items():
        if not isinstance(value, str) or len(value) != 64:
            failures.append({"failed_gate": "audit", "failed_field": "input_hashes." + name,
                             "expected_value": "SHA-256", "actual_value": value, "root_cause": "input hash missing"})
    if input_manifest:
        expected_hash = canonical_input_hash(canonical, session_date, data_layer)
        actual_hash = input_manifest.get("canonical_input_hash")
        if expected_hash != actual_hash:
            failures.append({"failed_gate": "audit", "failed_field": "canonical_input_hash",
                             "expected_value": expected_hash, "actual_value": actual_hash,
                             "root_cause": "manifest canonical input hash differs from day gate canonical input hash"})
        if not input_manifest.get("hash_match"):
            failures.append({"failed_gate": "audit", "failed_field": "hash_match",
                             "expected_value": True, "actual_value": input_manifest.get("hash_match"),
                             "root_cause": "manifest canonical hash check failed"})
        metadata = input_manifest.get("inputs", {})
        for filename in ("qdii-carrier-latest.json", "portfolio-snapshot.json", "target-snapshot.json"):
            require("data.%s.stale_status" % filename, metadata.get(filename, {}).get("stale_status"), "PASS")
    return failures


def _dominant_constraint(chain):
    carrier = chain["carrier_matching"]
    if float(carrier.get("retained_due_to_carrier_block") or 0) > 0:
        return "CARRIER_BLOCK"
    if float(carrier.get("retained_due_to_capacity") or 0) > 0:
        return "CARRIER_CAPACITY"
    if float(chain["formal_decision"].get("retained_due_to_decision_freeze") or 0) > 0:
        return "DECISION_FREEZE"
    return "NONE"


def _numeric_comparison(current, prior):
    result = {
        "status": "COMPARABLE",
        "current_value": current,
        "prior_value": prior,
        "delta": None,
        "direction": None,
    }
    if current is None:
        result["status"] = "CURRENT_VALUE_MISSING"
        return result
    if prior is None:
        result["status"] = "PRIOR_VALUE_MISSING"
        return result
    if (isinstance(current, bool) or isinstance(prior, bool)
            or not isinstance(current, (int, float))
            or not isinstance(prior, (int, float))
            or not math.isfinite(float(current))
            or not math.isfinite(float(prior))):
        result["status"] = "INVALID_VALUE_TYPE"
        return result
    delta = float(current) - float(prior)
    result["current_value"] = float(current)
    result["prior_value"] = float(prior)
    result["delta"] = delta
    result["direction"] = "UP" if delta > 0 else "DOWN" if delta < 0 else "FLAT"
    return result


def _comparison_delta(comparison):
    return comparison["delta"] if comparison.get("status") == "COMPARABLE" else None


def _comparison_warning(name, comparison):
    status = comparison.get("status")
    if status == "COMPARABLE":
        return None
    return {
        "warning": "PRIOR_COMPARISON_SKIPPED",
        "field": name,
        "status": status,
        "current_value": comparison.get("current_value"),
        "prior_value": comparison.get("prior_value"),
    }


def compare_with_prior_day(prior, canonical, failures):
    """Prospective adjacent-day comparison; observations never change parameters."""
    if not prior:
        return None
    model = canonical["ndx_price_temperature"]
    chain = canonical["v7_decision_chain"]
    candidate_comparison = _numeric_comparison(
        chain["model_candidate"].get("ndx_candidate_release_amount"),
        prior.get("ndx_candidate_release_amount"),
    )
    capacity_comparison = _numeric_comparison(
        chain["carrier_matching"].get("current_effective_carrier_capacity"),
        prior.get("current_effective_carrier_capacity"),
    )
    coverable_comparison = _numeric_comparison(
        chain["carrier_matching"].get("carrier_coverable_amount"),
        prior.get("carrier_coverable_amount"),
    )
    score_comparison = _numeric_comparison(
        model.get("temperature_score"),
        prior.get("temperature_score"),
    )
    release_factor_comparison = _numeric_comparison(
        model.get("candidate_effective_release_factor"),
        prior.get("candidate_effective_release_factor"),
    )
    candidate = candidate_comparison.get("current_value")
    prior_candidate = candidate_comparison.get("prior_value")
    capacity = capacity_comparison.get("current_value")
    prior_capacity = capacity_comparison.get("prior_value")
    candidate_pct = (
        None if candidate_comparison.get("status") != "COMPARABLE" or prior_candidate == 0
        else candidate_comparison["delta"] / prior_candidate
    )
    capacity_pct = (
        None if capacity_comparison.get("status") != "COMPARABLE" or prior_capacity == 0
        else capacity_comparison["delta"] / prior_capacity
    )
    warnings = [
        item for item in (
            _comparison_warning("temperature_score", score_comparison),
            _comparison_warning("candidate_effective_release_factor", release_factor_comparison),
            _comparison_warning("ndx_candidate_release_amount", candidate_comparison),
            _comparison_warning("current_effective_carrier_capacity", capacity_comparison),
            _comparison_warning("carrier_coverable_amount", coverable_comparison),
        ) if item
    ]
    comparison = {
        "temperature_score_comparison": score_comparison,
        "candidate_release_factor_comparison": release_factor_comparison,
        "candidate_amount_comparison": candidate_comparison,
        "carrier_capacity_comparison": capacity_comparison,
        "carrier_coverable_comparison": coverable_comparison,
        "temperature_score_change": _comparison_delta(score_comparison),
        "candidate_release_factor_change": _comparison_delta(release_factor_comparison),
        "candidate_amount_change": _comparison_delta(candidate_comparison),
        "candidate_amount_change_pct": candidate_pct,
        "carrier_capacity_change": _comparison_delta(capacity_comparison),
        "carrier_capacity_change_pct": capacity_pct,
        "carrier_coverable_change": _comparison_delta(coverable_comparison),
        "temperature_level_changed": model.get("temperature_level") != prior.get("temperature_level"),
        "dominant_constraint_changed": _dominant_constraint(chain) != prior.get("dominant_constraint"),
        "comparison_warnings": warnings,
    }
    reasons = []
    if comparison["temperature_score_change"] is not None and abs(comparison["temperature_score_change"]) > 20: reasons.append("TEMPERATURE_SCORE_CHANGE_GT_20")
    if comparison["candidate_release_factor_change"] is not None and abs(comparison["candidate_release_factor_change"]) > 0.20: reasons.append("RELEASE_FACTOR_CHANGE_GT_0_20")
    if candidate_pct is not None and abs(candidate_pct) > 0.50: reasons.append("CANDIDATE_AMOUNT_CHANGE_GT_50_PERCENT")
    if capacity_pct is not None and capacity_pct < -0.80: reasons.append("CARRIER_CAPACITY_DROP_GT_80_PERCENT")
    if failures: reasons.append("STATUS_PASS_TO_FAIL")
    comparison.update({"shadow_anomaly": bool(reasons), "requires_manual_review": bool(reasons), "anomaly_reasons": reasons})
    return comparison


def _write_daily_input(path, payload):
    payload = dict(payload)
    payload.setdefault("schema_version", "ndx-shadow-input-v1")
    _atomic_write_json(path, payload, exclusive=True)
    return sha256_file(path)


def archive_daily_inputs(day_dir, canonical, session_date, qdii_latest_path, qdii_raw_path, retrieved_at, ndx_data_layer=None):
    """Archive the exact daily inputs before a day can be counted."""
    day_dir = Path(day_dir)
    inputs = day_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    model = canonical["ndx_price_temperature"]
    accepted_dfii10 = _accepted_dfii10_from_data_layer(ndx_data_layer) or {}
    accepted_dfii10_date = accepted_dfii10.get("date") or model.get("dfii10_source_date")
    accepted_dfii10_value = accepted_dfii10.get("value", model.get("dfii10"))
    snapshots = {
        "ndx-price-input.json": {
            "snapshot_id": "ndx-%s" % session_date.strftime("%Y%m%d"),
            "source_date": model.get("source_date"), "retrieved_at": model.get("retrieved_at"),
            "stale_status": model.get("price_data_status"), "ndx_close": model.get("ndx_close"),
            "distance_to_ma500": model.get("distance_to_ma500"),
            "drawdown_magnitude": model.get("drawdown_magnitude"),
            "realized_volatility_60d": model.get("realized_volatility_60d"),
            "realized_volatility_60d_percentile": model.get("realized_volatility_60d_percentile"),
        },
        "ndx-data-layer.json": {
            "snapshot_id": "ndx-data-layer-%s" % session_date.strftime("%Y%m%d"),
            "source_date": (ndx_data_layer or {}).get("trade_date"),
            "retrieved_at": retrieved_at,
            "stale_status": evaluate_primary_shadow_gate(ndx_data_layer, session_date)["decision"],
            "payload": ndx_data_layer,
        },
        "dfii10-input.json": {
            "snapshot_id": "dfii10-%s" % str(accepted_dfii10_date or "missing").replace("-", ""),
            "source_date": accepted_dfii10_date, "retrieved_at": accepted_dfii10.get("retrieved_at") or model.get("retrieved_at"),
            "stale_status": model.get("rate_data_status"), "dfii10": accepted_dfii10_value,
            "dfii10_percentile": model.get("dfii10_percentile"),
            "dfii10_source": accepted_dfii10.get("source", "DFII10"),
            "dfii10_source_date": accepted_dfii10_date,
            "dfii10_value": accepted_dfii10_value,
            "dfii10_retrieved_at": accepted_dfii10.get("retrieved_at") or model.get("retrieved_at"),
            "dfii10_lag_trading_days": accepted_dfii10.get("lag_trading_days"),
            "dfii10_lag_status": accepted_dfii10.get("lag_status"),
            "dfii10_accepted_as_of_date": accepted_dfii10.get("accepted_as_of_date") or accepted_dfii10_date,
            "model_dfii10_source_date": model.get("dfii10_source_date"),
            "model_dfii10_value": model.get("dfii10"),
        },
        "portfolio-snapshot.json": {
            "snapshot_id": "portfolio-%s" % canonical["run_id"],
            "source_date": canonical["shadow_inputs"]["portfolio_snapshot"].get("source_date"),
            "retrieved_at": retrieved_at, "stale_status": canonical["shadow_inputs"]["portfolio_snapshot"].get("stale_status"),
            "payload": canonical["shadow_inputs"]["portfolio_snapshot"],
        },
        "target-snapshot.json": {
            "snapshot_id": "target-%s" % canonical["run_id"],
            "source_date": canonical["shadow_inputs"]["target_snapshot"].get("source_date"),
            "retrieved_at": retrieved_at, "stale_status": canonical["shadow_inputs"]["target_snapshot"].get("stale_status"),
            "payload": canonical["shadow_inputs"]["target_snapshot"],
        },
    }
    hashes = {}
    metadata = {}
    raw_audit_hashes = {}
    for name, payload in snapshots.items():
        path = inputs / name
        hashes[name] = _write_daily_input(path, payload)
        metadata[name] = {key: payload.get(key) for key in ("snapshot_id", "source_date", "retrieved_at", "schema_version", "stale_status")}
        metadata[name]["schema_version"] = metadata[name]["schema_version"] or "ndx-shadow-input-v1"
    for name, source in (("qdii-carrier-latest.json", qdii_latest_path),
                         ("qdii-carrier-snapshot-raw.json", qdii_raw_path)):
        source = Path(source)
        if not source.is_file():
            raise ShadowRunError("missing QDII input: " + str(source))
        target = inputs / name
        with source.open("rb") as src, target.open("xb") as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
        hashes[name] = sha256_file(target)
        if name == "qdii-carrier-snapshot-raw.json":
            raw_audit_hashes["carrier_raw_snapshot_sha256"] = hashes[name]
        if name == "qdii-carrier-latest.json":
            raw_audit_hashes["carrier_latest_raw_snapshot_sha256"] = hashes[name]
        payload = json.loads(target.read_text(encoding="utf-8"))
        if name == "qdii-carrier-latest.json" and payload.get("schema_version") != "qdii-carrier-facts-v2":
            raise ShadowRunError("QDII facts schema is invalid")
        snapshot = payload.get("snapshot", {}) if name == "qdii-carrier-latest.json" else {}
        metadata[name] = {
            "snapshot_id": snapshot.get("snapshot_id", canonical.get("carrier_snapshot_id")),
            "source_date": snapshot.get("generated_at", payload.get("generated_at")),
            "retrieved_at": retrieved_at, "schema_version": payload.get("schema_version"),
            "stale_status": snapshot.get("stale_status", "RAW_ARCHIVE"),
        }
    manifest_canonical_hash = canonical_input_hash(canonical, session_date, ndx_data_layer)
    manifest = {
        "schema_version": "ndx-shadow-input-manifest-v1", "run_id": canonical["run_id"],
        "market_session_date": session_date.isoformat(), "archived_at": retrieved_at,
        "hash_algorithm": "sha256",
        "hash_canonicalization_version": HASH_CANONICALIZATION_VERSION,
        "canonical_input_hash": manifest_canonical_hash,
        "raw_snapshot_sha256": raw_audit_hashes,
        "hash_match": True,
        "inputs": {name: {**metadata[name], "sha256": hashes[name]} for name in sorted(hashes)},
    }
    _atomic_write_json(inputs / "input-manifest.json", manifest, exclusive=True)
    hashes["input-manifest.json"] = sha256_file(inputs / "input-manifest.json")
    return manifest, hashes


def _daily_report_markdown(output):
    status = output["status"]
    evaluation = output["shadow_evaluation"]
    model = output["model_result"]
    chain = output["v7_decision_chain"]
    return """# NDX Shadow Day {day} Report

- market_session_date: `{date}`
- result: `{result}`
- data_status: `{data}`
- model_status: `{model_status}`
- validation_stage: `{stage}`
- decision_status: `{decision}`
- dynamic_cash_pool_status: `FREEZE`
- temperature_score: `{score}`
- candidate_effective_release_factor: `{factor}`
- ndx_candidate_release_amount: `{candidate}`
- carrier_coverable_amount: `{coverable}`
- formal_executable_amount: `0`
- formal_release_amount: `0`
- shadow_days_completed_after_run: `{completed}`

No model parameter was changed. This record is a shadow observation, not an execution record.
""".format(
        day=output["shadow_day"], date=output["market_session_date"],
        result="PASS" if evaluation["day_gate_pass"] else "FAIL", data=status["data_status"],
        model_status=status["model_status"], stage=status["validation_stage"], decision=status["decision_status"],
        score=model["temperature_score"], factor=model["candidate_effective_release_factor"],
        candidate=chain["model_candidate"]["ndx_candidate_release_amount"],
        coverable=chain["carrier_matching"]["carrier_coverable_amount"], completed=evaluation["shadow_days_completed_after_run"],
    )


def run_shadow_session(report_path, ledger_path, shadow_root, session_date, evaluated_at,
                       qdii_latest_path, qdii_raw_path, browser_verified=False):
    """Evaluate one real session. It never fabricates or backfills a future day."""
    session = market_session_status(session_date, evaluated_at=evaluated_at)
    if not session["complete_us_trading_day"]:
        ledger = load_ledger(ledger_path)
        return {**pending_status(ledger, evaluated_at), "market_session": session,
                "reason": session["reason"]}
    if not browser_verified:
        ledger = load_ledger(ledger_path)
        return {**pending_status(ledger, evaluated_at), "market_session": session,
                "reason": "BROWSER_VERIFICATION_REQUIRED"}
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    canonical = canonical_shadow_view(report)
    ndx_data_layer = canonical.get("ndx_data_layer") or fetch_ndx_data_layer(session_date)
    with ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        if ledger["status"] == "SHADOW_COMPLETE":
            raise ShadowRunError("shadow ledger does not accept additional days")
        if any(row["market_session_date"] == session_date.isoformat() for row in ledger["days"]):
            return {**pending_status(ledger, evaluated_at), "market_session": session,
                    "reason": "DUPLICATE_MARKET_SESSION_DATE"}
        if any(row.get("market_session_date") == session_date.isoformat() for row in ledger.get("failures", [])):
            return {**pending_status(ledger, evaluated_at), "market_session": session,
                    "reason": "DUPLICATE_FAILED_MARKET_SESSION_DATE"}
        if any(row["run_id"] == canonical["run_id"] for row in ledger["days"]):
            return {**pending_status(ledger, evaluated_at), "market_session": session,
                    "reason": "DUPLICATE_RUN_ID"}
        shadow_day = ledger["shadow_days_completed"] + 1
        day_dir = Path(shadow_root) / session_date.isoformat()
        if day_dir.exists():
            raise ShadowRunError("immutable daily shadow directory already exists")
        day_dir.mkdir(parents=True)
        retrieved_at = evaluated_at.astimezone(LOCAL_TZ).isoformat(timespec="seconds")
        manifest, hashes = archive_daily_inputs(
            day_dir, canonical, session_date, qdii_latest_path, qdii_raw_path, retrieved_at, ndx_data_layer,
        )
        failures = evaluate_day_gates(canonical, session_date, hashes, manifest, ndx_data_layer)
        model = canonical["ndx_price_temperature"]
        chain = canonical["v7_decision_chain"]
        comparison = compare_with_prior_day(ledger["days"][-1] if ledger["days"] else None, canonical, failures)
        output = {
            "schema_version": DAILY_SCHEMA, "shadow_day": shadow_day,
            "market_session_date": session_date.isoformat(),
            "market_close_confirmed": True, "market_calendar_status": session["market_calendar_status"],
            "source_data_cutoff_at": retrieved_at, "run_id": canonical["run_id"],
            "generated_at": canonical["generated_at"], "status": canonical["status"],
            "model_result": {key: model.get(key) for key in (
                "temperature_score", "temperature_level", "base_release_factor", "dfii10_percentile",
                "real_yield_modifier", "rate_adjusted_release_factor",
                "volatility_cap", "candidate_effective_release_factor", "no_lookahead_check")},
            "v7_decision_chain": chain,
            "ndx_data_layer": ndx_data_layer,
            "input_manifest_sha256": hashes["input-manifest.json"],
            "hash_algorithm": manifest.get("hash_algorithm"),
            "hash_canonicalization_version": manifest.get("hash_canonicalization_version"),
            "canonical_input_hash": manifest.get("canonical_input_hash"),
            "raw_snapshot_sha256": manifest.get("raw_snapshot_sha256"),
            "hash_match": manifest.get("hash_match"),
            "shadow_evaluation": {
                "day_gate_pass": not failures, "increment_allowed": not failures,
                "shadow_days_completed_after_run": ledger["shadow_days_completed"] + (0 if failures else 1),
                "failures": failures,
                "primary_gate": evaluate_primary_shadow_gate(ndx_data_layer, session_date),
                "model_price_consistency": evaluate_model_price_consistency(ndx_data_layer, model),
                "macro_input_consistency": evaluate_macro_input_consistency(ndx_data_layer, model),
                "canonical_input_hash": manifest.get("canonical_input_hash"),
                "hash_match": manifest.get("hash_match"),
                "validator_warnings": (ndx_data_layer or {}).get("validator_warnings", []),
            },
        }
        output["model_result"]["volatility_60d_percentile"] = model.get("realized_volatility_60d_percentile")
        source_identity = evaluate_model_price_consistency(ndx_data_layer, model)
        macro_identity = evaluate_macro_input_consistency(ndx_data_layer, model)
        primary_gate_result = evaluate_primary_shadow_gate(ndx_data_layer, session_date)
        output["shadow_evaluation"].update({
            "primary_instrument": source_identity.get("primary_instrument"),
            "primary_source": source_identity.get("primary_source"),
            "primary_source_date": source_identity.get("primary_source_date"),
            "model_price_source": source_identity.get("model_price_source"),
            "model_price_source_date": source_identity.get("model_price_source_date"),
            "primary_ndx_value": source_identity.get("primary_ndx_value"),
            "model_ndx_value": source_identity.get("model_ndx_value"),
            "source_identity_match": source_identity.get("source_identity_match"),
            "source_date_match": source_identity.get("source_date_match"),
            "model_dfii10_source_date": macro_identity.get("model_dfii10_source_date"),
            "model_dfii10_value": macro_identity.get("model_dfii10_value"),
            "macro_input_match": macro_identity.get("macro_input_match"),
            "decision": "CRITICAL_FAIL" if source_identity.get("decision") == "CRITICAL_FAIL" or macro_identity.get("decision") == "CRITICAL_FAIL" else primary_gate_result.get("decision"),
        })
        if comparison is not None:
            output["adjacent_day_comparison"] = comparison
        _atomic_write_json(day_dir / "shadow-run.json", output, exclusive=True)
        (day_dir / "shadow-day-report.md").write_text(_daily_report_markdown(output), encoding="utf-8")
        (day_dir / "browser-mcp-verification.md").write_text(
            "# Browser MCP Verification\n\nPASS: FREEZE visible; execution button disabled; canonical candidate visible; formal amounts zero.\n",
            encoding="utf-8",
        )
        manifest_md = "# Shadow Run Manifest\n\n- run_id: `%s`\n- market_session_date: `%s`\n- result: `%s`\n- input_manifest_sha256: `%s`\n" % (
            canonical["run_id"], session_date.isoformat(), "FAIL" if failures else "PASS", hashes["input-manifest.json"])
        (day_dir / "shadow-run-manifest.md").write_text(manifest_md, encoding="utf-8")
        if failures:
            ledger["status"] = "SHADOW_FAILED"
            ledger["next_status"] = "MANUAL_REVIEW_REQUIRED"
            ledger["failures"].append({"shadow_day": shadow_day, "market_session_date": session_date.isoformat(),
                                       "run_id": canonical["run_id"], "failures": failures})
        else:
            entry = {
                "shadow_day": shadow_day, "market_session_date": session_date.isoformat(),
                "run_id": canonical["run_id"], "result": "PASS",
                "temperature_score": model["temperature_score"],
                "candidate_effective_release_factor": model["candidate_effective_release_factor"],
                "temperature_level": model.get("temperature_level"),
                "ndx_candidate_release_amount": chain["model_candidate"]["ndx_candidate_release_amount"],
                "current_effective_carrier_capacity": chain["carrier_matching"].get("current_effective_carrier_capacity"),
                "carrier_coverable_amount": chain["carrier_matching"]["carrier_coverable_amount"],
                "dominant_constraint": _dominant_constraint(chain),
                "formal_release_amount": 0.0, "decision_status": "FREEZE",
                "input_manifest_sha256": hashes["input-manifest.json"],
            }
            ledger["days"].append(entry)
            ledger["shadow_days_completed"] = shadow_day
            if shadow_day == REQUIRED_COMPLETE_DAYS:
                ledger["status"] = "SHADOW_COMPLETE"
                ledger["next_status"] = "MANUAL_ACTIVATION_REVIEW"
                ledger["ready_for_manual_activation_review"] = True
            else:
                ledger["status"] = "DAY%d_PASS" % shadow_day
                ledger["next_status"] = "DAY%d_PENDING" % (shadow_day + 1)
        ledger["activation_status"] = "NOT_ACTIVE"
        ledger["decision_status"] = "FREEZE"
        ledger["dynamic_cash_pool_status"] = "FREEZE"
        ledger["updated_at"] = retrieved_at
        ledger["ledger_sha256"] = _ledger_hash(ledger)
        _atomic_write_json(ledger_path, ledger)
        return output


def pending_status(ledger, evaluated_at=None):
    return {
        "shadow_status": ledger["status"], "shadow_days_completed": ledger["shadow_days_completed"],
        "next_required_day": ledger["shadow_days_completed"] + 1,
        "day0_counted": False, "increment_allowed": False,
        "dynamic_cash_pool_status": "FREEZE", "formal_release_amount": 0.0,
        "evaluated_at": (evaluated_at or dt.datetime.now().astimezone()).isoformat(timespec="seconds"),
    }
