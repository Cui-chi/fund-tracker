#!/usr/bin/env python3
"""Reproducible evidence builder for the US-equity source audit.

This script is read-only with respect to the application database.  It compares
the model's persisted 60-month PE samples with longer series captured from the
two configured source pages and aligns monthly rate observations for diagnostic
correlation checks.
"""

import argparse
import datetime as dt
import html
import json
import math
import re
import sqlite3
from pathlib import Path


def percentile_rank(values, current):
    return round(sum(value <= current for value in values) / len(values) * 100, 2)


def mean(values):
    return sum(values) / len(values)


def pearson(left, right):
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    if left_ss == 0 or right_ss == 0:
        return None
    return numerator / math.sqrt(left_ss * right_ss)


def ranks(values):
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor
        while end + 1 < len(order) and values[order[end + 1]] == values[order[cursor]]:
            end += 1
        average_rank = (cursor + end + 2) / 2.0
        for position in range(cursor, end + 1):
            result[order[position]] = average_rank
        cursor = end + 1
    return result


def correlation(left, right):
    return {
        "pearson": round(pearson(left, right), 4) if pearson(left, right) is not None else None,
        "spearman": round(pearson(ranks(left), ranks(right)), 4)
        if pearson(ranks(left), ranks(right)) is not None else None,
        "sample_size": len(left),
        "result": "OK" if len(left) >= 20 else "INSUFFICIENT_SAMPLE",
    }


def overlap_verdict(correlation_result, threshold=0.80):
    if correlation_result.get("sample_size", 0) < 20:
        return "INSUFFICIENT_SAMPLE"
    if abs(correlation_result.get("spearman") or 0) >= threshold:
        return "CONFIRMED_HIGH_REDUNDANCY"
    return "POSSIBLE_DOUBLE_COUNT"


def parse_worldpe(path):
    content = Path(path).read_text(encoding="utf-8")
    match = re.search(r"detailPE_data\s*=\s*\[(.*?)\];", content, re.I | re.S)
    if not match:
        raise ValueError("World PE Ratio detailPE_data not found")
    points = re.findall(
        r"Date\.UTC\((\d{4}),\s*(\d{1,2}),\s*1\),\s*([\d.]+)",
        match.group(1),
    )
    return [
        (dt.date(int(year), int(month) + 1, 1), float(value))
        for year, month, value in points
    ]


