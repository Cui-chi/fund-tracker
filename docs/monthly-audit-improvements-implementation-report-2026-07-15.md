# 月度系统审计前两项改进实施报告

## 实施范围

本次只实施月度审计计划中的前两项：

1. 分离历史执行决策依据与刷新时的当前机会重算结果。
2. 修正计划金额、实际执行金额和未执行差额的页面名称及显示精度。

未修改 Score、Target、Gap、Release Factor、Dynamic Cash Pool 规则、持仓、
执行流水或历史 Ledger。

## 快照语义调整

- 新增 `execution_decision_snapshot`，用于保存执行时的 Score、Target、Gap、
  Release Ratio、Allocation Routing、原计划金额和原计划分配。
- 新增 `current_opportunity_assessment`，用于保存每次刷新时基于当前持仓和
  当前 Dynamic Cash Pool 重新计算的机会评估。
- 已执行月份的顶层计划与路由继续采用执行时快照；页面资产卡、数据门和最新
  机会指标从 `current_opportunity_assessment` 读取。
- 历史快照若已被后续重算覆盖且无法证明原始路由，会标记为
  `UNAVAILABLE_LEGACY_EXECUTION_BASIS`。系统仅保留不可变交易流水中的原计划，
  不用后来的路由数据伪装成执行依据。

## 页面金额语义

已执行状态现在明确显示：

```text
本月原计划       674.75 元
基金层实际执行   673.00 元
未执行差额         1.75 元
剩余资金池      3702.00 元
```

- 首页与资金流摘要不再把实际执行金额称为“当前建议”或“资产层计划”。
- Release Allocation Flow 保留原计划金额和原计划方向。
- 已执行资金流使用基金级实际执行分配。
- 金额链审计字段统一保留两位小数；购买输入仍保持整数约束。

## 修改文件

- `fund_tracker.py`
- `tests/test_dynamic_cash_pool_execution.py`
- `tests/test_report_outputs.py`
- `docs/monthly-audit-improvements-implementation-report-2026-07-15.md`

## 验证结果

```text
python3 -m py_compile fund_tracker.py
PASS

python3 -m pytest -q \
  tests/test_decision_snapshot.py \
  tests/test_dynamic_cash_pool_execution.py \
  tests/test_report_outputs.py
17 passed
```

真实账本只读复核：

```text
plan_amount=674.75
executed_amount=673.00
unexecuted_amount=1.75
remaining_dynamic_cash_pool=3702.00
```

## 数据影响

- 未重写 2026 年 6 月或 7 月执行记录。
- 未修改 Dynamic Cash Pool、持仓或配置。
- 未生成新的交易流水。
- 未改变模型公式、阈值或执行门禁。
