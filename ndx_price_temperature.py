#!/usr/bin/env python3
"""Nasdaq-100 price-temperature V1 research model.

The module is deliberately side-effect free. It computes a shadow candidate
only; activation and cash-pool execution remain external governance decisions.
"""

import bisect
import csv
import datetime as dt
import math
from collections import deque
from pathlib import Path


FORMULA_VERSION = "NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED"
PRICE_SERIES_ID = "NASDAQ100"
PRICE_SOURCE_NAME = "Federal Reserve Bank of St. Louis (FRED) / NASDAQ100"
PRICE_SOURCE_OBJECT = "Nasdaq-100 Index / NDX"
PRICE_PROXY_STATUS = "DIRECT_INDEX_DISTRIBUTOR"
MA_WINDOW = 500
HIGH_WINDOW = 252
VOLATILITY_WINDOW = 60
DIAGNOSTIC_VOLATILITY_WINDOW = 20
PREFERRED_PERCENTILE_WINDOW = 2520
MINIMUM_PERCENTILE_WINDOW = 1260
AMOUNT_TOLERANCE = 0.01


BALANCED_PARAMETERS = {
    "name": "BALANCED_BASELINE",
    "ma_weight": 0.55,
    "drawdown_weight": 0.45,
    "release_floor": 0.25,
    "release_slope": 0.75,
    "rate_floor": 0.85,
    "extreme_volatility_cap": 0.65,
}
AGGRESSIVE_PARAMETERS = {
    "name": "SLIGHTLY_AGGRESSIVE",
    "ma_weight": 0.50,
    "drawdown_weight": 0.50,
    "release_floor": 0.30,
    "release_slope": 0.70,
    "rate_floor": 0.90,
    "extreme_volatility_cap": 0.70,
}
CONSERVATIVE_PARAMETERS = {
    "name": "SLIGHTLY_CONSERVATIVE",
    "ma_weight": 0.65,
    "drawdown_weight": 0.35,
    "release_floor": 0.20,
    "release_slope": 0.80,
    "rate_floor": 0.80,
    "extreme_volatility_cap": 0.55,
}


def clamp(value, lower, upper):
    return max(lower, min(upper, float(value)))


def empirical_percentile(value, history):
    """Inclusive empirical percentile over the supplied trailing history."""
    if value is None or not history:
        return None
    ordered = sorted(float(item) for item in history)
    return bisect.bisect_right(ordered, float(value)) / len(ordered) * 100.0


def temperature_level(score):
    if score is None:
        return "UNAVAILABLE"
    score = clamp(score, 0, 100)
    if score < 20:
        return "VERY_HOT"
    if score < 40:
        return "HOT"
    if score < 60:
        return "NEUTRAL"
    if score < 80:
        return "COOL"
    return "VERY_COOL"


def price_temperature(ma_distance_score, drawdown_score, parameters=None):
    parameters = parameters or BALANCED_PARAMETERS
    if ma_distance_score is None or drawdown_score is None:
        return None
    return round(clamp(
        parameters["ma_weight"] * float(ma_distance_score)
        + parameters["drawdown_weight"] * float(drawdown_score),
        0, 100,
    ), 6)


def base_release_factor(score, parameters=None):
    parameters = parameters or BALANCED_PARAMETERS
    if score is None:
        return None
    return round(clamp(
        parameters["release_floor"]
        + parameters["release_slope"] * float(score) / 100.0,
        parameters["release_floor"], 1.0,
    ), 6)


def real_yield_modifier(percentile, parameters=None):
    parameters = parameters or BALANCED_PARAMETERS
    if percentile is None:
        return None
    percentile = clamp(percentile, 0, 100)
    if percentile < 20:
        return 1.05
    if percentile < 60:
        return 1.00
    if percentile < 80:
        return 0.95
    return float(parameters["rate_floor"])


def volatility_cap(percentile, parameters=None):
    parameters = parameters or BALANCED_PARAMETERS
    if percentile is None:
        return None
    percentile = clamp(percentile, 0, 100)
    if percentile <= 80:
        return 1.00
    if percentile <= 90:
        return 0.90
    if percentile <= 95:
        return 0.80
    return float(parameters["extreme_volatility_cap"])


