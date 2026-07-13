# Asset Allocation Copilot V7  
# A500 价格温度启用前清理与交接规范

## 2026-06-19 海外权益 UI 语义迁移补充

- `legacy_us_equity_score = RETIRED`，不再展示或计算旧美股 Score 22.6。
- Nasdaq100 PE 与 S&P500 PE 均为 `DISPLAY_ONLY`，不参与 Score、release factor 或 Blocking Issues。
- 新 NDX 价格温度与单一实际利率因子均为 `UNDER_VALIDATION`，因此 Dynamic Cash Pool 继续 `FREEZE`。
- 海外权益拆为 `NDX_INDEX_QDII_POOL` 与 `GLOBAL_ACTIVE_EQUITY_POOL`；270023 仅为 `HOLDING_DISPLAY_ONLY`。
- `qdii_carrier_snapshot.json` 是外部人工批准后的只读白名单；V7 已移除发现、观察、批准、激活和手动新增状态流。
- QDII 页面支持多选、金额调整、容量与未覆盖金额实时预览，但当前执行按钮禁用。
- 详细证据见 `docs/us-equity-ui-semantic-migration-report.md`。

> 文档用途：  
> 1. 作为 DeepSeek 本次代码清理任务的执行说明；  
> 2. 作为后续 Codex 接手项目时的变更依据；  
> 3. 记录“为什么修改、修改了什么、哪些内容禁止继续使用”；  
> 4. 防止旧估值逻辑、旧执行记录和新价格温度模型再次混用。

---

# 1. 项目背景

当前 Asset Allocation Copilot V7 已完成 A 股温度模型的方向调整。

旧方案主要依赖：

- 沪深300 PE；
- 沪深300 PB；
- A500 PE/PB；
- 历史估值百分位；
- AKShare / Legulegu / etf.run 等第三方代理或抓取链路。

新方案改为：

- 中证A500：实际投入载体，作为A股价格温度主判断；
- 沪深300：A股整体市场环境参考，只允许有限修正；
- PE/PB：仅作为展示参考，不再参与A股自动评分和资金释放；
- 核心输入：指数每日收盘价格；
- 核心指标：
  - 长期均线偏离；
  - 近一年高点回撤；
  - 60日实现波动率。

当前数据已经可以拉取并完成计算，但尚未正式启用。

当前已知数据状态：

```text
A500 index code: 000510
HS300 index code: 000300
Source: Eastmoney index daily kline
Latest date: 2026-06-18
Sample count: 5210
A500 confidence: HIGH
HS300 confidence: HIGH
A500 source approval: OFFICIAL_PASS
HS300 source approval: OFFICIAL_PASS
A500 methodology: PASS
A500 reproducibility: PASS
A500 freshness: PASS
```

A500当前计算结果示例：

```text
latestClose = 6219.79
MA500 = 4947.05028
movingAverageDistance = +25.7272%
oneYearDrawdown = -1.5551%
annualizedVolatility = 21.3732%
opportunityScore = 7.3326
volatilityPenalty = 4.0986
marketAdjustment = -5
finalScore = 0
level = VERY_HOT
releaseFactor = 0.2
```

当前主要问题不是“数据抓不到”，而是：

1. A500数据已经PASS，但模型仍被旧状态阻塞；
2. 旧沪深300估值乘数仍在影响A股基金计划；
3. 测试报告声称PASS，但实际测试统计为0；
4. 当前决策与历史执行记录仍存在字段混杂；
5. A500新模型和旧估值模型可能并存；
6. 数据质量门、模型启用门、资金执行门的语义不完全一致。

---

# 2. 本次任务目标

本次任务不是继续开发新指标。

本次任务是完成一次**启用前代码清理**，确保：

```text
A500价格数据PASS
→ A500价格温度允许参与A股评分
→ 沪深300只做有限修正
→ PE/PB彻底退出自动评分
→ 历史执行与当前决策严格分离
→ 测试真实执行
→ 状态字段语义统一
```

最终目标：

```text
A500价格温度模块可以安全启用
```

但是否最终解除整个 Dynamic Cash Pool 的 FREEZE，不由本次任务强行决定。

因为当前美股仍可能存在：

```text
nasdaq100_pe_percentile
sp500_pe_percentile
```

未审批或未通过的问题。

本次只清理 A500 和通用状态一致性。

---

# 3. 最高优先级原则

必须遵守：

1. 数据正确性优先于恢复执行。
2. 不得为了让系统 EXECUTE 而绕过数据质量门。
3. 不得把旧估值乘数和新价格温度同时作用于A股。
4. 不得把历史执行金额重新解释为当前可执行金额。
5. 不得删除历史执行记录。
6. 不得覆盖历史不可变快照。
7. 不得用默认值掩盖状态冲突。
8. 不得因为回溯历史存在就自动否定A500价格数据。
9. 不得因为数据源为东方财富而自动认定不可信；应以当前已完成的来源审批、可复算性、样本和新鲜度判断。
10. 所有修改必须有测试和文档证据。
11. 不得顺手重构美股、黄金、基金净值回撤、固定定投等无关模块。
12. 不得重新引入PE/PB参与A股Score。

