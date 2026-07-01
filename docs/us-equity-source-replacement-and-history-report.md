# Asset Allocation Copilot V7 — US Equity Source Replacement and History Report

Generated: 2026-06-19  
Scope: US-equity data foundation and replacement-source research only  
Decision authority: None; this report does not approve a source, rewrite a Score, or release cash

## 13.1 Executive Verdict

```text
RATE_HISTORY_STATUS: LOCAL_RATE_HISTORY_REMEDIATED
NASDAQ_REPLACEMENT_STATUS: MORE_SOURCE_RESEARCH_REQUIRED
SP500_REPLACEMENT_STATUS: CONDITIONAL_CANDIDATES_IDENTIFIED
US_MODEL_STATUS: REBUILD_REQUIRED / NOT CHANGED
DYNAMIC_CASH_POOL_STATUS: FREEZE
```

The P0 local rate-history deficiency is remediated: DFII5, DFII10, and DFF now share 198 monthly observations from 2010-01 through 2026-06. All were built from official FRED daily observations using one aggregation rule, `month_end_last_valid_value`.

The valuation-source P0 is not remediated. No Nasdaq candidate currently combines correct index object, disclosed PE methodology, reproducible long history, stable programmatic access, and clear licensing. The S&P DJI earnings workbook is the strongest S&P research candidate, but the endpoint returned HTTP 403 and its field mapping and revision behavior are not yet locally validated. No candidate is approved. Existing US proxy approvals remain `PENDING_PROXY_REVIEW`, and the Dynamic Cash Pool remains `FREEZE`.

## 13.2 Rate History Backfill

