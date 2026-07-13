#!/usr/bin/env python3
"""Auditable US-equity rate-history and replacement-source research helpers."""

import csv
import datetime as dt
import hashlib
import math
import uuid
from pathlib import Path


RATE_FIELDS = [
    "series_id", "month", "monthly_value", "source_date",
    "aggregation_method", "source", "fetched_at",
]
ATTEMPT_FIELDS = [
    "attempt_id", "source_name", "indicator", "attempted_at", "success",
    "http_status", "latency_ms", "row_count", "latest_observation_date",
    "schema_signature", "error_type", "error_message",
]
REVISION_FIELDS = [
    "source_name", "indicator", "observation_date", "old_value", "new_value",
    "detected_at", "revision_size", "revision_reason",
]
CANDIDATE_REQUIRED_FIELDS = [
    "source_name", "source_url", "provider_type", "object_type", "metric_type",
    "trailing_or_forward", "earnings_basis", "negative_earnings_policy",
    "aggregation_method", "revision_policy", "frequency", "history_start",
    "history_end", "sample_count", "access_method", "auth_required", "rate_limit",
    "html_dependency", "api_available", "download_available",
    "license_or_usage_notes", "reproducible", "stability_evidence",
]
WINDOW_FIELDS = [
    "current_value", "5y_percentile", "10y_percentile", "15y_percentile",
    "20y_percentile", "full_history_percentile", "max_percentile_spread",
    "window_sensitivity",
]


def _parse_date(value):
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def read_fred_daily(path, series_id):
    rows, non_numeric = [], 0
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = row.get(series_id)
            if raw in (None, "", "."):
                non_numeric += 1
                continue
            try:
                rows.append((_parse_date(row["observation_date"]), float(raw)))
            except (TypeError, ValueError):
                non_numeric += 1
    return rows, non_numeric


def month_end_last_valid_value(daily_rows, series_id, fetched_at):
    grouped = {}
    for observation_date, value in daily_rows:
        grouped.setdefault(observation_date.strftime("%Y-%m"), []).append(
            (observation_date, value)
        )
    result = []
    for month in sorted(grouped):
        source_date, value = max(grouped[month], key=lambda item: item[0])
        result.append({
            "series_id": series_id,
            "month": month,
            "monthly_value": value,
            "source_date": source_date.isoformat(),
            "aggregation_method": "month_end_last_valid_value",
            "source": "Federal Reserve Bank of St. Louis (FRED)",
            "fetched_at": fetched_at,
        })
    return result