---

# 4. 本次允许修改范围

允许修改：

- A股价格温度模型启用逻辑；
- A500数据稳定性门；
- A股评分组件；
- A股资金释放系数；
- 数据质量状态映射；
- A股基金计划文案；
- 报告输出字段；
- 当前决策/历史执行分离；
- 测试；
- 文档；
- 与以上内容直接相关的最小接口。

禁止修改：

- 美股估值公式；
- 黄金Score公式；
- 固定定投计划；
- 已执行的历史资金记录；
- 历史决策快照；
- 战略目标配置；
- 用户持仓；
- 基金代码；
- 基金净值历史；
- PE/PB原始缓存；
- 其他数据源抓取器；
- 页面整体UI结构；
- 资金池总额规则；
- 手工执行记录。

---

# 5. 执行前扫描

执行修改前，先搜索以下关键词：

```text
BLOCKED_BY_A500_PRICE_DATA
LIVE_SCORING_DISABLED_PENDING_STABLE_A500_PRICE_DATA
modelEnabled
activationStatus
effectiveReleaseFactor
price_temperature_score
fallback_compatibility_score
valuation_multiplier
valuation_plan
A500不参与判断
沪深300估值代理偏热
hs300_pe_percentile
hs300_pb_percentile
a500_pe_percentile
a500_pb
used_in_score
OFFICIAL_PASS
PENDING_PROXY_REVIEW
allow_execution
allow_auto_execution
current_decision_amount
current_release_amount
historical_executed_amount
deploy_amount
plan_amount
executed_amount
currentMonth
allocationHistory
immutable_decision_snapshot
```

必须先输出：

```text
1. 命中文件
2. 命中行号
3. 当前逻辑作用
4. 是否需要修改
5. 修改风险
```

不得直接盲改。

---

# 6. P0-1：清理A500状态冲突

当前存在冲突：

```text
approval_status = OFFICIAL_PASS
gate_result = PASS
confidence = High
reproducible = true
freshness = PASS
```

同时：

```text
modelEnabled = false
activationStatus = BLOCKED_BY_A500_PRICE_DATA
effectiveReleaseFactor = 1.0
```

这是不允许继续存在的状态。

## 6.1 建立统一启用条件

建议实现统一函数：

```python
def is_a500_price_model_eligible(indicator, temperature_result):
    return all([
        indicator.approval_status == "OFFICIAL_PASS",
        indicator.gate_result == "PASS",
        indicator.methodology_status == "PASS",
        indicator.reproducible_status == "PASS",
        indicator.stale_status == "PASS",
        indicator.confidence in ("High", "HIGH"),
        temperature_result.carrierIndex.sampleCount >= 250,
        temperature_result.carrierIndex.freshnessStatus == "FRESH",
        temperature_result.finalScore is not None,
        temperature_result.releaseFactor is not None,
    ])
```

按项目实际语言实现。

不得分散在多个文件中重复判断。

## 6.2 满足条件时

必须输出：

```text
modelEnabled = true
activationStatus = ACTIVE
used_in_score = true
effectiveReleaseFactor = releaseFactor
```

## 6.3 不满足条件时

必须输出明确原因：

```text
modelEnabled = false
activationStatus = <具体原因>
used_in_score = false
effectiveReleaseFactor = 1.0
```

具体原因枚举建议：

```text
A500_DATA_STALE
A500_SAMPLE_INSUFFICIENT
A500_SOURCE_NOT_APPROVED
A500_METHOD_NOT_REPRODUCIBLE
A500_METRICS_INCOMPLETE
A500_PRICE_INVALID
```

禁止继续使用笼统的：

```text
BLOCKED_BY_A500_PRICE_DATA
```

除非它只是兼容旧字段，且必须同时附带具体原因。

## 6.4 回溯历史处理

`isBackfilledHistory = true` 只产生 warning：

```text
CONTAINS_BACKFILLED_HISTORY
```

不得单独阻止模型启用。

原因：

```text
MA500仅依赖最近500个交易日
近一年回撤仅依赖最近250个交易日
60日波动率仅依赖最近61个有效价格
```

如最近500日仍包含回溯数据，应显示置信度提示，但不得自动降级为不可用。

---

# 7. P0-2：彻底移除旧估值乘数对A股的影响

当前A股基金计划中仍存在：

```text
valuation_multiplier = 0.75
valuation_plan = "沪深300估值代理偏热，未来定投节奏参考基础频率的75%；A500不参与判断"
```

