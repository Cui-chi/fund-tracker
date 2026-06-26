# 资产配置监控

这个目录用于跟踪基金回撤、资产配置和 Market Temperature 市场温度。

## 输出目录规范

每次 CLI、数据审计、调度审计或本地页面重建都会先创建唯一目录：

```text
reports/runs/YYYY-MM-DD_HHMMSS_phase-or-version/
```

报告、JSON、HTML 快照、CSV 和日志分别写入该目录的 `reports/`、`json/`、`html/`、`csv/`、`logs/` 子目录，并生成 `run-manifest.md`。当前可访问页面同步到 `dist/`，同时保留对应 run 快照。

迁移旧根目录产物：

```bash
python3 scripts/organize_outputs.py
```

迁移只移动文件到 `reports/legacy/{timestamp}/`，不会删除或覆盖历史文件。

## 使用

```bash
python3 fund_tracker.py --update --report --alert --export
```

首次建库或需要修复历史数据时：

```bash
python3 fund_tracker.py --update --backfill-year --report --alert --export
```

脚本会创建并更新本地数据库：

```text
data/fund_tracker.sqlite
```

核心表：

- `funds`：基金配置、持仓、仓位上限
- `nav_history`：每日净值历史
- `macro_history`：TIPS 实际利率和盈亏平衡通胀率历史
- `market_valuation_history`：中证A500、沪深300 PE 与历史百分位
- `market_update_status`：各市场指标最近尝试、成功时间和失败原因
- `pe_history`：美股指数月度PE历史、口径、来源和质量标记
- `alerts`：触发提醒记录

同时会生成两个可视化/导出文件：

- `dist/dashboard.html`：当前本地可视化页面
- `reports/runs/{run_id}/json/report.json`：本次结构化日报数据
- `pe_history.json`：标准化美股PE月度历史
- `pe_history_quality.json`：样本数、缺失月份、口径和评分可用性

强制回填美股PE最近60个月：

```bash
python3 fund_tracker.py --backfill-pe-history --export
```

美股估值评分仅在 Nasdaq-100 与 S&P 500 样本均不少于20、且
`metric_type` 相同时启用。系统不对缺失月份插值。

V7 动态资金规则区分两类配置缺口触发：

- `GapValue` 月度变化：与上月 Baseline 比较，达到组合总值的 3% 才触发常规释放。
- `GapValue` 绝对值：任一风险资产达到组合总值的 10% 时，非初始化月份可触发强释放。
- 初始化首月不使用评分变化、温度变化或 GapValue 变化；是否允许绝对 GapValue 触发由
  `copilot_v7.release_rules.allow_absolute_gap_on_initialization` 控制，且首月最多释放 25%。
- 没有任何具体触发规则成立时，释放比例和金额均为 0。

V7 执行记录与市值字段相互独立：

- `allocation_events` 是不可变交易流水，保存 `plan_amount`、原始分配、
  `deploy_amount`、实际分配和 `executed_at`。
- `fund_execution_log` 保存基金级真实入账：月份、基金代码、基金名称、资产分类、
  计划金额、实际执行金额和执行时间。
- `executedAmount` 不从持仓市值或涨跌反推。
- `CurrentValue`、`TargetValue`、`GapValue` 和 `Gain/Loss` 可随持仓市值更新，
  但不会覆盖本月执行金额和原始分配。
- `DynamicCashPool` 仅由月度新增资金和实际释放金额改变。
- 资产层方案会映射到 `copilot_v7.execution_funds` 指定的基金载体。执行时以确认
  表单中的基金实际金额入账并增加对应基金 `holding_amount`；未执行差额保留在资金池，
  本月状态仍锁定为 `executed`。

## 本地配置页

启动本地服务：

```bash
python3 local_server.py
```

页面地址：

- 监控页：`http://127.0.0.1:8765/dashboard.html`
- 配置页：`http://127.0.0.1:8765/settings.html`

配置页保存后会写回 `config.json`，并立即同步数据库和刷新监控页面。
基金清单支持在配置页新增和删除；删除基金只会移出监控，不清理数据库中的历史净值。

## 当前规则

- 回撤 10%：只提醒观察
- 回撤 20%：低于单只仓位上限才补
- 回撤 30%：动用更高补仓档，但仍不超过仓位上限
- 黄金和固收不按股票基金回撤规则补仓
- `10Y TIPS实际利率` 和 `10Y盈亏平衡通胀率` 仅作为宏观观察指标，不直接触发补仓
- 美股 QDII 和科技类基金使用 `macro_multiplier` 调整 20%/30% 回撤档建议金额
- TIPS 实际利率偏高且 20 日上升至少 20bp：`macro_multiplier = 0.5`
- TIPS 实际利率 20 日下降至少 20bp：`macro_multiplier = 1.25`
- 其他情况：`macro_multiplier = 1.0`

## Market Temperature

- A股估值：中证A500、沪深300 PE(TTM) 与历史百分位
- 黄金环境：5Y TIPS、10Y TIPS、10Y Breakeven 的当前值、日变化、周变化
- Gold Score：按三指标模型输出黄金价值区、黄金友好区、中性区或黄金拥挤区
- 通胀预期：10Y Breakeven 当前值、日变化、周变化
- 综合温度：A股、黄金、通胀分别展示冷、正常、热
- 模块仅展示市场状态
- 黄金温度仅用于长期资产配置环境参考，不生成交易信号或短期预测
- 数据缓存 24 小时，更新失败时保留最近一次成功数据
- 估值数据源支持在 `config.json` 的 `market_temperature.indexes.*.sources` 中配置多个来源
- `022459`、`022485` 的未来计划会结合 A500 与沪深300估值分位展示定投节奏参考

## 调整配置

修改 `config.json` 即可调整：

- `holding_amount`：当前持仓金额
- `max_holding_amount`：单只基金持仓上限
- `drawdown_20_buy_amount`：20% 回撤档补仓金额
- `drawdown_30_buy_amount`：30% 回撤档补仓金额
- `daily_auto_invest` / `weekly_auto_invest`：当前定投金额
- `asset_class`：V7资产分类，可选 `a_share`、`us_equity`、`gold`、`cash`
# fund-tracker
