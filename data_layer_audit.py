import datetime as dt
import json
from pathlib import Path
from utils import output_paths


PHASE_1_BLOCK_CODE = "FUND_NAV_AUDIT_FAILED"
WINDOWS = {"6m": 183, "12m": 365}
MIN_COVERAGE_RATIO = 0.80


def _lag_status(is_qdii, lag_days):
    pass_limit, warning_limit = (4, 7) if is_qdii else (2, 5)
    if lag_days <= pass_limit:
        return "PASS"
    if lag_days <= warning_limit:
        return "WARNING"
    return "FAIL"


def _drawdown(rows, window_days):
    latest = rows[-1]
    end = dt.date.fromisoformat(latest["nav_date"])
    start = end - dt.timedelta(days=window_days)
    window = [r for r in rows if dt.date.fromisoformat(r["nav_date"]) >= start]
    earliest = dt.date.fromisoformat(window[0]["nav_date"])
    max_row = max(window, key=lambda r: (r["unit_nav"], r["nav_date"]))
    required_samples = sum(
        1 for offset in range((end - start).days + 1)
        if (start + dt.timedelta(days=offset)).weekday() < 5
    )
    coverage_days = (end - max(start, earliest)).days + 1
    coverage_ratio = min(1.0, len(window) / required_samples) if required_samples else 0.0
    status = "PASS" if coverage_ratio >= MIN_COVERAGE_RATIO else "INSUFFICIENT"
    return {
        "current_nav": latest["unit_nav"],
        "current_nav_date": latest["nav_date"],
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "max_nav_in_window": max_row["unit_nav"],
        "max_nav_date": max_row["nav_date"],
        "drawdown": latest["unit_nav"] / max_row["unit_nav"] - 1,
        "sample_size": len(window),
        "coverage_days": coverage_days,
        "required_samples": required_samples,
        "coverage_ratio": coverage_ratio,
        "coverage_status": status,
        "display_note": "" if status == "PASS" else "样本不足，仅供参考",
    }


def audit_fund_nav(conn, config, as_of=None):
    as_of = as_of or dt.date.today()
    funds = []
    blocking_issues = []
    for fund in config["funds"]:
        code = fund["code"]
        execution_only_without_holding = bool(
            fund.get("execution_only")
            and float(fund.get("holding_amount", 0) or 0) <= 0
        )
        db_rows = conn.execute(
            """
            SELECT code, nav_date, nav, accumulated_nav, source, source_url,
                   COALESCE(fetch_time, created_at) AS fetch_time, is_qdii
            FROM nav_history WHERE code = ? ORDER BY nav_date
            """,
            (code,),
        ).fetchall()
        rows = [
            {
                "fund_code": row["code"],
                "fund_name": fund["name"],
                "nav_date": row["nav_date"],
                "unit_nav": row["nav"],
                "accumulated_nav": row["accumulated_nav"],
                "source": row["source"],
                "source_url": row["source_url"],
                "fetch_time": row["fetch_time"],
                "is_qdii": bool(row["is_qdii"]),
            }
            for row in db_rows
        ]
        is_qdii = "QDII" in fund.get("type", "").upper()
        issues = []
        if not rows:
            if execution_only_without_holding:
                issues.append("execution_only_without_holding")
            else:
                issues.append("missing_raw_nav_series")
                blocking_issues.append(f"{code}:missing_raw_nav_series")
            missing = {
                "fund_code": code,
                "fund_name": fund["name"],
                "source": None,
                "latest_nav_date": None,
                "latest_nav": None,
                "is_qdii": is_qdii,
                "data_lag_days": None,
                "qdii_lag_status": "FAIL" if is_qdii else "NOT_APPLICABLE",
                "status": "NOT_APPLICABLE" if execution_only_without_holding else "FAIL",
                "issues": issues,
            }
            for label in ("6m", "12m"):
                missing.update({
                    "%s_window_start" % label: None,
                    "%s_window_end" % label: None,
                    "%s_max_nav" % label: None,
                    "%s_max_nav_date" % label: None,
                    "%s_drawdown" % label: None,
                    "%s_sample_size" % label: 0,
                    "%s_coverage_ratio" % label: 0.0,
                    "%s_coverage_status" % label: "INSUFFICIENT",
                })
            funds.append(missing)
            continue
        latest_date = dt.date.fromisoformat(rows[-1]["nav_date"])
        lag_days = (as_of - latest_date).days
        lag_status = _lag_status(is_qdii, lag_days)
        missing_source = sum(not row["source"] for row in rows)
        missing_fetch_time = sum(not row["fetch_time"] for row in rows)
        missing_accumulated = sum(row["accumulated_nav"] is None for row in rows)
        if missing_source or missing_fetch_time:
            issues.append("raw_lineage_incomplete")
            blocking_issues.append(f"{code}:raw_lineage_incomplete")
        if lag_status == "FAIL":
            issues.append("latest_nav_stale")
            blocking_issues.append(f"{code}:latest_nav_stale")
        drawdowns = {
            label: _drawdown(rows, days) for label, days in WINDOWS.items()
        }
        funds.append({
            "fund_code": code,
            "fund_name": fund["name"],
            "is_qdii": is_qdii,
            "source": rows[-1]["source"],
            "source_url": rows[-1]["source_url"],
            "latest_nav_date": rows[-1]["nav_date"],
            "latest_nav": rows[-1]["unit_nav"],
            "data_lag_days": lag_days,
            "lag_status": lag_status,
            "qdii_lag_status": lag_status if is_qdii else "NOT_APPLICABLE",
            "raw_sample_size": len(rows),
            "earliest_nav_date": rows[0]["nav_date"],
            "latest_fetch_time": max(row["fetch_time"] for row in rows if row["fetch_time"]),
            "missing_accumulated_nav_count": missing_accumulated,
            "drawdowns": drawdowns,
            "status": "FAIL" if issues else ("WARNING" if lag_status == "WARNING" else "PASS"),
            "issues": issues,
        })
        target = funds[-1]
        for label in ("6m", "12m"):
            row = drawdowns[label]
            target.update({
                "%s_window_start" % label: row["window_start"],
                "%s_window_end" % label: row["window_end"],
                "%s_max_nav" % label: row["max_nav_in_window"],
                "%s_max_nav_date" % label: row["max_nav_date"],
                "%s_drawdown" % label: row["drawdown"],
                "%s_sample_size" % label: row["sample_size"],
                "%s_coverage_ratio" % label: row["coverage_ratio"],
                "%s_coverage_status" % label: row["coverage_status"],
            })
    status = "BLOCKED" if blocking_issues else "PASS"
    return {
        "phase": 1,
        "audit_name": "Fund NAV and Drawdown Audit",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "block_code": PHASE_1_BLOCK_CODE if blocking_issues else None,
        "blocking_issues": blocking_issues,
        "method": "drawdown = current_nav / max_nav_in_window - 1",
        "minimum_coverage_ratio": MIN_COVERAGE_RATIO,
        "funds": funds,
    }