这与新模型冲突。

## 7.1 必须完成

对A股资产和A股基金：

```text
PE
PE_TTM
PB
PE percentile
PB percentile
HS300 valuation proxy
```

全部不得再影响：

- A股Score；
- A股目标配置；
- A股temperature multiplier；
- A股releaseFactor；
- A股基金计划金额；
- A股自动执行状态；
- A股定投节奏。

## 7.2 保留方式

估值字段可以保留：

```text
role = DISPLAY_ONLY
used_in_score = false
```

UI或报告中显示：

```text
估值参考，不参与当前自动评分
```

## 7.3 旧字段兼容

如旧代码或前端仍读取：

```text
valuation_multiplier
valuation_plan
```

则：

```text
valuation_multiplier = 1.0
valuation_plan = "估值数据仅供参考，不参与A股价格温度和资金释放"
```

但更推荐新增：

```text
price_temperature_release_factor
price_temperature_plan
```

并逐步停止旧字段。

## 7.4 新A股基金计划文案

例如：

```text
A500价格温度：很热
A500相对MA500：+25.73%
近一年高点回撤：-1.56%
60日年化波动率：21.37%
沪深300环境修正：-5
A股价格温度释放系数：0.20
PE/PB仅供参考，不参与自动评分
```

禁止再出现：

```text
A500不参与判断
沪深300估值代理偏热
参考基础频率的75%
```

---

# 8. P0-3：统一A股Score与releaseFactor

当前系统可能同时存在：

```text
a_share score = 50 fallback
price temperature finalScore = 0
releaseFactor = 0.2
effectiveReleaseFactor = 1.0
```

必须统一。

## 8.1 启用后

建议：

```text
a_share.price_temperature_score = finalScore
a_share.fallback_compatibility_score = null
a_share.score = finalScore
```

如业务层仍需要0-100分。

资金路由使用：

```text
temperature_multiplier = map(finalScore)
```

或者直接使用：

```text
releaseFactor
```

但不得两次重复修正。

必须选择一种：

### 方案A

```text
finalScore
→ temperature_multiplier
```

### 方案B

```text
finalScore
→ releaseFactor
```

不能：

```text
finalScore先影响temperature_multiplier
再用releaseFactor影响一次
```

否则会重复压缩A股资金。

## 8.2 推荐方案

当前已有 `releaseFactor`，建议：

```text
配置缺口决定基础权重
A股releaseFactor仅作用于A股理论分配额
未释放部分留在Dynamic Cash Pool
```

公式：

```text
a_share_theoretical_amount
× effectiveReleaseFactor
= a_share_executable_amount
```

同时：

```text
temperature_multiplier = 1.0
```

或仅用于展示。

如保留 `temperature_multiplier`，则 `releaseFactor` 必须只做展示，不再二次执行。

## 8.3 明确写入文档

完成后必须说明最终采用哪个方案。

---

# 9. P0-4：清理当前决策与历史执行混杂

当前必须严格分离：

```text
current_decision_amount
current_release_amount
historical_executed_amount
```

## 9.1 当前决策

表示本次最新计算：

```text
current_decision_amount
current_release_amount
current_allocation_plan
current_allow_execution
current_decision_status
```

## 9.2 历史执行

表示已发生事实：

```text
historical_executed_amount
historical_executed_allocations
historical_execution_records
```

## 9.3 禁止混用字段

以下旧字段如果仍保留：

```text
deploy_amount
plan_amount
allocations
allocation_plan
executed_amount
status
currentMonth
```

必须明确其语义。

建议：

```text
deploy_amount -> deprecated
plan_amount -> deprecated
allocations -> deprecated
```

顶层报告统一使用：

```json
{
  "current_decision": {
    "decision_status": "FREEZE",
    "allow_execution": false,
    "release_amount": 0,
    "allocation_plan": {}
  },
  "historical_execution": {
    "executed_amount": 625,
    "executed_allocations": {
      "a_share": 301.68,
      "us_equity": 120.02,
      "gold": 203.3
    },
    "executed_at": "2026-06-12T11:08:59"
  }
}
```

## 9.4 历史记录不可修改

不得修改：

```text
2026-06-12 已执行625元
```

只允许改变展示和字段归类。

## 9.5 当前报告验收

如当前系统仍FREEZE：

```text
current_decision_amount = 0
current_release_amount = 0
historical_executed_amount = 625
```

页面不得把625显示为“本次建议”。

---

# 10. P0-5：真实运行测试

当前测试报告存在：

```text
test_summary.total = 0
test_summary.passed = 0
test_summary.failed = 0
```

因此必须实际执行测试。

## 10.1 必须运行

至少运行：

```text
test_a_share_price_temperature
test_a_share_valuation
test_data_quality_gate
test_allocation_routing
test_report_outputs
test_decision_snapshot
test_source_approval
test_output_paths
```

