# NDX Price Temperature V1 Handoff

## Current State

- `NDX_PRICE_TEMPERATURE_V1 = UNDER_VALIDATION`
- `NDX_MODEL_VALIDATION_STAGE = OFFLINE_PASS`
- `ACTIVATION_STATUS = NOT_ACTIVE`
- `DYNAMIC_CASH_POOL = FREEZE`
- `current_release_amount = 0`
- Current targets: A-share 40%, overseas equity 35%, gold 5%, cash 20%
- Proposed but inactive overseas target: 40%

## Implemented Components

- Core model: `ndx_price_temperature.py`
- Offline runner: `scripts/run_ndx_validation.py`
- Price history: `data/ndx_history/ndx_daily.csv`
- DFII10 history: `data/ndx_history/dfii10_daily.csv`
- Required 35 tests: `tests/test_ndx_price_temperature_v1.py`
- UI integration and strict state machine: `fund_tracker.py`, `qdii_carrier.py`

## Completed Closeout Gates

1. The dot-com period is a price-pressure gate only: 34 months passed without requiring or imputing DFII10.
2. The full-chain gate begins after DFII10 percentile warm-up in 2007-12: 222 complete months passed.
3. VERY_HOT median is 35.2375%, passing the 35% target with the approved ±1 percentage-point tolerance.
4. Browser MCP passed formal STALE/BLOCKED and controlled FRESH fixture checks.
5. Stale carrier data forces executable capacity to zero while retaining the full candidate amount as carrier-blocked.

## Remaining Activation Blockers

1. Three complete prospective trading-day shadow observations have not been collected.
2. User activation confirmation is absent.

## Next Run Procedure

1. Create a new versioned run directory before writing artifacts.
2. Refresh the two FRED CSVs without replacing NDX with QQQ.
3. Run `scripts/run_ndx_validation.py --run-dir <run_dir>`.
4. If offline gates remain unchanged, retain `OFFLINE_PASS` and `FREEZE`.
5. Record only complete prospective trading days; the validation closeout observation is `READY_FOR_SHADOW_NOT_COUNTED`.
6. Even after shadow pass, expose only `READY_FOR_MANUAL_ACTIVATION`; never activate automatically.

## Governance Invariants

- PE, S&P500 PE, DFII5, Fed Funds and breakeven are excluded from the formal NDX formula.
- Temperature changes release cadence only; it does not change the 35% current target.
- QDII capacity cannot increase score, target, routed amount or candidate amount.
- Global active equity remains `HOLDING_DISPLAY_ONLY`.
- Formal release is zero until explicit activation.
- QDII `VALID` requires exact test/assigned/effective-cover matching within 0.01 yuan and no row breach.

## Evidence

Use the latest run's:

- `reports/ndx-price-temperature-validation.json`
- `reports/ndx-historical-replay.csv`
- `reports/ndx-shadow-run.csv`
- `html/Asset Allocation Copilot V7.html`
- `run-manifest.md`

The current shadow CSV contains one non-counting observation marked `READY_FOR_SHADOW_NOT_COUNTED`. It is not evidence of three complete shadow trading days.
