# UI Consistency Final Patch Report

## Executive Verdict

**PASS** — 7 项 UI/语义补丁全部完成。99 tests pass。QDII 交互可正常使用。

---

## Files Changed

| File | Change |
|------|--------|
| `fund_tracker.py` | `_fee_label_html()` — 费率标签生成 |
| `fund_tracker.py` | QDII carrier table — 购买渠道/数据来源分列 |
| `fund_tracker.py` | QDII simulation — 用户可编辑输入框，默认 0 |
| `fund_tracker.py` | QDII JS — 行级额度校验+超额红色边框 |
| `fund_tracker.py` | Decision card — 中文状态文案 |
| `fund_tracker.py` | Target explanation — target_mode/target_source 替换 score_adjustment |
| `fund_tracker.py` | Cash target — RESIDUAL_TARGET 语义 |
| `fund_tracker.py` | Data & Audit — Target Governance 表 |
| `tests/test_us_equity_ui_semantic_migration.py` | 更新测试断言 |

---

## Purchase Channel Fix

- 购买渠道列: 显示 `purchase_channels`（缺失时"待补齐"）
- 数据来源列: 显示 `source_name`
- 021000: personal_purchase_supported=true

## Fee Label Fix

| 条件 | 标签 |
|------|------|
| 管理/托管/销售服务/申购费全部已知 | 综合费率较低 |
| 仅申购费已知 | 申购费较低 |
| 字段缺失 | 综合费率待核验 |

## Simulation Input Fix

- 468.75 硬编码已删除
- 改为 `type="number"` 输入框，默认 0，min=0，step=0.01
- 说明文字: "仅用于测试QDII载体承接能力，不是资产级建议金额"

## Row Limit Validation

- 输入金额 > effective_limit_rmb → 红色边框 + title 错误提示
- `preview_status = INVALID` when any row over limit
- 有效覆盖金额 = sum(min(assigned, effective_limit))

## Homepage Chinese Semantics

- 决策卡: A股资产层：通过 · 纳指资产层：模型验证中 · 黄金资产层：通过
- 英文 UNDER_VALIDATION/HOLDING_DISPLAY_ONLY 降为次级信息

## Overseas Target Semantics

- US equity target_reason: CARRY_FORWARD_LAST_VALID_TARGET（不再显示 score_adjustment=-5%）
- Target explanation 表增加 target_mode 列

## Cash Target Semantics

- Cash = RESIDUAL_TARGET (100% - 40% - 35% - 5% = 20%)
- Target Governance 表中明确标注

## Test Results

```
Ran 99 tests in 0.214s — OK
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
