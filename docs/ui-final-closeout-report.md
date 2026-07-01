# UI Final Closeout Report

## Executive Verdict

**PASS** — 8 项修复全部完成。99 tests pass。页面可交给 Codex 开始 NDX 温度公式重构。

---

## Files Changed

| File | Key Changes |
|------|-------------|
| `fund_tracker.py` | Data/Model/Decision 状态分离 (数据：通过/模型：验证中/决策：冻结) |
| `fund_tracker.py` | QDII carrier 默认分配金额归零 |
| `fund_tracker.py` | preview 状态机 (EMPTY/VALID/INVALID) + 页面展示 |
| `fund_tracker.py` | 行级可见错误文字 (qdii-row-error + aria-invalid) |
| `fund_tracker.py` | 透明标签"费率较低"→正确费率标签 |
| `fund_tracker.py` | 首页中文主视觉 (模型验证中/仅计入仓位/可用) |
| `fund_tracker.py` | 现金 strategic_target→N/A (RESIDUAL_TARGET) |
| `fund_tracker.py` | 购买渠道/数据来源列分离 |

## Purchase Channel Verification

- 购买渠道列: `purchase_channels` (缺失→"待补齐")
- 数据来源列: `source_name` (天天基金监控快照)
- 021000: personal_purchase_supported=true ✓
- "渠道便利"标签仅在 purchase_channels 明确时显示 ✓

## Default Simulation State

| Field | Initial Value |
|-------|--------------|
| test_amount | 0 元 |
| selected_capacity | 150 元 (已选基金容量) |
| assigned_amount | 0 元 |
| effective_covered_amount | 0 元 |
| uncovered_amount | 0 元 |
| over_assigned_amount | 0 元 |
| preview_status | EMPTY |

## Preview State Machine

| Condition | preview_status |
|-----------|---------------|
| test=0, assigned=0 | EMPTY |
| test>0, no over-limit | VALID |
| any row over limit | INVALID |
| test=0, assigned>0 | INVALID |

## Visible Row Validation

- 超限: 输入框红色边框 + aria-invalid="true" + 可见错误文字"分配金额超过当前额度，超出 X 元"
- 恢复: 清除所有错误样式和文字

## Data/Model/Decision Status Separation

- 顶部: 数据：通过 / 模型：验证中 / 决策：冻结
- 审计页: Data Blockers=NONE / Model=UNDER_VALIDATION / Execution=FREEZE
- 不再使用 "Data Status = FREEZE"

## Fee Label Cleanup

- "费率较低" → 0 occurrences (全部替换)
- 仅申购费已知 → "申购费较低"
- 全部费用已知 → "综合费率较低"
- 费用缺失 → "综合费率待核验"

## Homepage Chinese Semantics

- 纳指卡: 模型验证中 (UNDER_VALIDATION 降为小字)
- 全球主动: 仅计入仓位 (HOLDING_DISPLAY_ONLY 降为小字)
- QDII: 载体可用 (ACTIVE 降为小字)
- 决策卡: A股通过 · 纳指模型验证中 · 黄金通过 · QDII可用

## Cash Residual Target Semantics

- cash.strategic_target = N/A
- cash.target_mode = RESIDUAL_TARGET
- cash.final_target = 20%
- 公式: 100% - 40% - 35% - 5% = 20%

## Test Results

```
Ran 99 tests in 0.187s — OK
Passed: 99, Failed: 0, Skipped: 0
```

## Regression Results

- A500 model: unchanged
- Gold model: unchanged
- Fixed DCA: unchanged
- Historical 625元: unchanged
- Dynamic Cash Pool: FREEZE maintained
- Targets: 40/35/5/20 maintained
- Gap: 15,898元 maintained

## Acceptance Status

```
P0_DATA_CONSISTENCY = PASS
QDII_UI_INTERACTION = PASS
HOMEPAGE_SEMANTICS = PASS
TARGET_SEMANTICS = PASS
STATUS_SEPARATION = PASS
OVERALL_ACCEPTANCE = PASS
READY_FOR_NDX_MODEL_REFACTOR = true
```
