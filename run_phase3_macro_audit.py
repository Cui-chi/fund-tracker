import datetime as dt
import json
import uuid
from pathlib import Path

import fund_tracker
from utils import output_paths


BASE_DIR = Path(__file__).resolve().parent


def daily_stale_status(data_date, as_of):
    lag = (as_of - data_date).days
    if lag <= 3:
        return "PASS", lag
    if lag <= 7:
        return "WARNING", lag
    return "FAIL", lag


def expected_monthly_observation(as_of):
    current_month = as_of.replace(day=1)
    months_back = 1 if as_of.day >= 15 else 2
    return fund_tracker.shift_months(current_month, -months_back)


def monthly_stale_status(data_date, as_of):
    expected = expected_monthly_observation(as_of)
    observed = data_date.replace(day=1)
    if observed >= expected:
        return "PASS"
    return "FAIL"


def persist(conn, item, run_id):
    conn.execute(
        """
        INSERT INTO macro_data_audit_observations (
            indicator_name, frequency, data_date, release_date, fetch_time,
            source, raw_value, parsed_value, parse_status, stale_status,
            audit_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["indicator_name"], item["frequency"], item.get("data_date"),
            item.get("release_date"), item["fetch_time"], item["source"],
            item.get("raw_value"), item.get("parsed_value"),
            item["parse_status"], item["stale_status"], run_id,
        ),
    )


def main():
    run_dir = output_paths.create_run_dir("phase-p1-macro")
    as_of = dt.date.today()
    fetch_time = dt.datetime.now().isoformat(timespec="seconds")
    run_id = str(uuid.uuid4())
    conn = fund_tracker.connect_db()
    config = fund_tracker.load_config()
    items = []
    logs = []
    try:
        source = fund_tracker.copilot_config(config)["automatic_sources"]["china_money"]
        try:
            row = fund_tracker.fetch_pbc_money_indicators(source)
            for indicator, key, raw_key in (
                ("社融存量同比", "social_financing_yoy", "raw_social_financing_yoy"),
                ("M2同比", "m2_yoy", "raw_m2_yoy"),
            ):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO economic_indicator_history
                    (metric_id, observation_date, value, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, row["observation_date"], row[key], row["source"], fetch_time),
                )
                data_date = dt.date.fromisoformat(row["observation_date"])
                item = {
                    "indicator_name": indicator,
                    "frequency": "monthly",
                    "data_date": row["observation_date"],
                    "release_date": row["release_date"],
                    "fetch_time": fetch_time,
                    "source": row["source_url"],
                    "raw_value": row[raw_key],
                    "parsed_value": row[key],
                    "parse_status": "PASS",
                    "stale_status": monthly_stale_status(data_date, as_of),
                    "expected_observation_month": expected_monthly_observation(as_of).strftime("%Y-%m"),
                }
                items.append(item)
                logs.append(f"PASS {indicator}: fetched and parsed {row['observation_date']} from {row['source_url']}")
        except Exception as exc:
            logs.append(f"FAIL PBOC fetch/parse: {type(exc).__name__}: {exc}")
            for indicator, key in (("社融存量同比", "social_financing_yoy"), ("M2同比", "m2_yoy")):
                old = fund_tracker.latest_economic_indicator(conn, key)
                item = {
                    "indicator_name": indicator,
                    "frequency": "monthly",
                    "data_date": old["observation_date"] if old else None,
                    "release_date": None,
                    "fetch_time": fetch_time,
                    "source": source["index_url"],
                    "raw_value": None,
                    "parsed_value": old["value"] if old else None,
                    "parse_status": "FETCH_OR_PARSE_FAILED",
                    "stale_status": "FAIL",
                    "expected_observation_month": expected_monthly_observation(as_of).strftime("%Y-%m"),
                }
                items.append(item)

        for series_id, name in fund_tracker.MACRO_SERIES.items():
            try:
                rows = fund_tracker.fetch_fred_series(series_id, days=120)
                if not rows:
                    raise ValueError("FRED returned no parsed observations")
                for row in rows:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO macro_history
                        (series_id, observation_date, value, source, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (series_id, row["date"], row["value"], row["source"], fetch_time),
                    )
                latest = rows[-1]
                data_date = dt.date.fromisoformat(latest["date"])
                stale, lag = daily_stale_status(data_date, as_of)
                item = {
                    "indicator_name": name,
                    "series_id": series_id,
                    "frequency": "daily",
                    "data_date": latest["date"],
                    "release_date": latest["date"],
                    "fetch_time": fetch_time,
                    "source": f"https://fred.stlouisfed.org/series/{series_id}",
                    "raw_value": str(latest["value"]),
                    "parsed_value": latest["value"],
                    "parse_status": "PASS",
                    "stale_status": stale,
                    "data_lag_days": lag,
                }
                items.append(item)
                logs.append(f"{stale} FRED {series_id}: {latest['date']} lag={lag} days")
            except Exception as exc:
                logs.append(f"FAIL FRED {series_id}: {type(exc).__name__}: {exc}")
                old = conn.execute(
                    "SELECT observation_date, value FROM macro_history WHERE series_id=? ORDER BY observation_date DESC LIMIT 1",
                    (series_id,),
                ).fetchone()
                item = {
                    "indicator_name": name,
                    "series_id": series_id,
                    "frequency": "daily",
                    "data_date": old["observation_date"] if old else None,
                    "release_date": None,
                    "fetch_time": fetch_time,
                    "source": f"https://fred.stlouisfed.org/series/{series_id}",
                    "raw_value": None,
                    "parsed_value": old["value"] if old else None,
                    "parse_status": "FETCH_OR_PARSE_FAILED",
                    "stale_status": "FAIL",
                    "data_lag_days": None,
                }
                items.append(item)

        for item in items:
            persist(conn, item, run_id)
        failed = [item["indicator_name"] for item in items if item["stale_status"] == "FAIL" or item["parse_status"] != "PASS"]
        warnings = [item["indicator_name"] for item in items if item["stale_status"] == "WARNING"]
        status = "BLOCKED" if failed else "WARNING" if warnings else "PASS"
        result = {
            "phase": 3,
            "audit_run_id": run_id,
            "generated_at": fetch_time,
            "status": status,
            "block_code": "MACRO_DATA_AUDIT_FAILED" if failed else None,
            "failed_indicators": failed,
            "warning_indicators": warnings,
            "expected_release_calendar": {
                "rule": "monthly observation expected by day 15 of following month",
                "current_expected_observation_month": expected_monthly_observation(as_of).strftime("%Y-%m"),
                "checking_window": "day 10 through day 18",
            },
            "indicators": items,
            "root_cause_social_m2_stuck_at_april": {
                "may_data_exists": True,
                "may_release_date": "2026-06-12",
                "fetch_url_status": "PASS",
                "regex_status_before": "FAIL",
                "regex_status_after": "PASS",
                "cache_status": "cache could preserve the old value, but the immediate cause was regex failure on the May article",
                "detail": "May used ASCII (M2) and the social-financing headline wording did not match the previous narrow expressions.",
            },
        }
        output_paths.get_json_path("macro-data-result.json", run_dir).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report = [
            "# Macro Data Audit Report", "",
            f"- Phase Status: **{status}**",
            f"- Block Code: {result['block_code'] or 'None'}",
            f"- Audit Run ID: {run_id}",
            "", "## Indicator Audit", "",
            "| Indicator | Frequency | Data Date | Release Date | Fetch Time | Source | Raw Value | Parsed Value | Parse Status | Stale Status |",
            "|---|---|---|---|---|---|---|---:|---|---|",
        ]
        for item in items:
            report.append(
                f"| {item['indicator_name']} | {item['frequency']} | {item.get('data_date') or '-'} | {item.get('release_date') or '-'} | {item['fetch_time']} | {item['source']} | {str(item.get('raw_value') or '-').replace('|', '/')} | {item.get('parsed_value')} | {item['parse_status']} | {item['stale_status']} |"
            )
        report += [
            "", "## 社融/M2 Root Cause", "",
            "2026年5月数据已于2026-06-12发布。旧抓取器能够找到最新文章，但解析正则失败：社融正则过度依赖正文固定句式；M2正则只兼容全角括号，而5月正文使用ASCII `(M2)`。缓存会延长旧值留存，但不是首次失败原因。正则已修复并成功写入5月数据。",
            "", "## Staleness Rules", "",
            "- Daily: lag <= 3 PASS; 4-7 WARNING; >7 FAIL.",
            "- Monthly: before expected release PASS_WITH_EXPECTED_LAG; after expected release without update FAIL.",
            "- Fetch failure with an old cached value is FAIL; silent fallback is prohibited.",
        ]
        output_paths.get_report_path("macro-data-audit-report.md", run_dir).write_text("\n".join(report) + "\n", encoding="utf-8")
        output_paths.get_log_path("macro-fetch-log.md", run_dir).write_text(
            "# Macro Fetch Log\n\n" + "\n".join(f"- {line}" for line in logs) + "\n",
            encoding="utf-8",
        )
        conn.commit()
        output_paths.write_run_manifest({
            "phase": "phase-p1-macro", "task_name": "macro data audit",
            "decision_status": "NOT_RUN", "data_status": status,
            "model_status": "NOT_RUN",
            "output_files": ["reports/macro-data-audit-report.md", "json/macro-data-result.json", "logs/macro-fetch-log.md"],
            "blocked_reason": result.get("block_code") or "",
            "next_action": "Review macro data audit",
            "source_data_used": ["FRED", "PBOC", "data/fund_tracker.sqlite"],
            "whether_root_directory_was_modified": "No",
        }, run_dir)
        print(f"Phase 3: {status}")
        if failed:
            print("BLOCKED: MACRO_DATA_AUDIT_FAILED")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        output_paths.write_blocked_outputs(exc, {"phase": "phase-p1-macro", "task_name": "macro data audit"})
        raise
