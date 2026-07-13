# UI Consistency Fix Report

## Executive Verdict

**PASS** — 核心数据口径已统一，阻断项语义已修正。部分 QDII 交互细节留待后续。

---

## Files Changed

| File | Change | Reason |
|------|--------|--------|
| `fund_tracker.py` | `generate_copilot_snapshot()` targets: us_equity 40%→35%, cash 15%→20% | CARRY_FORWARD_LAST_VALID_TARGET |
| `fund_tracker.py` | `generate_copilot_snapshot()` us_equity blocking issues | NDX/SINGLE_REAL_YIELD UNDER_VALIDATION |
| `fund_tracker.py` | `write_copilot_dashboard()` trigger gap display | 使用 copilot.gaps 而非 trigger.gap_value |
| `fund_tracker.py` | `write_copilot_dashboard()` blocker display | 拆分为 Data/Model/Execution 三类 |
| `tests/test_report_outputs.py` | 更新测试断言 | 匹配新目标值和阻断项文案 |

---

## Target Semantics

| Asset | Strategic Target | Current Effective Target | Mode |
|-------|-----------------|--------------------------|------|
| A-share | 40% | 40% | ACTIVE (A500 price temp) |
| US Equity | 40% | 35% | CARRY_FORWARD_LAST_VALID_TARGET |
| Gold | 10% | 5% | Score-adjusted (floor hit) |
| Cash | 10% | 20% | Residual (100-40-35-5) |

US Equity strategic=40%, current=35% 原因：旧 PE 模型已退役，NDX 模型 UNDER_VALIDATION，沿用上一有效快照的 final_target=35%。

---

## Gap Consistency

| Asset | Allocation Gap | Trigger Gap (before) | Trigger Gap (after) | Match |
|-------|---------------|---------------------|---------------------|-------|
| A-share | 19,328 | 19,103 | 19,328 | PASS |
| US Equity | 15,898 | 16,230 | 15,898 | PASS |

---

## Blocker Semantics

旧显示: "无阻断项" + FREEZE (矛盾)
新显示:
- **Data Blockers**: NONE
- **Model Activation Blockers**: NDX_PRICE_TEMPERATURE_V1 UNDER_VALIDATION, SINGLE_REAL_YIELD_FACTOR UNDER_VALIDATION
- **Execution Blockers**: DYNAMIC_CASH_POOL FREEZE

---

## Test Results

```
Ran 99 tests in 0.190s — OK
Passed: 99, Failed: 0, Skipped: 0
```

---

## Regression Results

- A500 model: unchanged
- Gold model: unchanged
- Fixed DCA: unchanged
- Historical 625元: unchanged
- Dynamic Cash Pool: FREEZE maintained

---

## Remaining Issues (for next phase)

1. QDII 购买渠道/data source 字段分离
2. 费率标签修复（仅在完整费用已知时显示"综合费率较低"）
3. 模拟金额改为用户可编辑（移除 468.75 硬编码）
4. 行级额度校验
5. 首页纳指/全球主动权益卡片中文状态文案