def write_csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_csv(path, row, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def append_attempt(path, **values):
    row = {field: values.get(field, "") for field in ATTEMPT_FIELDS}
    row["attempt_id"] = row["attempt_id"] or str(uuid.uuid4())
    row["attempted_at"] = row["attempted_at"] or dt.datetime.now().isoformat(timespec="seconds")
    append_csv(path, row, ATTEMPT_FIELDS)
    return row


def record_revision_if_changed(path, source_name, indicator, observation_date,
                               old_value, new_value, reason="UNKNOWN"):
    if old_value is None or float(old_value) == float(new_value):
        return False
    append_csv(path, {
        "source_name": source_name,
        "indicator": indicator,
        "observation_date": observation_date,
        "old_value": old_value,
        "new_value": new_value,
        "detected_at": dt.datetime.now().isoformat(timespec="seconds"),
        "revision_size": float(new_value) - float(old_value),
        "revision_reason": reason or "UNKNOWN",
    }, REVISION_FIELDS)
    return True


def ensure_revision_log(path):
    path = Path(path)
    if not path.exists():
        write_csv(path, [], REVISION_FIELDS)


def quality_summary(rows, non_numeric_rows=0, as_of=None):
    as_of = as_of or dt.date.today()
    months = [row["month"] for row in rows]
    duplicates = len(months) - len(set(months))
    missing = []
    if months:
        start = dt.datetime.strptime(months[0], "%Y-%m").date()
        end = dt.datetime.strptime(months[-1], "%Y-%m").date()
        observed = set(months)
        cursor = start
        while cursor <= end:
            key = cursor.strftime("%Y-%m")
            if key not in observed:
                missing.append(key)
            index = cursor.year * 12 + cursor.month
            cursor = dt.date(index // 12, index % 12 + 1, 1)
        latest_source = _parse_date(rows[-1]["source_date"])
        stale_tail = (as_of - latest_source).days > 31
    else:
        stale_tail = True
    return {
        "start_month": months[0] if months else None,
        "end_month": months[-1] if months else None,
        "sample_count": len(rows),
        "missing_months": missing,
        "duplicate_months": duplicates,
        "non_numeric_rows": non_numeric_rows,
        "stale_tail": stale_tail,
    }


def mean(values):
    return sum(values) / len(values)


def pearson(left, right):
    if len(left) < 3 or len(left) != len(right):
        return None
    lm, rm = mean(left), mean(right)
    numerator = sum((a - lm) * (b - rm) for a, b in zip(left, right))
    left_ss = sum((a - lm) ** 2 for a in left)
    right_ss = sum((b - rm) ** 2 for b in right)
    return numerator / math.sqrt(left_ss * right_ss) if left_ss and right_ss else None


def ranks(values):
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor
        while end + 1 < len(order) and values[order[end + 1]] == values[order[cursor]]:
            end += 1
        rank = (cursor + end + 2) / 2.0
        for position in range(cursor, end + 1):
            result[order[position]] = rank
        cursor = end + 1
    return result


def redundancy_label(value):
    absolute = abs(value) if value is not None else 0
    if absolute >= 0.80:
        return "HIGH_REDUNDANCY"
    if absolute >= 0.50:
        return "MODERATE_REDUNDANCY"
    return "LOW_REDUNDANCY"


def correlation_result(left, right):
    p = pearson(left, right)
    s = pearson(ranks(left), ranks(right))
    return {
        "pearson": round(p, 4) if p is not None else None,
        "spearman": round(s, 4) if s is not None else None,
        "sample_count": len(left),
        "redundancy": redundancy_label(s),
    }


def align_rates(series_rows):
    mappings = {
        name: {row["month"]: float(row["monthly_value"]) for row in rows}
        for name, rows in series_rows.items()
    }
    common = sorted(set.intersection(*(set(values) for values in mappings.values())))
    return [{
        "month": month,
        "tips5y": mappings["tips5y"][month],
        "tips10y": mappings["tips10y"][month],
        "fed_funds": mappings["fed_funds"][month],
    } for month in common]


def rate_correlations(aligned):
    windows = {"full_period": None, "last_10y": 120, "last_5y": 60}
    result = {}
    for label, size in windows.items():
        rows = aligned[-size:] if size else aligned
        result[label] = {}
        for left, right in (("tips5y", "tips10y"), ("tips5y", "fed_funds"),
                            ("tips10y", "fed_funds")):
            result[label][left + "_vs_" + right] = correlation_result(
                [row[left] for row in rows], [row[right] for row in rows]
            )
    return result


def percentile(values, current):
    return round(sum(value <= current for value in values) / len(values) * 100, 2)


def window_metrics(rows):
    values = [float(value) for _, value in sorted(rows)]
    if not values:
        result = {field: "N/A" for field in WINDOW_FIELDS}
        result["window_sensitivity"] = "N/A"
        return result
    current = values[-1]
    result = {"current_value": current}
    available = []
    for years in (5, 10, 15, 20):
        size = years * 12
        key = "%sy_percentile" % years
        value = percentile(values[-size:], current) if len(values) >= size else "N/A"
        result[key] = value
        if value != "N/A":
            available.append(value)
    result["full_history_percentile"] = percentile(values, current)
    available.append(result["full_history_percentile"])
    spread = round(max(available) - min(available), 2)
    result["max_percentile_spread"] = spread
    result["window_sensitivity"] = (
        "STABLE" if spread <= 10 else "MODERATE" if spread <= 25 else "WINDOW_SENSITIVE"
    )
    return result


def schema_signature(fields):
    return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()[:16]
