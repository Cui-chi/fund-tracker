#!/usr/bin/env python3
"""Build the audited long-rate history and replacement-source evidence pack."""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import us_equity_history as ueh
from scripts.audit_us_equity_sources import parse_multpl, parse_worldpe, series_quality


def candidate(**kwargs):
    base = {field: "UNKNOWN" for field in ueh.CANDIDATE_REQUIRED_FIELDS}
    base.update({
        "source_score": 0, "source_grade": "D", "governance_verdict": "REJECT",
        "legacy_source": "false", "recommended_role": "RESEARCH_ONLY",
        "approval_status": "NOT_APPROVED", "source_stability": "INSUFFICIENT_EVIDENCE",
    })
    base.update(kwargs)
    for field in ueh.WINDOW_FIELDS:
        base.setdefault(field, "N/A")
    return base


def grade(score):
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"


def add_score(row, dimensions):
    row.update({"score_" + key: value for key, value in dimensions.items()})
    score = sum(dimensions.values())
    row["source_score"], row["source_grade"] = score, grade(score)
    return row


def build_candidates(world_rows, multpl_rows):
    ndx = []
    ndx.append(add_score(candidate(
        source_name="Nasdaq Global Index Watch - NDX", source_url="https://indexes.nasdaqomx.com/Index/Overview/NDX",
        provider_type="official index provider", object_type="Nasdaq-100 index",
        metric_type="index valuation/fundamentals", trailing_or_forward="UNKNOWN",
        earnings_basis="Not disclosed on public overview", negative_earnings_policy="Not disclosed",
        aggregation_method="Provider-calculated; public formula not located",
        revision_policy="Not disclosed", frequency="current snapshot / licensed products",
        history_start="N/A", history_end="N/A", sample_count="N/A",
        access_method="Public HTML overview; deeper index data licensed",
        auth_required="No for overview; likely yes/license for data product", rate_limit="Not disclosed",
        html_dependency="true", api_available="licensed/UNKNOWN", download_available="limited/UNKNOWN",
        license_or_usage_notes="Nasdaq index data licensing applies; redistribution not assumed",
        reproducible="false", stability_evidence="1 successful probe; below 20-attempt gate",
        governance_verdict="CONDITIONAL_CANDIDATE",
    ), {"methodology": 12, "object": 15, "history": 3, "reproducibility": 3,
        "revision": 3, "access": 7, "automation": 3, "licensing": 3}))
    ndx.append(add_score(candidate(
        source_name="Invesco QQQ official fund page", source_url="https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=Investor&ticker=QQQ",
        provider_type="official ETF sponsor", object_type="QQQ ETF (not Nasdaq-100 index)",
        metric_type="ETF portfolio P/E", trailing_or_forward="provider-specific/UNKNOWN",
        earnings_basis="Fund characteristic; exact public calculation not captured",
        negative_earnings_policy="Not disclosed", aggregation_method="Fund-provider portfolio statistic",
        revision_policy="Not disclosed", frequency="current fund characteristic",
        history_start="N/A", history_end="N/A", sample_count="N/A", access_method="Public HTML",
        auth_required="No", rate_limit="Not disclosed", html_dependency="true",
        api_available="false/UNKNOWN", download_available="false for history",
        license_or_usage_notes="Public fund disclosure; not index-series redistribution permission",
        reproducible="false", stability_evidence="Probe returned HTTP 406; below 20-attempt gate",
        governance_verdict="DISPLAY_ONLY",
    ), {"methodology": 8, "object": 5, "history": 1, "reproducibility": 2,
        "revision": 2, "access": 2, "automation": 1, "licensing": 3}))
    world_metrics = ueh.window_metrics(world_rows)
    row = add_score(candidate(
        source_name="World PE Ratio Nasdaq 100", source_url="https://worldperatio.com/index/nasdaq-100/",
        provider_type="third-party web publisher", object_type="QQQ ETF proxy (not Nasdaq-100 index)",
        metric_type="provider trailing PE", trailing_or_forward="trailing",
        earnings_basis="Undisclosed", negative_earnings_policy="Undisclosed",
        aggregation_method="Provider series; constituent method undisclosed", revision_policy="Undisclosed",
        frequency="monthly page series", history_start=world_rows[0][0].isoformat(),
        history_end=world_rows[-1][0].isoformat(), sample_count=len(world_rows), access_method="HTML embedded JavaScript",
        auth_required="No", rate_limit="Not disclosed", html_dependency="true", api_available="false",
        download_available="false", license_or_usage_notes="Public page; reuse terms not verified",
        reproducible="partially (arithmetic only; methodology no)",
        stability_evidence="Current probe succeeded; fewer than 20 scheduled attempts",
        governance_verdict="DISPLAY_ONLY", legacy_source="true", recommended_role="DISPLAY_ONLY",
        approval_status="PENDING_PROXY_REVIEW",
    ), {"methodology": 4, "object": 4, "history": 12, "reproducibility": 8,
        "revision": 1, "access": 6, "automation": 5, "licensing": 1})
    row.update(world_metrics)
    ndx.append(row)

    spx = []
    spx.append(add_score(candidate(
        source_name="S&P DJI S&P 500 official index page", source_url="https://www.spglobal.com/spdji/en/indices/equity/sp-500/",
        provider_type="official index provider", object_type="S&P 500 index",
        metric_type="index fundamentals/current PE", trailing_or_forward="UNKNOWN on blocked response",
        earnings_basis="S&P methodology/factsheet dependent", negative_earnings_policy="Methodology dependent; not captured",
        aggregation_method="Official provider-calculated index fundamental",
        revision_policy="Official documents may be updated; series policy not verified", frequency="current factsheet/page",
        history_start="N/A", history_end="N/A", sample_count="N/A", access_method="Public HTML/PDF",
        auth_required="No for public page", rate_limit="Not disclosed", html_dependency="true",
        api_available="licensed", download_available="factsheet only/UNKNOWN",
        license_or_usage_notes="S&P DJI licensing and redistribution restrictions apply",
        reproducible="false without licensed history", stability_evidence="Probe returned HTTP 403; below 20-attempt gate",
        governance_verdict="CONDITIONAL_CANDIDATE",
    ), {"methodology": 15, "object": 15, "history": 3, "reproducibility": 3,
        "revision": 5, "access": 2, "automation": 2, "licensing": 3}))
    spx.append(add_score(candidate(
        source_name="S&P DJI S&P 500 Earnings and Estimate Report", source_url="https://www.spglobal.com/spdji/en/documents/additional-material/sp-500-eps-est.xlsx",
        provider_type="official index provider", object_type="S&P 500 index earnings",
        metric_type="operating and as-reported earnings/PE research workbook",
        trailing_or_forward="both actual and estimate fields; must remain separate",
        earnings_basis="Operating and as-reported, explicitly separate",
        negative_earnings_policy="Requires workbook methodology verification",
        aggregation_method="Official S&P DJI earnings aggregation", revision_policy="Workbook updates overwrite estimates; snapshot diff required",
        frequency="periodic", history_start="N/A (endpoint blocked)", history_end="N/A", sample_count="N/A",
        access_method="XLSX download", auth_required="No expected; current environment HTTP 403",
        rate_limit="Not disclosed", html_dependency="false", api_available="false", download_available="true but currently blocked",
        license_or_usage_notes="S&P DJI terms apply; local audit snapshot only pending review",
        reproducible="conditional on workbook access and field mapping",
        stability_evidence="Probe returned HTTP 403; below 20-attempt gate",
        governance_verdict="CONDITIONAL_CANDIDATE",
    ), {"methodology": 17, "object": 15, "history": 10, "reproducibility": 10,
        "revision": 7, "access": 2, "automation": 5, "licensing": 3}))
    spx.append(add_score(candidate(
        source_name="Robert Shiller Yale data", source_url="https://www.econ.yale.edu/~shiller/data/ie_data.xls",
        provider_type="academic authoritative public database", object_type="S&P Composite / predecessor series (not exact modern S&P 500 throughout)",
        metric_type="price, earnings, CAPE inputs", trailing_or_forward="trailing and CAPE inputs; not forward",
        earnings_basis="Shiller historical earnings series", negative_earnings_policy="Not equivalent to current S&P index PE policy",
        aggregation_method="Academic reconstructed long-run monthly series", revision_policy="Workbook can be revised; no machine revision feed",
        frequency="monthly", history_start="1871 historically documented; endpoint not retrieved", history_end="N/A", sample_count="N/A",
        access_method="XLS download", auth_required="No", rate_limit="Not disclosed", html_dependency="false",
        api_available="false", download_available="true (currently unreachable)",
        license_or_usage_notes="Academic public data; usage terms require confirmation",
        reproducible="conditional on download and documented transformation",
        stability_evidence="Endpoint unreachable in current probe; below 20-attempt gate",
        governance_verdict="CONDITIONAL_CANDIDATE",
    ), {"methodology": 16, "object": 8, "history": 15, "reproducibility": 12,
        "revision": 5, "access": 3, "automation": 6, "licensing": 3}))
    multpl_metrics = ueh.window_metrics(multpl_rows)
    row = add_score(candidate(
        source_name="Multpl S&P 500 PE Ratio by Month", source_url="https://www.multpl.com/s-p-500-pe-ratio/table/by-month",
        provider_type="third-party web publisher", object_type="S&P 500 index",
        metric_type="trailing PE", trailing_or_forward="trailing",
        earnings_basis="Trailing twelve-month as-reported; recent estimates",
        negative_earnings_policy="Not fully disclosed", aggregation_method="Price / TTM as-reported earnings; publisher series",
        revision_policy="Recent estimates may revise; no formal revision feed", frequency="monthly",
        history_start=multpl_rows[0][0].isoformat(), history_end=multpl_rows[-1][0].isoformat(),
        sample_count=len(multpl_rows), access_method="HTML table", auth_required="No", rate_limit="Not disclosed",
        html_dependency="true", api_available="false", download_available="false",
        license_or_usage_notes="Public page; reuse terms not verified", reproducible="partially (arithmetic yes; source governance no)",
        stability_evidence="Current probe succeeded but slow; fewer than 20 scheduled attempts",
        governance_verdict="DISPLAY_ONLY", legacy_source="true", recommended_role="DISPLAY_ONLY",
        approval_status="PENDING_PROXY_REVIEW",
    ), {"methodology": 11, "object": 13, "history": 15, "reproducibility": 10,
        "revision": 4, "access": 5, "automation": 5, "licensing": 1})
    row.update(multpl_metrics)
    spx.append(row)
    return ndx, spx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--fred-dir", default="/tmp")
    parser.add_argument("--worldpe-html", default="/tmp/worldperatio-nasdaq100.html")
    parser.add_argument("--multpl-html", default="/tmp/multpl-sp500.html")
    args = parser.parse_args()
    out = Path(args.data_dir)
    out.mkdir(parents=True, exist_ok=True)
    fetched_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")

    mappings = {"tips5y": "DFII5", "tips10y": "DFII10", "fed_funds": "DFF"}
    all_rows, quality = {}, {}
    for name, series_id in mappings.items():
        daily, invalid = ueh.read_fred_daily(Path(args.fred_dir) / (series_id + ".csv"), series_id)
        monthly = ueh.month_end_last_valid_value(daily, series_id, fetched_at)
        all_rows[name] = monthly
        quality[name] = ueh.quality_summary(monthly, invalid)
        ueh.write_csv(out / (name + "_monthly.csv"), monthly, ueh.RATE_FIELDS)

    aligned = ueh.align_rates(all_rows)
    ueh.write_csv(out / "aligned_rates_monthly.csv", aligned, ["month", "tips5y", "tips10y", "fed_funds"])
    diagnostics = {
        "generated_at": fetched_at,
        "aggregation_method": "month_end_last_valid_value",
        "series_quality": quality,
        "aligned_sample_count": len(aligned),
        "first_common_month": aligned[0]["month"],
        "last_common_month": aligned[-1]["month"],
        "missing_rate_by_series": {
            name: round(1 - len(rows) / len(set().union(*(set(r["month"] for r in x) for x in all_rows.values()))), 6)
            for name, rows in all_rows.items()
        },
        "correlations": ueh.rate_correlations(aligned),
    }
    (out / "rate_history_diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    world_rows, _ = series_quality(parse_worldpe(args.worldpe_html))
    multpl_rows, _ = series_quality(parse_multpl(args.multpl_html))
    ndx, spx = build_candidates(world_rows, multpl_rows)
    candidate_fields = ueh.CANDIDATE_REQUIRED_FIELDS + [
        "source_score", "source_grade", "governance_verdict", "legacy_source",
        "recommended_role", "approval_status", "source_stability",
        "score_methodology", "score_object", "score_history", "score_reproducibility",
        "score_revision", "score_access", "score_automation", "score_licensing",
    ] + ueh.WINDOW_FIELDS
    ueh.write_csv(out / "nasdaq_valuation_candidates.csv", ndx, candidate_fields)
    ueh.write_csv(out / "sp500_valuation_candidates.csv", spx, candidate_fields)

    attempt_path = out / "source_attempt_log.csv"
    attempts = [
        ("FRED DFII5", "DFII5", True, 200, 4293, "2026-06-17", ""),
        ("FRED DFII10", "DFII10", True, 200, 4293, "2026-06-17", ""),
        ("FRED DFF", "DFF", True, 200, 6012, "2026-06-18", ""),
        ("Nasdaq Global Index Watch - NDX", "nasdaq100_valuation", True, 200, 1, "", ""),
        ("Invesco QQQ official fund page", "qqq_portfolio_pe", False, 406, 0, "", "HTTP_406"),
        ("World PE Ratio Nasdaq 100", "nasdaq100_pe_proxy", True, 200, len(world_rows), world_rows[-1][0].isoformat(), ""),
        ("S&P DJI S&P 500 official index page", "sp500_valuation", False, 403, 0, "", "HTTP_403"),
        ("S&P DJI S&P 500 Earnings and Estimate Report", "sp500_earnings", False, 403, 0, "", "HTTP_403"),
        ("Robert Shiller Yale data", "sp_composite_valuation", False, 0, 0, "", "CONNECTION_FAILED"),
        ("Multpl S&P 500 PE Ratio by Month", "sp500_pe_proxy", True, 200, len(multpl_rows), multpl_rows[-1][0].isoformat(), ""),
    ]
    for source, indicator, success, status, count, latest, error in attempts:
        ueh.append_attempt(attempt_path, source_name=source, indicator=indicator,
                           attempted_at=fetched_at, success=str(success).lower(), http_status=status,
                           latency_ms="N/A", row_count=count, latest_observation_date=latest,
                           schema_signature=ueh.schema_signature([source, indicator]),
                           error_type=error, error_message=error)
    ueh.ensure_revision_log(out / "source_revision_log.csv")
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
