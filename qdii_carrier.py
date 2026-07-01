#!/usr/bin/env python3
"""Read-only QDII whitelist and transparent carrier-capacity helpers.

The JSON snapshot is curated outside V7. Every fund present in that snapshot is
already approved. This module does not discover, approve, add, or buy funds.
"""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


BASE_DIR = Path(__file__).resolve().parent
RAW_SNAPSHOT_PATH = BASE_DIR.parent / "qdii-monitor" / "carrier_snapshot.json"
CARRIER_JSON_PATH = BASE_DIR / "data" / "qdii-carrier-latest.json"
FEE_LOOKUP_PATH = BASE_DIR / "data" / "qdii_fee_lookup.json"
DEFAULT_SNAPSHOT_PATH = RAW_SNAPSHOT_PATH  # canonical raw source from qdii-monitor
FACT_SCHEMA_VERSION = "qdii-carrier-facts-v2"
LOCAL_TIMEZONE = dt.timezone(dt.timedelta(hours=8))
SOFT_STALE_MINUTES = 15
HARD_STALE_MINUTES = 60
KNOWN_OFFICIAL_LIMITS = {
    "016452": {"amount": 50.0, "effective_date": "2026-06-18"},
    "021000": {"amount": 1000.0, "effective_date": "2026-06-18"},
}


def _load_fee_lookup(path=FEE_LOOKUP_PATH):
    """Load fund fee lookup table from disk. Returns {} on any failure."""
    try:
        if Path(path).is_file():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _enrich_carrier_fees(carriers):
    """Enrich carrier dicts with management/custody/service fees from lookup.

    Reads data/qdii_fee_lookup.json (scraped from eastmoney F10 pages).
    Mutates carriers in-place. Silently skips if lookup is unavailable.
    """
    lookup = _load_fee_lookup()
    if not lookup:
        return
    for c in carriers:
        code = str(c.get("code") or c.get("fund_code") or "")
        fees = lookup.get(code)
        if not fees:
            continue
        for field in ("management_fee_pct", "custody_fee_pct", "service_fee_pct"):
            if c.get(field) is None and fees.get(field) is not None:
                c[field] = fees[field]
        # purchase_fee_display: keep original if already set, else use discount
        if not c.get("purchase_fee_display") and fees.get("purchase_fee_discount_pct") is not None:
            c["purchase_fee_display"] = f"{fees['purchase_fee_discount_pct']}%"


class CarrierContractError(ValueError):
    pass


def _positive_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise CarrierContractError("generated_at is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CarrierContractError("generated_at is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed


def _iso_timestamp(value):
    parsed = _parse_timestamp(value) if isinstance(value, str) else value
    if not isinstance(parsed, dt.datetime):
        raise CarrierContractError("timestamp is invalid")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE).isoformat(timespec="seconds")


def _runtime_status(generated_at, now=None, declared_valid=True):
    generated = _parse_timestamp(generated_at)
    now = now or dt.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=LOCAL_TIMEZONE)
    age = max(0.0, (now.astimezone(generated.tzinfo) - generated).total_seconds() / 60.0)
    if not declared_valid:
        return "INVALID", "BLOCKED", age, ["carrier snapshot declares snapshot_valid=false"]
    if age > HARD_STALE_MINUTES:
        return "STALE", "BLOCKED", age, ["QDII carrier snapshot exceeds hard stale threshold"]
    if age > SOFT_STALE_MINUTES:
        return "STALE", "PARTIAL_CAPACITY", age, ["QDII carrier snapshot exceeds soft stale threshold"]
    return "ACTIVE", "AVAILABLE", age, []


def effective_limit_rmb(official_limit, observed_limit):
    values = [
        item for item in (
            _positive_number(official_limit), _positive_number(observed_limit)
        ) if item is not None
    ]
    return min(values) if values else None


def _snapshot_contract(payload):
    required = ("schema_version", "generated_at", "producer", "contract", "funds")
    missing = [key for key in required if key not in payload]
    if missing:
        raise CarrierContractError("missing snapshot fields: " + ", ".join(missing))
    if payload.get("contract", {}).get("not_investment_signal") is not True:
        raise CarrierContractError("contract.not_investment_signal must be true")
    if not isinstance(payload.get("funds"), list):
        raise CarrierContractError("funds must be a list")
    if any(not isinstance(row, dict) or not str(row.get("code", "")) for row in payload["funds"]):
        raise CarrierContractError("fund row is invalid")


