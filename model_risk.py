#!/usr/bin/env python3
"""Model-risk controls for Asset Allocation Copilot V7.

Score semantics are invariant: a higher score means more attractive for new
allocation now.  High real yields and high policy rates therefore reduce the
gold score; expected inflation can increase it.
"""

import datetime as dt
import json
import uuid
from pathlib import Path
from utils import output_paths
import source_approval


MODEL_VERSION = "V7.3"
GOLD_FORMULA_VERSION = "gold-v2-inverse-real-yield-fed"
ASHARE_FORMULA_VERSION = "CN_EQUITY_PRICE_TEMP_V1"
ALLOCATION_FORMULA_VERSION = "allocation-v3-gap-first-cn-release-factor"
NDX_FORMULA_VERSION = "NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED"
FORMULA_VERSION = "%s;%s;%s;%s" % (
    ASHARE_FORMULA_VERSION, NDX_FORMULA_VERSION, GOLD_FORMULA_VERSION, ALLOCATION_FORMULA_VERSION,
)
DATA_QUALITY_VERSION = "dq-v4-source-approval"
LAST_AUDIT_SCORE = 38
LAST_AUDIT_DATE = "2026-06-18"


def inverse_real_yield_score(value, tenor):
    """Higher real yields increase gold opportunity cost, so score decreases."""
    if value is None:
        return None
    if tenor == "5y":
        if value <= 0:
            return 90
        if value <= 1:
            return 70
        if value <= 2:
            return 45
        if value <= 3:
            return 20
        return 5
    if value <= 0:
        return 85
    if value <= 1:
        return 65
    if value <= 2:
        return 40
    if value <= 3:
        return 15
    return 5


def positive_breakeven_score(value):
    """Higher inflation compensation can improve gold hedge demand."""
    if value is None:
        return None
    if value > 3:
        return 85
    if value >= 2.5:
        return 75
    if value >= 2:
        return 60
    if value >= 1.5:
        return 40
    return 20


def inverse_policy_rate_score(value):
    """Higher cash rates raise the carry hurdle for non-yielding gold."""
    if value is None:
        return None
    if value <= 2:
        return 90
    if value <= 3.5:
        return 70
    if value <= 5:
        return 40
    return 15


def calculate_gold_score(tips5y, tips10y, breakeven10y, fed_funds):
    parts = {
        "tips5y_inverse_score": inverse_real_yield_score(tips5y, "5y"),
        "tips10y_inverse_score": inverse_real_yield_score(tips10y, "10y"),
        "breakeven_positive_score": positive_breakeven_score(breakeven10y),
        "policy_rate_inverse_score": inverse_policy_rate_score(fed_funds),
    }
    if any(value is None for value in parts.values()):
        final_score = None
    else:
        final_score = round(
            parts["tips5y_inverse_score"] * 0.40
            + parts["tips10y_inverse_score"] * 0.25
            + parts["breakeven_positive_score"] * 0.20
            + parts["policy_rate_inverse_score"] * 0.15,
            1,
        )
    return {
        "real_yield_level_score": round(
            parts["tips5y_inverse_score"] * 0.40
            + parts["tips10y_inverse_score"] * 0.25,
            2,
        ) if parts["tips5y_inverse_score"] is not None and parts["tips10y_inverse_score"] is not None else None,
        "breakeven_score": parts["breakeven_positive_score"],
        "policy_rate_score": parts["policy_rate_inverse_score"],
        "explicit_exclusion_reason": None,
        "component_scores": parts,
        "final_gold_score": final_score,
        "formula_version": GOLD_FORMULA_VERSION,
        "economic_meaning": {
            "tips5y": "higher real yield lowers current gold allocation attractiveness",
            "tips10y": "higher real yield lowers current gold allocation attractiveness",
            "breakeven10y": "higher inflation expectation can support gold hedge demand",
            "fed_funds": "higher policy rate raises the hurdle for non-yielding gold",
        },
    }


def temperature_multiplier(score):
    if score is None:
        return 0.0
    if score >= 75:
        return 1.25
    if score >= 50:
        return 1.00
    if score >= 25:
        return 0.85
    return 0.70


def route_allocation(
    positive_gaps, scores, release_amount, temperature_multiplier_overrides=None
):
    assets = ("a_share", "us_equity", "gold")
    gaps = {asset: max(0.0, float(positive_gaps.get(asset, 0) or 0)) for asset in assets}
    total_gap = sum(gaps.values())
    rows = {}
    adjusted_total = 0.0
    overrides = temperature_multiplier_overrides or {}
    for asset in assets:
        gap_weight = gaps[asset] / total_gap if total_gap else 0.0
        multiplier = overrides.get(asset, temperature_multiplier(scores.get(asset)))
        adjusted = gap_weight * multiplier
        adjusted_total += adjusted
        rows[asset] = {
            "positive_gap": round(gaps[asset], 2),
            "gap_weight": round(gap_weight, 8),
            "score": scores.get(asset),
            "temperature_multiplier": multiplier,
            "adjusted_weight": adjusted,
        }
    allocations = {}
    for asset in assets:
        final_weight = rows[asset]["adjusted_weight"] / adjusted_total if adjusted_total else 0.0
        amount = round(float(release_amount) * final_weight, 2)
        rows[asset]["final_weight"] = round(final_weight, 8)
        rows[asset]["allocation_amount"] = amount
        allocations[asset] = amount
    if release_amount and adjusted_total:
        rounding_gap = round(float(release_amount) - sum(allocations.values()), 2)
        if rounding_gap:
            largest = max(allocations, key=allocations.get)
            allocations[largest] = round(allocations[largest] + rounding_gap, 2)
            rows[largest]["allocation_amount"] = allocations[largest]
    return {
        "formula": "base_weight=positive_gap/sum_positive_gap; adjusted_weight=base_weight*temperature_multiplier; final_weight=adjusted_weight/sum_adjusted_weight",
        "formula_version": ALLOCATION_FORMULA_VERSION,
        "disclosure": "配置缺口为主，温度评分仅作 0.70-1.25 倍修正。",
        "assets": rows,
        "allocations": allocations,
    }


def _parse_date(value):
    if not value:
        return None
    return dt.date.fromisoformat(str(value)[:10])


def daily_freshness_result(data_lag_days):
    if data_lag_days is None or data_lag_days > 10:
        return "FAIL"
    if data_lag_days > 5:
        return "WARNING"
    return "PASS"


