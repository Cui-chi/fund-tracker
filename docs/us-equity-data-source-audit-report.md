# Asset Allocation Copilot V7 — US Equity Data Source Audit Report

## 13.1 Executive Verdict

```text
US_EQUITY_DATA_STATUS: FAIL
US_EQUITY_MODEL_STATUS: REBUILD_REQUIRED / REFERENCE_ONLY
DYNAMIC_CASH_POOL_STATUS: FREEZE
```

The current 60-month percentile calculations are reproducible, but the source and model chain is not approved for automatic allocation. The Nasdaq-100 input is a third-party **QQQ ETF proxy** whose aggregation method and treatment of loss-making constituents are not disclosed. The S&P 500 input is a third-party index series with a clearer trailing-earnings definition, but it depends on an HTML table, contains estimated recent observations, has a recorded timeout, and lacks an append-only stability history.

The two valuation percentiles are highly redundant: over the common 60-month sample, Pearson correlation is **0.8769** and Spearman correlation is **0.8771**. This is **CONFIRMED_HIGH_REDUNDANCY**; retaining both at material weights creates **POSSIBLE_DOUBLE_COUNT**, but the label does not by itself prove identical economic information. The additional inverse scoring of 5Y TIPS, 10Y TIPS, and Fed Funds also creates **POSSIBLE_DOUBLE_COUNT** of the high-rate regime.

`LOCAL_RATE_HISTORY_REMEDIATED`: the official FRED monthly audit history now contains a 198-month common sample from 2010-01 through 2026-06. This remediates the prior local-history blocker but does not authorize a model or weight change.

Final governance conclusion:

- Nasdaq100 PE source: **REPLACE** for automatic scoring; retain only as **DISPLAY_ONLY** while replacement is pending.
- S&P500 PE source: **REPLACE** for automatic scoring; retain only as **DISPLAY_ONLY** while replacement is pending.
- US equity model: **REBUILD_US_VALUATION_MODEL** before any request to approve sources or lift `FREEZE`.
- Source approvals remain `PENDING_PROXY_REVIEW`. No approval or execution status was changed.

## 13.2 Source Inventory