def _facts_contract(payload):
    if payload.get("schema_version") != FACT_SCHEMA_VERSION:
        raise CarrierContractError("schema_version is not qdii-carrier-facts-v2")
    for key in ("snapshot", "availability", "contract", "carriers"):
        if key not in payload:
            raise CarrierContractError("missing facts field: " + key)
    snapshot = payload["snapshot"]
    availability = payload["availability"]
    contract = payload["contract"]
    if not isinstance(payload["carriers"], list):
        raise CarrierContractError("carriers must be a list")
    if not snapshot.get("snapshot_id") or not snapshot.get("generated_at"):
        raise CarrierContractError("snapshot identity is incomplete")
    _parse_timestamp(snapshot["generated_at"])
    if contract.get("not_investment_signal") is not True:
        raise CarrierContractError("contract.not_investment_signal must be true")
    if contract.get("contains_v7_decisions") is not False:
        raise CarrierContractError("facts snapshot cannot contain V7 decisions")
    if contract.get("contains_allocation_plan") is not False:
        raise CarrierContractError("facts snapshot cannot contain allocation plans")
    if availability.get("carrier_availability_status") not in ("AVAILABLE", "PARTIAL_CAPACITY", "BLOCKED"):
        raise CarrierContractError("carrier_availability_status is invalid")
    for row in payload["carriers"]:
        if not isinstance(row, dict) or not str(row.get("fund_code", "")):
            raise CarrierContractError("carrier row is invalid")


def normalize_carrier_fact_snapshot(payload, now=None):
    """Normalize facts-v2 or a one-release legacy payload without importing allocations."""
    if not isinstance(payload, dict):
        raise CarrierContractError("snapshot payload must be an object")
    if payload.get("schema_version") == FACT_SCHEMA_VERSION:
        _facts_contract(payload)
        snapshot = payload["snapshot"]
        availability = payload["availability"]
        data_status, selection_status, age, reasons = _runtime_status(
            snapshot["generated_at"], now=now,
            declared_valid=bool(snapshot.get("snapshot_valid")),
        )
        funds = []
        for item in payload["carriers"]:
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            funds.append({
                "code": str(item.get("fund_code")),
                "name": item.get("fund_name"),
                "pool": item.get("pool"),
                "benchmark": item.get("benchmark"),
                "approved": bool(item.get("approved")),
                "purchase_status": item.get("purchase_status"),
                "redemption_status": item.get("redemption_status"),
                "channel_available": item.get("available"),
                "personal_purchase_supported": item.get("personal_purchase_supported", item.get("available")),
                "observed_channel_limit_rmb": item.get("observed_channel_limit_rmb"),
                "official_fund_limit_rmb": item.get("official_fund_limit_rmb"),
                "effective_limit_rmb": item.get("effective_limit_rmb"),
                "official_limit_effective_date": item.get("official_limit_effective_date"),
                "nav_date": item.get("nav_date"), "nav": item.get("nav"),
                "estimated_nav": item.get("estimated_nav"),
                "estimated_change_pct": item.get("estimated_change_pct"),
                "tracking_error_pct": item.get("tracking_error_pct"),
                "purchase_fee_display": item.get("purchase_fee_display"),
                "management_fee_pct": item.get("management_fee_pct"),
                "custody_fee_pct": item.get("custody_fee_pct"),
                "service_fee_pct": item.get("service_fee_pct"),
                "fund_type": item.get("fund_type"), "fund_company": item.get("fund_company"),
                "fund_size_rmb": item.get("fund_size_rmb"), "inception_date": item.get("inception_date"),
                "source": source, "verification_note": item.get("verification_note"),
                "limit_volatility_flag": bool(item.get("limit_volatility_flag")),
            })
        _enrich_carrier_fees(funds)
        return {
            "schema_version": FACT_SCHEMA_VERSION,
            "generated_at": snapshot["generated_at"],
            "snapshot_id": snapshot["snapshot_id"],
            "source_confidence": snapshot.get("source_confidence", "UNAVAILABLE"),
            "carrier_data_status": data_status,
            "carrier_selection_status": selection_status,
            "carrier_availability_status": selection_status,
            "snapshot_age_minutes": round(age, 2),
            "stale_status": "PASS" if data_status == "ACTIVE" else data_status,
            "blocking_reasons": reasons,
            "last_known_approved_capacity": float(availability.get("last_known_approved_capacity", 0) or 0),
            "declared_current_effective_capacity": float(availability.get("current_effective_capacity", 0) or 0),
            "funds": funds,
            "contract": payload["contract"],
        }

    # One-release compatibility: legacy raw monitor payload. Selection, plans,
    # allocated amounts and recommendations are deliberately not copied.
    if "funds" in payload:
        _snapshot_contract(payload)
        generated_at = _iso_timestamp(payload["generated_at"])
        data_status, selection_status, age, reasons = _runtime_status(generated_at, now=now)
        return {
            "schema_version": payload.get("schema_version"),
            "generated_at": generated_at,
            "snapshot_id": "qdii-" + _parse_timestamp(generated_at).strftime("%Y%m%d-%H%M%S"),
            "source_confidence": "MIXED",
            "carrier_data_status": data_status,
            "carrier_selection_status": selection_status,
            "carrier_availability_status": selection_status,
            "snapshot_age_minutes": round(age, 2),
            "stale_status": "PASS" if data_status == "ACTIVE" else data_status,
            "blocking_reasons": reasons,
            "funds": [dict(row) for row in payload["funds"]],
            "recent_changes": payload.get("recent_changes", []),
            "contract": {"not_investment_signal": True},
        }

    # Compatibility with the prior flat curated artifact. Only capacity/status
    # and carrier facts are accepted; any embedded legacy allocation is ignored.
    if isinstance(payload.get("carriers"), list):
        generated_at = _iso_timestamp(payload.get("generated_at"))
        declared = bool(payload.get("snapshot_valid", True))
        data_status, selection_status, age, reasons = _runtime_status(generated_at, now=now, declared_valid=declared)
        funds = []
        for item in payload["carriers"]:
            funds.append({
                "code": str(item.get("fund_code")), "name": item.get("fund_name"),
                "pool": item.get("pool", "NDX_INDEX_QDII_POOL"),
                "benchmark": item.get("benchmark", "NASDAQ_100"),
                "observed_channel_limit_rmb": item.get("observed_channel_limit_rmb", item.get("channel_limit")),
                "official_fund_limit_rmb": item.get("official_fund_limit_rmb", item.get("official_limit")),
                "effective_limit_rmb": item.get("effective_limit_rmb", item.get("current_limit")),
                "purchase_status": item.get("purchase_status", "开放申购"),
                "source": item.get("source") if isinstance(item.get("source"), dict) else {
                    "name": item.get("source"), "observed_at": item.get("source_observed_at")
                },
            })
        return {
            "schema_version": payload.get("schema_version"), "generated_at": generated_at,
            "snapshot_id": payload.get("snapshot_id") or "qdii-" + _parse_timestamp(generated_at).strftime("%Y%m%d-%H%M%S"),
            "source_confidence": payload.get("source_confidence", "MIXED"),
            "carrier_data_status": data_status, "carrier_selection_status": selection_status,
            "carrier_availability_status": selection_status, "snapshot_age_minutes": round(age, 2),
            "stale_status": "PASS" if data_status == "ACTIVE" else data_status,
            "blocking_reasons": reasons,
            "last_known_approved_capacity": float(payload.get("last_known_approved_capacity", 0) or 0),
            "funds": funds, "contract": {"not_investment_signal": True},
        }
    raise CarrierContractError("unsupported carrier snapshot schema")


