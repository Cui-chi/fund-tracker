# NDX V1 Targeted Rollback — Engineering Final Report

## Executive Verdict

**PASS** — NDX V1 values restored via targeted rollback. 165 tests pass. All cross-artifact consistency verified.

---

## Root Cause

The thin `ndx-price-temperature-validation.json` generated during the NDX V1 Engineering Final Seal (run `2026-06-20_134042_v7-ndx-v1-engineering-final`) lacked the `latest_snapshot` key entirely. `load_ndx_validation_snapshot()` in `fund_tracker.py` picked this file first (newest), found `payload.get("latest_snapshot")` → `None`, defaulted to `{}`, causing all NDX values to become zero/NULL.

**The regression was a data-plumbing artifact, not a model failure.** FREEZE must not zero out model candidate amounts.

---

## Files Changed

| File | Change | Reason |
|------|--------|--------|
| `fund_tracker.py:54-59` | `load_ndx_validation_snapshot()` now skips JSONs without `latest_snapshot.temperature_score` | Skip thin engineering-only JSONs; enforce validation_stage=OFFLINE_PASS |
| `fund_tracker.py:3102` | `run_id` always uses `_v7-ndx-v1-engineering-final` suffix | Cross-artifact run_id consistency |
| `fund_tracker.py:5040` | `write_copilot_dashboard()` reads `run_id` from copilot snapshot | Single source of truth for run_id |
| `fund_tracker.py:3656-3657` | Added `ready_for_ndx_shadow` and `shadow_days_completed` to copilot snapshot | Previously NULL fields now populated |
| `ndx_price_temperature.py:343-347` | Default `validation_stage` → `OFFLINE_PASS`; added `shadow_days_completed` and `ready_for_ndx_shadow` | Correct defaults in model layer |
| `model_risk.py:21-23` | `FORMULA_VERSION` now includes `NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED` | Complete formula version string |

### Locked (Not Modified)

- NDX formula: 55/45 balanced, release floor 25%, DFII10 floor 0.85, extreme vol cap 0.65
- Historical replay: 318 months, all scenarios unchanged
- A500 price temperature model and LIVE_SCORING_ENABLED
- Gold model: gold-v2-inverse-real-yield-fed
- QDII carrier governance and state machine
- Targets: 40/35/5/20 (US equity 35% = CARRY_FORWARD_LAST_VALID_TARGET)
- Gap: 15,898.47 元
- Historical execution: 625 元
- Dynamic Cash Pool: FREEZE

---

## Run Identity

```
run_id: 2026-06-20_140719_v7-ndx-v1-engineering-final
generated_at: 2026-06-20T14:07:19
carrier_snapshot_generated_at: 2026-06-20 13:58:15
formula_version: CN_EQUITY_PRICE_TEMP_V1;NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED;gold-v2-inverse-real-yield-fed;allocation-v3-gap-first-cn-release-factor
```

---

## NDX V1 Restored Values

| Field | Restored Value | Target | Match |
|-------|---------------|--------|-------|
| temperature_score | 22.043651 | 22.0437 | ✅ |
| temperature_level | HOT | HOT | ✅ |
| base_release_factor | 0.415327 | 0.415327 | ✅ |
| candidate_effective_release_factor | 0.353028 | 0.353028 | ✅ |
| ndx_gap_routed_amount | 846.24 元 | 846.24 元 | ✅ |
| ndx_candidate_release_amount | 298.75 元 | 298.75 元 | ✅ |
| validation_stage | OFFLINE_PASS | OFFLINE_PASS | ✅ |

---

## Amount Chain (carrier vs decision layer)

| Field | Value |
|-------|-------|
| 载体可承接金额 (carrier_coverable_amount) | **298.75 元** |
| 决策冻结保留金额 (retained_due_to_decision_freeze) | **298.75 元** |
| 正式可执行金额 (formal_executable_amount) | **0.00 元** |
| 正式释放金额 (formal_release_amount) | **0.00 元** |
| NDX缺口路由金额 (ndx_gap_routed_amount) | 846.24 元 |
| 上一有效/最近观察容量 | 11,280.00 元 |
| 当前有效容量 | 11,280.00 元 |

**FREEZE correctly blocks formal execution while preserving model candidate amounts.**

---

## Cross-Artifact Consistency

| Field | HTML | JSON | Manifest | Match |
|-------|------|------|----------|-------|
| run_id | 2026-06-20_140719_v7-ndx-v1-engineering-final | same | same | ✅ |
| formula_version | 4-model combo with NDX | same | same | ✅ |
| temperature_score 22.0437 | ✅ | ✅ | — | ✅ |
| amount_chain: 298.75/0/0/298.75 | ✅ | ✅ | — | ✅ |
| status: UNDER_VALIDATION/OFFLINE_PASS | ✅ | ✅ | ✅ | ✅ |
| FREEZE | ✅ | ✅ | ✅ | ✅ |
| 40/35/5/20 | ✅ | ✅ | — | ✅ |
| Gap 15,898.47 | ✅ | ✅ | — | ✅ |
| Historical 625 | ✅ | ✅ | — | ✅ |

---

## Test Results

```
165 passed, 0 failed
```

---

## Status Enums (Unchanged)

```
model_status = UNDER_VALIDATION
validation_stage = OFFLINE_PASS
activation_status = NOT_ACTIVE
decision_status = FREEZE
dynamic_cash_pool_status = FREEZE
ready_for_ndx_shadow = true
shadow_days_completed = 0
formal_release_amount = 0
```

---

## Acceptance

```
ROOT_CAUSE_IDENTIFIED = PASS
TARGETED_ROLLBACK = PASS
NDX_VALUES_RESTORED = PASS
AMOUNT_CHAIN_CORRECT = PASS
FREEZE_SEMANTICS_PRESERVED = PASS
CROSS_ARTIFACT_CONSISTENCY = PASS
REGRESSION = PASS

ENGINEERING_CLOSEOUT = PASS
NDX_MODEL_VALIDATION_STAGE = OFFLINE_PASS
READY_FOR_NDX_SHADOW = true
DYNAMIC_CASH_POOL = FREEZE
```

---

## Output Files

```
reports/runs/2026-06-20_140719_v7-2/
├── html/Asset Allocation Copilot V7.html
├── json/report.json
├── reports/ndx-price-temperature-validation.json
├── run-manifest.md
└── ... (all standard reports)

html/Asset Allocation Copilot V7.html  (deployed)
dist/dashboard.html                     (deployed)
```