按实际项目测试框架执行：

```bash
pytest
```

或：

```bash
python -m unittest
```

不得只扫描测试文件。

## 10.2 测试结果必须写入

```json
{
  "test_summary": {
    "total": N,
    "passed": N,
    "failed": 0,
    "skipped": M
  }
}
```

## 10.3 禁止

不得在测试失败后：

- 删除测试；
- 跳过测试；
- 修改断言为永远通过；
- 只输出“逻辑已验证”；
- 把PENDING算作PASS。

---

# 11. 必须新增的测试

## 11.1 A500启用门测试

### Case 1

```text
OFFICIAL_PASS
PASS
High
reproducible
fresh
sample >= 250
```

结果：

```text
modelEnabled = true
activationStatus = ACTIVE
used_in_score = true
effectiveReleaseFactor = releaseFactor
```

### Case 2

数据陈旧：

```text
modelEnabled = false
activationStatus = A500_DATA_STALE
effectiveReleaseFactor = 1.0
```

### Case 3

仅存在回溯历史warning：

```text
isBackfilledHistory = true
```

结果仍允许：

```text
modelEnabled = true
```

## 11.2 旧估值隔离测试

即使：

```text
hs300_pe_percentile = 100
hs300_pb_percentile = 100
valuation_multiplier = 0.1
```

A股最终价格温度和资金释放不得变化。

## 11.3 A500主判断测试

改变A500：

- MA偏离；
- 一年回撤；
- 60日波动率；

应影响A股价格温度。

改变沪深300：

- 只允许修正±5。

## 11.4 双重修正测试

保证：

```text
A股温度不会同时被temperature_multiplier和releaseFactor重复调整
```

## 11.5 当前与历史分离测试

历史已执行625元，当前FREEZE：

```text
current_release_amount = 0
historical_executed_amount = 625
```

## 11.6 报告文案测试

不得出现：

```text
A500不参与判断
沪深300估值代理偏热
未来定投节奏参考基础频率的75%
```

---

# 12. 数据质量门语义统一

统一以下状态：

```text
data_status
model_status
decision_status
dynamic_cash_pool_status
asset_level_status
allow_execution
allow_auto_execution
```

## 12.1 数据状态

只回答数据是否可用：

```text
PASS
WARNING
FAIL
```

## 12.2 模型状态

只回答模型是否参与计算：

```text
ACTIVE
REFERENCE_ONLY
DISABLED
```

## 12.3 资产执行状态

每个资产：

```text
ELIGIBLE
BLOCKED
```

## 12.4 资金池状态

全局：

```text
EXECUTE
FREEZE
```

## 12.5 禁止冲突

例如不得出现：

```text
a500 data gate = PASS
a500 model = DISABLED
reason = source not stable
```

除非有独立明确门：

```text
scheduled_run_stability = FAIL
```

如果存在独立稳定性门，必须输出证据：

```text
required_success_count
actual_success_count
last_failure
failure_rate
```

不能靠硬编码阻塞。

---

# 13. 连续抓取稳定性

当前如已有运行日志，建立统计：

```text
scheduled_fetch_count
scheduled_fetch_success_count
scheduled_fetch_failure_count
consecutive_success_count
last_success_at
last_failure_at
latest_trade_date_progressed
empty_response_count
```

建议门槛：

```text
连续5次成功
最近交易日正常推进
无空数组
无Cookie依赖
无手工修复
```

但注意：

本次如缺少足够历史运行记录，不得否定数据本身。

可以输出：

```text
source_stability = CONDITIONAL_PASS
```

同时允许模型进入：

```text
ACTIVE_WITH_WARNING
```

如果系统不支持该状态，则：

```text
modelStatus = ACTIVE
warnings += INSUFFICIENT_SCHEDULED_RUN_HISTORY
```

不得继续使用旧的笼统阻塞。

---

# 14. A500公式检查

当前公式：

```text
finalScore =
clamp(
  opportunityScore
  - volatilityPenalty
  + marketAdjustment,
  0,
  100
)
```

当前结果为0。

本次不得修改公式阈值。

但需新增诊断字段：

```text
preClampScore
clampApplied
```

例如：

```text
preClampScore = -1.766
finalScore = 0
clampApplied = true
```

目的是后续观察是否大量结果堆积在0。

本次只记录，不优化参数。

---

# 15. 输出文件要求

执行完成后，必须生成：

```text
docs/a500-price-temperature-cleanup-handoff.md
reports/a500-price-temperature-cleanup-report.md
json/a500-price-temperature-cleanup-result.json
```

## 15.1 cleanup-result.json

至少包含：

