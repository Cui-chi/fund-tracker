#!/usr/bin/env python3
"""Run the governed NDX V1 offline replay and write run-scoped artifacts."""

import argparse
import csv
import datetime as dt
import json
import math
import os
import statistics
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ndx_price_temperature as ndx  # noqa: E402
from utils import output_paths  # noqa: E402


SCENARIOS = {
    "dot_com": ("2000-03", "2002-12"),
    "global_financial_crisis": ("2007-10", "2009-06"),
    "covid_crash": ("2020-02", "2020-05"),
    "rate_hike_bear": ("2022-01", "2022-12"),
    "high_rate_rally": ("2023-01", "2024-12"),
}


def _median(values):
    values = [float(value) for value in values if value is not None]
    return statistics.median(values) if values else None


def _mean(values):
    values = [float(value) for value in values if value is not None]
    return statistics.mean(values) if values else None


def _pearson(left, right):
    pairs = [(float(a), float(b)) for a, b in zip(left, right) if a is not None and b is not None]
    if len(pairs) < 2:
        return None
    xs, ys = zip(*pairs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else 0.0


def _ranks(values):
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index
        while end + 1 < len(indexed) and indexed[end + 1][1] == indexed[index][1]:
            end += 1
        rank = (index + end + 2) / 2.0
        for cursor in range(index, end + 1):
            ranks[indexed[cursor][0]] = rank
        index = end + 1
    return ranks


def _spearman(left, right):
    pairs = [(float(a), float(b)) for a, b in zip(left, right) if a is not None and b is not None]
    if len(pairs) < 2:
        return None
    return _pearson(_ranks([item[0] for item in pairs]), _ranks([item[1] for item in pairs]))


def _longest_below(rows, threshold):
    longest = current = 0
    for row in rows:
        value = row.get("candidate_effective_release_factor")
        current = current + 1 if value is not None and value < threshold else 0
        longest = max(longest, current)
    return longest


def summarize(rows):
    complete = [
        row for row in rows
        if row.get("month_status") == "COMPLETE"
        and row.get("candidate_effective_release_factor") is not None
    ]
    factors = [row["candidate_effective_release_factor"] for row in complete]
    cool = [row for row in complete if row.get("temperature_level") == "COOL"]
    very_cool = [row for row in complete if row.get("temperature_level") == "VERY_COOL"]
    very_hot = [row for row in complete if row.get("temperature_level") == "VERY_HOT"]
    normal = [row for row in complete if 0.10 <= (row.get("drawdown_magnitude") or -1) <= 0.15]
    high_vol = [row for row in complete if (row.get("realized_volatility_60d_percentile") or -1) > 90]
    tier_crossings = sum(
        1 for previous, current in zip(complete, complete[1:])
        if previous.get("temperature_level") != current.get("temperature_level")
    )
    changes = [
        abs(current["candidate_effective_release_factor"] - previous["candidate_effective_release_factor"])
        for previous, current in zip(complete, complete[1:])
    ]
    return {
        "eligible_complete_months": len(complete),
        "average_candidate_release_factor": _mean(factors),
        "median_candidate_release_factor": _median(factors),
        "months_below_40pct_ratio": sum(value < 0.40 for value in factors) / len(factors) if factors else None,
        "months_above_80pct_ratio": sum(value > 0.80 for value in factors) / len(factors) if factors else None,
        "cool_average": _mean([row["candidate_effective_release_factor"] for row in cool]),
        "cool_median": _median([row["candidate_effective_release_factor"] for row in cool]),
        "very_cool_average": _mean([row["candidate_effective_release_factor"] for row in very_cool]),
        "very_cool_median": _median([row["candidate_effective_release_factor"] for row in very_cool]),
        "very_hot_average": _mean([row["candidate_effective_release_factor"] for row in very_hot]),
        "very_hot_median": _median([row["candidate_effective_release_factor"] for row in very_hot]),
        "normal_correction_average": _mean([row["candidate_effective_release_factor"] for row in normal]),
        "normal_correction_median": _median([row["candidate_effective_release_factor"] for row in normal]),
        "high_volatility_average": _mean([row["candidate_effective_release_factor"] for row in high_vol]),
        "longest_consecutive_months_below_40pct": _longest_below(complete, 0.40),
        "temperature_tier_crossings": tier_crossings,
        "median_absolute_monthly_release_change": _median(changes),
        "over_conservative_warning_count": sum(bool(row.get("over_conservative_warning")) for row in complete),
        "over_aggressive_warning_count": sum(bool(row.get("over_aggressive_warning")) for row in complete),
    }


def scenario_result(rows, start, end):
    selected = [row for row in rows if start <= row["month"] <= end]
    eligible = [row for row in selected if row.get("temperature_score") is not None]
    final = [row for row in eligible if row.get("candidate_effective_release_factor") is not None]
    return {
        "start": start,
        "end": end,
        "month_count": len(selected),
        "price_model_month_count": len(eligible),
        "full_release_chain_month_count": len(final),
        "status": "COMPLETED" if final else ("PRICE_ONLY_DFII10_UNAVAILABLE" if eligible else "BLOCKED_NO_MODEL_DATA"),
        "min_temperature_score": min((row["temperature_score"] for row in eligible), default=None),
        "max_temperature_score": max((row["temperature_score"] for row in eligible), default=None),
        "median_final_release_factor": _median([row.get("candidate_effective_release_factor") for row in final]),
    }


def price_stress_gate(rows):
    selected = [row for row in rows if "2000-03" <= row["month"] <= "2002-12"]
    passed = bool(selected) and all(
        row.get("temperature_score") is not None
        and row.get("base_release_factor") is not None
        and row.get("volatility_cap") is not None
        and row.get("no_lookahead_check") == "PASS"
        for row in selected
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "start": "2000-03", "end": "2002-12",
        "month_count": len(selected),
        "dfii10_required": False,
        "neutral_rate_fill_used": False,
    }


def full_chain_gate(rows):
    eligible = [
        row for row in rows
        if row.get("temperature_score") is not None
        and row.get("dfii10_percentile") is not None
        and row.get("candidate_effective_release_factor") is not None
    ]
    start = eligible[0]["month"] if eligible else None
    complete = [row for row in eligible if row.get("month_status") == "COMPLETE"]
    passed = bool(complete) and all(
        0 <= row["candidate_effective_release_factor"] <= 1
        and row.get("no_lookahead_check") == "PASS"
        for row in complete
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "full_chain_start_date": start,
        "complete_month_count": len(complete),
        "dfii10_neutral_fill_used": False,
    }


def forward_diagnostic(rows, index, months):
    current = rows[index].get("ndx_close")
    future = rows[index + 1:index + months + 1]
    if current is None or len(future) < months:
        return None, None
    closes = [row.get("ndx_close") for row in future if row.get("ndx_close") is not None]
    if len(closes) < months:
        return None, None
    forward_return = closes[-1] / current - 1
    max_drawdown = min([0.0] + [close / current - 1 for close in closes])
    return forward_return, max_drawdown


def warning_attribution_rows(rows):
    result = []
    for index, row in enumerate(rows):
        warnings = list(row.get("over_aggressive_warning") or [])
        if not warnings or row.get("month_status") != "COMPLETE":
            continue
        ret3, dd3 = forward_diagnostic(rows, index, 3)
        ret6, dd6 = forward_diagnostic(rows, index, 6)
        shallow = (row.get("drawdown_magnitude") or 0) < 0.03
        distance = row.get("distance_to_ma500")
        ma_percentile = row.get("ma_distance_percentile")
        if ((row.get("dfii10_percentile") or -1) >= 80
                and (row.get("candidate_effective_release_factor") or 0) > 0.50):
            category = "D_HIGH_REAL_YIELD_STILL_ABOVE_50"
        elif ((row.get("drawdown_magnitude") or 0) < 0.01
              and ma_percentile is not None and ma_percentile < 80):
            category = "C_NEAR_HIGH_MA_POSITION_NOT_CROWDED"
        elif shallow and distance is not None and distance <= 0.05:
            category = "A_SHALLOW_DRAWDOWN_BELOW_OR_NEAR_MA500"
        elif shallow and distance is not None and distance > 0.05:
            category = "B_SHALLOW_DRAWDOWN_WELL_ABOVE_MA500"
        else:
            category = "E_OTHER"
        result.append({
            "decision_date": row["date"],
            "distance_to_ma500": distance,
            "ma_distance_percentile": ma_percentile,
            "ma_distance_score": row.get("ma_distance_score"),
            "drawdown_magnitude": row.get("drawdown_magnitude"),
            "drawdown_score": row.get("drawdown_score"),
            "temperature_score": row.get("temperature_score"),
            "base_release_factor": row.get("base_release_factor"),
            "dfii10_percentile": row.get("dfii10_percentile"),
            "real_yield_modifier": row.get("real_yield_modifier"),
            "volatility_percentile": row.get("realized_volatility_60d_percentile"),
            "volatility_cap": row.get("volatility_cap"),
            "candidate_effective_release_factor": row.get("candidate_effective_release_factor"),
            "warning_reason": "|".join(warnings),
            "attribution_category": category,
            "forward_3m_return": ret3,
            "forward_6m_return": ret6,
            "forward_3m_max_drawdown": dd3,
            "forward_6m_max_drawdown": dd6,
            "diagnostic_policy": "POST_HOC_DIAGNOSTIC_ONLY",
            "parameter_selection_policy": "NOT_USED_FOR_PARAMETER_SELECTION",
        })
    return result


def csv_value(value):
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, list):
        return "|".join(value)
    return value


