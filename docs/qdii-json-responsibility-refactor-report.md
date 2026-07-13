# QDII Carrier JSON Responsibility Convergence & V7 Three-Layer Decision Refactoring Report

## Executive Verdict

**PASS** — Architecture refactored. JSON provides carrier facts only, V7 owns all decision logic in three explicit layers, HTML displays V7 results. 172 tests pass.

---

## Responsibility Boundary Achieved

```
JSON提供事实 → V7负责判断 → HTML负责展示
```

| Component | Responsibility | What It Owns |
|-----------|---------------|--------------|
| `data/qdii-carrier-latest.json` | Carrier Facts | snapshot_valid, carrier_selection_status, current_effective_capacity, carriers[] |
| V7 Model Candidate Layer | Model Calculation | ndx_gap_routed_amount, ndx_candidate_release_amount |
| V7 Carrier Matching Layer | Capacity Matching | carrier_coverable_amount, retained_due_to_capacity, retained_due_to_carrier_block |
| V7 Formal Decision Layer | Execution Policy | formal_executable_amount, formal_release_amount, retained_due_to_decision_freeze |
| HTML | Display Only | Reads from v7_decision_chain, no hardcoded amounts |

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `qdii_carrier.py` | Added `apply_carrier_matching()`, `write_carrier_snapshot()`, `CARRIER_JSON_PATH` | +90 |
| `ndx_price_temperature.py` | Simplified `candidate_amount_chain()` — removed 5 carrier params | ~-30 |
| `fund_tracker.py` | Three-layer chain in `generate_copilot_snapshot()` | ~+80 |
| `fund_tracker.py` | Updated `write_copilot_dashboard()` HTML tables to three-layer display | ~+50 changed |
| `tests/test_ndx_validation_closeout.py` | Updated 6 tests + added 7 new tests | +60 |
| `tests/test_ndx_price_temperature_v1.py` | Updated 2 tests for new signature | ~5 |

---

## V7 Three-Layer Decision Chain (Verified)

```
Layer 1 — 模型候选层:
  ndx_gap_routed_amount       = 846.24 元
  ndx_candidate_release_amount = 298.75 元

Layer 2 — 载体匹配层:
  carrier_coverable_amount     = 298.75 元
  retained_due_to_capacity     =   0.00 元
  retained_due_to_carrier_block =   0.00 元
  current_effective_capacity   = 11,280.00 元

Layer 3 — 正式决策层:
  formal_executable_amount     =   0.00 元
  formal_release_amount        =   0.00 元
  retained_due_to_decision_freeze = 298.75 元
```

### Identity Checks

```
Identity 1: 298.75 = 298.75 + 0 + 0  ✅  (candidate → carrier)
Identity 2: 298.75 = 0 + 298.75      ✅  (carrier → decision)
```

---

## Fallback Behavior

| Scenario | Model Candidate | Carrier Coverable | Formal Executable |
|----------|----------------|-------------------|-------------------|
| Normal (AVAILABLE) | 298.75 | 298.75 | 0 (FREEZE) |
| Carrier STALE | 298.75 | 0 | 0 |
| Carrier BLOCKED | 298.75 | 0 | 0 |
| Carrier JSON missing | 298.75 | 0 | 0 |

**FREEZE blocks formal execution. Carrier failure zeros coverable. Neither zeros the model candidate.**

---

## Dynamic JSON vs Run Archive

| File | Location | Mutable | Contains V7 decisions? |
|------|----------|---------|----------------------|
| Raw carrier snapshot | `qdii-monitor/carrier_snapshot.json` | Yes | No |
| Curated carrier facts | `data/qdii-carrier-latest.json` | Yes | No |
| Run-archived copy | `reports/runs/<id>/inputs/qdii-carrier-latest.json` | No | No |
| Run-archived raw | `reports/runs/<id>/inputs/qdii-carrier-snapshot-raw.json` | No | No |

---

## Compatibility Aliases

Old `ndx_amount_chain` flat dict preserved with all fields mapped from `v7_decision_chain`:
- `current_carrier_executable_amount` → `carrier_matching.carrier_coverable_amount`
- `carrier_executable_amount` → same
- All 18 legacy fields mapped with `_compat_note` deprecation markers

---

## Test Results

```
172 passed, 0 failed

New tests (7):
- test_32: identity candidate→carrier
- test_33: identity carrier→decision  
- test_34: carrier failure zeros coverable not candidate
- test_35: locked values preserved
- test_36: HTML three-layer structure
- test_37: identity verification rendered in HTML
- test_38: v7_decision_chain in report.json
```

---

## Acceptance

```
JSON_FACT_ONLY = PASS
V7_CANDIDATE_LAYER = PASS
V7_CARRIER_MATCHING_LAYER = PASS
V7_FORMAL_DECISION_LAYER = PASS
FREEZE_DOES_NOT_ZERO_CANDIDATE = PASS
DYNAMIC_JSON_AND_RUN_ARCHIVE_SEPARATED = PASS
FALLBACK_BEHAVIOR = PASS
COMPATIBILITY = PASS
REGRESSION = PASS

ARCHITECTURE_REFACTOR = PASS
NDX_MODEL_VALIDATION_STAGE = OFFLINE_PASS
READY_FOR_NDX_SHADOW = true
DYNAMIC_CASH_POOL = FREEZE
```

---

## Output Files

```
data/qdii-carrier-latest.json                    ← new: carrier facts only
reports/runs/2026-06-20_143526_v7-2/              ← full run artifacts
reports/runs/2026-06-20_143526_v7-2/inputs/       ← new: archived carrier snapshots
html/Asset Allocation Copilot V7.html              ← deployed
dist/dashboard.html                                 ← deployed
```