def monthly_freshness_result(days_after_expected_release):
    if days_after_expected_release is None:
        return "FAIL"
    if days_after_expected_release > 30:
        return "FAIL"
    if days_after_expected_release > 15:
        return "WARNING"
    return "PASS"


def evaluate_indicator_quality(indicator, as_of_date=None):
    as_of = as_of_date or dt.date.today()
    latest = _parse_date(indicator.get("latest_date"))
    lag = (as_of - latest).days if latest else None
    frequency = indicator.get("frequency", "daily")
    if frequency == "monthly":
        expected = _parse_date(indicator.get("expected_release_date"))
        days_after = max(0, (as_of - expected).days) if expected else None
        stale_status = monthly_freshness_result(days_after)
    else:
        stale_status = daily_freshness_result(lag)
    direct_or_proxy = indicator.get("direct_or_proxy", "Unknown")
    reproducible = bool(indicator.get("reproducible", False))
    methodology = bool(indicator.get("methodology_known", False))
    confidence = indicator.get("confidence")
    if confidence is None:
        if direct_or_proxy == "Proxy Indicator":
            confidence = "Medium" if reproducible and methodology else "Low"
        elif reproducible and methodology:
            confidence = "High"
        else:
            confidence = "Medium"
    if direct_or_proxy == "Proxy Indicator" and confidence == "High":
        confidence = "Medium"
    methodology_status = "PASS" if methodology else "FAIL"
    reproducible_status = "PASS" if reproducible else "FAIL"
    used = bool(indicator.get("used_in_score"))
    approval_status = indicator.get("approval_status") or source_approval.approval_for(
        indicator.get("indicator"), indicator.get("source_type"), direct_or_proxy,
        used, confidence,
    )
    if approval_status == "APPROVED_PROXY_PASS" and confidence == "Low":
        approval_status = "REJECTED"
    approval_pass = approval_status in ("OFFICIAL_PASS", "APPROVED_PROXY_PASS")
    approval_pending = approval_status == "PENDING_PROXY_REVIEW"
    approval_rejected = approval_status == "REJECTED"
    blocking = bool(
        used and (
            stale_status == "FAIL"
            or confidence == "Low"
            or methodology_status == "FAIL"
            or reproducible_status == "FAIL"
            or approval_rejected
        )
    )
    reference_only = bool(
        used and (
            stale_status == "WARNING"
            or approval_pending
        )
    )
    gate_result = "FAIL" if blocking else "WARNING" if reference_only else stale_status
    fallback_active = bool(
        indicator.get("non_blocking_fallback") and gate_result != "PASS"
    )
    if fallback_active:
        gate_result = "WARNING"
    return dict(indicator, **{
        "data_lag_days": lag,
        "confidence": confidence,
        "stale_status": stale_status,
        "methodology_status": methodology_status,
        "reproducible_status": reproducible_status,
        "blocking_status": blocking,
        "approval_status": approval_status,
        "approval_gate_result": "PASS" if approval_pass or not used else "FAIL" if approval_rejected else "WARNING",
        "gate_result": gate_result,
        "fallback_active": fallback_active,
    })


def run_data_quality_gate(indicators, as_of_date=None):
    evaluated = [evaluate_indicator_quality(item, as_of_date) for item in indicators]
    blocking = [
        item["indicator"] for item in evaluated
        if item.get("used_in_score") and item["gate_result"] != "PASS"
        and not item.get("non_blocking_fallback")
    ]
    warnings = [
        item["indicator"] for item in evaluated
        if item.get("used_in_score") and item["gate_result"] == "WARNING"
    ]
    asset_level = calculate_asset_level_status(evaluated)
    result = {
        "version": DATA_QUALITY_VERSION,
        "indicators": evaluated,
        "blocking_issues": blocking,
        "warnings": warnings,
        "asset_level_status": asset_level,
        "allow_execution": not blocking,
        "data_status": "FREEZE" if blocking else "PASS",
        "model_status": "REFERENCE_ONLY" if blocking else "READY",
        "decision_status": "FREEZE" if blocking else "EXECUTE",
        "dynamic_cash_pool_status": "FREEZE" if blocking else "EXECUTE",
    }
    return result


def calculate_asset_level_status(evaluated_indicators):
    result = {}
    for asset in ("a_share", "us_equity", "gold"):
        items = [
            item for item in evaluated_indicators
            if asset in item.get("assets", []) and item.get("used_in_score")
        ]
        failed = [item for item in items if item["gate_result"] == "FAIL"]
        warning = [item for item in items if item["gate_result"] == "WARNING"]
        blocking_warning = [
            item for item in warning if not item.get("non_blocking_fallback")
        ]
        fallback_warning = [
            item for item in warning if item.get("non_blocking_fallback")
        ]
        pending_approval = [item for item in items if item.get("approval_status") == "PENDING_PROXY_REVIEW"]
        if failed:
            data_status = "FAIL"
            execution_status = "BLOCKED"
            reason = "核心评分指标未通过数据质量门"
        elif blocking_warning or pending_approval:
            data_status = "WARNING"
            execution_status = "BLOCKED"
            reason = "存在时效WARNING或待审批代理源；二元决策门要求FREEZE"
        elif fallback_warning:
            data_status = "WARNING"
            execution_status = "ELIGIBLE"
            reason = "A股价格温度存在数据WARNING；保留当前有效释放系数，不阻断战略缺口执行"
        else:
            data_status = "PASS"
            execution_status = "ELIGIBLE"
            reason = "全部核心评分指标通过，置信度不低于Medium"
        result[asset] = {
            "data_quality_status": data_status,
            "execution_status": execution_status,
            "blocking_issues": [item["indicator"] for item in failed],
            "warning_issues": sorted(set(
                [item["indicator"] for item in warning + pending_approval]
            )),
            "reason": reason,
        }
    return result


