# NDX Price Temperature V1 Refactor and Validation Report

## Executive Verdict

**Status: PARTIALLY_COMPLETED**  
**Model: UNDER_VALIDATION**  
**Validation Stage: OFFLINE_VALIDATION**  
**Decision / Dynamic Cash Pool: FREEZE**  
**Activation blocking: true**

The formula, no-lookahead replay, amount chain, strict QDII preview state machine, UI and tests are implemented. The model is not eligible for shadow-pass or activation because (1) official DFII10 starts in 2003 and cannot complete the full dot-com release chain, and (2) the balanced formula's VERY_HOT median final factor is 35.2375%, slightly above the required 35% gate. Parameters were not changed to manufacture a pass.

## Formula Definition

- `distance_to_ma500 = ndx_close / ma500 - 1`
- `ma_distance_score = 100 - no_lookahead_percentile(distance_to_ma500)`
- `drawdown_magnitude = abs(min(ndx_close / rolling_252d_high - 1, 0))`
- `drawdown_score = no_lookahead_percentile(drawdown_magnitude)`
- `temperature_score = 0.55 * ma_distance_score + 0.45 * drawdown_score`
- `base_release_factor = clamp(0.25 + 0.75 * score / 100, 0.25, 1.00)`
- DFII10 modifier: `<20%=1.05`, `20%-60%=1.00`, `60%-80%=0.95`, `>=80%=0.85`
- 60-day volatility cap: `<=80%=1.00`, `80%-90%=0.90`, `90%-95%=0.80`, `>95%=0.65`
- `candidate_effective_release_factor = min(rate_adjusted_release_factor, volatility_cap)`

No initial 80% cap, second volatility multiplier or crisis discount was added. PE, S&P 500 PE, DFII5, Fed Funds and breakeven do not enter the formal NDX calculation.

## Why The Formula Is Not Overly Conservative

The balanced base mapping is continuous from 25% to 100%. In 222 eligible complete months its median final release was 53.49%; COOL median was 76.98%, VERY_COOL median 80.00%, and normal 10%-15% correction median 75.64%. All three minimum-release gates passed. Five historical months triggered over-conservative warnings; these remain audit flags and do not silently modify the formula.

## Data Sources And Object Control

- Price: FRED-distributed `NASDAQ100`, object `Nasdaq-100 Index / NDX`, `proxy_status=DIRECT_INDEX_DISTRIBUTOR`.
- Price history: 1986-01-02 through 2026-06-18, 10,196 valid observations.
- Real yield: official FRED `DFII10`, 2003-01-02 through 2026-06-17, 6,120 valid observations.
- QQQ is not used. QDII NAVs and capacities are execution-carrier constraints only.
- Nasdaq100 PE and S&P500 PE remain `DISPLAY_ONLY`, `used_in_score=false`, `used_in_release_factor=false`, `blocking=false`.

## No-Lookahead Method

Each decision date uses only observations at or before that date. Price percentiles use trailing 2,520 valid trading days, with a 1,260-observation minimum. Monthly decisions use the last valid value in that calendar month. June 2026 is marked `PARTIAL_MONTH` and excluded from calibration statistics. DFII10 percentiles use the trailing 120 monthly last-valid observations with a 60-month minimum.

`no_lookahead_check=PASS`. Prefix-stability is covered by an automated test.

## Historical Replay

The replay contains 318 monthly rows from 2000-01 through 2026-06; 222 complete months have a full eligible release chain. Balanced statistics:

| Metric | Result |
|---|---:|
| Average final factor | 55.89% |
| Median final factor | 53.49% |
| Months below 40% | 23.42% |
| Months above 80% | 8.56% |
| COOL average / median | 76.68% / 76.98% |
| VERY_COOL average / median | 77.52% / 80.00% |
| VERY_HOT average / median | 35.19% / 35.24% |
| Normal correction average / median | 74.24% / 75.64% |
| High-volatility average | 65.67% |
| Longest consecutive months below 40% | 9 |
| Temperature tier crossings | 117 |
| Median absolute monthly factor change | 7.72pp |

`ma_drawdown_pearson=0.6236`; `ma_drawdown_spearman=0.6273`. Both are price-position signals and the model therefore does not add a third similar price factor.

## Dot-com Scenario

2000-03 through 2002-12 has 34 complete price-model months. Temperature reached 99.98, but DFII10 did not yet exist. Status: `PRICE_ONLY_DFII10_UNAVAILABLE`; full scenario gate is blocked rather than neutral-filled.

## 2008 Scenario

2007-10 through 2009-06 completed 19 full-chain months; median final factor was 78.40%, with temperature spanning 19.01 to 85.96.

## 2020 Scenario

2020-02 through 2020-05 completed all four months. Median final factor was 65.00%; the volatility cap slowed, but did not cancel, the cold-price signal.

## 2022 Scenario

All 12 months completed. Temperature ranged from 71.67 to 99.98 and median final factor was 79.38%.

## 2023-2024 Scenario

All 24 months completed. High actual rates reduced release cadence through the single DFII10 modifier; median final factor was 48.32%. No DFII5 or Fed Funds duplicate penalty was applied.