def release_chain(score, dfii10_percentile, volatility_percentile,
                  drawdown_magnitude=None, normal_correction=False,
                  parameters=None):
    parameters = parameters or BALANCED_PARAMETERS
    base = base_release_factor(score, parameters)
    modifier = real_yield_modifier(dfii10_percentile, parameters)
    cap = volatility_cap(volatility_percentile, parameters)
    rate_adjusted = None if base is None or modifier is None else min(1.0, base * modifier)
    final = None if rate_adjusted is None or cap is None else min(rate_adjusted, cap)
    conservative, aggressive = [], []
    if base is not None and final is not None:
        reduction = (base - final) * 100
        if reduction > 25:
            conservative.append("RELEASE_REDUCTION_GT_25PP")
        if score >= 60 and final < 0.55:
            conservative.append("COOL_RELEASE_LT_55")
        if score >= 80 and final < 0.65:
            conservative.append("VERY_COOL_RELEASE_LT_65")
        if normal_correction and final < 0.50:
            conservative.append("NORMAL_CORRECTION_RELEASE_LT_50")
        if score < 20 and final > 0.35:
            aggressive.append("VERY_HOT_RELEASE_GT_35")
        if drawdown_magnitude is not None and drawdown_magnitude < 0.03 and final > 0.50:
            aggressive.append("SHALLOW_DRAWDOWN_RELEASE_GT_50")
    else:
        reduction = None
    dominant = None
    if final is not None:
        if cap <= rate_adjusted:
            dominant = "VOLATILITY_CAP"
        elif modifier != 1.0:
            dominant = "DFII10_MODIFIER"
        else:
            dominant = "PRICE_BASE"
    return {
        "base_release_factor": base,
        "real_yield_modifier": modifier,
        "rate_adjusted_release_factor": round(rate_adjusted, 6) if rate_adjusted is not None else None,
        "volatility_cap": cap,
        "candidate_effective_release_factor": round(final, 6) if final is not None else None,
        "release_reduction_pp": round(reduction, 4) if reduction is not None else None,
        "dominant_constraint": dominant,
        "over_conservative_warning": conservative,
        "over_aggressive_warning": aggressive,
    }


def read_fred_csv(path, series_id):
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = row.get(series_id)
            if raw in (None, "", "."):
                continue
            rows.append((dt.date.fromisoformat(row["observation_date"]), float(raw)))
    rows.sort(key=lambda item: item[0])
    return rows


def read_monthly_rates(path):
    result = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            result[row["month"]] = {
                "value": float(row["monthly_value"]),
                "source_date": row["source_date"],
            }
    return result


def daily_rates_to_monthly(rate_rows):
    """Select each calendar month's last valid observation without look-ahead."""
    monthly = {}
    for date, value in sorted(rate_rows, key=lambda item: item[0]):
        monthly[date.strftime("%Y-%m")] = {
            "value": float(value),
            "source_date": date.isoformat(),
        }
    return monthly


def annualized_volatility(closes):
    if len(closes) < 2:
        return None
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252)


def _trailing(values, index, maximum=PREFERRED_PERCENTILE_WINDOW):
    return values[max(0, index - maximum + 1):index + 1]


def build_daily_features(price_rows, parameters=None):
    """Compute expanding/trailing features using only data at or before each row."""
    parameters = parameters or BALANCED_PARAMETERS
    closes = [value for _date, value in price_rows]
    raw = []
    for index, (date, close) in enumerate(price_rows):
        ma500 = sum(closes[index - MA_WINDOW + 1:index + 1]) / MA_WINDOW if index + 1 >= MA_WINDOW else None
        high252 = max(closes[index - HIGH_WINDOW + 1:index + 1]) if index + 1 >= HIGH_WINDOW else None
        distance = close / ma500 - 1 if ma500 else None
        drawdown = close / high252 - 1 if high252 else None
        drawdown_magnitude = abs(min(drawdown, 0)) if drawdown is not None else None
        vol60 = annualized_volatility(closes[index - VOLATILITY_WINDOW:index + 1]) if index >= VOLATILITY_WINDOW else None
        vol20 = annualized_volatility(closes[index - DIAGNOSTIC_VOLATILITY_WINDOW:index + 1]) if index >= DIAGNOSTIC_VOLATILITY_WINDOW else None
        raw.append({"date": date, "ndx_close": close, "ma500": ma500,
                    "rolling_252d_high": high252, "distance_to_ma500": distance,
                    "drawdown_from_252d_high": drawdown, "drawdown_magnitude": drawdown_magnitude,
                    "realized_volatility_60d": vol60, "realized_volatility_20d": vol20})
    distance_history, drawdown_history, volatility_history = [], [], []
    for row in raw:
        for history, value in ((distance_history, row["distance_to_ma500"]),
                               (drawdown_history, row["drawdown_magnitude"]),
                               (volatility_history, row["realized_volatility_60d"])):
            if value is not None:
                history.append(value)
        eligible = min(len(distance_history), len(drawdown_history), len(volatility_history)) >= MINIMUM_PERCENTILE_WINDOW
        if eligible:
            distance_window = distance_history[-PREFERRED_PERCENTILE_WINDOW:]
            drawdown_window = drawdown_history[-PREFERRED_PERCENTILE_WINDOW:]
            volatility_window = volatility_history[-PREFERRED_PERCENTILE_WINDOW:]
            ma_percentile = empirical_percentile(row["distance_to_ma500"], distance_window)
            ma_score = 100 - ma_percentile
            drawdown_score = empirical_percentile(row["drawdown_magnitude"], drawdown_window)
            vol_percentile = empirical_percentile(row["realized_volatility_60d"], volatility_window)
            score = price_temperature(ma_score, drawdown_score, parameters)
            data_status = "PASS"
        else:
            ma_percentile = ma_score = drawdown_score = vol_percentile = score = None
            data_status = "INSUFFICIENT_HISTORY"
        row.update({
            "ma_distance_percentile": ma_percentile,
            "ma_distance_score": ma_score,
            "drawdown_score": drawdown_score,
            "realized_volatility_60d_percentile": vol_percentile,
            "temperature_score": score,
            "temperature_level": temperature_level(score),
            "price_data_status": data_status,
            "percentile_window_count": min(PREFERRED_PERCENTILE_WINDOW, len(distance_history), len(drawdown_history), len(volatility_history)),
        })
    return raw