```json
{
  "status": "COMPLETED",
  "a500_data_status": "PASS",
  "a500_model_status": "ACTIVE",
  "a500_used_in_score": true,
  "old_valuation_logic_removed": true,
  "double_adjustment_removed": true,
  "current_and_historical_execution_separated": true,
  "tests": {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "skipped": 0
  },
  "remaining_blocking_issues": [],
  "modified_files": [],
  "warnings": []
}
```

数值必须是真实结果，不得照抄示例。

---

# 16. 本文档交接要求

DeepSeek完成后，不得覆盖本文件原有内容。

必须在本文档末尾追加：

```text
# DeepSeek Execution Record
```

包含：

## 16.1 执行时间

```text
started_at
completed_at
```

## 16.2 修改文件

逐一列出：

```text
file
reason
key_changes
```

## 16.3 删除或禁用的旧逻辑

明确列出：

```text
旧字段
旧函数
旧文案
旧Score来源
旧乘数
```

## 16.4 新逻辑入口

明确列出：

```text
A500启用函数
A股温度计算函数
资金释放函数
报告生成函数
```

## 16.5 测试结果

列出所有执行命令和结果。

## 16.6 未完成事项

不得写“无”除非确实全部完成。

## 16.7 Codex后续注意事项

必须写明：

```text
1. 不得重新启用A股PE/PB自动评分
2. 不得恢复valuation_multiplier对A股资金的作用
3. 不得删除历史625元执行记录
4. 不得用旧snapshot覆盖新报告
5. 美股阻塞问题仍独立存在
6. A500公式参数暂未优化
```

---

# 17. 最终验收标准

只有以下全部满足，任务才算完成：

```text
[ ] A500价格源OFFICIAL_PASS时不再被旧硬编码阻塞
[ ] modelEnabled与数据门状态一致
[ ] activationStatus不再使用笼统旧原因
[ ] A500 used_in_score = true
[ ] effectiveReleaseFactor使用实际releaseFactor
[ ] PE/PB不再影响A股Score
[ ] valuation_multiplier不再影响A股基金计划
[ ] 页面不再显示“A500不参与判断”
[ ] 沪深300只做±5环境修正
[ ] A500与沪深300未生成两个完整Score后平均
[ ] A股未发生双重温度调整
[ ] 当前决策与历史执行分离
[ ] 历史625元执行记录保留
[ ] 当前FREEZE时当前释放金额为0
[ ] 测试真实执行
[ ] test_summary.total > 0
[ ] test_summary.failed = 0
[ ] 输出cleanup报告
[ ] 本文档追加DeepSeek执行记录
```

---

# 18. 失败处理

如遇到以下情况：

```text
数据库不可访问
测试环境缺失
A500原始价格不可读取
报告生成链路断裂
旧字段依赖过深
```

不得假装完成。

输出：

```text
status = PARTIALLY_COMPLETED
```

并说明：

```text
blocked_step
blocked_reason
affected_files
completed_steps
remaining_steps
```

---

# 19. 最终终端摘要

执行结束后，终端只输出：

```text
A500 cleanup completed.

Status:
A500 data:
A500 model:
Used in score:
Old valuation logic removed:
Double adjustment removed:
Decision/history separated:

Tests:
Total:
Passed:
Failed:
Skipped:

Remaining blockers:
1.
2.

Documents:
- docs/a500-price-temperature-cleanup-handoff.md
- reports/a500-price-temperature-cleanup-report.md
- json/a500-price-temperature-cleanup-result.json
```

---

# 20. DeepSeek执行指令

现在开始：

```text
Step 1：扫描项目
Step 2：输出影响清单
Step 3：修复A500状态冲突
Step 4：移除旧估值逻辑
Step 5：统一A股Score与releaseFactor
Step 6：分离当前决策和历史执行
Step 7：补充测试
Step 8：真实运行测试
Step 9：生成报告
Step 10：在本文档末尾追加执行记录
```

不得跳过Step 1和Step 8。

---

# DeepSeek Execution Record

> 由DeepSeek执行完成后追加，当前不要删除本标题。

---

# DeepSeek Execution Record

## 执行时间

- **started_at**: 2026-06-19T17:45:00+08:00
- **completed_at**: 2026-06-19T18:15:00+08:00

## 修改文件

| file | reason | key_changes |
|------|--------|-------------|
| `cn_equity_temperature.py` | A500模型启用 | `LIVE_SCORING_ENABLED: False → True` |
| `fund_tracker.py` | A500状态清理 + 旧估值移除 | 见下方详情 |

### fund_tracker.py 详细变更

1. **新增 `_a500_price_model_eligibility()`** (line ~914): 统一A500价格温度启用条件函数，检查 sample>=250, freshness=FRESH, finalScore/releaseFactor 非空, confidence HIGH。返回 `(eligible: bool, reason: str)`

