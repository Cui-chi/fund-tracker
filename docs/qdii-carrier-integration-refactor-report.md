# Asset Allocation Copilot V7 - QDII Carrier Integration Refactor Report

Generated: 2026-06-19  
Scope: QDII carrier interface, registry, capacity governance, selection, confirmation boundary, and UI disclosure

## Executive Verdict

```text
QDII_MONITOR_INTEGRATION = IMPLEMENTED
QDII_CARRIER_SNAPSHOT = ACTIVE_DATA_INTERFACE
NDX_CARRIER_REGISTRY = IMPLEMENTED
GLOBAL_ACTIVE_EQUITY_POOL = HOLDING_DISPLAY_ONLY
AUTOMATIC_BUY_ON_LIMIT_CHANGE = PROHIBITED
DYNAMIC_CASH_POOL = FREEZE
```

V7 asset decisions are now separated from QDII carrier availability. The monitoring center supplies availability and limit observations only; it cannot create an investment signal, approve a carrier, alter allocation, or trigger a purchase. Current approved NDX capacity is 150 yuan: 539001 contributes 100 yuan and 016452 contributes 50 yuan. Any excess remains unallocated. Current release remains 0 and historical execution remains read-only.

## JSON Contract

Formal input: `/Users/cuichi/Documents/New project/qdii-monitor/carrier_snapshot.json`. V7 validates `schema_version`, `generated_at`, `producer`, `contract.not_investment_signal = true`, and `funds`. It does not parse monitor HTML.

Missing files return `UNAVAILABLE`; invalid JSON or contract fields return `INVALID`; snapshots older than 15 minutes return `STALE`; snapshots older than 60 minutes set `carrier_selection_status = BLOCKED`. Invalid, unavailable, or hard-stale data never falls back to unlimited capacity.

## Pool Split

### NDX_INDEX_QDII_POOL

This pool may carry an already-created Nasdaq-100 asset amount; it does not create that amount.

| Fund | Registry status | Effective limit | Role |
|---|---|---:|---|
| 539001 建信纳斯达克100指数(QDII)A | ACTIVE_CARRIER | 100 | Preferred execution carrier |
| 016452 南方纳斯达克100指数发起(QDII)A | APPROVED_CARRIER | 50 | Approved alternative carrier |

Other observed NDX funds remain `DISCOVERED` unless the user explicitly changes registry status.

### GLOBAL_ACTIVE_EQUITY_POOL

270023 广发全球精选 is `HOLDING_DISPLAY_ONLY`. It remains in total assets, overseas-equity position, allocation deviation, and return statistics. It is excluded from Nasdaq-100 temperature, NDX release, and carrier ranking. No `GLOBAL_ACTIVE_SCORE` or `GLOBAL_ACTIVE_RELEASE_FACTOR` was created.

## Carrier Registry

The persistent registry is `data/qdii_carrier_registry.json`. Supported states are `DISCOVERED`, `WATCHLIST`, `APPROVED_CARRIER`, `ACTIVE_CARRIER`, `SUSPENDED`, `RETIRED`, and the explicit pool role `HOLDING_DISPLAY_ONLY`.

Availability Score cannot promote a source. Moving a carrier to `APPROVED_CARRIER` or `ACTIVE_CARRIER` requires `user_confirmed = true`. Setting a new active carrier demotes the previous active carrier to approved. `HOLDING_DISPLAY_ONLY` cannot be promoted into the NDX execution pool.

## Manual Carrier Addition

The allocation page accepts fund code, name, share class, benchmark, sales channel, and notes. Every manually added carrier is forced to `WATCHLIST`, regardless of submitted status. Addition does not create a holding, purchase, future quota reservation, or execution plan.

APIs:

- `GET /api/qdii/carriers`
- `POST /api/qdii/carriers/manual`
- `POST /api/qdii/carriers/status`

Approval status changes require explicit user confirmation.

## Limit Governance

The integration retains distinct fields:

```text
official_fund_limit_rmb
observed_channel_limit_rmb
effective_limit_rmb
```