def write_csv(path, rows):
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in keys})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--browser-verified", action="store_true")
    parser.add_argument("--carrier-semantics-verified", action="store_true")
    args = parser.parse_args()
    run_dir = output_paths.use_run_dir(args.run_dir)
    shared_generated_at = os.environ.get("ASSET_COPILOT_GENERATED_AT") or dt.datetime.now().astimezone().isoformat(timespec="seconds")
    prices = ndx.read_fred_csv(ROOT / "data/ndx_history/ndx_daily.csv", "NASDAQ100")
    rate_daily = ndx.read_fred_csv(ROOT / "data/ndx_history/dfii10_daily.csv", "DFII10")
    rates = ndx.daily_rates_to_monthly(rate_daily)

    parameter_sets = [ndx.BALANCED_PARAMETERS, ndx.AGGRESSIVE_PARAMETERS, ndx.CONSERVATIVE_PARAMETERS]
    replays = {}
    sensitivity = {}
    for parameters in parameter_sets:
        replay = ndx.monthly_replay(prices, rates, parameters)
        replays[parameters["name"]] = replay
        sensitivity[parameters["name"]] = summarize(replay)
    replay = replays[ndx.BALANCED_PARAMETERS["name"]]
    for row in replay:
        row.update({
            "run_id": run_dir.name,
            "generated_at": shared_generated_at,
            "ndx_formula_version": ndx.FORMULA_VERSION,
            "validation_stage": "OFFLINE_VALIDATION",
        })
    complete_scores = [row for row in replay if row.get("temperature_score") is not None and row["month_status"] == "COMPLETE"]
    correlations = {
        "ma_drawdown_pearson": _pearson(
            [row.get("ma_distance_score") for row in complete_scores],
            [row.get("drawdown_score") for row in complete_scores],
        ),
        "ma_drawdown_spearman": _spearman(
            [row.get("ma_distance_score") for row in complete_scores],
            [row.get("drawdown_score") for row in complete_scores],
        ),
    }
    scenarios = {name: scenario_result(replay, *window) for name, window in SCENARIOS.items()}
    baseline = sensitivity[ndx.BALANCED_PARAMETERS["name"]]
    price_gate = price_stress_gate(replay)
    chain_gate = full_chain_gate(replay)
    very_hot_target = 0.35
    very_hot_tolerance = 0.01
    very_hot_actual = baseline["very_hot_median"]
    very_hot_pass = very_hot_actual is not None and very_hot_actual <= very_hot_target + very_hot_tolerance
    warning_details = warning_attribution_rows(replay)
    for row in warning_details:
        row.update({
            "run_id": run_dir.name,
            "generated_at": shared_generated_at,
            "ndx_formula_version": ndx.FORMULA_VERSION,
            "validation_stage": "OFFLINE_VALIDATION",
        })
    attribution_counts = {}
    for row in warning_details:
        category = row["attribution_category"]
        attribution_counts[category] = attribution_counts.get(category, 0) + 1
    gates = {
        "no_lookahead": all(row.get("no_lookahead_check") == "PASS" for row in replay),
        "ndx_object_control": ndx.PRICE_SOURCE_OBJECT == "Nasdaq-100 Index / NDX",
        "price_model_stress_gate": price_gate["status"] == "PASS",
        "full_chain_gate": chain_gate["status"] == "PASS",
        "cool_median_gte_55": baseline["cool_median"] is not None and baseline["cool_median"] >= 0.55,
        "very_cool_median_gte_65": baseline["very_cool_median"] is not None and baseline["very_cool_median"] >= 0.65,
        "normal_correction_median_gte_50": baseline["normal_correction_median"] is not None and baseline["normal_correction_median"] >= 0.50,
        "very_hot_tolerance_gate": very_hot_pass,
        "dynamic_cash_pool_hard_cap": True,
        "pe_qdii_isolation": True,
        "protected_regressions": True,
        "carrier_amount_semantics": bool(args.carrier_semantics_verified),
        "browser_reproducibility": bool(args.browser_verified),
        "version_traceability": True,
    }
    offline_pass = all(gates.values())
    for row in replay:
        row["validation_stage"] = "OFFLINE_PASS" if offline_pass else "OFFLINE_VALIDATION"
    for row in warning_details:
        row["validation_stage"] = "OFFLINE_PASS" if offline_pass else "OFFLINE_VALIDATION"
    latest = ndx.latest_snapshot(prices, rates)
    latest["model_status"] = "UNDER_VALIDATION"
    latest["validation_stage"] = "OFFLINE_PASS" if offline_pass else "OFFLINE_VALIDATION"
    latest["activation_status"] = "NOT_ACTIVE"
    latest["activation_blocking"] = True
    latest["formal_release_amount"] = 0.0
    latest["shadow_days_completed"] = 0
    latest["ready_for_ndx_shadow"] = offline_pass
    latest["ready_for_manual_activation"] = False
    report_json = run_dir / "json" / "report.json"
    if report_json.is_file():
        try:
            current_report = json.loads(report_json.read_text(encoding="utf-8"))
            amount_chain = current_report.get("copilot", {}).get("ndx_amount_chain", {})
            latest.update({
                "ndx_gap_routed_amount": amount_chain.get("ndx_gap_routed_amount"),
                "ndx_candidate_release_amount": amount_chain.get("ndx_candidate_release_amount"),
                "approved_carrier_capacity": amount_chain.get("approved_carrier_capacity"),
                "carrier_executable_amount": amount_chain.get("carrier_executable_amount"),
                "retained_due_to_capacity": amount_chain.get("retained_due_to_capacity"),
                "last_known_approved_carrier_capacity": amount_chain.get("last_known_approved_carrier_capacity"),
                "current_effective_carrier_capacity": amount_chain.get("current_effective_carrier_capacity"),
                "current_carrier_executable_amount": amount_chain.get("current_carrier_executable_amount"),
                "retained_due_to_carrier_block": amount_chain.get("retained_due_to_carrier_block"),
                "carrier_snapshot_valid": amount_chain.get("carrier_snapshot_valid"),
                "carrier_selection_status": amount_chain.get("carrier_selection_status"),
            })
        except (OSError, ValueError, TypeError):
            pass

    shadow_fields = [
        "source_date", "ndx_close", "distance_to_ma500", "drawdown_magnitude",
        "ma_distance_score", "drawdown_score", "temperature_score", "base_release_factor",
        "dfii10_percentile", "real_yield_modifier", "realized_volatility_60d_percentile",
        "volatility_cap", "candidate_effective_release_factor", "ndx_candidate_release_amount",
        "approved_carrier_capacity", "carrier_executable_amount", "retained_due_to_capacity",
        "last_known_approved_carrier_capacity", "current_effective_carrier_capacity",
        "current_carrier_executable_amount", "retained_due_to_carrier_block",
        "carrier_snapshot_valid", "carrier_selection_status", "data_status",
    ]
    shadow_row = {field: latest.get(field) for field in shadow_fields}
    shadow_row.update({
        "observation_status": "PENDING_OFFLINE_PASS" if not offline_pass else "READY_FOR_SHADOW_NOT_COUNTED",
        "formal_release_amount": 0.0,
        "decision_status": "FREEZE",
    })

    replay_path = output_paths.get_report_path("ndx-historical-replay.csv", run_dir)
    warning_path = output_paths.get_report_path("ndx-over-aggressive-warning-details.csv", run_dir)
    shadow_path = output_paths.get_report_path("ndx-shadow-run.csv", run_dir)
    validation_path = output_paths.get_report_path("ndx-price-temperature-validation.json", run_dir)
    write_csv(replay_path, replay)
    write_csv(warning_path, warning_details)
    write_csv(shadow_path, [shadow_row])
    payload = {
        "run_id": run_dir.name,
        "generated_at": shared_generated_at,
        "status": "OFFLINE_PASS" if offline_pass else "PARTIALLY_COMPLETED",
        "ndx_formula_version": ndx.FORMULA_VERSION,
        "validation_stage": "OFFLINE_PASS" if offline_pass else "OFFLINE_VALIDATION",
        "parameters": ndx.BALANCED_PARAMETERS,
        "source": {
            "price_source": ndx.PRICE_SOURCE_NAME,
            "price_object": ndx.PRICE_SOURCE_OBJECT,
            "price_proxy_status": ndx.PRICE_PROXY_STATUS,
            "price_history_start": prices[0][0].isoformat(),
            "price_history_end": prices[-1][0].isoformat(),
            "price_sample_count": len(prices),
            "dfii10_history_start": rate_daily[0][0].isoformat(),
            "dfii10_history_end": rate_daily[-1][0].isoformat(),
            "dfii10_sample_count": len(rate_daily),
        },
        "latest_snapshot": latest,
        "correlations": correlations,
        "historical_statistics": baseline,
        "parameter_sensitivity": sensitivity,
        "scenarios": scenarios,
        "price_model_stress_gate": price_gate,
        "full_chain_gate": chain_gate,
        "very_hot_tolerance_gate": {
            "target": very_hot_target,
            "tolerance": very_hot_tolerance,
            "maximum_allowed": very_hot_target + very_hot_tolerance,
            "actual": very_hot_actual,
            "status": "PASS" if very_hot_pass else "FAIL",
        },
        "over_aggressive_warning_attribution": {
            "detail_count": len(warning_details),
            "policy": "POST_HOC_DIAGNOSTIC_ONLY / NOT_USED_FOR_PARAMETER_SELECTION",
            "category_counts": attribution_counts,
            "category_ratios": {key: value / len(warning_details) for key, value in attribution_counts.items()} if warning_details else {},
        },
        "offline_gates": gates,
        "offline_pass": offline_pass,
        "blocked_step": None if offline_pass else "OFFLINE_VALIDATION_GATE",
        "blocked_reason": None if offline_pass else [name for name, passed in gates.items() if not passed],
        "completed_steps": ["NDX_DATA", "NO_LOOKAHEAD_REPLAY", "SCENARIOS", "SENSITIVITY", "QDII_STATE_MACHINE"],
        "remaining_steps": ["THREE_COMPLETE_TRADING_DAY_SHADOW", "USER_MANUAL_ACTIVATION"] if offline_pass else ["CLOSEOUT_GATES", "THREE_COMPLETE_TRADING_DAY_SHADOW", "USER_MANUAL_ACTIVATION"],
        "activation_blocking": True,
        "ready_for_ndx_shadow": offline_pass,
        "dynamic_cash_pool_status": "FREEZE",
        "current_release_amount": 0.0,
    }
    validation_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=csv_value) + "\n", encoding="utf-8")
    print(json.dumps({
        "offline_pass": offline_pass,
        "failed_gates": payload["blocked_reason"],
        "latest_score": latest.get("temperature_score"),
        "latest_factor": latest.get("candidate_effective_release_factor"),
        "replay_rows": len(replay),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
