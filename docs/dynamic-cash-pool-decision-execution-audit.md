# Dynamic Cash Pool 决策与执行扣减专项审计

审计日期：2026-07-13
结论：`SAFE_TO_EXECUTE`

## 资金池来源

当前持久化余额为 `4375.00` 元，且 2026-07 尚无执行流水：

```text
2026-06 月初及流入资金：2500.00
- 2026-06 实际执行：625.00
= 2026-06 结余：1875.00
+ 2026-07 月度流入：2500.00
- 2026-07 实际执行：0.00
= 当前 Dynamic Cash Pool：4375.00
```

因此 `4375.00` 是扣除已实际执行金额后的可用余额，不是预扣本月计划后的余额。

## 金额状态机

| 字段 | 当前值 | 语义 |
|---|---:|---|
| `opening_pool` | 1875.00 | 7 月月初可用余额 |
| `monthly_inflow` | 2500.00 | 7 月新增资金 |
| `proposed_release` | 674.75 | 模型资产层建议，只属于计划层 |
| `planned_execution` | 674.75 | 当前默认基金计划，不触发扣款 |
| `confirmed_execution` | 尚未确认 | 以用户弹窗最终提交金额为准 |
| `actual_executed` | 0.00 | 已成功写入执行流水的金额 |
| `uncovered_amount` | 0.00 | I 类获批后，当前资产计划已完整承接 |
| `remaining_pool` | 4375.00 | `opening_pool + monthly_inflow - actual_executed` |

当前基金计划为：A 股 `022459` 计划 `250.54` 元；纳指指数型 QDII `021000`（南方 I 类）计划 `424.21` 元。`021000` 的当前有效额度为 `1000.00` 元，足以承接本月全部纳指计划。

批准 I 类之前的旧计划为 `360.54` 元、未覆盖 `314.21` 元。该 `314.21` 从未被预扣，因此无需“回流”；它始终留在资金池中。

## 代码链路

- 月度流入：`fund_tracker.ensure_monthly_contribution()` 仅在月份变化时增加一次资金池。
- 决策生成：`fund_tracker.generate_copilot_snapshot()` 读取余额并写计划快照，不修改持久化余额。
- 载体路由：`qdii_carrier.build_carrier_selection()` 仅允许配置中显式批准的 I 类，并优先使用 `execution_funds.us_equity`。
- 人工确认：`local_server.py` 的 `/api/copilot/decision` 将基金级最终金额传给 `apply_copilot_decision()`。
- 实际扣减：`fund_tracker._apply_copilot_decision()` 汇总用户最终确认金额后，执行 `pool - actual_total`；不使用资产建议或默认计划作为扣减基数。
- 执行事实：`allocation_events.deploy_amount` 与 `fund_execution_log.actual_executed_amount` 保存实际执行；`plan_amount` 单独保存原计划。
- 页面展示：同时展示资产建议、默认基金计划、未覆盖金额、已执行金额和“扣除已实际执行金额后的余额”。

## 风险与修复

原逻辑已有同月 `allocation_event` 幂等检查，重复请求返回 `ALREADY_EXECUTED`。本次为完整执行链增加 SQLite savepoint：资金池、基金执行日志、月度事件和历史快照任一步失败都会回滚，同时恢复内存配置，消除流水与资金池非原子风险。全零确认被明确拒绝，不产生事件或扣款。

新增的零持仓 `execution_only` 载体在尚未执行前允许无本地 NAV 且不阻断数据门；一旦形成持仓，缺失 NAV 仍按原规则阻断。

## 验证证据

- 决策生成只读验证：资金池运行前后均为 `4375.00`，2026-07 执行事件均为 `0`。
- 部分执行：确认 `360.54` 仅扣 `360.54`，余额 `4014.46`，未执行 `314.21`。
- 用户下调：确认 `310.54` 仅扣 `310.54`，余额 `4064.46`。
- I 类完整承接：确认计划 `674.75` 时余额为 `3700.25`。
- 重复请求、注入写入失败、零元执行均未重复扣款或留下部分写入。
- `python3 -m pytest -q tests/`：`376 passed`。
- `py_compile` 与 `git diff --check`：通过。

未执行真实“确认执行并入账”，未修改正式执行 Ledger，未手工调整 Dynamic Cash Pool。