`effective_limit_rmb` is the minimum of available trusted official and observed-channel limits. If both are missing, capacity is unavailable rather than infinite.

- 016452: official limit 50 yuan, effective 2026-06-18. The old 1,000,000-yuan value is not used.
- 021000: official limit 1,000 yuan, effective 2026-06-18.
- 019441: current channel observation 10,000 yuan, but repeated 50/10,000 changes set `limit_volatility_flag = true`. It remains `DISCOVERED` and contributes zero approved capacity.

## Carrier Selection Flow

```text
V7 asset amount
→ validate carrier snapshot
→ merge registry and snapshot
→ exclude non-NDX and non-approved carriers
→ calculate strict effective limits
→ rank transparently
→ calculate per-carrier capacity
→ retain any shortfall
→ require user execution confirmation before holdings change
```

Ranking is explicit: registry status, benchmark match, effective limit, tracking error, purchase fee, fund size, NAV freshness, source confidence, and limit-volatility flag. The page displays the reason instead of a black-box suitability score.

For an illustrative 625-yuan asset amount, approved capacity is 150 yuan and 475 yuan remains unallocated. WATCHLIST and DISCOVERED capacity is not used to fill the shortfall. Under the current frozen decision, current releasable amount remains 0 even though theoretical carrier research stays visible.

## Guangfa Display-Only Treatment

```text
fund_code = 270023
pool = GLOBAL_ACTIVE_EQUITY_POOL
role = HOLDING_DISPLAY_ONLY
ndx_pool_eligible = false
dynamic_release_eligible = false
```

Its weekly fixed investment remains 100 yuan. The refactor does not stop, change, or reinterpret that independent rule.

## UI Changes

The V7.2.2 tab structure, typography, panels, and frozen-decision semantics were retained. The allocation tab now shows the NDX asset state, current releasable amount, approved capacity, unmet amount, carrier table, ranking explanation, approved-carrier selection, manual WATCHLIST entry, and a separate Guangfa global-active card.

The data-audit tab shows snapshot time, age, source confidence, stale status, data status, selection status, and `not_investment_signal`. Browser verification confirmed current release 0, `FREEZE`, historical executed amount 625 only in the historical section, and a disabled execution button.

## Test Results

```text
command: python3 -m unittest discover -s tests -v
total: 88
passed: 88
failed: 0
skipped: 0
duration: 0.277s
```

The 15 new tests cover contract validation, hard-stale blocking, missing-limit handling, strict-limit selection, WATCHLIST exclusion, approved eligibility, shortfall retention, 019441 volatility, 016452 correction, 270023 exclusion, manual-add defaults, explicit approval, pre-confirmation holding immutability, regression controls, and retained freeze.

## Regression Results

| Protected area | Result |
|---|---|
| A500 model and release factor | Unchanged |
| Gold Score | Unchanged; 39.8 regression passes |
| Strategic allocation | Unchanged: 40/40/10/10 |
| 270023 fixed investment | Unchanged: weekly 100 |
| Historical allocation events | No event written by discovery or selection |
| Source approvals | No proxy approved |
| Dynamic Cash Pool | FREEZE; `allow_auto_execution = false` |
| Automatic purchase on quota change | Prohibited |

## Remaining Risks

- Most observations are secondary channel data; official fund-company limits remain preferable.
- Fund size is absent from the snapshot and cannot differentiate current candidates.
- Tracking-error methodology and window remain insufficiently disclosed.
- Soft-stale snapshots remain visible with warning until the 60-minute hard block.
- Discovered funds require benchmark, share-class, fee, and risk review before approval.
- Capacity can change after snapshot generation; final execution still requires user confirmation at the channel.

## Change Log

- Added `qdii_carrier.py` for contract, staleness, registry, limits, selection, and capacity.
- Added `data/qdii_carrier_registry.json` with initial active, approved, and display-only entries.
- Integrated carrier evidence into the V7 snapshot without changing asset Scores.
- Added local registry APIs and UI panels while retaining existing page format.
- Added 15 QDII tests; all 88 repository tests pass.
- Retained `FREEZE`; no automatic buy, source approval, or model change was introduced.