## Normal Correction Scenario

Months with a 10%-15% 252-day-high drawdown had a 75.64% median final factor, above the 50% minimum. The model does not require crisis-level drawdowns before increasing release.

## New High Scenario

At a new 252-day high, drawdown magnitude is exactly zero and its score remains near the historical low end. It is not misclassified as cold.

## Sideways Scenario

The continuous base mapping prevents discrete release jumps. Median absolute month-to-month factor movement was 7.72pp; tier labels are display-only and do not alter the continuous formula.

## Parameter Sensitivity

| Set | Median | COOL Median | VERY_COOL Median | VERY_HOT Median | Below 40% |
|---|---:|---:|---:|---:|---:|
| Balanced 55/45 | 53.49% | 76.98% | 80.00% | 35.24% | 23.42% |
| Slightly aggressive 50/50 | 57.69% | 79.06% | 80.00% | 40.33% | 8.56% |
| Slightly conservative 65/35 | 48.52% | 74.77% | 79.05% | 29.78% | 32.43% |

Balanced remains the configured candidate. The conservative set is not selected merely because it passes the VERY_HOT gate, and the aggressive set is not selected for higher release.

## Over-Conservative Audit

Five balanced months triggered at least one conservative warning. COOL, VERY_COOL and normal-correction median gates passed. No automatic formula mutation was performed.

## Over-Aggressive Audit

Fifty-three balanced months triggered an aggressive warning, primarily because shallow drawdown can coexist with a factor above 50%. This is an audit signal. The explicit monthly Dynamic Cash Pool and gap caps remain hard constraints.

## Target Policy Comparison 35% vs 40%

Current effective overseas target remains 35%: target value 38,287.47 yuan, NDX target space after 7,565 yuan global-active holding 30,722.47 yuan, NDX gap 15,898.47 yuan, and residual cash target 20%.

The unactivated 40% strategic comparison gives NDX target space 36,192.11 yuan and residual cash 15%. This comparison does not change the live target.

## QDII Exact Allocation

`preview_status=VALID` only when test, assigned and effective-covered amounts match within 0.01 yuan, uncovered and over-assigned amounts are within tolerance, every row is within its effective limit, unselected rows are zero and the carrier snapshot is valid. Capacity can only reduce executable amount; the shortfall remains in Dynamic Cash Pool.

Current shadow chain: routed 846.24 yuan → candidate 298.75 yuan → approved capacity 11,280 yuan → carrier executable 298.75 yuan → retained for capacity 0 yuan. Formal release remains 0 yuan.

## Browser MCP Results

| Scenario | Actual result | Verdict |
|---|---|---|
| A Model card | `22.0/100`, HOT, base 41.5%, rate-adjusted 35.3%, cap 100%, candidate 35.3%, formal 0 | PASS |
| B PE isolation | Audit page shows both PE fields `used_in_score=false`, `used_in_release_factor=false`, `blocking=false`; perturbation isolation is covered by tests | PASS |
| C QDII capacity isolation | UI separates candidate 298.75 from carrier capacity; capacity perturbation isolation is covered by tests | PASS |
| D Exact 1000 | 021000=1000 produced `VALID`; button remained disabled, `aria-disabled=true`, because pool is frozen | PASS |
| E Under 900 | `INVALID`, button disabled | PASS |
| F Over 1100 | `INVALID`, button disabled | PASS |
| G Row limit | 539001=500 at 100 limit displayed `超出 400.00 元`; `INVALID` | PASS |
| H Freeze | Even with `VALID`, button text was `Execution disabled because Dynamic Cash Pool is Frozen`, disabled=true | PASS |

## Regression Results

134 automated tests passed, including the required 35 NDX/QDII tests. Locked targets remain 40/35/5/20; overseas Gap remains 15,898 yuan; historical execution remains 625 yuan; A500, gold and fixed-investment regressions passed.

## Activation Gates

- [x] NDX object control
- [x] No-lookahead replay
- [x] 2008, 2020 and 2022 full scenarios
- [x] COOL, VERY_COOL and normal-correction minimums
- [ ] Dot-com full chain (DFII10 unavailable before 2003)
- [ ] VERY_HOT median <=35% (actual 35.2375%)
- [ ] Offline pass
- [ ] Three complete trading-day shadow run
- [ ] User manual activation

## Remaining Risks

The formal rate series does not cover the dot-com episode; the two price-position components are moderately correlated; carrier limits remain secondary-channel observations; and the VERY_HOT gate narrowly fails. None of these is hidden or auto-approved.

## Change Log

- Added `ndx_price_temperature.py` and governed FRED history under `data/ndx_history/`.
- Added no-lookahead replay/validation runner and run-scoped CSV/JSON outputs.
- Replaced the empty NDX card with the shadow calculation chain without changing the page design system.
- Added the NDX amount chain and 35% vs 40% comparison.
- Replaced loose QDII preview logic with exact 0.01-yuan matching in Python and JavaScript.
- Kept Decision and Dynamic Cash Pool at `FREEZE`; no execution record was written.
