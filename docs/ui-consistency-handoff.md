# UI Consistency Handoff — Final State (Ready for NDX Model Refactor)

## 1. Current Final Effective State

```
LEGACY_US_EQUITY_SCORE = RETIRED
NASDAQ100_PE = DISPLAY_ONLY
SP500_PE = DISPLAY_ONLY

OVERSEAS_EQUITY_STRATEGIC_TARGET = 40%
OVERSEAS_EQUITY_CURRENT_FINAL_TARGET = 35%
TARGET_MODE = CARRY_FORWARD_LAST_VALID_TARGET
CASH_TARGET_MODE = RESIDUAL_TARGET

NDX_PRICE_TEMPERATURE_V1 = UNDER_VALIDATION
SINGLE_REAL_YIELD_FACTOR = UNDER_VALIDATION

DATA_STATUS = PASS
MODEL_STATUS = UNDER_VALIDATION
DECISION_STATUS = FREEZE
DYNAMIC_CASH_POOL = FREEZE

QDII_PREVIEW_STATUS = EMPTY (default)
HISTORICAL_EXECUTED = 625元
```

## 2. Current Target Configuration (LOCKED)

```
A-share:       40% (ACTIVE - A500 price temp)
US Equity:     35% (CARRY_FORWARD_LAST_VALID_TARGET)
Gold:           5% (Score-adjusted, floor hit)
Cash:          20% (RESIDUAL_TARGET: 100-40-35-5)
Total:        100%
```

## 3. Preview State Machine

| test_amount | assigned | over_limit | preview_status |
|------------|----------|------------|----------------|
| 0 | 0 | — | EMPTY |
| >0 | any | none | VALID |
| >0 | any | yes | INVALID |
| 0 | >0 | — | INVALID |

## 4. Code Entry Points

| Function | File | Approx Line |
|----------|------|-------------|
| `_fee_label_html()` | fund_tracker.py | ~938 |
| `_a500_price_model_eligibility()` | fund_tracker.py | ~960 |
| `generate_copilot_snapshot()` | fund_tracker.py | ~3140 |
| `write_copilot_dashboard()` | fund_tracker.py | ~4840 |
| QDII carrier rendering + JS | fund_tracker.py | ~5900-6750 |

## 5. NDX Formula Integration Points

When NDX_PRICE_TEMPERATURE_V1 is ready:
1. Set `scores["us_equity"]` to computed NDX score
2. Remove CARRY_FORWARD, allow score_adjustment on US equity
3. Recalculate cash as residual
4. Connect releaseFactor to NDX model output
5. Update Model Status from UNDER_VALIDATION to ACTIVE

## 6. Regression Protection (DO NOT MODIFY)

- A500 model / Score / releaseFactor
- Gold model / Score
- Fixed DCA plans
- Historical 625元 execution record
- PE DISPLAY_ONLY / Non-Blocking
- Dynamic Cash Pool FREEZE
- Current targets 40/35/5/20
- Gap 15,898元 consistency
- QDII manual whitelist boundary
- Preview state machine logic

## 7. Test Entry

```
cd ~/Documents/New\ project/fund_tracker
python3 -m unittest discover -s tests -v
# 99 tests, 0 failures
```