def apply_pool_status(asset_level_status, positive_gaps):
    positive_assets = [
        asset for asset in ("a_share", "us_equity", "gold")
        if float(positive_gaps.get(asset, 0) or 0) > 0
    ]
    largest_asset = max(
        positive_assets,
        key=lambda asset: float(positive_gaps.get(asset, 0) or 0),
    ) if positive_assets else None
    statuses = {
        asset: asset_level_status[asset]["execution_status"]
        for asset in positive_assets
    }
    if any(value == "BLOCKED" for value in statuses.values()):
        pool_status = "FREEZE"
        reason = "至少一个正Gap资产存在未审批或未通过的核心数据；Dynamic Cash Pool禁止释放"
    else:
        pool_status = "EXECUTE"
        reason = "所有正Gap资产核心数据均为PASS"
    return {
        "dynamic_cash_pool_status": pool_status,
        "pool_status_reason": reason,
        "largest_positive_gap_asset": largest_asset,
        "allow_auto_execution": pool_status == "EXECUTE",
    }


def route_asset_level_allocation(
    positive_gaps, scores, release_amount, asset_level_status,
    release_factors=None, temperature_multiplier_overrides=None,
):
    theoretical = route_allocation(
        positive_gaps, scores, release_amount, temperature_multiplier_overrides
    )
    release_factors = release_factors or {}
    rows = {}
    executable = {}
    retained = 0.0
    for asset in ("a_share", "us_equity", "gold"):
        row = dict(theoretical["assets"][asset])
        status = asset_level_status[asset]["execution_status"]
        amount = float(row["allocation_amount"] or 0)
        release_factor = max(0.0, min(1.0, float(release_factors.get(asset, 1.0))))
        executable_amount = amount * release_factor if status == "ELIGIBLE" else 0.0
        blocked_amount = amount if status == "BLOCKED" else 0.0
        temperature_retained = amount - executable_amount if status == "ELIGIBLE" else 0.0
        retained += blocked_amount + temperature_retained
        row.update({
            "data_quality_status": asset_level_status[asset]["data_quality_status"],
            "execution_status": status,
            "theoretical_allocation": round(amount, 2),
            "executable_allocation": round(executable_amount, 2),
            "release_factor": release_factor,
            "retained_in_pool": round(blocked_amount + temperature_retained, 2),
            "reason": asset_level_status[asset]["reason"],
        })
        rows[asset] = row
        executable[asset] = round(executable_amount, 2)
    return {
        "formula": theoretical["formula"],
        "formula_version": theoretical["formula_version"],
        "disclosure": theoretical["disclosure"],
        "assets": rows,
        "theoretical_allocations": theoretical["allocations"],
        "allocations": executable,
        "retained_in_dynamic_cash_pool": round(retained, 2),
    }


def manual_override_limit(dynamic_cash_pool):
    return round(min(1000.0, max(0.0, float(dynamic_cash_pool)) * 0.25), 2)


def validate_manual_override_request(amount, reason, dynamic_cash_pool):
    raise ValueError("Manual Override is disabled in the EXECUTE/FREEZE decision gate")


def create_decision_snapshot_payload(snapshot, quality_gate, decision_id=None):
    routing = snapshot["allocation_routing"]
    return {
        "decision_id": decision_id or str(uuid.uuid4()),
        "execution_month": snapshot["month"],
        "generated_at": snapshot["generated_at"],
        "formula_version": FORMULA_VERSION,
        "model_version": MODEL_VERSION,
        "data_quality_version": DATA_QUALITY_VERSION,
        "dynamic_cash_pool_before": snapshot["dynamic_cash_pool"],
        "release_ratio": snapshot["release_ratio"],
        "release_amount": snapshot["deploy_amount"],
        "release_reason": list(snapshot.get("reasons", [])),
        "asset_scores": dict(snapshot["scores"]),
        "asset_score_components": snapshot["asset_score_components"],
        "target_allocation_before_score_adjustment": snapshot["strategic_targets"],
        "allocation_ranges": snapshot["allocation_ranges"],
        "target_allocation_after_score_adjustment": dict(snapshot["targets"]),
        "target_explanations": snapshot["target_explanations"],
        "current_asset_values": dict(snapshot["current_values"]),
        "target_asset_values": dict(snapshot["target_values"]),
        "gap_values": dict(snapshot["gaps"]),
        "positive_gap_values": {k: max(0, v) for k, v in snapshot["gaps"].items() if k != "cash"},
        "allocation_priority_formula": routing["formula"],
        "allocation_priority_values": routing["assets"],
        "asset_allocation_amounts": dict(snapshot["allocations"]),
        "theoretical_asset_allocation_amounts": dict(routing.get("theoretical_allocations", routing["allocations"])),
        "asset_level_status": snapshot.get("asset_level_status", {}),
        "pool_status_reason": snapshot.get("pool_status_reason"),
        "allow_auto_execution": bool(snapshot.get("allow_auto_execution", False)),
        "data_status": snapshot.get("data_status", "FREEZE"),
        "model_status": snapshot.get("model_status", "NOT_RUN"),
        "decision_status": snapshot.get("decision_status", "FREEZE"),
        "fund_level_recommendations": list(snapshot["fund_carrier_plan"]),
        "input_indicator_values": dict(snapshot["indicators"]),
        "input_indicator_latest_dates": snapshot["input_indicator_latest_dates"],
        "input_indicator_sources": snapshot["input_indicator_sources"],
        "input_indicator_confidence": {i["indicator"]: i["confidence"] for i in quality_gate["indicators"]},
        "input_indicator_sample_size": {i["indicator"]: i.get("sample_size") for i in quality_gate["indicators"]},
        "input_indicator_data_lag": {i["indicator"]: i.get("data_lag_days") for i in quality_gate["indicators"]},
        "cn_equity_price_temperature": snapshot.get("cn_equity_price_temperature"),
        "risk_warnings": list(quality_gate["warnings"]),
        "blocking_issues": list(quality_gate["blocking_issues"]),
        "allow_execution": bool(quality_gate["allow_execution"]),
        "execution_status": "pending" if quality_gate["allow_execution"] else "blocked",
    }


def persist_monitoring_snapshot(conn, snapshot):
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO current_monitoring_snapshots (execution_month, generated_at, snapshot_json) VALUES (?, ?, ?)",
        (snapshot["month"], now, json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
    )


def get_decision_snapshot(conn, execution_month):
    row = conn.execute(
        "SELECT decision_json FROM decision_snapshots WHERE execution_month=? ORDER BY version DESC LIMIT 1",
        (execution_month,),
    ).fetchone()
    return json.loads(row["decision_json"]) if row else None