def read_snapshot(path=DEFAULT_SNAPSHOT_PATH, now=None):
    path = Path(path)
    if not path.exists():
        return {"carrier_data_status": "UNAVAILABLE", "carrier_selection_status": "BLOCKED",
                "blocking_reasons": ["QDII carrier snapshot is unavailable"], "funds": [],
                "snapshot_path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        normalized = normalize_carrier_fact_snapshot(payload, now=now)
    except (OSError, json.JSONDecodeError, CarrierContractError) as exc:
        return {"carrier_data_status": "INVALID", "carrier_selection_status": "BLOCKED",
                "blocking_reasons": [str(exc)], "funds": [], "snapshot_path": str(path)}
    return {**normalized,
            "soft_stale_after_minutes": SOFT_STALE_MINUTES,
            "hard_stale_after_minutes": HARD_STALE_MINUTES,
            "snapshot_path": str(path)}


def _limit_volatility_codes(snapshot):
    values, counts = {}, {}
    for change in snapshot.get("recent_changes", []):
        code = str(change.get("code", ""))
        counts[code] = counts.get(code, 0) + 1
        for key in ("old_limit", "new_limit"):
            value = _positive_number(change.get(key))
            if value is not None:
                values.setdefault(code, set()).add(value)
    return {code for code, observed in values.items() if counts.get(code, 0) >= 2 and len(observed) > 1}


def _share_class(row):
    name = str(row.get("name", "")).upper()
    if name.endswith("I") or str(row.get("code")) == "021000":
        return "I"
    if name.endswith("C"):
        return "C"
    return "A"


def _holding_map(config):
    return {str(row.get("code")): float(row.get("holding_amount", 0) or 0)
            for row in (config or {}).get("funds", [])}


def whitelist_carriers(snapshot, config=None):
    """Normalize the external, manually curated whitelist without adding states."""
    volatile = _limit_volatility_codes(snapshot)
    holdings = _holding_map(config)
    result = []
    for raw in snapshot.get("funds", []):
        code = str(raw.get("code", ""))
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        known = KNOWN_OFFICIAL_LIMITS.get(code, {})
        official = _positive_number(known.get("amount", raw.get("official_fund_limit_rmb")))
        observed = _positive_number(raw.get("observed_channel_limit_rmb"))
        effective = effective_limit_rmb(official, observed)
        share_class = _share_class(raw)
        channel_available = effective is not None and effective > 0 and raw.get("purchase_status") != "暂停申购"
        result.append({
            **raw, "fund_code": code, "fund_name": raw.get("name") or code,
            "approved": True, "approved_by": "manual_review",
            "approval_note": "JSON whitelist entry; approval occurs outside V7",
            "share_class": share_class,
            "purchase_channels": raw.get("purchase_channels") or (
                ["指定商家APP（人工核验）"] if code == "021000" else []
            ),
            "channel_available": channel_available,
            "personal_purchase_supported": bool(channel_available),
            "official_fund_limit_rmb": official,
            "official_limit_effective_date": known.get("effective_date") or raw.get("official_limit_effective_date"),
            "observed_channel_limit_rmb": observed, "effective_limit_rmb": effective,
            "current_holding_amount": holdings.get(code, 0.0),
            "current_holding": holdings.get(code, 0.0) > 0,
            "limit_volatility_flag": code in volatile,
            "source_name": source.get("name"), "source_type": source.get("type"),
            "source_confidence": source.get("confidence"),
            "last_updated": source.get("observed_at") or snapshot.get("generated_at"),
            "management_fee_pct": raw.get("management_fee_pct"),
            "custody_fee_pct": raw.get("custody_fee_pct"),
            "service_fee_pct": raw.get("service_fee_pct"),
            "fund_size_rmb": raw.get("fund_size_rmb"),
            "inception_date": raw.get("inception_date"),
            "ndx_pool_eligible": raw.get("benchmark") == "NASDAQ_100" and code != "270023",
            "dynamic_release_eligible": raw.get("benchmark") == "NASDAQ_100" and code != "270023",
        })
    _enrich_carrier_fees(result)
    return result


# Compatibility alias: semantics are whitelist-only; no registry is consulted.
def merged_carriers(snapshot, registry=None, config=None):
    return whitelist_carriers(snapshot, config=config)


def _fee_number(value):
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def _rank_key(row, remaining):
    limit = float(row.get("effective_limit_rmb") or 0)
    tracking = _positive_number(row.get("tracking_error_pct"))
    fee = _fee_number(row.get("purchase_fee_display"))
    return (
        0 if row.get("current_holding") else 1,
        0 if limit >= remaining and remaining > 0 else 1,
        tracking if tracking is not None else 999999,
        fee if fee is not None else 999999,
        1 if row.get("limit_volatility_flag") else 0,
        -limit,
        row["fund_code"],
    )


def transparent_tags(carriers, asset_amount=0):
    ndx = [row for row in carriers if row.get("ndx_pool_eligible")]
    max_limit = max((float(row.get("effective_limit_rmb") or 0) for row in ndx), default=0)
    tracking_values = [float(row["tracking_error_pct"]) for row in ndx if _positive_number(row.get("tracking_error_pct")) is not None]
    min_tracking = min(tracking_values) if tracking_values else None
    output = {}
    for row in ndx:
        advantages, risks = [], []
        limit = float(row.get("effective_limit_rmb") or 0)
        if row.get("current_holding"): advantages.append("已有持仓")
        if limit == max_limit and max_limit > 0: advantages.append("额度最高")
        if asset_amount > 0 and limit >= asset_amount: advantages.append("单只可覆盖")
        if min_tracking is not None and row.get("tracking_error_pct") == min_tracking: advantages.append("跟踪误差最低")
        mgmt = row.get("management_fee_pct")
        cust = row.get("custody_fee_pct")
        svc = row.get("service_fee_pct")
        if all(v not in (None, "", "--") for v in (mgmt, cust, svc)):
            total = (mgmt or 0) + (cust or 0) + (svc or 0)
            advantages.append(f"综合费率 {total:.2f}%")
        elif row.get("purchase_fee_display") not in (None, "", "--"):
            advantages.append(f"申购 {row['purchase_fee_display']}")
        else:
            risks.append("费率待补齐")
        if not row.get("limit_volatility_flag"): advantages.append("额度稳定")
        else: risks.append("额度波动")
        if row.get("channel_available"): advantages.append("渠道便利")
        if row.get("fund_size_rmb") is None: risks.append("规模待补齐")
        if row.get("source_type") == "SECONDARY_CHANNEL_OBSERVATION": risks.append("数据来自二级来源")
        output[row["fund_code"]] = {"advantages": advantages, "risks": risks}
    return output


def select_carriers(asset_allocated_amount, snapshot=None, config=None):
    amount = round(float(asset_allocated_amount or 0), 2)
    if amount < 0:
        raise ValueError("asset_allocated_amount不能小于0")
    snapshot = snapshot or read_snapshot()
    carriers = whitelist_carriers(snapshot, config=config)
    ndx = [row for row in carriers if row.get("ndx_pool_eligible") and row.get("personal_purchase_supported")]
    remaining, plan, candidates = amount, [], list(ndx)
    while candidates:
        candidates.sort(key=lambda row: _rank_key(row, remaining))
        row = candidates.pop(0)
        capacity = float(row.get("effective_limit_rmb") or 0)
        planned = min(remaining, capacity)
        if planned > 0:
            plan.append({"fund_code": row["fund_code"], "fund_name": row["fund_name"],
                         "asset_class": "us_equity", "asset_name": "纳指指数型QDII",
                         "planned_amount": round(planned, 2), "carrier_capacity": capacity,
                         "selection_reason": "已有持仓优先；单只覆盖优先；再比较跟踪误差、费率、额度稳定性和渠道"})
            remaining = round(remaining - planned, 2)
        if remaining <= 0:
            break
    total_capacity = round(sum(float(row.get("effective_limit_rmb") or 0) for row in ndx), 2)
    held_capacity = round(sum(float(row.get("effective_limit_rmb") or 0) for row in ndx if row.get("current_holding")), 2)
    snapshot_available = (
        snapshot.get("carrier_data_status") == "ACTIVE"
        and snapshot.get("carrier_selection_status") == "AVAILABLE"
    )
    capacity_status = (
        "BLOCKED" if not snapshot_available
        else "AVAILABLE" if total_capacity >= amount
        else "PARTIAL_CAPACITY"
    )
    tags = transparent_tags(carriers, amount)
    for row in carriers:
        row["transparent_tags"] = tags.get(row["fund_code"], {"advantages": [], "risks": []})
    return {
        "asset_allocated_amount": amount, "approved_carrier_count": len(ndx),
        "approved_total_capacity": total_capacity, "current_holding_carrier_capacity": held_capacity,
        "carrier_capacity_status": capacity_status, "remaining_unallocated_amount": max(0, remaining),
        "allocated_amount": round(amount - max(0, remaining), 2), "carrier_plan": plan,
        "recommended_carrier": next((row for row in carriers if plan and row["fund_code"] == plan[0]["fund_code"]), None),
        "alternative_carriers": [], "all_carriers": carriers,
        "ndx_carriers": [row for row in carriers if row.get("ndx_pool_eligible")],
        "blocking_reasons": snapshot.get("blocking_reasons", []),
        "warnings": ["019441当前额度较高，但近期在50元与10000元之间反复切换，执行前请再次确认渠道实际限额。"] if any(row.get("limit_volatility_flag") for row in ndx) else [],
    }


def calculate_multi_select(selected_allocations, asset_amount, carriers,
                           selected_codes=None, snapshot_valid=True,
                           carrier_selection_status="AVAILABLE",
                           tolerance=0.01):
    """Return a strict, auditable QDII carrier-capacity preview.

    ``VALID`` requires the test amount, assigned amount and effective covered
    amount to match within ``tolerance``; every selected row must remain within
    its effective limit; unselected rows cannot carry a non-zero allocation;
    and the carrier snapshot must itself be valid.
    """
    amount = max(0.0, float(asset_amount or 0))
    allocations = {
        str(code): max(0.0, float(value or 0))
        for code, value in (selected_allocations or {}).items()
    }
    selected = set(str(code) for code in (
        allocations.keys() if selected_codes is None else selected_codes
    ))
    by_code = {
        row["fund_code"]: row
        for row in carriers if row.get("ndx_pool_eligible")
    }
    rows, assigned, covered, capacity = [], 0.0, 0.0, 0.0
    row_over_limit = False
    unselected_nonzero = False
    for code, carrier in by_code.items():
        requested = allocations.get(code, 0.0)
        is_selected = code in selected
        limit = max(0.0, float(carrier.get("effective_limit_rmb") or 0))
        if not is_selected and requested > tolerance:
            unselected_nonzero = True
        effective_covered = min(requested, limit) if is_selected else 0.0
        over_limit = is_selected and requested > limit + tolerance
        row_over_limit = row_over_limit or over_limit
        if is_selected or requested > tolerance:
            rows.append({
                "fund_code": code,
                "selected": is_selected,
                "requested_amount": round(requested, 2),
                "covered_amount": round(effective_covered, 2),
                "capacity": round(limit, 2),
                "capacity_excess": round(max(0.0, requested - limit), 2),
                "row_over_limit": over_limit,
            })
        assigned += requested
        covered += effective_covered
        if is_selected:
            capacity += limit

    # Allocations for unknown/non-whitelisted codes are never effective cover.
    unknown_allocations = {
        code: value for code, value in allocations.items()
        if code not in by_code and value > tolerance
    }
    assigned += sum(unknown_allocations.values())
    unselected_nonzero = unselected_nonzero or bool(unknown_allocations)
    uncovered = max(0.0, amount - covered)
    over_assigned = max(0.0, assigned - amount)
    exact_match = (
        abs(assigned - amount) <= tolerance
        and abs(covered - amount) <= tolerance
        and uncovered <= tolerance
        and over_assigned <= tolerance
    )
    if amount <= tolerance and assigned <= tolerance:
        preview_status = "EMPTY"
    elif (amount > tolerance and exact_match and not row_over_limit
          and not unselected_nonzero and snapshot_valid
          and carrier_selection_status == "AVAILABLE"):
        preview_status = "VALID"
    else:
        preview_status = "INVALID"

    return {
        "preview_status": preview_status,
        "test_amount": round(amount, 2),
        "assigned_total_amount": round(assigned, 2),
        "effective_covered_amount": round(covered, 2),
        "remaining_uncovered_amount": round(uncovered, 2),
        "over_assigned_amount": round(over_assigned, 2),
        "row_over_limit": row_over_limit,
        "unselected_nonzero_amount": unselected_nonzero,
        "snapshot_valid": bool(snapshot_valid),
        "carrier_selection_status": carrier_selection_status,
        "amount_tolerance": float(tolerance),
        # Backward-compatible fields used by existing UI/report consumers.
        "asset_allocated_amount": round(amount, 2),
        "selected_carrier_count": len(selected & set(by_code)),
        "selected_total_capacity": round(capacity, 2),
        "allocated_amount": round(assigned, 2),
        "covered_amount": round(covered, 2),
        "over_selected_amount": round(over_assigned, 2),
        "capacity_excess_amount": round(sum(row["capacity_excess"] for row in rows), 2),
        "unknown_allocations": unknown_allocations,
        "complexity_warning": "当前选择基金数量较多，底层指数高度重合，增加的是载体复杂度，不是市场分散。" if len(selected) > 3 else None,
        "rows": rows,
    }


def overseas_equity_split(config, carriers):
    holdings = _holding_map(config)
    ndx_codes = {row["fund_code"] for row in carriers if row.get("ndx_pool_eligible")}
    # Portfolio holdings come from V7 config, never from the carrier-facts file.
    global_codes = {"270023"}
    ndx_amount = round(sum(holdings.get(code, 0) for code in ndx_codes), 2)
    global_amount = round(sum(holdings.get(code, 0) for code in global_codes), 2)
    total = round(ndx_amount + global_amount, 2)
    return {"overseas_equity_total": total, "ndx_qdii_amount": ndx_amount,
            "global_active_amount": global_amount,
            "ndx_qdii_ratio": round(ndx_amount / total, 6) if total else 0,
            "global_active_ratio": round(global_amount / total, 6) if total else 0}


def integration_snapshot(asset_allocated_amount, path=DEFAULT_SNAPSHOT_PATH, now=None, config=None):
    snapshot = read_snapshot(path, now=now)
    selection = select_carriers(asset_allocated_amount, snapshot, config=config)
    carrier_snapshot_valid = (
        snapshot.get("carrier_data_status") == "ACTIVE"
        and snapshot.get("carrier_selection_status") == "AVAILABLE"
        and selection.get("carrier_capacity_status") == "AVAILABLE"
    )
    observed_capacity = float(selection.get("approved_total_capacity", 0) or 0)
    global_active = next((row for row in selection["all_carriers"] if row["fund_code"] == "270023"), None)
    return {"integration_status": "IMPLEMENTED", "interface": "qdii_carrier_snapshot.json",
            "qdii_json_whitelist": "ACTIVE", "automatic_buy_on_limit_change": False,
            "carrier_snapshot_id": snapshot.get("snapshot_id"),
            "snapshot_generated_at": snapshot.get("generated_at"), "snapshot_age_minutes": snapshot.get("snapshot_age_minutes"),
            "source_confidence": "MIXED" if snapshot.get("funds") else "UNAVAILABLE",
            "stale_status": snapshot.get("stale_status", snapshot.get("carrier_data_status")),
            "carrier_data_status": snapshot.get("carrier_data_status"),
            "carrier_snapshot_valid": carrier_snapshot_valid,
            "carrier_selection_status": selection["carrier_capacity_status"],
            "carrier_availability_status": selection["carrier_capacity_status"],
            "selection": selection,
            "last_known_approved_carrier_capacity": round(observed_capacity, 2),
            "current_effective_carrier_capacity": round(observed_capacity, 2) if carrier_snapshot_valid else 0.0,
            "last_known_snapshot_generated_at": snapshot.get("generated_at"),
            "last_known_snapshot_status": snapshot.get("carrier_data_status", "UNAVAILABLE"),
            "overseas_equity_split": overseas_equity_split(config or {}, selection["all_carriers"]),
            "pools": {"NDX_INDEX_QDII_POOL": {"role": "ASSET_EXECUTION_CARRIER_POOL",
                "asset_allocated_amount": round(float(asset_allocated_amount or 0), 2), "automatic_release": False},
                "GLOBAL_ACTIVE_EQUITY_POOL": {"role": "HOLDING_DISPLAY_ONLY", "fund": global_active,
                "ndx_pool_eligible": False, "dynamic_release_eligible": False, "score": None, "release_factor": None}}}


def build_carrier_fact_projection(raw_snapshot, *, generated_at=None):
    """Project an upstream raw snapshot into the facts-only v2 contract."""
    _snapshot_contract(raw_snapshot)
    source_time = generated_at or _parse_timestamp(raw_snapshot["generated_at"])
    generated_iso = _iso_timestamp(source_time)
    volatile = _limit_volatility_codes(raw_snapshot)
    carriers = []
    for raw in raw_snapshot.get("funds", []):
        if raw.get("benchmark") != "NASDAQ_100" or raw.get("pool") != "NDX_INDEX_QDII_POOL":
            continue
        code = str(raw.get("code"))
        known = KNOWN_OFFICIAL_LIMITS.get(code, {})
        official = _positive_number(known.get("amount", raw.get("official_fund_limit_rmb")))
        observed = _positive_number(raw.get("observed_channel_limit_rmb"))
        effective = effective_limit_rmb(official, observed)
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        source_observed = source.get("observed_at") or raw_snapshot["generated_at"]
        carriers.append({
            "fund_code": code,
            "fund_name": raw.get("name") or code,
            "pool": raw.get("pool"),
            "benchmark": raw.get("benchmark"),
            "approved": True,
            "approved_by": raw.get("approved_by", "manual_review"),
            "approval_note": raw.get("approval_note", "Upstream whitelist; approval occurs outside V7"),
            "available": bool(effective is not None and effective > 0 and raw.get("purchase_status") != "暂停申购"),
            "channel_available": bool(effective is not None and effective > 0 and raw.get("purchase_status") != "暂停申购"),
            "personal_purchase_supported": bool(effective is not None and effective > 0 and raw.get("purchase_status") != "暂停申购"),
            "purchase_status": raw.get("purchase_status"),
            "redemption_status": raw.get("redemption_status"),
            "observed_channel_limit_rmb": observed,
            "official_fund_limit_rmb": official,
            "effective_limit_rmb": effective,
            "official_limit_effective_date": known.get("effective_date") or raw.get("official_limit_effective_date"),
            "nav_date": raw.get("nav_date"), "nav": raw.get("nav"),
            "estimated_nav": raw.get("estimated_nav"),
            "estimated_change_pct": raw.get("estimated_change_pct"),
            "tracking_error_pct": raw.get("tracking_error_pct"),
            "purchase_fee_display": raw.get("purchase_fee_display"),
            "management_fee_pct": raw.get("management_fee_pct"),
            "custody_fee_pct": raw.get("custody_fee_pct"),
            "service_fee_pct": raw.get("service_fee_pct"),
            "fund_type": raw.get("fund_type"), "fund_company": raw.get("fund_company"),
            "fund_size_rmb": raw.get("fund_size_rmb"), "inception_date": raw.get("inception_date"),
            "source": {
                "name": source.get("name"), "type": source.get("type"),
                "observed_at": _iso_timestamp(source_observed),
                "confidence": source.get("confidence"),
            },
            "verification_note": raw.get("verification_note"),
            "limit_volatility_flag": code in volatile,
        })
    _enrich_carrier_fees(carriers)
    capacity = round(sum(float(row.get("effective_limit_rmb") or 0) for row in carriers if row["approved"]), 2)
    projection = {
        "schema_version": FACT_SCHEMA_VERSION,
        "artifact_mode": "MUTABLE_LATEST",
        "snapshot": {
            "snapshot_id": "qdii-" + _parse_timestamp(generated_iso).strftime("%Y%m%d-%H%M%S"),
            "generated_at": generated_iso,
            "source_confidence": "MIXED",
            "stale_status": "PASS",
            "snapshot_valid": True,
        },
        "availability": {
            "carrier_data_status": "ACTIVE",
            "carrier_availability_status": "AVAILABLE",
            "approved_carrier_count": len(carriers),
            "last_known_approved_capacity": capacity,
            "current_effective_capacity": capacity,
        },
        "contract": {
            "not_investment_signal": True,
            "contains_v7_decisions": False,
            "contains_allocation_plan": False,
        },
        "carriers": carriers,
    }
    _facts_contract(projection)
    return projection


def write_carrier_fact_snapshot(raw_snapshot, output_path=CARRIER_JSON_PATH, generated_at=None):
    """Validate and atomically replace the mutable latest facts projection."""
    output_path = Path(output_path)
    projection = build_carrier_fact_projection(raw_snapshot, generated_at=generated_at)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=output_path.name + ".", suffix=".tmp", dir=str(output_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(projection, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        verified = json.loads(Path(temporary).read_text(encoding="utf-8"))
        _facts_contract(verified)
        os.replace(temporary, str(output_path))
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return projection


def write_carrier_snapshot(payload, path=CARRIER_JSON_PATH):
    """Compatibility wrapper accepting raw monitor payloads only."""
    if not isinstance(payload, dict) or "funds" not in payload:
        raise CarrierContractError("write_carrier_snapshot requires the raw monitor snapshot")
    write_carrier_fact_snapshot(payload, path)
    return str(path)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_run_inputs(run_dir, latest_path=CARRIER_JSON_PATH, raw_path=RAW_SNAPSHOT_PATH, archived_at=None):
    """Create immutable, hash-traceable input copies for one V7 run."""
    run_dir, latest_path, raw_path = Path(run_dir), Path(latest_path), Path(raw_path)
    inputs = run_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    targets = {
        "latest": inputs / "qdii-carrier-latest.json",
        "raw": inputs / "qdii-carrier-snapshot-raw.json",
    }
    manifest_path = inputs / "input-manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not targets["latest"].is_file() or not targets["raw"].is_file()
                or _sha256(targets["latest"]) != existing.get("carrier_latest_sha256")
                or _sha256(targets["raw"]) != existing.get("carrier_raw_sha256")):
            raise CarrierContractError("immutable carrier input archive failed hash verification")
        return existing
    for source, target in ((latest_path, targets["latest"]), (raw_path, targets["raw"])):
        if not source.is_file():
            raise CarrierContractError("carrier input is missing: " + str(source))
        if target.exists():
            if _sha256(source) != _sha256(target):
                raise CarrierContractError("immutable carrier input archive already exists with different content")
        else:
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
    latest_payload = json.loads(targets["latest"].read_text(encoding="utf-8"))
    normalized = normalize_carrier_fact_snapshot(latest_payload)
    archived_at = archived_at or dt.datetime.now().astimezone()
    manifest = {
        "v7_run_id": run_dir.name,
        "carrier_snapshot_id": normalized.get("snapshot_id"),
        "carrier_snapshot_generated_at": normalized.get("generated_at"),
        "carrier_latest_sha256": _sha256(targets["latest"]),
        "carrier_raw_sha256": _sha256(targets["raw"]),
        "archived_at": _iso_timestamp(archived_at),
    }
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    manifest_path.write_text(encoded, encoding="utf-8")
    return manifest


def apply_carrier_matching(ndx_candidate_release_amount, carrier_snapshot):
    """V7 Carrier Matching Layer: apply carrier capacity constraints to a model candidate.

    Given a candidate amount from the model layer and carrier facts from the
    integration snapshot, return coverable/retained amounts.

    Responsibility boundary:
    - Carrier AVAILABLE:  coverable = min(candidate, current_capacity)
    - Carrier BLOCKED:    coverable = 0, retained_due_to_carrier_block = candidate
    - Carrier STALE:      same as BLOCKED
    - Carrier PARTIAL:    coverable = min(candidate, current_capacity)

    IMPORTANT: carrier failure zeros coverable but NEVER zeros the candidate.
    The candidate amount is owned by the Model Candidate Layer and passed in as
    a parameter — this function does not modify it.
    """
    candidate = max(0.0, float(ndx_candidate_release_amount or 0))
    carrier_available = bool(
        carrier_snapshot.get("carrier_snapshot_valid")
        and carrier_snapshot.get("carrier_selection_status") == "AVAILABLE"
    )
    last_known_capacity = max(0.0, float(
        carrier_snapshot.get("last_known_approved_carrier_capacity", 0) or 0
    ))
    current_capacity = last_known_capacity if carrier_available else 0.0
    coverable = min(candidate, current_capacity) if carrier_available else 0.0
    retained_due_to_capacity = max(0.0, candidate - current_capacity) if carrier_available else 0.0
    retained_due_to_carrier_block = candidate if not carrier_available else 0.0
    return {
        "carrier_snapshot_id": carrier_snapshot.get("carrier_snapshot_id"),
        "carrier_coverable_amount": round(coverable, 2),
        "retained_due_to_capacity": round(retained_due_to_capacity, 2),
        "retained_due_to_carrier_block": round(retained_due_to_carrier_block, 2),
        "carrier_snapshot_valid": carrier_available,
        "carrier_selection_status": carrier_snapshot.get("carrier_selection_status", "BLOCKED"),
        "last_known_approved_carrier_capacity": round(last_known_capacity, 2),
        "current_effective_carrier_capacity": round(current_capacity, 2),
        "last_known_snapshot_generated_at": carrier_snapshot.get("last_known_snapshot_generated_at"),
        "last_known_snapshot_status": carrier_snapshot.get("last_known_snapshot_status"),
    }