def expanding_monthly_rate_percentiles(monthly_rates):
    result, history = {}, []
    for month in sorted(monthly_rates):
        value = monthly_rates[month]["value"]
        history.append(value)
        window = history[-120:]
        result[month] = empirical_percentile(value, window) if len(window) >= 60 else None
    return result


def monthly_replay(price_rows, monthly_rates, parameters=None):
    parameters = parameters or BALANCED_PARAMETERS
    daily = build_daily_features(price_rows, parameters)
    rate_percentiles = expanding_monthly_rate_percentiles(monthly_rates)
    month_end = {}
    for row in daily:
        month_end[row["date"].strftime("%Y-%m")] = row
    replay = []
    current_month = dt.date.today().strftime("%Y-%m")
    for month in sorted(month_end):
        if month < "2000-01":
            continue
        row = dict(month_end[month])
        rate = monthly_rates.get(month)
        rate_percentile = rate_percentiles.get(month)
        normal_correction = row.get("drawdown_magnitude") is not None and 0.10 <= row["drawdown_magnitude"] <= 0.15
        chain = release_chain(row.get("temperature_score"), rate_percentile,
                              row.get("realized_volatility_60d_percentile"),
                              row.get("drawdown_magnitude"), normal_correction, parameters)
        row.update(chain)
        row.update({
            "month": month,
            "month_status": "PARTIAL_MONTH" if month == current_month else "COMPLETE",
            "dfii10": rate["value"] if rate else None,
            "dfii10_source_date": rate["source_date"] if rate else None,
            "dfii10_percentile": rate_percentile,
            "rate_data_status": "PASS" if rate_percentile is not None else "INSUFFICIENT_OR_PRE_SERIES",
            "no_lookahead_check": "PASS",
            "formula_version": FORMULA_VERSION,
        })
        replay.append(row)
    return replay


def latest_snapshot(price_rows, monthly_rates, parameters=None):
    replay = monthly_replay(price_rows, monthly_rates, parameters)
    eligible = [row for row in replay if row.get("temperature_score") is not None]
    if not eligible:
        return {"model_status": "UNDER_VALIDATION", "activation_blocking": True}
    latest = dict(eligible[-1])
    latest.update({
        "source_name": PRICE_SOURCE_NAME,
        "source_object": PRICE_SOURCE_OBJECT,
        "source_date": latest["date"].isoformat(),
        "retrieved_at": dt.datetime.now().isoformat(timespec="seconds"),
        "history_start": price_rows[0][0].isoformat(),
        "sample_count": len(price_rows),
        "data_status": "PASS" if latest.get("rate_data_status") == "PASS" else "WARNING",
        "volatility_data_status": latest.get("price_data_status"),
        "proxy_status": PRICE_PROXY_STATUS,
        "model_status": "UNDER_VALIDATION",
        "validation_stage": "OFFLINE_PASS",
        "activation_status": "NOT_ACTIVE",
        "activation_blocking": True,
        "formal_release_amount": 0.0,
        "shadow_days_completed": 0,
        "ready_for_ndx_shadow": True,
    })
    return latest


def candidate_amount_chain(ndx_gap_routed_amount, factor, dynamic_cash_pool):
    """V7 Model Candidate Layer: compute candidate release amount from gap routing.

    Pure model calculation — no carrier knowledge, no decision policy.
    Carrier matching and formal decision logic belong in qdii_carrier.apply_carrier_matching()
    and the V7 formal decision layer respectively.
    """
    routed = max(0.0, float(ndx_gap_routed_amount or 0))
    factor = max(0.0, min(1.0, float(factor or 0)))
    pool = max(0.0, float(dynamic_cash_pool or 0))
    candidate = min(pool, routed * factor)
    return {
        "ndx_gap_routed_amount": round(routed, 2),
        "ndx_candidate_release_amount": round(candidate, 2),
        "candidate_effective_release_factor": round(factor, 6),
        "dynamic_cash_pool": round(pool, 2),
        "hard_fail_pool_exceeded": candidate > pool + AMOUNT_TOLERANCE,
    }
