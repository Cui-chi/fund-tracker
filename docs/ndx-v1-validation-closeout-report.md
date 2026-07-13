# NDX Price Temperature V1 Validation Closeout Report

## Executive Verdict

**OFFLINE_PASS / READY_FOR_NDX_SHADOW**. All closeout gates passed. This verdict does not activate the model: Dynamic Cash Pool remains `FREEZE`, current release remains `0`, and the next permissible stage is three complete prospective trading-day shadow observations.

## Files Changed

- `ndx_price_temperature.py`
- `qdii_carrier.py`
- `fund_tracker.py`
- `scripts/run_ndx_validation.py`
- `scripts/build_ndx_closeout_fixtures.py`
- `tests/test_ndx_validation_closeout.py`
- `docs/ndx-model-handoff.md`
- Run-scoped validation artifacts under `reports/runs/2026-06-20_124131_v7-ndx-v1-validation/`

## Locked Formula Confirmation

No locked parameter was changed:

| Parameter | Value |
|---|---:|
| MA500 distance weight | 55% |
| 252-day high drawdown weight | 45% |
| Base release range | 25%–100% |
| DFII10 minimum modifier | 85% |
| Extreme-volatility minimum cap | 65% |

PE, S&P 500 PE, 5Y TIPS and Fed Funds remain excluded from formal calculation.

## Carrier Snapshot Semantics

`STALE` or `BLOCKED` carrier data now forces current effective capacity and executable amount to zero. The most recently observed capacity is retained as historical context only and cannot be treated as current capacity.

## Candidate vs Executable Amount Chain

| Field | Amount |
|---|---:|
| NDX gap-routed amount | 846.24 |
| NDX candidate release amount | 298.75 |
| Last-known approved carrier capacity | 11,280.00 |
| Current effective carrier capacity | 0.00 |
| Current carrier executable amount | 0.00 |
| Retained due to capacity | 0.00 |
| Retained due to carrier block | 298.75 |

The candidate is preserved for auditability; it is not converted into an executable recommendation.

## Formal STALE Browser Result

Browser MCP verified the formal HTML as `STALE / BLOCKED / FREEZE`. An exact 1,000 yuan carrier allocation still produced `INVALID`; the execution button remained disabled.

## Fresh Fixture Browser Result

Browser MCP separately verified:

- `FRESH / AVAILABLE / FREEZE`: preview `VALID`, execution disabled.
- `FRESH / AVAILABLE / ACTIVE`: exact match `VALID` and enabled; under-allocation, over-allocation and row-limit breach all `INVALID` and disabled.

Both fixtures are visibly marked `CONTROLLED TEST FIXTURE · NOT FORMAL OUTPUT`.

## Dot-com Price Stress Gate

**PASS** for 34 months from 2000-03 through 2002-12. This gate tests price pressure, base release and volatility only. DFII10 is explicitly not required and no neutral rate value is imputed.

## Post-2003 Full Chain Gate

**PASS**. The first fully warmed-up month is 2007-12; 222 complete monthly observations are available. No neutral DFII10 fill is used.

## VERY_HOT Tolerance Gate

**PASS**. Target is 35%, with a reasonable ±1 percentage-point tolerance. Actual VERY_HOT median release is 35.2375%, below the 36% maximum. No release parameter was reduced to obtain this result.

## Over-Aggressive Warning Attribution

The 53 warning months were classified for diagnostic review:

| Category | Months | Share |
|---|---:|---:|
| Shallow drawdown while well above MA500 | 36 | 67.92% |
| Near high / MA position not crowded | 13 | 24.53% |
| High real yield while release remains above 50% | 3 | 5.66% |
| Other | 1 | 1.89% |

Each row includes 3/6-month forward return and maximum drawdown. Those outcomes are marked `POST_HOC_DIAGNOSTIC_ONLY` and `NOT_USED_FOR_PARAMETER_SELECTION`; no parameter was automatically tuned from hindsight.

## Version Traceability

- Run ID: `2026-06-20_124131_v7-ndx-v1-validation`
- Generated at: `2026-06-20 12:41:31+08:00`
- Application: `V7.3-NDX-V1-VALIDATION`
- NDX formula: `NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED`
- QDII carrier contract: `1.0.0`
- Validation stage: `OFFLINE_PASS`

The same identifiers are embedded in JSON, replay CSV, warning-detail CSV and formal HTML.

## Regression Results

`python3 -m unittest discover -s tests -p 'test_*.py'`: 165 tests passed. The closeout suite contributes 31 targeted tests covering carrier semantics, amount-chain retention, scenario gates, tolerance, traceability and shadow-stage behavior.

## Shadow Readiness

`READY_FOR_NDX_SHADOW = true`; `shadow_days_completed = 0`. The closeout run is not counted as shadow day 1. Formal release remains zero.

## Remaining Risks

1. Three complete prospective trading-day shadow observations remain outstanding.
2. User activation approval remains outstanding after shadow completion.
3. Carrier freshness must be re-evaluated on every decision run; last-known capacity is never executable capacity.
4. The 53 warning months remain a monitoring population, not a basis for retrospective parameter fitting.