| Indicator | Source | Object | Metric Type | Frequency | Sample | Stability | Approval |
|---|---|---|---|---|---:|---|---|
| `nasdaq100_pe_percentile` | [World PE Ratio Nasdaq 100](https://worldperatio.com/index/nasdaq-100/) | QQQ ETF proxy for Nasdaq-100 | Trailing PE, provider-calculated | Monthly model sample; page estimate can update intra-month | 60 model / 434 source | INSUFFICIENT_EVIDENCE; 5/5 current probes succeeded | PENDING_PROXY_REVIEW |
| `sp500_pe_percentile` | [Multpl S&P 500 PE Ratio by Month](https://www.multpl.com/s-p-500-pe-ratio/table/by-month) | S&P 500 index | Trailing 12-month as-reported PE | Monthly | 60 model / 1,866 source | INSUFFICIENT_EVIDENCE; recorded timeout; 5/5 current probes succeeded | PENDING_PROXY_REVIEW |
| `tips5y` | FRED `DFII5` | 5Y Treasury inflation-indexed security | Real yield | Daily | Local history insufficient for 60-month correlation | Official source; not re-audited in this task | OFFICIAL_PASS |
| `tips10y` | FRED `DFII10` | 10Y Treasury inflation-indexed security | Real yield | Daily | Local history insufficient for 60-month correlation | Official source; not re-audited in this task | OFFICIAL_PASS |
| `fed_funds` | FRED `DFF` | Effective federal funds rate | Policy rate | Daily | Local history insufficient for 60-month correlation | Official source; not re-audited in this task | OFFICIAL_PASS |

## Impact Inventory Before Changes

| File | Function/Class | Current Logic | Risk | Needs Change |
|---|---|---|---|---|
| `fund_tracker.py` | `fetch_worldpe_nasdaq100_history` | Parses a JavaScript array, labels it trailing PE, and notes that it is calculated on QQQ | ETF proxy is presented as Nasdaq-100; exact aggregation is opaque | Yes — definition and methodology disclosure only |
| `fund_tracker.py` | `fetch_multpl_sp500_history` | Parses `table#datatable`; marks recent estimates; stores trailing as-reported PE | HTML dependency, estimated observations, and timeout exposure | No fetch redesign in this audit; disclose and recommend replacement |
| `fund_tracker.py` | `recent_monthly_rows` | Retains only the latest 60 calendar months | Five-year rank can be mistaken for long history | Yes — explicit `recent_5y_percentile` label |
| `fund_tracker.py` | `latest_us_valuation` | Inclusive empirical rank over all persisted rows | Algorithm is reproducible, but persisted window is capped at 60 months | Yes — expose window name and length |
| `fund_tracker.py` | `generate_copilot_snapshot` | 60% Nasdaq percentile score + 40% S&P percentile score; valuation 60% of US Score | Correlated valuation signals can be double counted | Audit only; do not alter weights in this task |
| `fund_tracker.py` | `generate_copilot_snapshot` | US liquidity = 40% 5Y TIPS + 40% 10Y TIPS + 20% Fed Funds; liquidity 40% of US Score | High-rate regime can be penalized repeatedly | Audit only; recommend factor redesign |
| `fund_tracker.py` | `build_data_quality_inputs` | Both PE series are proxies, Used In Score, methodology previously treated as known | Nasdaq underlying calculation is not sufficiently disclosed | Yes — Nasdaq methodology flag downgraded; remains blocked |
| `model_risk.py` | `evaluate_indicator_quality` / `calculate_asset_level_status` | Unknown method, stale data, failed source, or pending proxy blocks execution | Correct fail-closed behavior | No |
| `source_approval.py` / `data/approved-sources.json` | approval registry | Both sources remain pending until explicit user approval | Auto-approval would bypass governance | No |
| `config.json` | US funds and 60/40 strategic target | Two Nasdaq funds plus one active global equity fund | S&P proxy weight is not based on portfolio look-through | No configuration change; audit only |

## 13.3 Methodology — The Two PE Labels Are Only Partially Comparable

### Nasdaq-100 Current Indicator

```text
indicator_name: nasdaq100_pe_percentile
source_name: World PE Ratio Nasdaq 100
source_url: https://worldperatio.com/index/nasdaq-100/
metric_type: trailing_pe
underlying_object: QQQ ETF proxy for Nasdaq-100
calculation_method: provider-calculated QQQ trailing P/E; constituent aggregation, ETF cash effects, loss-company treatment, and earnings normalization are not disclosed
update_frequency: page estimate can update during the month; model stores monthly observations
publication_delay: not contractually disclosed
```

The page explicitly states that its PE is calculated on the **QQQ ETF**, whose benchmark is Nasdaq-100, and separately labels the statistics as trailing P/E. It does not provide enough methodology to reproduce the PE from constituents. The historical sequence and its percentile can be reproduced from the published array; the underlying PE construction cannot.

### S&P 500 Current Indicator

```text
indicator_name: sp500_pe_percentile
source_name: Multpl S&P 500 PE Ratio by Month
source_url: https://www.multpl.com/s-p-500-pe-ratio/table/by-month
metric_type: trailing_pe
underlying_object: S&P 500 index
calculation_method: price divided by trailing twelve-month as-reported earnings; historical source attributed to Robert Shiller; recent values can be estimates
update_frequency: monthly table
publication_delay: recent observations can remain estimated until reported earnings are available
```

Multpl discloses the earnings basis more clearly than World PE Ratio. It is still not an official S&P DJI series, and nine of the 60 locally stored observations are marked estimated. The current page headline can differ from the month-start value used by the model: the model stores the June 1 monthly observation, not an intra-month market-close PE.

### Comparability Verdict

```text
Nasdaq100 PE vs S&P500 PE: PARTIALLY_COMPARABLE
```

Both are labelled trailing PE and stored monthly, but they are not the same object or fully the same methodology. One is an opaque QQQ ETF proxy; the other is an S&P 500 index series based on as-reported earnings. Equal metric labels do not establish denominator, loss-company, estimation, or aggregation equivalence.

## 13.4 Historical Window Audit — Five Years Is Not Long-Term History

| Indicator | Local Start | Local End | Local N | Missing | Duplicates | Extreme Jumps | 5Y Percentile | 10Y Percentile | Full-History Percentile |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| Nasdaq100 / QQQ proxy | 2021-07-01 | 2026-06-01 | 60 | 0 | 0 | 0 | 71.67% | 82.50% | 75.81% |
| S&P 500 / Multpl | 2021-07-01 | 2026-06-01 | 60 | 0 | 0 | 0 | 100.00% | 93.33% | 97.64% |

Source-page coverage is longer than the model window:

- Nasdaq source: 434 monthly points, May 1990–June 2026.
- S&P source: 1,866 monthly points, January 1871–June 2026; two moves above 50% were detected in August 1932 and December 2008 and should be reviewed rather than silently removed.

The five-year percentile is a recent-regime rank. It is sensitive to the post-2021 rate and mega-cap environment and cannot be called a long-term historical percentile. The code and page disclosure now use `recent_5y_percentile`; 10-year and full-history ranks remain audit evidence only and were not inserted into the live score.

## 13.5 Reproducibility — The Rank Is Correct but the Nasdaq PE Method Is Not Reproducible

Current model algorithm:

```text
percentile_rank = count(sample_value <= current_value) / sample_size * 100
inclusive/exclusive = inclusive
ties = every value equal to current is included in the numerator
rolling = no inside latest_us_valuation; ingestion separately caps persisted source refresh to the most recent 60 months
missing values = not inserted by validated parsers
duplicate dates = rejected during monthly selection
```

| Indicator | Reported Percentile | Recomputed Percentile | Difference | Result |
|---|---:|---:|---:|---|
| Nasdaq100 / QQQ proxy | 71.67% | 71.67% | 0.00 pp | PASS |
| S&P 500 / Multpl | 100.00% | 100.00% | 0.00 pp | PASS |

This PASS applies only to percentile arithmetic. It does not validate the provider's PE construction, source suitability, approval, or long-window interpretation.

## 13.6 Stability — Current Probes Passed but Historical Evidence Is Insufficient

The application stores only the latest attempt/success/error state; it does not maintain an append-only fetch-attempt ledger. Therefore the requested scheduled-run counts and historical success rate cannot be reconstructed.

| Metric | Nasdaq / World PE Ratio | S&P / Multpl |
|---|---|---|
| scheduled_fetch_count | N/A | N/A |
| success_count / failure_count | N/A / N/A | N/A / N/A |
| historical success_rate | N/A | N/A |
| current controlled probes | 5/5 success | 5/5 success |
| average probe latency | 1.851 s | 0.775 s |
| consecutive probe successes | 5 | 5 |
| last_success_at in status table | 2026-06-19 10:04:29 | 2026-06-18 09:11:21 |
| last_failure_at | not retained separately | 2026-06-19 10:13:43 attempt |
| latest recorded failure | none | timeout / curl exit 28 |
| timeout_count | N/A | N/A; at least one current recorded timeout |
| empty_response_count | N/A | N/A |
| schema_change_count | N/A | N/A |
| cookie_dependency in probes | No | No |
| manual intervention required in probes | No | No |
| parser dependency | JavaScript variable `detailPE_data` | HTML `table#datatable` |
| source_stability | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE |

The five successful probes demonstrate current reachability, not operational stability. A source must not be approved without durable attempt-level logging, latency/error classification, and a defined stale-value retention policy.

## 13.7 Double Counting Analysis — Valuation Is Confirmed; Rate Duplication Remains Possible

### Empirical Correlations

| Pair | Pearson | Spearman | N | Result |
|---|---:|---:|---:|---|
| Nasdaq percentile vs S&P percentile | 0.8769 | 0.8771 | 60 | CONFIRMED_HIGH_REDUNDANCY / POSSIBLE_DOUBLE_COUNT |
| Nasdaq percentile vs 10Y TIPS | 0.1370 | 0.1000 | 5 | INSUFFICIENT_SAMPLE |
| S&P percentile vs 10Y TIPS | 0.5458 | 0.9000 | 5 | INSUFFICIENT_SAMPLE |
| Fed Funds vs 10Y TIPS | -0.9159 | -0.6669 | 5 | INSUFFICIENT_SAMPLE |

Rate correlations must not be interpreted from five observations. The signs are unstable at this sample size.

### Factor Decomposition

| Current Component | Weight Within Component | Effective Weight in US Score | Factor |
|---|---:|---:|---|
| Nasdaq PE percentile score | 60% of valuation | 36% | Valuation / growth-heavy equity |
| S&P PE percentile score | 40% of valuation | 24% | Valuation / broad US equity |
| 5Y TIPS inverse score | 40% of liquidity | 16% | Real-rate / duration |
| 10Y TIPS inverse score | 40% of liquidity | 16% | Real-rate / duration |
| Fed Funds inverse score | 20% of liquidity | 8% | Policy rate |

Conclusions:

- **High valuation overlap:** `CONFIRMED_HIGH_REDUNDANCY`; retaining both creates `POSSIBLE_DOUBLE_COUNT`. The two PE ranks are strongly correlated and overlap in mega-cap US exposure.
- **High-rate risk deducted twice:** `POSSIBLE_DOUBLE_COUNT`. 5Y TIPS, 10Y TIPS, and Fed Funds all fall when rates are restrictive; the model has no orthogonalization or aggregate rate-factor cap.
- **Valuation plus rate pressure:** `POSSIBLE_DOUBLE_COUNT`. High real/policy rates can already compress valuation percentiles, but the local overlap sample is insufficient to quantify the incremental penalty.

## 13.8 Portfolio Fit — The S&P Weight Is Not Supported by Look-Through Evidence

Current US-equity holdings total 22,389 yuan:

| Fund | Amount | Share of US Bucket | Known Exposure |
|---|---:|---:|---|
| 建信纳斯达克100指数(QDII)A | 14,624 | 65.32% | Nasdaq-100 |
| 南方纳斯达克100指数发起(QDII)A | 200 | 0.89% | Nasdaq-100 |
| 广发全球精选股票 | 7,565 | 33.79% | Active global equity; current look-through unavailable locally |

Known direct Nasdaq-100 exposure is **66.21%** of the US bucket. Known direct S&P 500 exposure is **0%**. The global fund may hold US and mega-cap stocks, but no current holdings look-through is stored, so its S&P overlap cannot be quantified.

The current 60% Nasdaq / 40% S&P valuation mix is therefore not demonstrably portfolio-weighted. It is directionally close to the known Nasdaq share only by coincidence. Before rebuilding, obtain the global fund's dated holdings, country allocation, and benchmark. A single primary valuation indicator or a holdings-weighted composite is preferable to assuming two proxies are independent.

## 13.9 Failure & Degradation — Fail-Closed Behavior Is Correct

### Nasdaq Source Failure

- asset status: `BLOCKED`
- model status: `REFERENCE_ONLY`
- pool impact: positive-gap US equity blocks the Dynamic Cash Pool; `FREEZE` remains
- stale behavior: the old stored value remains in `pe_history`; no neutral substitution occurs
- disclosure: stale/methodology/approval results remain visible in the quality gate

### S&P500 Source Failure

- asset status: `BLOCKED`
- model status: `REFERENCE_ONLY`
- pool impact: positive-gap US equity blocks the Dynamic Cash Pool; `FREEZE` remains
- stale behavior: the old stored value remains after a failed refresh; monthly freshness uses the expected release date and fails after the configured limit
- disclosure: the recorded timeout remains in `market_update_status`

The fail-closed decision behavior is appropriate. The unresolved control gap is retention governance: there is no explicit maximum age for keeping old PE rows at the storage layer and no append-only attempt history for stability statistics.

## 13.10 Test Results

```text
command: python3 -m unittest discover -s tests -p 'test_*.py'
total: 66
passed: 66
failed: 0
skipped: 0
duration: 0.165 seconds
```

Added coverage confirms source definitions, inclusive percentile reproduction, five-year window labeling, pending-proxy blocking, missing-source fail-closed behavior, correlated-signal detection, and regression protection for the current A500/Gold/strategic-target/fixed-invest baselines.

## 13.11 Final Source Verdicts

### Nasdaq100 PE

```text
verdict: REPLACE
reason: QQQ proxy is explicit, but the provider does not disclose enough aggregation and denominator methodology to reproduce index-level PE; current model also truncates a longer source series to five years
confidence: Low for automatic model use; Medium for display of the published provider series
methodology: FAIL for institutional automatic scoring
reproducibility: percentile PASS; underlying PE construction FAIL
stability: INSUFFICIENT_EVIDENCE
window_quality: recent_5y_percentile is valid as a recent-regime statistic, not as a long-term percentile
recommended_role: DISPLAY_ONLY until replaced by a disclosed, consistent, index-level or licensed series
```

### S&P500 PE

```text
verdict: REPLACE
reason: trailing as-reported definition is clearer and arithmetic is reproducible, but the source is an unofficial HTML table, recent months are estimates, a timeout is recorded, and durable stability evidence is absent
confidence: Medium for display; insufficient for automatic execution
methodology: PARTIAL_PASS
reproducibility: percentile PASS; recent estimate lineage PARTIAL
stability: INSUFFICIENT_EVIDENCE
window_quality: recent_5y_percentile is valid as a recent-regime statistic; 10Y and full-history ranks differ and must remain separately labelled
recommended_role: DISPLAY_ONLY until replaced by an approved source with disclosed revisions and service controls
```

Neither verdict is `APPROVED_PROXY_PASS`. `data/approved-sources.json` remains unchanged.

## 13.12 Model Recommendation

```text
US_EQUITY_MODEL_RECOMMENDATION: REBUILD_US_VALUATION_MODEL
```

Recommended design work, not implemented in this audit:

1. Select one primary valuation definition with a disclosed object, denominator, loss-company policy, revision policy, and at least a 10-year reproducible history.
2. If more than one valuation signal is retained, weight by dated portfolio look-through and apply an overlap test or factor cap; do not treat correlated ranks as independent.
3. Build a single real-rate factor from 5Y/10Y TIPS or justify their separate tenors; treat Fed Funds as a policy overlay with a bounded incremental effect.
4. Preserve `recent_5y_percentile`, `10y_percentile`, and `long_term_percentile` as distinct fields.
5. Add append-only source-attempt logging before any stability approval request.

This recommendation does not alter the live formula or execution state.

## 13.13 Remaining Risks

- The Nasdaq provider's constituent aggregation, negative-earnings treatment, and ETF-versus-index basis remain unknown.
- S&P recent estimated observations can be revised; revision history is not stored.
- Only five aligned monthly rate observations are available locally, so valuation/rate overlap is not statistically assessable.
- The global equity fund has no local holdings look-through, preventing portfolio-weighted proxy validation.
- Fetch-attempt counts, error taxonomy, and long-run success rates are not retained.
- Source pages can change JavaScript or HTML structure without a versioned schema.
- A500 is currently `ACTIVE` in the pre-audit baseline. This audit did not modify its formula, score, release factor, target, or execution semantics.

## 13.14 Codex Change Log

| File | Change | Reason |
|---|---|---|
| `fund_tracker.py` | Added auditable US source definitions; labelled 60-month ranks `recent_5y_percentile`; marked Nasdaq methodology as not fully known | Prevent five-year/long-term ambiguity and reflect QQQ-method opacity in the quality gate |
| `scripts/audit_us_equity_sources.py` | Added read-only percentile, quality, long-window, correlation, and overlap evidence builder | Make audit results reproducible |
| `tests/test_us_equity_source_audit.py` | Added six US-source and regression tests | Enforce definitions, window labels, fail-closed behavior, and no unrelated model drift |
| `docs/us-equity-data-source-audit-report.md` | Added this single audit report | Required institutional review artifact |
| `docs/a500-price-temperature-cleanup-handoff.md` | Appended US-equity audit handoff | Preserve cross-phase governance boundaries |

Explicitly unchanged:

- A500 formula, score logic, release factor, source approval, and UI model semantics;
- Gold formula and score logic;
- strategic targets and ranges;
- fixed investment instructions;
- historical execution records;
- Dynamic Cash Pool decision rules;
- proxy approval registry and `FREEZE` status.