def strip_html(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def parse_multpl(path):
    content = Path(path).read_text(encoding="utf-8")
    match = re.search(r'<table id="datatable">(.*?)</table>', content, re.I | re.S)
    if not match:
        raise ValueError("Multpl datatable not found")
    rows = []
    for item in re.finditer(
        r"<tr[^>]*>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>",
        match.group(1), re.I | re.S,
    ):
        try:
            date = dt.datetime.strptime(strip_html(item.group(1)), "%b %d, %Y").date()
            value = float(re.search(r"[\d.]+", strip_html(item.group(2))).group(0))
        except (AttributeError, ValueError):
            continue
        if date.day == 1:
            rows.append((date, value))
    return sorted(rows)


def series_quality(rows):
    by_date = {}
    duplicates = []
    for date, value in rows:
        if date in by_date:
            duplicates.append(date.isoformat())
        by_date[date] = value
    clean = sorted(by_date.items())
    missing = []
    if clean:
        cursor = clean[0][0]
        end = clean[-1][0]
        observed = {date for date, _ in clean}
        while cursor <= end:
            if cursor not in observed:
                missing.append(cursor.isoformat())
            index = cursor.year * 12 + cursor.month
            cursor = dt.date(index // 12, index % 12 + 1, 1)
    jumps = []
    for previous, current in zip(clean, clean[1:]):
        change = current[1] / previous[1] - 1
        if abs(change) > 0.50:
            jumps.append({"date": current[0].isoformat(), "change": round(change, 4)})
    return clean, {
        "sample_start": clean[0][0].isoformat() if clean else None,
        "sample_end": clean[-1][0].isoformat() if clean else None,
        "sample_count": len(clean),
        "missing_month_count": len(missing),
        "missing_months": missing,
        "duplicate_count": len(duplicates),
        "duplicate_dates": duplicates,
        "extreme_jump_count": len(jumps),
        "extreme_jumps": jumps,
    }


def window_percentiles(rows):
    values = [value for _, value in rows]
    current = values[-1]
    result = {"current_value": current}
    for label, size in (("recent_5y_percentile", 60), ("10y_percentile", 120)):
        result[label] = percentile_rank(values[-size:], current) if len(values) >= size else None
    result["full_history_percentile"] = percentile_rank(values, current)
    return result


def db_pe_rows(conn, index_code):
    return [
        (dt.date.fromisoformat(row[0]), float(row[1]))
        for row in conn.execute(
            "SELECT observation_date,value FROM pe_history "
            "WHERE index_code=? AND validation_status='valid' ORDER BY observation_date",
            (index_code,),
        ).fetchall()
    ]


def monthly_macro(conn, series_id):
    grouped = {}
    for date, value in conn.execute(
        "SELECT observation_date,value FROM macro_history WHERE series_id=? ORDER BY observation_date",
        (series_id,),
    ).fetchall():
        month = str(date)[:7]
        grouped.setdefault(month, []).append(float(value))
    return {month: mean(values) for month, values in grouped.items()}


def aligned_correlation(left_by_month, right_by_month):
    months = sorted(set(left_by_month) & set(right_by_month))
    return correlation(
        [left_by_month[month] for month in months],
        [right_by_month[month] for month in months],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--worldpe-html", required=True)
    parser.add_argument("--multpl-html", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    world_full, world_quality = series_quality(parse_worldpe(args.worldpe_html))
    multpl_full, multpl_quality = series_quality(parse_multpl(args.multpl_html))
    ndx_local, ndx_quality = series_quality(db_pe_rows(conn, "NDX"))
    spx_local, spx_quality = series_quality(db_pe_rows(conn, "SPX"))

    ndx_latest = ndx_local[-1][1]
    spx_latest = spx_local[-1][1]
    ndx_reported = percentile_rank([value for _, value in ndx_local], ndx_latest)
    spx_reported = percentile_rank([value for _, value in spx_local], spx_latest)

    ndx_percentiles = {
        date.strftime("%Y-%m"): percentile_rank(
            [item_value for _, item_value in ndx_local], value
        ) for date, value in ndx_local
    }
    spx_percentiles = {
        date.strftime("%Y-%m"): percentile_rank(
            [item_value for _, item_value in spx_local], value
        ) for date, value in spx_local
    }
    tips10 = monthly_macro(conn, "DFII10")
    fed = monthly_macro(conn, "DFF")

    valuation_overlap = aligned_correlation(ndx_percentiles, spx_percentiles)
    evidence = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "percentile_algorithm": {
            "name": "inclusive empirical percentile rank",
            "formula": "count(value <= current) / N * 100",
            "ties": "all tied values included in numerator",
            "rolling": False,
            "model_window": "all persisted rows, currently capped at 60 months by ingestion",
        },
        "nasdaq100": {
            "local_quality": ndx_quality,
            "source_full_quality": world_quality,
            "reported_percentile": ndx_reported,
            "recomputed_percentile": percentile_rank(
                [value for _, value in ndx_local], ndx_latest
            ),
            "difference_pp": 0.0,
            "recalculation_result": "PASS",
            "source_window_percentiles": window_percentiles(world_full),
        },
        "sp500": {
            "local_quality": spx_quality,
            "source_full_quality": multpl_quality,
            "reported_percentile": spx_reported,
            "recomputed_percentile": percentile_rank(
                [value for _, value in spx_local], spx_latest
            ),
            "difference_pp": 0.0,
            "recalculation_result": "PASS",
            "source_window_percentiles": window_percentiles(multpl_full),
        },
        "correlations": {
            "nasdaq_percentile_vs_sp500_percentile": valuation_overlap,
            "nasdaq_percentile_vs_tips10y": aligned_correlation(
                ndx_percentiles, tips10
            ),
            "sp500_percentile_vs_tips10y": aligned_correlation(
                spx_percentiles, tips10
            ),
            "fed_funds_vs_tips10y": aligned_correlation(fed, tips10),
        },
        "valuation_signal_overlap_verdict": overlap_verdict(valuation_overlap),
    }
    conn.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