2. **更新 `calculate_cn_equity_price_temperature()`** (line ~953):
   - `modelEnabled`: 改用 eligibility 函数动态判断，不再硬编码 `LIVE_SCORING_ENABLED`
   - `activationStatus`: `"ACTIVE"` 或具体原因码（`A500_SAMPLE_INSUFFICIENT` / `A500_DATA_STALE` / `A500_METRICS_INCOMPLETE` / `A500_SOURCE_NOT_APPROVED`）
   - `effectiveReleaseFactor`: 启用时使用实际 `releaseFactor`(0.2)，不再强制 1.0
   - 新增诊断字段: `preClampScore`, `clampApplied`, `sourceStability`

3. **更新 `a_share_investment_plan()`** (line ~2234): 固定返回 `multiplier=1.0`，文案改为 "估值数据仅供参考，不参与A股价格温度和资金释放"

4. **更新 `make_signal()`** (line ~2348): 新增 `price_temperature_plan` 字段，包含 A500价格温度数据（MA偏离、回撤、波动率、释放系数）

5. **更新数据质量门回退** (line ~3166): 使用具体原因码替代笼统的 `BLOCKED_BY_A500_PRICE_DATA`

## 删除或禁用的旧逻辑

| 旧字段/函数/文案 | 处理方式 |
|------------------|----------|
| `BLOCKED_BY_A500_PRICE_DATA` (笼统阻塞) | 替换为具体原因码 |
| `LIVE_SCORING_DISABLED_PENDING_STABLE_A500_PRICE_DATA` (warning) | 仅在不满足 eligibility 时触发 |
| `valuation_multiplier = 0.50/0.75/1.25` | 永久固定为 1.0 |
| "沪深300估值代理偏热，未来定投节奏参考基础频率的75%；A500不参与判断" | 替换为 "估值数据仅供参考..." |
| "沪深300估值代理偏冷...125%" | 同上 |
| "沪深300估值代理处于高估区...50%" | 同上 |
| `effectiveReleaseFactor = 1.0` (强制) | 启用时使用实际 releaseFactor |
| `a_share_score = 50.0` (回退值) | 启用时使用实际 finalScore |

## 新逻辑入口

| 函数 | 文件 | 行号 |
|------|------|------|
| `_a500_price_model_eligibility()` | fund_tracker.py | ~914 |
| `calculate_cn_equity_price_temperature()` | fund_tracker.py | ~953 |
| `generate_copilot_snapshot()` (A-share score) | fund_tracker.py | ~3001 |
| `write_copilot_dashboard()` (HTML template) | fund_tracker.py | ~5757 |
| `a_share_investment_plan()` | fund_tracker.py | ~2234 |

## 测试结果

```
命令: python3 -m unittest discover -s tests -v
结果: Ran 60 tests in 0.187s — OK
Passed: 60, Failed: 0, Skipped: 0
```

关键测试:
- `test_pe_failure_does_not_block_price_model`: PASS — PE/PB不再阻塞价格模型
- `test_disabled_a_share_price_model_does_not_gate_hs300_environment`: PASS — HS300环境修正独立
- `test_freeze_dashboard_separates_current_and_historical_amounts`: PASS — 当前/历史分离

## 未完成事项

1. A500公式参数未优化（finalScore=0, preClampScore=-1.766, clampApplied=true — 需后续观察是否大量结果堆积在0）
2. 连续抓取稳定性统计未建立（source_stability=CONDITIONAL_PASS，缺少足够历史运行记录）
3. 美股 nasdaq100_pe_percentile 和 sp500_pe_percentile 仍为 PENDING_PROXY_REVIEW，Dynamic Cash Pool 全局仍为 FREEZE

## Codex后续注意事项

1. **不得重新启用A股PE/PB自动评分** — PE/PB已永久设为 DISPLAY_ONLY, used_in_score=false
2. **不得恢复valuation_multiplier对A股资金的作用** — multiplier已永久固定为1.0
3. **不得删除历史625元执行记录** — allocation_events 表不可变
4. **不得用旧snapshot覆盖新报告** — immutable_decision_snapshot 不可修改
5. **美股阻塞问题仍独立存在** — nasdaq100_pe_percentile/sp500_pe_percentile PENDING_PROXY_REVIEW 需用户显式批准
6. **A500公式参数暂未优化** — preClampScore=-1.766, clampApplied=true, finalScore=0; 不要修改公式阈值或clamp范围
7. **temperature_multiplier_overrides={"a_share": 1.0}** — 不要恢复旧的temperature_multiplier
8. **effectiveReleaseFactor 现在使用实际 releaseFactor** — EXECUTE状态下释放金额会受releaseFactor(0.2)约束


---

# Final A500 Presentation and Semantics Update

**Date**: 2026-06-19
**Run ID**: 2026-06-19 201551_v7-a500-final
**Generated At**: 2026-06-19 20:15:51

## 最终语义