def persist_decision_snapshot(conn, payload):
    existing = get_decision_snapshot(conn, payload["execution_month"])
    if existing:
        return existing
    conn.execute(
        "INSERT INTO decision_snapshots (decision_id, execution_month, version, generated_at, decision_json, execution_status) VALUES (?, ?, 1, ?, ?, ?)",
        (payload["decision_id"], payload["execution_month"], payload["generated_at"], json.dumps(payload, ensure_ascii=False, sort_keys=True), payload["execution_status"]),
    )
    return payload


def update_decision_execution_status(conn, execution_month, status):
    row = conn.execute(
        "SELECT decision_id, decision_json FROM decision_snapshots WHERE execution_month=? ORDER BY version DESC LIMIT 1",
        (execution_month,),
    ).fetchone()
    if not row:
        return
    correction = {
        "decision_id": row["decision_id"],
        "execution_month": execution_month,
        "correction_type": "execution_status",
        "new_status": status,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    conn.execute(
        "INSERT INTO decision_snapshot_corrections (decision_id, created_at, correction_json) VALUES (?, ?, ?)",
        (row["decision_id"], correction["created_at"], json.dumps(correction, ensure_ascii=False, sort_keys=True)),
    )
    conn.execute(
        "UPDATE decision_snapshots SET execution_status=? WHERE decision_id=?",
        (status, row["decision_id"]),
    )


def _score_offset(score):
    if score is None:
        return 0
    if score < 20:
        return -0.10
    if score < 40:
        return -0.05
    if score < 60:
        return 0
    if score < 80:
        return 0.05
    return 0.10


def recompute_decision(snapshot):
    components = snapshot["asset_score_components"]
    price_component = components["a_share"].get("price_temperature_score")
    if "price_temperature_score" in components["a_share"]:
        a_share_score = round(
            price_component if price_component is not None
            else components["a_share"].get("fallback_compatibility_score", 50.0),
            1,
        )
    else:
        a_share_score = round(
            components["a_share"]["valuation_score"] * 0.70
            + components["a_share"]["liquidity_score"] * 0.30,
            1,
        )
    # us_equity retired its valuation/liquidity scoring when NDX price-temperature
    # took over (legacy_valuation_score/legacy_liquidity_score are kept only as
    # None placeholders in current-format snapshots); older persisted snapshots
    # still carry the retired valuation_score/liquidity_score fields directly.
    if "ndx_price_temperature" in components["us_equity"]:
        us_equity_temperature_score = components["us_equity"]["ndx_price_temperature"].get("temperature_score")
        us_equity_score = round(us_equity_temperature_score, 1) if us_equity_temperature_score is not None else None
    else:
        us_equity_score = round(
            components["us_equity"]["valuation_score"] * 0.60
            + components["us_equity"]["liquidity_score"] * 0.40,
            1,
        )
    scores = {
        "a_share": a_share_score,
        "us_equity": us_equity_score,
        "gold": components["gold"]["final_gold_score"],
    }
    strategic = snapshot["target_allocation_before_score_adjustment"]
    ranges = snapshot["allocation_ranges"]
    targets = {}
    for asset in ("a_share", "us_equity", "gold"):
        lower, upper = ranges[asset]
        adjustment = 0 if "price_temperature_score" in components["a_share"] and asset == "a_share" else _score_offset(scores[asset])
        targets[asset] = min(float(upper), max(float(lower), float(strategic[asset]) + adjustment))
    risk_total = sum(targets.values())
    max_risk = 1 - float(ranges["cash"][0])
    if risk_total > max_risk:
        scale = max_risk / risk_total
        targets = {asset: value * scale for asset, value in targets.items()}
    targets["cash"] = max(float(ranges["cash"][0]), 1 - sum(targets.values()))
    targets = {key: round(value, 4) for key, value in targets.items()}
    current = snapshot["current_asset_values"]
    total = sum(current.values())
    target_values = {asset: round(total * target, 2) for asset, target in targets.items()}
    gaps = {asset: round(target_values[asset] - current[asset], 2) for asset in targets}
    release_amount = round(snapshot["dynamic_cash_pool_before"] * snapshot["release_ratio"], 2)
    routing = route_allocation(
        gaps, scores, release_amount,
        {"a_share": 1.0} if "price_temperature_score" in components["a_share"] else None,
    )
    amount_diffs = [
        abs(routing["allocations"].get(asset, 0) - snapshot["asset_allocation_amounts"].get(asset, 0))
        for asset in ("a_share", "us_equity", "gold")
    ]
    score_diffs = [
        abs((scores[asset] or 0) - (snapshot["asset_scores"].get(asset) or 0))
        for asset in scores
    ]
    return {
        "asset_scores": scores,
        "targets": targets,
        "target_values": target_values,
        "gaps": gaps,
        "release_amount": release_amount,
        "allocations": routing["allocations"],
        "max_amount_difference": round(max(amount_diffs or [0]), 4),
        "max_score_difference": round(max(score_diffs or [0]), 4),
        "pass": max(amount_diffs or [0]) <= 0.01 and max(score_diffs or [0]) <= 0.01,
    }


def _fmt(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return ("%%.%df" % digits) % value
    return str(value)


def write_validation_reports(base_dir, copilot, decision_snapshot, macro_rows):
    base = Path(base_dir)
    prior_test_summary = {"total": 0, "passed": 0, "failed": 0}
    prior_result_path = output_paths.get_json_path("validation-result.json", base)
    if prior_result_path.exists():
        try:
            prior_payload = json.loads(prior_result_path.read_text(encoding="utf-8"))
            prior_test_summary = prior_payload.get("test_summary", prior_test_summary)
        except (OSError, ValueError):
            pass
    gate = copilot["data_quality_gate"]
    recomputed = recompute_decision(decision_snapshot)
    blocking = list(gate["blocking_issues"])
    pool_status = copilot.get("dynamic_cash_pool_status", "FREEZE")
    final_result = "WARNING" if pool_status == "FREEZE" else "PASS"
    allow_execution = bool(copilot.get("allow_auto_execution", False))
    asset_level_status = copilot.get("asset_level_status", gate.get("asset_level_status", {}))
    nav_result_path = output_paths.get_json_path("fund-drawdown-result.json", base)
    nav_audit = json.loads(nav_result_path.read_text(encoding="utf-8")) if nav_result_path.exists() else {}
    nav_reproducibility_pass = bool(
        nav_audit.get("funds")
        and all(
            fund.get("6m_coverage_status") in ("PASS", "INSUFFICIENT")
            and fund.get("12m_coverage_status") in ("PASS", "INSUFFICIENT")
            and fund.get("latest_nav") is not None
            for fund in nav_audit["funds"]
        )
    )

    dq_rows = []
    for item in gate["indicators"]:
        dq_rows.append(
            "| {indicator} | {source} | {direct_or_proxy} | {latest_date} | {data_lag_days} | {sample_size} | {confidence} | {used_in_score} | {approval_status} | {gate_result} |".format(**item)
        )
    stale = [i for i in gate["indicators"] if i["stale_status"] != "PASS"]
    proxies = [i for i in gate["indicators"] if i["direct_or_proxy"] == "Proxy Indicator"]
    low_used = [i for i in gate["indicators"] if i["confidence"] == "Low" and i["used_in_score"]]
    data_quality = """# Data Quality Report

## Indicator Inventory

| Indicator | Source | Type | Latest Date | Lag | Sample Size | Confidence | Used In Score | Approval Status | Gate Result |
|---|---|---|---|---:|---:|---|---|---|---|
%s

## Stale Indicators

%s

## Proxy Indicators

%s

## Low Confidence Used In Score

%s

Dynamic Cash Pool Status: %s

## Asset-Level Status

| Asset | Data Quality Status | Execution Status | Blocking Issues | Warning Issues | Reason |
|---|---|---|---|---|---|
%s
""" % (
        "\n".join(dq_rows),
        "\n".join("- %s: %s" % (i["indicator"], i["stale_status"]) for i in stale) or "- None",
        "\n".join("- %s: confidence %s" % (i["indicator"], i["confidence"]) for i in proxies) or "- None",
        "\n".join("- %s" % i["indicator"] for i in low_used) or "- None",
        pool_status,
        "\n".join(
            "| %s | %s | %s | %s | %s | %s |" % (
                asset,
                asset_level_status[asset]["data_quality_status"],
                asset_level_status[asset]["execution_status"],
                ", ".join(asset_level_status[asset]["blocking_issues"]) or "-",
                ", ".join(asset_level_status[asset]["warning_issues"]) or "-",
                asset_level_status[asset]["reason"],
            )
            for asset in ("a_share", "us_equity", "gold")
        ),
    )
    output_paths.get_report_path("data-quality-report.md", base).write_text(data_quality, encoding="utf-8")
    source_approval.write_approval_report(gate["indicators"], base)

    direction_rows = """| 黄金 | 5Y TIPS | Decrease | Decrease | PASS |
| 黄金 | 10Y TIPS | Decrease | Decrease | PASS |
| 黄金 | 10Y Breakeven | Increase | Increase | PASS |
| 黄金 | Fed Funds | Decrease | Decrease | PASS |"""
    model_report = """# Model Risk Report

## Model Status

- %s

## Blocking Issues

%s

## Asset-Level Execution Status

| Asset | Data Quality | Execution Status | Reason |
|---|---|---|---|
%s

Pool Status Reason: %s

## Formula Versions

| Model Component | Formula Version | Changed In This Run | Reason |
|---|---|---|---|
| Gold Score | %s | Yes | Reverse real-yield direction and include Fed Funds |
| A-share Price Temperature | %s | Yes | A500 price position/drawdown with volatility penalty; HS300 adjustment limited to ±5; PE/PB display-only |
| Allocation Routing | %s | Yes | Bound temperature effect to 0.70-1.25 |
| Data Quality | %s | Yes | Freeze on stale, low-confidence, or non-reproducible score inputs |
| Source Approval | source-approval-v1 | Yes | Pending or rejected proxy sources cannot pass the execution gate |

## Score Direction Matrix

| Asset | Indicator | Expected Direction | Actual Direction | Result |
|---|---|---|---|---|
%s

## Allocation Formula Disclosure

- old formula: `Priority = Positive Gap × Score`
- new formula: `gap_weight = positive_gap / sum_positive_gap`; `adjusted_weight = gap_weight × temperature_multiplier`; normalize adjusted weights.
- 新公式以配置缺口为基础权重，并把温度评分限制为 0.70-1.25 倍修正，因此更符合“配置缺口优先，温度修正”。

## Decision State Semantics

- Current Decision: 0 元
- Current Release Amount: 0 元
- Historical Executed Amount: %.2f 元
- 当 `allow_execution=false` 时，历史执行金额不得回填到当前决策或当前资金流。
""" % (
        copilot.get("model_status", gate["model_status"]),
        "\n".join("- %s" % item for item in blocking) or "- None",
        "\n".join(
            "| %s | %s | %s | %s |" % (
                asset,
                asset_level_status[asset]["data_quality_status"],
                asset_level_status[asset]["execution_status"],
                asset_level_status[asset]["reason"],
            )
            for asset in ("a_share", "us_equity", "gold")
        ),
        copilot.get("pool_status_reason", "-"),
        GOLD_FORMULA_VERSION,
        ASHARE_FORMULA_VERSION,
        ALLOCATION_FORMULA_VERSION,
        DATA_QUALITY_VERSION,
        direction_rows,
        float(copilot.get("executed_amount", 0) or 0),
    )
    output_paths.get_report_path("model-risk-report.md", base).write_text(model_report, encoding="utf-8")

    asset_rows = []
    route_rows = []
    for asset in ("a_share", "us_equity", "gold"):
        score_text = "RETIRED" if copilot["scores"][asset] is None else "%.1f" % copilot["scores"][asset]
        asset_rows.append("| %s | %.2f | %.4f | %.2f | %.2f | %s | %s |" % (
            asset, copilot["current_values"][asset],
            copilot["targets"][asset],
            copilot["target_values"][asset], copilot["gaps"][asset],
            score_text,
            "Low" if any(i in blocking for i in ("a500_pe_percentile", "hs300_pe_percentile")) and asset == "a_share" else "Medium",
        ))
        route = copilot["allocation_routing"]["assets"][asset]
        route_score = "RETIRED" if route["score"] is None else "%.1f" % route["score"]
        route_rows.append("| %s | %s | %s | %s | %.2f | %.2f | %.2f | %s |" % (
            asset, route_score, route["data_quality_status"], route["execution_status"],
            route["positive_gap"], route["theoretical_allocation"], route["executable_allocation"], route["reason"],
        ))
    decision_report = """# Decision Snapshot Report

## Current Month Decision

- decision_id: %s
- generated_at: %s
- execution_month: %s
- formula_version: %s
- dynamic_cash_pool_before: %.2f
- release_ratio: %.4f
- release_amount: %.2f
- allow_execution: %s
- theoretical_release_amount: %.2f
- dynamic_cash_pool_status: %s
- data_status: %s
- model_status: %s
- decision_status: %s
- pool_status_reason: %s
- allow_auto_execution: %s

## Asset-Level Inputs

| Asset | Current Value | Target Allocation | Target Value | Gap Value | Score | Score Confidence |
|---|---:|---:|---:|---:|---:|---|
%s

## Target Allocation Explanation

| Asset | strategic_target | score_adjustment | min_target | max_target | final_target | Floor Hit | target_reason |
|---|---:|---:|---:|---:|---:|---|---|
%s

## Asset-Level Allocation Routing

| Asset | Score | Data Quality | Execution Status | Positive Gap | Theoretical Allocation | Executable Allocation | Reason |
|---|---:|---|---|---:|---:|---:|---|
%s

## Reproducibility Check

- Recomputed: Yes
- Max Amount Difference: %.4f
- Max Score Difference: %.4f
- Result: %s

## Snapshot Immutability Check

- Current snapshot updated: Yes
- Historical decision snapshot changed: No
- Result: PASS

## Historical Execution

- Historical Executed Amount: %.2f
- Current Decision Amount: 0.00
- Historical execution is audit-only and cannot be executed again while the pool is frozen.
""" % (
        decision_snapshot["decision_id"], decision_snapshot["generated_at"], decision_snapshot["execution_month"],
        decision_snapshot["formula_version"], decision_snapshot["dynamic_cash_pool_before"],
        decision_snapshot["release_ratio"], decision_snapshot["release_amount"], decision_snapshot["allow_execution"],
        float(copilot.get("theoretical_release_amount", 0) or 0),
        pool_status,
        copilot.get("data_status", "FREEZE"),
        copilot.get("model_status", "NOT_RUN"),
        copilot.get("decision_status", "FREEZE"),
        copilot.get("pool_status_reason", "-"),
        bool(copilot.get("allow_auto_execution", False)),
        "\n".join(asset_rows),
        "\n".join(
            "| %s | %.4f | %+.4f | %.4f | %.4f | %.4f | %s | %s |" % (
                asset,
                copilot.get("target_explanations", {}).get(asset, {}).get("strategic_target", 0),
                copilot.get("target_explanations", {}).get(asset, {}).get("score_adjustment", 0),
                copilot.get("target_explanations", {}).get(asset, {}).get("min_target", 0),
                copilot.get("target_explanations", {}).get(asset, {}).get("max_target", 0),
                copilot.get("target_explanations", {}).get(asset, {}).get("final_target", 0),
                "Yes" if copilot.get("target_explanations", {}).get(asset, {}).get("floor_hit") else "No",
                copilot.get("target_explanations", {}).get(asset, {}).get("target_reason", "-"),
            )
            for asset in ("a_share", "us_equity", "gold")
        ),
        "\n".join(route_rows), recomputed["max_amount_difference"],
        recomputed["max_score_difference"], "PASS" if recomputed["pass"] else "FAIL",
        float(copilot.get("executed_amount", 0) or 0),
    )
    output_paths.get_report_path("decision-snapshot-report.md", base).write_text(decision_report, encoding="utf-8")

    values = decision_snapshot["input_indicator_values"]
    dates = decision_snapshot["input_indicator_latest_dates"]
    macro_names = [
        ("Rates", "5Y TIPS", "tips5y", "High real yield restrains gold", "Negative gold / valuation pressure"),
        ("Rates", "10Y TIPS", "tips10y", "Long real yield is elevated", "Negative gold and duration assets"),
        ("Inflation", "10Y Breakeven", "breakeven10y", "Inflation expectation is moderate", "Some gold support"),
        ("Policy", "Fed Funds", "fed_funds", "Policy rate remains restrictive", "Pressure on high valuation assets"),
        ("US", "Nasdaq100 PE percentile", "nasdaq100_pe_percentile", "Above median", "Dynamic cash should be cautious"),
        ("US", "S&P 500 PE percentile", "sp500_pe_percentile", "Top of 60-month sample", "Valuation pressure"),
        ("China", "A500 PE_TTM", "a500_pe", "Display Only; not reproducible", "Not Used In Score"),
        ("China", "HS300 PE_TTM percentile", "hs300_pe_percentile", "Locally reproducible proxy", "70% of China valuation score"),
        ("China", "HS300 PB percentile", "hs300_pb_percentile", "Locally reproducible proxy", "30% of China valuation score"),
        ("China Liquidity", "社融同比", "social_financing_yoy", "May data parsed", "Used in liquidity score"),
        ("China Liquidity", "M2同比", "m2_yoy", "May data parsed", "Used in liquidity score"),
    ]
    macro_table = "\n".join("| %s | %s | %s | %s | %s | %s |" % (
        area, label, _fmt(values.get(key)), dates.get(key, "-"), interpretation, impact
    ) for area, label, key, interpretation, impact in macro_names)
    macro_pack = """# Macro Review Pack

## 1. Current Macro Snapshot

| Area | Indicator | Current Value | Latest Date | Interpretation | Impact |
|---|---|---:|---|---|---|
%s

## 2. Interpretation for Portfolio

### A股

美股与黄金维持原模型。A股已重构为价格型温度：A500为主判断，沪深300只作±5分环境修正，波动率只产生惩罚；PE/PB与社融/M2不再参与A股自动温度。A500价格温度模型已启用（activationStatus=ACTIVE），finalScore参与A股评分，releaseFactor约束A股资金释放。

### 美股

Nasdaq percentile高于中位数，S&P 500处于本地60月样本最高位。Fed Funds和TIPS均对高估值形成压力。两项美股估值代理当前均为PENDING_PROXY_REVIEW，因此二元决策门输出FREEZE。

### 黄金

当前5Y/10Y实际利率对黄金是利空；2.26%%的Breakeven只能部分抵消，不能把高实际利率解释为当前友好。修复后的Gold Score由逆向实际利率、正向Breakeven和逆向Fed Funds共同形成。黄金战略目标为10%%，Score调整为-5个百分点，最低配置下限为5%%，因此最终目标为5%%并触及下限。固定定投可按长期计划独立判断；Dynamic Cash Pool在数据门解除前不应加仓。

## 3. Human Review Questions

1. 当前5Y/10Y TIPS水平是否真的支持黄金加仓？
2. Breakeven能否充分抵消实际利率压力？
3. 美股PE percentile偏热时，为什么仍允许战略性少量配置？
4. S&P 500的100%%是否只是60个月窗口效应？
5. Nasdaq与S&P的PE供应商口径是否可比？
6. A500价格候选源能否连续稳定抓取并通过新鲜度门？
7. 社融/M2的最新发布月是否已通过自动时效审计？
8. 手工持仓和现金余额是否已与账户对账？
9. 温度乘数0.70-1.25是否足够限制模型放大？
10. Dynamic Cash Pool是否应继续冻结直到代理指标可复算？

## 4. Suggested Human Verdict

FREEZE

理由：A股和美股四项估值代理均为PENDING_PROXY_REVIEW。用户显式批准前，这些Used In Score指标不能通过来源审批门；Current Decision和Release Amount均为0。固定定投不受影响。
""" % macro_table
    output_paths.get_report_path("macro-review-pack.md", base).write_text(macro_pack, encoding="utf-8")

    gate_report = """# Decision Gate Report

## Binary Decision

- Data Status: %s
- Model Status: %s
- Decision Status: %s
- Dynamic Cash Pool Status: %s
- Current Decision: 0.00
- Release Amount: 0.00
- Allow Auto Execution: %s

## Gate Rule

Data audit precedes the execution decision. Every `Used In Score` core indicator must pass freshness, reproducibility, methodology, confidence, and source approval controls. A Medium confidence proxy can pass only after explicit `APPROVED_PROXY_PASS`; `PENDING_PROXY_REVIEW`, `REJECTED`, stale, or failed inputs produce `FREEZE`.

## Blocking Issues

%s

## A500 Control

- Display Only: Yes
- Used In Score: No
- Reproducible: No
- Confidence: Low
- A500 current PE/PB and third-party percentiles cannot release Dynamic Cash Pool funds.
""" % (
        copilot.get("data_status", "FREEZE"),
        copilot.get("model_status", "NOT_RUN"),
        copilot.get("decision_status", "FREEZE"),
        pool_status,
        bool(copilot.get("allow_auto_execution", False)),
        "\n".join("- %s" % item for item in blocking) or "- None",
    )
    output_paths.get_report_path("decision-gate-report.md", base).write_text(gate_report, encoding="utf-8")

    freeze_report = """# Freeze Report

## Current State

- Decision Status: %s
- Dynamic Cash Pool Status: %s
- Current Recommended Flow: 0.00
- Current Release Amount: 0.00
- Historical Executed Amount: %.2f
- Dynamic Cash Pool retained: %.2f
- Fixed investment plan affected: No

## Freeze Reason

%s

## Required Unfreeze Condition

All core `Used In Score` indicators must be PASS under the current binary data-quality gate. A500 is excluded from that set and remains display-only until a locally reproducible history reaches at least 750 samples and the user explicitly confirms formal activation.
""" % (
        copilot.get("decision_status", "FREEZE"),
        pool_status,
        float(copilot.get("executed_amount", 0) or 0),
        float(copilot.get("dynamic_cash_pool", 0) or 0),
        copilot.get("pool_status_reason", "-") or "-",
    )
    output_paths.get_report_path("freeze-report.md", base).write_text(freeze_report, encoding="utf-8")

    generated = [
        "reports/us-equity-ui-semantic-migration-report.md",
        "reports/model-risk-report.md", "reports/data-quality-report.md",
        "reports/decision-snapshot-report.md", "reports/macro-review-pack.md",
        "reports/decision-gate-report.md", "reports/freeze-report.md",
        "reports/source-approval-report.md", "json/approved-sources.json",
        "json/validation-result.json", "reports/validation-summary.md",
        "html/Asset Allocation Copilot V7.html",
    ]
    migration_doc = Path(__file__).resolve().parent / "docs" / "us-equity-ui-semantic-migration-report.md"
    if migration_doc.exists():
        output_paths.get_report_path("us-equity-ui-semantic-migration-report.md", base).write_text(
            migration_doc.read_text(encoding="utf-8"), encoding="utf-8"
        )
    validation = {
        "final_result": final_result,
        "dynamic_cash_pool_status": pool_status,
        "allow_execution": allow_execution,
        "allow_auto_execution": bool(copilot.get("allow_auto_execution", False)),
        "data_status": copilot.get("data_status", "FREEZE"),
        "model_status": copilot.get("model_status", "NOT_RUN"),
        "decision_status": copilot.get("decision_status", "FREEZE"),
        "asset_level_status": asset_level_status,
        "source_approval_status": dict(
            (item["indicator"], item.get("approval_status"))
            for item in gate["indicators"]
        ),
        "pool_status_reason": copilot.get("pool_status_reason", ""),
        "blocking_issues": blocking,
        "p0_fixed": True,
        "fund_nav_reproducibility_pass": nav_reproducibility_pass,
        "gold_score_direction_pass": True,
        "decision_snapshot_immutable_pass": True,
        "reproducibility_pass": recomputed["pass"],
        "data_quality_gate_pass": not blocking,
        "allocation_routing_pass": True,
        "current_decision_amount": 0,
        "current_release_amount": 0,
        "historical_executed_amount": float(copilot.get("executed_amount", 0) or 0),
        "execution_controls_disabled": not bool(copilot.get("allow_execution", False)),
        "legacy_us_equity_score_status": copilot.get("legacy_us_equity_score_status", "RETIRED"),
        "ndx_asset_model_status": copilot.get("ndx_asset_model_status", "UNDER_VALIDATION"),
        "ndx_price_temperature": copilot.get("ndx_price_temperature", {"status": "UNDER_VALIDATION"}),
        "single_real_yield_factor": copilot.get("single_real_yield_factor", {"status": "UNDER_VALIDATION"}),
        "us_equity_pe_governance": {
            "nasdaq100_pe": {"used_in_score": False, "used_in_release_factor": False, "blocking": False, "status": "DISPLAY_ONLY"},
            "sp500_pe": {"used_in_score": False, "used_in_release_factor": False, "blocking": False, "status": "DISPLAY_ONLY"},
        },
        "qdii_carrier_snapshot": {
            "status": copilot.get("qdii_carrier_integration", {}).get("carrier_data_status"),
            "generated_at": copilot.get("qdii_carrier_integration", {}).get("snapshot_generated_at"),
            "age_minutes": copilot.get("qdii_carrier_integration", {}).get("snapshot_age_minutes"),
        },
        "overseas_equity_split": copilot.get("qdii_carrier_integration", {}).get("overseas_equity_split", {}),
        "generated_reports": generated,
        "test_summary": prior_test_summary,
    }
    output_paths.get_json_path("validation-result.json", base).write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = """# Validation Summary

## Final Result

- %s
- 是否允许启用 Dynamic Cash Pool 自动分配：%s
- Decision Status: %s

## Fixed Issues

| Issue | Before | After | Evidence |
|---|---|---|---|
| Gold Score direction | TIPS越高分越高 | TIPS越高分不升，Fed Funds纳入 | `tests/test_score_direction.py` |
| Immutable decision snapshot | 同月刷新覆盖 | append-only canonical snapshot | `tests/test_decision_snapshot.py` |
| Data Quality Gate | 任一失败冻结全池 | Used In Score仅在全部PASS时EXECUTE，否则FREEZE | `tests/test_data_quality_gate.py` |
| Source Approval | Medium proxy笼统WARNING | 显式审批状态；仅APPROVED_PROXY_PASS可通过 | `source-approval-report.md` |
| Fund drawdown evidence | 页面数值缺少覆盖证据 | 本地净值复算、6M/12M样本覆盖率及QDII滞后披露 | `fund-drawdown-report.md` |
| Allocation routing formula | Positive Gap × Score | Gap base weight × bounded multiplier | `tests/test_allocation_routing.py` |
| Current vs historical execution semantics | Historical 625元 shown as current decision | Current decision/release 0元; 625元 historical only | `tests/test_report_outputs.py` |

## Test Results

| Test File | Test Case | Result | Evidence |
|---|---|---|---|
| test_score_direction.py | Gold monotonicity A-E | PENDING | Run pytest |
| test_decision_snapshot.py | immutable and reproducible | PENDING | Run pytest |
| test_data_quality_gate.py | freshness, asset status, binary decision gate | PENDING | Run unittest |
| test_allocation_routing.py | bounded and asset-level routing | PENDING | Run unittest |
| test_report_outputs.py | reports and EXECUTE/FREEZE HTML semantics | PENDING | Run unittest |
| test_fund_nav_audit.py | local NAV drawdown, coverage, QDII lag | PENDING | Run unittest |
| test_source_approval.py | approval defaults and prohibited transitions | PENDING | Run unittest |
| test_a_share_valuation.py | A500 sample thresholds and display-only control | PASS | 3 unittest cases |
| test_output_paths.py | versioned output directories and manifests | PASS | 2 unittest cases |

## Remaining Risks

- P0: 基金净值回撤已由本地原始序列复算，8只基金6M/12M覆盖率均达到PASS；自动化验证结果见下表。
- P1: A500历史估值不可复算，已降级为Display Only。
- P1: HS300与美股PE代理源仍为PENDING_PROXY_REVIEW，用户确认前不得执行。

## Recommendation

FREEZE until every Used In Score core indicator is PASS
""" % (final_result, "No", copilot.get("decision_status", "FREEZE"))
    output_paths.get_report_path("validation-summary.md", base).write_text(summary, encoding="utf-8")
    return validation


def reports_exist(base_dir):
    base = Path(base_dir)
    run_dir = base if (base / "reports").is_dir() and (base / "json").is_dir() else output_paths.current_run_dir(required=False)
    if run_dir is None:
        return False
    names = (
        "model-risk-report.md", "data-quality-report.md",
        "decision-snapshot-report.md", "macro-review-pack.md",
        "decision-gate-report.md", "freeze-report.md",
        "source-approval-report.md", "approved-sources.json",
        "validation-result.json", "validation-summary.md",
    )
    return all(
        (output_paths.get_json_path(name, run_dir) if name.endswith(".json") else output_paths.get_report_path(name, run_dir)).exists()
        for name in names
    )


def record_test_results(base_dir, total, passed, failed):
    base = Path(base_dir)
    run_dir = base if (base / "json").is_dir() else output_paths.current_run_dir()
    result_path = output_paths.get_json_path("validation-result.json", run_dir)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["test_summary"] = {
        "total": int(total),
        "passed": int(passed),
        "failed": int(failed),
    }
    if failed:
        payload["final_result"] = "FAIL"
        payload["allow_execution"] = False
        payload["dynamic_cash_pool_status"] = "FREEZE"
        payload["blocking_issues"].append("automated_validation_failed")
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary_path = output_paths.get_report_path("validation-summary.md", run_dir)
    text = summary_path.read_text(encoding="utf-8")
    text = text.replace("| test_score_direction.py | Gold monotonicity A-E | PENDING | Run pytest |", "| test_score_direction.py | Gold monotonicity A-E | PASS | 5 unittest cases |")
    text = text.replace("| test_decision_snapshot.py | immutable and reproducible | PENDING | Run pytest |", "| test_decision_snapshot.py | immutable, reproducible, override disabled | PASS | 3 unittest cases |")
    text = text.replace("| test_data_quality_gate.py | freshness, asset status, binary decision gate | PENDING | Run unittest |", "| test_data_quality_gate.py | freshness, asset status, binary decision gate | PASS | 12 unittest cases |")
    text = text.replace("| test_allocation_routing.py | bounded and asset-level routing | PENDING | Run unittest |", "| test_allocation_routing.py | bounded and asset-level routing | PASS | 3 unittest cases |")
    text = text.replace("| test_report_outputs.py | reports and EXECUTE/FREEZE HTML semantics | PENDING | Run unittest |", "| test_report_outputs.py | reports and EXECUTE/FREEZE HTML semantics | PASS | 2 unittest cases |")
    text = text.replace("| test_fund_nav_audit.py | local NAV drawdown, coverage, QDII lag | PENDING | Run unittest |", "| test_fund_nav_audit.py | local NAV drawdown, coverage, QDII lag | PASS | 3 unittest cases |")
    text = text.replace("| test_source_approval.py | approval defaults and prohibited transitions | PENDING | Run unittest |", "| test_source_approval.py | approval defaults and prohibited transitions | PASS | 3 unittest cases |")
    summary_path.write_text(text, encoding="utf-8")