Source: [Federal Reserve Bank of St. Louis FRED](https://fred.stlouisfed.org/)  
Series: [DFII5](https://fred.stlouisfed.org/series/DFII5), [DFII10](https://fred.stlouisfed.org/series/DFII10), [DFF](https://fred.stlouisfed.org/series/DFF)  
Daily-to-monthly method: `month_end_last_valid_value` for every series; no monthly averages or mixed methods.

| Series | Start | End | Monthly samples | Missing months | Duplicate months | Non-numeric daily rows excluded | Stale tail |
|---|---|---:|---:|---:|---:|---:|---|
| DFII5 / 5Y TIPS | 2010-01 | 2026-06 | 198 | 0 | 0 | 176 | No |
| DFII10 / 10Y TIPS | 2010-01 | 2026-06 | 198 | 0 | 0 | 176 | No |
| DFF / Fed Funds | 2010-01 | 2026-06 | 198 | 0 | 0 | 0 | No |

Aligned sample: 198 months; first common month 2010-01; last common month 2026-06; missing rate for each aligned series 0.00%. The CSV preserves both the month label and the actual daily `source_date` selected as that month's last valid observation.

### Rate-factor diagnostics

| Window | Pair | Pearson | Spearman | N | Redundancy |
|---|---|---:|---:|---:|---|
| Full, 2010-01 onward | 5Y TIPS vs 10Y TIPS | 0.9503 | 0.9233 | 198 | HIGH_REDUNDANCY |
| Full, 2010-01 onward | 5Y TIPS vs Fed Funds | 0.8567 | 0.7974 | 198 | MODERATE_REDUNDANCY |
| Full, 2010-01 onward | 10Y TIPS vs Fed Funds | 0.7804 | 0.6849 | 198 | MODERATE_REDUNDANCY |
| Last 10 years | 5Y TIPS vs 10Y TIPS | 0.9751 | 0.9619 | 120 | HIGH_REDUNDANCY |
| Last 10 years | 5Y TIPS vs Fed Funds | 0.9140 | 0.9478 | 120 | HIGH_REDUNDANCY |
| Last 10 years | 10Y TIPS vs Fed Funds | 0.9138 | 0.9004 | 120 | HIGH_REDUNDANCY |
| Last 5 years | 5Y TIPS vs 10Y TIPS | 0.9737 | 0.7653 | 60 | MODERATE_REDUNDANCY |
| Last 5 years | 5Y TIPS vs Fed Funds | 0.9456 | 0.8155 | 60 | HIGH_REDUNDANCY |
| Last 5 years | 10Y TIPS vs Fed Funds | 0.9128 | 0.5907 | 60 | MODERATE_REDUNDANCY |

Interpretation: real yields and policy rates are not independent deductions in all regimes. The 10-year window shows high rank redundancy for all three pairs. This is diagnostic evidence only; it does not select a new model window or authorize a weight change.

## 13.3 Nasdaq Candidate Sources

| Candidate | Object and metric | History / access evidence | Reproducibility and methodology risk | Verdict |
|---|---|---|---|---|
| [Nasdaq Global Index Watch — NDX](https://indexes.nasdaqomx.com/Index/Overview/NDX) | Correct Nasdaq-100 index; public overview does not expose a validated historical PE series or public PE formula | HTTP 200; overview accessible; deeper data appear licensed; no audited history obtained | Correct object, but trailing/forward basis, earnings aggregation, loss-company policy, revision policy, and redistribution rights remain unresolved | CONDITIONAL_CANDIDATE |
| [Invesco QQQ official fund page](https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=Investor&ticker=QQQ) | QQQ ETF portfolio statistic, not Nasdaq-100 index PE | HTTP 406 in current probe; no public historical PE download validated | Official ETF sponsor but wrong model object; current fund statistic cannot be spliced into index PE history | DISPLAY_ONLY |
| [World PE Ratio Nasdaq 100](https://worldperatio.com/index/nasdaq-100/) | Provider-calculated QQQ proxy trailing PE | HTTP 200; 434 monthly points, 1990-05 through 2026-06, embedded in HTML JavaScript | Arithmetic is reproducible; constituent aggregation, earnings basis, loss policy, revisions, and license remain insufficiently disclosed | DISPLAY_ONLY |

Object-control conclusion: Nasdaq-100 index PE, QQQ ETF PE, Nasdaq Composite PE, and provider synthetic PE are distinct data objects. This audit found no basis to substitute one for another. No Nasdaq candidate is ready for approval.

## 13.4 S&P500 Candidate Sources

| Candidate | Object and metric | History / access evidence | Reproducibility and methodology risk | Verdict |
|---|---|---|---|---|
| [S&P DJI S&P 500 official index page](https://www.spglobal.com/spdji/en/indices/equity/sp-500/) | Correct S&P 500 object; exact public PE basis not captured | HTTP 403 in current probe; no local historical series | Correct provider/object, but automated history and exact trailing/forward basis are unavailable in this run | CONDITIONAL_CANDIDATE |
| [S&P DJI S&P 500 Earnings and Estimate Report](https://www.spglobal.com/spdji/en/documents/additional-material/sp-500-eps-est.xlsx) | Official operating and as-reported earnings/estimate workbook; fields must remain separate | HTTP 403; workbook not retrieved, mapped, or version-diffed | Best structural candidate if access is restored. Actual vs estimate, operating vs as-reported, and revision snapshots require explicit mapping | CONDITIONAL_CANDIDATE |
| [Robert Shiller Yale data](https://www.econ.yale.edu/~shiller/data/ie_data.xls) | S&P Composite/predecessor long history and CAPE inputs, not exact modern S&P 500 throughout | Endpoint unreachable in this environment | Strong historical depth, but CAPE, trailing PE, and modern official index PE are not interchangeable; index continuity risk is material | CONDITIONAL_CANDIDATE |
| [Multpl S&P 500 PE by Month](https://www.multpl.com/s-p-500-pe-ratio/table/by-month) | Third-party S&P 500 trailing as-reported PE with recent estimates | HTTP 200; 1,866 monthly points, 1871-01 through 2026-06 | Arithmetic is reproducible; third-party governance, estimate revisions, license, and full negative-earnings policy are insufficient | DISPLAY_ONLY |

Metric-control conclusion: trailing PE, forward PE, Shiller CAPE, as-reported earnings PE, and operating earnings PE must remain separate series with separate lineage. The official earnings workbook is the best current S&P research candidate, not an approved input.

## 13.5 Source Scoring

Weights: methodology transparency 20, object correctness 15, historical depth 15, reproducibility 15, revision transparency 10, access stability 10, automation suitability 10, licensing clarity 5. Grades: A 85–100, B 70–84, C 55–69, D below 55. A high score would still not constitute approval.

| Market | Source | Score | Grade | Governance verdict |
|---|---|---:|---|---|
| Nasdaq | Nasdaq Global Index Watch — NDX | 49 | D | CONDITIONAL_CANDIDATE |
| Nasdaq | Invesco QQQ official fund page | 24 | D | DISPLAY_ONLY |
| Nasdaq | World PE Ratio Nasdaq 100 | 41 | D | DISPLAY_ONLY |
| S&P 500 | S&P DJI official index page | 48 | D | CONDITIONAL_CANDIDATE |
| S&P 500 | S&P DJI Earnings and Estimate Report | 69 | C | CONDITIONAL_CANDIDATE |
| S&P 500 | Robert Shiller Yale data | 68 | C | CONDITIONAL_CANDIDATE |
| S&P 500 | Multpl S&P 500 PE by Month | 64 | C | DISPLAY_ONLY |

The detailed per-dimension scores and all mandatory methodology fields are in the candidate CSVs. No row uses an approval verdict.

## 13.6 Window Sensitivity

Percentile formula: inclusive empirical rank, `count(value <= current) / N × 100`. Each window ends at the candidate series' latest observation. N/A means that a consistent, locally auditable history was not obtained; it is not treated as a neutral percentile.

| Source | Current | 5Y | 10Y | 15Y | 20Y | Full history | Max spread | Classification |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Nasdaq official NDX | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Invesco QQQ | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| World PE Ratio QQQ proxy | 32.9504 | 71.67% | 82.50% | 88.33% | 90.83% | 75.81% | 19.16 pp | MODERATE |
| S&P DJI official page | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| S&P DJI earnings workbook | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Yale Shiller | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Multpl S&P 500 | 31.93 | 100.00% | 93.33% | 95.56% | 91.67% | 97.64% | 8.33 pp | STABLE |

The legacy-source comparison confirms that a 60-month rank is only `5y_percentile`; it is not a long-term percentile. Full-history ranks are not automatically superior because index constituents, accounting standards, earnings definitions, loss-company treatment, valuation structure, and technology weights have changed materially. No new model design is inferred from the 60-month result.

## 13.7 Stability Infrastructure

`source_attempt_log.csv` is append-only and records successful and failed probes with attempt UUID, timestamp, HTTP status, latency field, row count, latest observation, schema signature, and error details. This run recorded 10 attempts: six successes and four failures. The observed success rate is not comparable across heterogeneous sources and is not an approval statistic.

`source_revision_log.csv` is append-only. It is initialized with the required schema and currently contains no detected revisions because this is the first persisted snapshot. The helper records any future change for the same source, indicator, and observation date with `revision_reason=UNKNOWN` when the provider gives no reason.

All candidates remain `source_stability=INSUFFICIENT_EVIDENCE`. Future eligibility requires at least 20 scheduled attempts, success rate at least 95%, at least five consecutive successes, no manual intervention, no unexplained schema changes, and normal advancement of the latest date.

## 13.8 Legacy Source Status

| Source | Legacy | Recommended role | Approval status | History retained | Current model governance |
|---|---|---|---|---|---|
| World PE Ratio Nasdaq 100 | true | DISPLAY_ONLY | PENDING_PROXY_REVIEW | Yes, 434 source observations and existing local model rows | Must not be the sole automatic model input |
| Multpl S&P 500 | true | DISPLAY_ONLY | PENDING_PROXY_REVIEW | Yes, 1,866 source observations and existing local model rows | Must not be the sole automatic model input |

No legacy row or lineage was deleted. The source-approval registry was not changed and contains no user approval for either US proxy.

## 13.9 Corrected Redundancy Conclusion

The prior valuation conclusion is corrected to `CONFIRMED_HIGH_REDUNDANCY`: Nasdaq and S&P percentile signals had Pearson 0.8769 and Spearman 0.8771 over the common 60-month sample. Retaining both at material weights creates `POSSIBLE_DOUBLE_COUNT`; high correlation alone does not prove identical information.

The new 198-month rate history establishes that 5Y TIPS, 10Y TIPS, and Fed Funds are also highly redundant in some windows. Their simultaneous use therefore creates `POSSIBLE_DOUBLE_COUNT` of the rate regime. This evidence is not a model rewrite instruction.

`LOCAL_RATE_HISTORY_REMEDIATED` replaces the prior P0 blocker `LOCAL_RATE_HISTORY_INSUFFICIENT`. Valuation-source governance remains a P0 barrier to automated execution.

## 13.10 Test Results

```text
command: python3 -m unittest discover -s tests -v
total: 73
passed: 73
failed: 0
skipped: 0
duration: 0.157s
```

Coverage includes month-end last-valid aggregation, at least 10 years of common monthly rate history, duplicate and freshness controls, mandatory candidate fields, distinct 5/10/15/20/full labels, append-only success/failure attempts, revision detection, legacy retention, pending approval, FREEZE protection, and A500/Gold/strategic-target/fixed-investment regression protection.

## 13.11 Final Candidate Verdicts

| Candidate | Verdict | Institutional rationale |
|---|---|---|
| Nasdaq Global Index Watch — NDX | CONDITIONAL_CANDIDATE | Correct object, but no validated public historical PE, methodology, or automation license |
| Invesco QQQ official fund page | DISPLAY_ONLY | Official ETF data but wrong object and no reproducible history |
| World PE Ratio Nasdaq 100 | DISPLAY_ONLY | Long history but QQQ proxy and insufficient methodology/governance |
| S&P DJI official index page | CONDITIONAL_CANDIDATE | Correct object/provider but automated history inaccessible |
| S&P DJI Earnings and Estimate Report | CONDITIONAL_CANDIDATE | Best S&P research candidate; access, field mapping, and revision snapshots unresolved |
| Robert Shiller Yale data | CONDITIONAL_CANDIDATE | Long authoritative history but object/metric continuity differs from modern S&P 500 PE |
| Multpl S&P 500 PE by Month | DISPLAY_ONLY | Reproducible table but third-party estimate/revision and governance limitations |

There are no `APPROVE`, `APPROVED_PROXY_PASS`, or equivalent decisions in this report.

## 13.12 Next-Step Recommendation

```text
MORE_SOURCE_RESEARCH_REQUIRED
```

Next work should first obtain a reproducible Nasdaq-100 index-level valuation history with disclosed methodology and usable rights. In parallel, restore access to the S&P DJI earnings workbook, map operating/as-reported and actual/estimate fields without mixing them, and begin scheduled attempt/revision snapshots. Model design is premature until those source questions are resolved; 60 months must not be used as the direct design basis.

## 13.13 Change Log

- Added official FRED monthly audit exports for DFII5, DFII10, and DFF.
- Added a 198-month aligned rate matrix and rate-history diagnostics.
- Added Nasdaq and S&P candidate inventories with mandatory methodology fields, weighted scoring, governance verdicts, and window-sensitivity evidence.
- Added append-only attempt and revision log infrastructure.
- Added regression and data-integrity tests.
- Corrected the prior double-count overstatement to `CONFIRMED_HIGH_REDUNDANCY` plus `POSSIBLE_DOUBLE_COUNT`.
- Retained legacy sources and their `PENDING_PROXY_REVIEW` status.
- Did not modify US Score formula/weights, A500, Gold, strategic targets, fixed investment, historical execution, Dynamic Cash Pool rules, or source approvals.