```
A500 finalScore=0 表示极热 / 极度拥挤
分数越高表示越冷（100分=极冷，新增资金环境最友好）
40% 为长期战略目标，不是当前立即买入至40%的指令
releaseFactor=0.2 表示当前仅释放理论A股金额的20%
```

## 最终执行路径

```
战略配置缺口
→ 理论A股分配金额（297.47元）
→ × effectiveReleaseFactor（0.20）
→ 理论可执行A股金额（59.49元）
→ 全局数据质量门（美股PENDING_PROXY_REVIEW → FREEZE）
→ 当前实际执行金额（0元）
```

## 当前实例

```
A股理论分配：297.47元
releaseFactor：0.20
理论可执行：59.49元
当前实际执行：0元
原因：全局资金池因美股代理源待审批而FREEZE
```

## 最终状态

```
A500 Price Model: ACTIVE
A500 Used In Score: Yes
A500 Data: PASS
Global Model: REFERENCE_ONLY
Global Decision: FREEZE
```

A500 子模型 ACTIVE ≠ 全局资金池可执行。

## HTML 展示更新

本轮新增：
- `<meta name="run-id">`, `<meta name="generated-at">`, `<meta name="formula-version">`, `<meta name="data-quality-version">`
- Header 可见 Run ID
- A 股资产卡片：温度等级（极度拥挤）、释放系数、0分=极热说明
- Tab 3 A 股分配明细：长期战略目标说明、理论/可执行/实际三层金额
- Tab 4 A500 子模型状态表：ACTIVE、Final Score、Release Factor 等

本轮删除：
- 重复的 Historical Executed Flow 区块
- 重复的配置依据区块

## Codex 后续禁止事项

1. 不得把 A 股 0 分解释为低估或冷
2. 不得把 40% 解释为立即买入目标
3. 不得恢复 A 股 PE/PB 自动评分
4. 不得恢复 0.75 估值乘数
5. 不得让 temperature_multiplier 和 releaseFactor 重复作用
6. 不得删除历史 625 元执行记录
7. 不得隐藏理论金额与当前实际金额的区别
8. 不得重新删除 HTML 中的 run_id
9. 不得删除 A500 子模型状态表
10. 不得把 A500 子模型 ACTIVE 等同于全局 EXECUTE

## 测试

```
Ran 60 tests in 0.105s — OK
Passed: 60, Failed: 0, Skipped: 0
```

# US Equity Data Source Audit Handoff

**Date**: 2026-06-19  
**Audit report**: `docs/us-equity-data-source-audit-report.md`

## Governance Boundary

- A500模块保持当前已确认状态，不得在美股数据源任务中修改。本次审计开始时基线为 `ACTIVE`，其 `finalScore=0`、`releaseFactor=0.2`、公式、目标与资金语义均未变更。
- 黄金模型保持不变。
- 战略目标、配置范围、固定定投和历史执行记录保持不变。
- Dynamic Cash Pool继续 `FREEZE`；本次审计未解除、未绕过数据门。

## US Equity Source Conclusions

```text
Nasdaq100 PE / World PE Ratio:
  Verdict: REPLACE
  Interim Role: DISPLAY_ONLY
  Approval: PENDING_PROXY_REVIEW
  Reason: QQQ ETF proxy; underlying PE aggregation and loss-company treatment are not reproducible

S&P500 PE / Multpl:
  Verdict: REPLACE
  Interim Role: DISPLAY_ONLY
  Approval: PENDING_PROXY_REVIEW
  Reason: unofficial HTML source, estimated recent data, recorded timeout, insufficient stability evidence
```

The local 60-month percentile arithmetic passes reproduction, but it must be labelled `recent_5y_percentile`. It is not a long-term historical percentile. Source-page 10-year and full-history percentiles differ materially and remain audit evidence only.

## Double Counting

- Nasdaq and S&P percentile correlation: Pearson `0.8769`, Spearman `0.8771`, N=`60`.
- Valuation overlap verdict: `CONFIRMED_HIGH_REDUNDANCY`; retaining both creates `POSSIBLE_DOUBLE_COUNT`.

# US Equity Replacement Source and History Handoff

## Scope and control status

- Rate history: `LOCAL_RATE_HISTORY_REMEDIATED`. Official FRED DFII5, DFII10, and DFF are aligned monthly from 2010-01 through 2026-06 using `month_end_last_valid_value`; common sample is 198 months.
- US model: `REBUILD_REQUIRED`; Score formula and weights were not changed.
- Dynamic Cash Pool: `FREEZE` retained. No proxy source was approved.
- A500 and Gold: unchanged. Strategic targets, fixed investment, and historical execution were not modified.

## Candidate source handoff

Nasdaq candidates:

1. Nasdaq Global Index Watch — NDX: `CONDITIONAL_CANDIDATE`; correct index object, but historical PE, public methodology, automated access, and licensing are unresolved.
2. Invesco QQQ official fund page: `DISPLAY_ONLY`; official ETF sponsor but QQQ portfolio PE is not Nasdaq-100 index PE.
3. World PE Ratio Nasdaq 100: legacy `DISPLAY_ONLY`, `PENDING_PROXY_REVIEW`; long QQQ-proxy history but incomplete methodology and governance.

S&P 500 candidates:

1. S&P DJI S&P 500 Earnings and Estimate Report: `CONDITIONAL_CANDIDATE`; strongest research candidate, subject to restored access and strict separation of operating/as-reported and actual/estimate fields.
2. S&P DJI official index page: `CONDITIONAL_CANDIDATE`; correct object/provider but no reproducible historical series captured.
3. Robert Shiller Yale data: `CONDITIONAL_CANDIDATE`; deep history but S&P Composite/CAPE continuity differs from modern S&P 500 trailing PE.
4. Multpl S&P 500 PE: legacy `DISPLAY_ONLY`, `PENDING_PROXY_REVIEW`; source history retained.

## Readiness and next action

- Model-design readiness: No.
- Required next state: `MORE_SOURCE_RESEARCH_REQUIRED`.
- Priority: obtain a reproducible index-level Nasdaq-100 valuation history, then validate and snapshot the S&P DJI earnings workbook.
- Stability status: every candidate remains `INSUFFICIENT_EVIDENCE` until at least 20 scheduled attempts meet the defined stability gate.
- The 5-year percentile is explicitly a recent-window statistic and must not be used as a substitute for a long-history design window.

- 5Y TIPS, 10Y TIPS, and Fed Funds create `POSSIBLE_DOUBLE_COUNT` of the rate regime.
- Local FRED rate history is now remediated to 198 aligned monthly observations; valuation-versus-rate interaction still requires a separate model-design review.

## Portfolio Fit

- Known direct Nasdaq-100 funds represent `66.21%` of the current US-equity bucket.
- Known direct S&P 500 exposure is `0%`.
- The remaining `33.79%` is an active global equity fund without local holdings look-through.
- The current 60% Nasdaq / 40% S&P signal weights are not proven portfolio weights.

## Next Phase Rule

The current sources may continue only as audit/display references. They must not be approved automatically and should be replaced before automatic scoring. The next permitted task is source replacement and US valuation model redesign with consistent methodology, dated portfolio look-through, overlap controls, and append-only stability logging.

```text
May apply to lift FREEZE now: No
May submit a future unfreeze application after replacement, revalidation, explicit approval, and full regression tests: Yes
FREEZE already lifted: No
```

## Regression Result

```text
python3 -m unittest discover -s tests -p 'test_*.py'
Ran 66 tests in 0.165s — OK
Passed: 66
Failed: 0
Skipped: 0
```

# QDII Carrier Integration Refactor Handoff

## Implemented boundary

- V7 remains responsible for asset direction, asset amount, strategic position, and release status.
- `/Users/cuichi/Documents/New project/qdii-monitor/carrier_snapshot.json` is the only formal monitor interface; monitor HTML is not parsed.
- QDII monitor data is availability evidence, never an investment signal.
- Limit changes cannot trigger automatic purchase or release.

## Pools and registry

- `NDX_INDEX_QDII_POOL`: 539001 is `ACTIVE_CARRIER`; 016452 is `APPROVED_CARRIER`.
- `GLOBAL_ACTIVE_EQUITY_POOL`: 270023 is `HOLDING_DISPLAY_ONLY`, excluded from NDX temperature, release, and carrier ranking.
- Other monitored Nasdaq-100 funds remain `DISCOVERED` until explicit review.
- Manual additions always start as `WATCHLIST`; approval requires explicit user confirmation.

## Current limit controls

- 016452 official and effective limit: 50 yuan from 2026-06-18.
- 021000 official limit: 1,000 yuan from 2026-06-18; still `DISCOVERED` in V7.
- 019441 observed channel limit: 10,000 yuan with `limit_volatility_flag=true`; it remains ineligible.
- Missing limits never become unlimited capacity.

## Current decision state

- QDII integration: implemented; shared snapshot is the active interface.
- Dynamic Cash Pool: `FREEZE`.
- Current release: 0 yuan.
- Historical executed amount: 625 yuan, read-only.
- Approved NDX carrier capacity: 150 yuan under the latest snapshot.
- No Score, A500, Gold, strategic target, fixed-investment, proxy-approval, or historical-execution rule was changed.

## Next operational step

Continue refreshing the snapshot and review DISCOVERED funds through the registry workflow. Do not approve a carrier from Availability Score alone. When a future independent asset decision passes the V7 gate, the user must still confirm carrier and actual amount before holdings are written.