def write_phase1_reports(base_dir, result):
    base = Path(base_dir)
    output_paths.get_json_path("fund-drawdown-result.json", base).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    nav_lines = [
        "# Fund NAV Audit Report", "",
        f"- Phase Status: **{result['status']}**",
        f"- Block Code: {result['block_code'] or 'None'}",
        f"- Generated At: {result['generated_at']}",
        "- Primary Source Policy: official fund-company history first; Eastmoney historical disclosed NAV is the current fallback.",
        "- Prohibited Inputs: intraday estimated NAV, undisclosed period return, third-party drawdown result.",
        "",
        "| Fund Code | Fund Name | Source | Latest NAV Date | Latest NAV | QDII | Data Lag Days | QDII Lag Status | Raw Samples | Lineage Status |",
        "|---|---|---|---|---:|---:|---:|---|---:|---|",
    ]
    for fund in result["funds"]:
        latest_nav = fund.get("latest_nav")
        latest_nav_text = "N/A" if latest_nav is None else f"{latest_nav:.4f}"
        nav_lines.append(
            f"| {fund['fund_code']} | {fund['fund_name']} | {fund.get('source', 'N/A')} | "
            f"{fund.get('latest_nav_date') or 'N/A'} | {latest_nav_text} | {fund['is_qdii']} | "
            f"{fund.get('data_lag_days', 'N/A')} | {fund.get('qdii_lag_status', 'N/A')} | "
            f"{fund.get('raw_sample_size', 0)} | "
            f"{fund['status']} |"
        )
    nav_lines += ["", "## Blocking Issues", ""]
    nav_lines += [f"- {item}" for item in result["blocking_issues"]] or ["- None"]
    output_paths.get_report_path("fund-nav-audit-report.md", base).write_text("\n".join(nav_lines) + "\n", encoding="utf-8")

    dd_lines = [
        "# Fund Drawdown Report", "",
        f"- Phase Status: **{result['status']}**",
        f"- Formula: `{result['method']}`",
        "- Coverage ratio: observed NAV samples / expected weekdays in the requested window; below 80% is `INSUFFICIENT`.",
        "",
        "| Fund Code | Fund Name | 6M Window | 6M Max NAV/Date | 6M Drawdown | 6M Samples | 6M Coverage | 6M Status | 12M Window | 12M Max NAV/Date | 12M Drawdown | 12M Samples | 12M Coverage | 12M Status |",
        "|---|---|---|---|---:|---:|---:|---|---|---:|---:|---:|---|",
    ]
    for fund in result["funds"]:
        six = fund.get("drawdowns", {}).get("6m")
        twelve = fund.get("drawdowns", {}).get("12m")
        if not six or not twelve:
            dd_lines.append(f"| {fund['fund_code']} | {fund['fund_name']} | - | - | - | 0 | 0% | INSUFFICIENT | - | - | - | 0 | 0% | INSUFFICIENT |")
            continue
        dd_lines.append(
            f"| {fund['fund_code']} | {fund['fund_name']} | {six['window_start']} to {six['window_end']} | {six['max_nav_in_window']:.4f} / {six['max_nav_date']} | {six['drawdown']:.2%} | {six['sample_size']} | {six['coverage_ratio']:.1%} | {six['coverage_status']} | {twelve['window_start']} to {twelve['window_end']} | {twelve['max_nav_in_window']:.4f} / {twelve['max_nav_date']} | {twelve['drawdown']:.2%} | {twelve['sample_size']} | {twelve['coverage_ratio']:.1%} | {twelve['coverage_status']} |"
        )
    output_paths.get_report_path("fund-drawdown-report.md", base).write_text("\n".join(dd_lines) + "\n", encoding="utf-8")
    return result
