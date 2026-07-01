# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 项目概述

资产配置副驾驶 V7（Asset Allocation Copilot V7）/ fund_tracker——个人投资组合的回撤监控、市场温度（Market Temperature）和动态资金配置决策系统。纯 Python + SQLite，无 Web 框架（仅 `http.server`），领域语言为中文。

**重要：Git 仓库根目录是上一级 `New project/`，不是 `fund_tracker/`。** 命令在 `fund_tracker/` 内运行，但 git 操作会跨越同级项目（尤其是 `../qdii-monitor/`，见下文 QDII 依赖）。

## 常用命令

所有命令从 `fund_tracker/` 目录运行。

```bash
# 完整日常更新：拉净值、写提醒、导出 dashboard/report.json
python3 fund_tracker.py --update --report --alert --export

# 首次建库 / 修复历史（回填近一年净值与宏观）
python3 fund_tracker.py --update --backfill-year --report --alert --export

# 强制回填美股 PE 最近 60 个月
python3 fund_tracker.py --backfill-pe-history --export

# 本地服务（监控页 + 配置页，端口 8765）
python3 local_server.py
#   http://127.0.0.1:8765/dashboard.html
#   http://127.0.0.1:8765/settings.html

# 测试（pytest 7.x，约 272 个 unittest 风格用例，必须从项目根运行）
python3 -m pytest tests/
python3 -m pytest tests/test_ndx_shadow_run.py                       # 单文件
python3 -m pytest tests/test_decision_snapshot.py::ClassName::test_x  # 单用例

# 迁移历史遗留在根目录的产物到 reports/legacy/{timestamp}/（只移动不删除）
python3 scripts/organize_outputs.py
```

依赖很少：`akshare==1.18.64`（A股估值，见 `requirements-audit.txt`）、`python-dateutil`（`ndx_shadow_run.py` 的时区），其余几乎全是标准库（`urllib` 做 HTTP、`sqlite3`、`http.server`）。`.audit-runtime/` 是审计脚本用的 vendored pandas/numpy——**不要把它当业务代码读或改**。

无 lint/format 配置；遵循现有代码风格。

## 架构总览

### 主管线（`fund_tracker.py`，约 7400 行单体编排器，103 个顶层函数）

`main()` 是唯一入口，按固定顺序串起整条管线：

```
load_config() → connect_db() → sync_funds()
  → update_nav/macro/valuation/index_price_history()   # --update 时
  → generate_macro_report() → generate_market_temperature()
  → generate_report()（回撤信号 + 写 alerts）
  → generate_copilot_snapshot()（V7 配置决策，核心）
  → model_risk.get_decision_snapshot()
  → write_report_json() + write_copilot_dashboard()     # 导出
```

`fund_tracker.py` 同时承担取数、建表、计算、HTML 渲染。改动时优先复用其中已有的 fetch/score/render 辅助函数，而不是新建并行实现。

### 治理状态机（本仓库最核心的概念，跨 `fund_tracker.py` + `model_risk.py` + `ndx_shadow_run.py`）

每个模型/快照携带一组**治理状态字段**，它们决定系统是否允许真正动钱：

- `data_status` — 数据质量闸门结果（`data_layer_audit` + `model_risk.run_data_quality_gate`）
- `model_status` — 例如 `UNDER_VALIDATION`
- `validation_stage` — 例如 `OFFLINE_VALIDATION` → `OFFLINE_PASS`
- `activation_status` — 例如 `NOT_ACTIVE`
- `decision_status` / `dynamic_cash_pool_status` — 例如 `FREEZE`

**默认一切冻结（FREEZE / NOT_ACTIVE）。** 新模型先以「影子（shadow）」方式运行：只计算候选释放金额，绝不移动 `DynamicCashPool`。模型必须通过多日前瞻影子验证才可能被提升为激活态。任何放宽闸门、跳过 FREEZE、或让影子模型直接动钱的改动都是对设计意图的违背——除非用户明确要求，否则不要这么做。

### 模型计算模块（被设计为无副作用，纯函数）

- `ndx_price_temperature.py`（`NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED`）——纳指 100 价格温度，**刻意零副作用**，只产出影子候选；激活与资金池执行属外部治理决策。
- `cn_equity_temperature.py`（`CN_EQUITY_PRICE_TEMP_V1`）——纯价格的 A 股温度（中证A500 / 沪深300）。
- `model_risk.py`（`MODEL_VERSION = V7.3`）——评分语义、数据质量闸门、配置路由、决策快照。

**评分语义不变量（贯穿全仓库）：分数越高 = 当下越值得新增配置。** 因此高实际利率 / 高政策利率会*降低*黄金分数，通胀预期会*提高*它。任何新评分必须遵守这个方向，否则会与既有路由逻辑冲突。

### NDX V1 影子运行（当前活跃工作，分支 `codex/ndx-v1-shadow-run`）

- `ndx_shadow_run.py` — 受治理的三会话（3 个完整美股交易日）状态机，`REQUIRED_COMPLETE_DAYS = 3`，账本 schema `ndx-shadow-ledger-v1`。
- `scripts/run_ndx_shadow_daily.py` — 每日 13:10（SGT）编排器：检查 FRED 新鲜度（`NASDAQ100`、`DFII10`）、就绪时刷新本地 CSV、写一条 SLA 账本、再委托 `scripts/run_ndx_shadow.py`。**它不改 NDX 公式，也不改 V7 报告。**
- 价格来源遵循 SSOT（单一可信源），DFII10 作为宏观输入需与模型 as-of 日期对齐（见近期 commit）。

### 输出路径纪律（`utils/output_paths.py` — 输出路径的 SSOT）

每次运行创建唯一目录 `reports/runs/YYYY-MM-DD_HHMMSS_<phase>/`，内含 `reports/ json/ html/ csv/ logs/` 子目录和 `run-manifest.md`。运行目录通过环境变量 `ASSET_COPILOT_RUN_DIR` 发布给子进程继承（同一逻辑运行内所有产物落在同一目录）。当前可访问页面同步到 `dist/`。

**不要把生成产物写到项目根目录**——这是被刻意禁止的；遗留产物用 `scripts/organize_outputs.py` 归档。通过 `output_paths.get_*_path()` 取写入路径，不要硬编码。

控制运行身份的环境变量：`ASSET_COPILOT_PHASE`、`ASSET_COPILOT_VERSION`、`ASSET_COPILOT_TASK_NAME`、`ASSET_COPILOT_RUN_DIR`/`RUN_ID`、`ASSET_COPILOT_CARRIER_SNAPSHOT_PATH`、`ASSET_COPILOT_CARRIER_AS_OF`。

### 数据闸门与来源治理

- `data_layer_audit.py` — Phase-1 净值审计闸门（阻断码 `FUND_NAV_AUDIT_FAILED`），覆盖率需 ≥ 0.80，滞后阈值对 QDII 更宽松（PASS≤4天 / WARN≤7天 vs 普通 2/5）。
- `source_approval.py` — 用户控制的来源审批注册表（`data/approved-sources.json`）。非官方评分源需用户显式批准；`PENDING_PROXY_REVIEW` **不允许进入评分/执行**，低置信度不可批准。改动评分源前先看这里的状态。

### QDII 载体（跨仓库只读依赖）

`qdii_carrier.py` 读取 **`../qdii-monitor/carrier_snapshot.json`**（同级独立项目，外部维护）。本模块**只读**：不发现、不审批、不新增、不买入基金——快照中存在的基金即视为已批准。修改本仓库时不要试图在此生成或变更基金白名单。

### 本地服务与配置

`local_server.py`（裸 `http.server`）提供 `dashboard.html`、`settings.html`。配置页保存即写回 `config.json` 并同步数据库、刷新监控页。`config.json` 是基金清单、持仓、仓位上限、回撤补仓档、V7 战略配置（`copilot_v7`）与市场温度数据源的真相源。

## 关键不变量与约束（非显而易见，改动前务必遵守）

- **执行流水不可变 / 不反推：** `allocation_events`（含 `plan_amount`、`deploy_amount`、`executed_at`）和 `fund_execution_log` 是只增交易记录。`executedAmount` **绝不**从持仓市值或涨跌反推。`CurrentValue`/`TargetValue`/`GapValue`/`Gain/Loss` 可随市值更新，但不得覆盖本月执行金额与原始分配。
- **DynamicCashPool 只由两件事改变：** 月度新增资金、实际释放金额。其它任何路径都不该改它。
- **缺失不插值：** 美股 PE 缺失月份不插值；美股估值评分仅在 Nasdaq-100 与 S&P 500 样本各 ≥ 20 且 `metric_type` 相同时启用。
- **缓存与降级：** 市场温度数据缓存 24 小时，更新失败保留最近一次成功数据。
- **公式版本一致性：** 跨产物的 `formula_version` 必须一致（`model_risk.FORMULA_VERSION` 拼接 A股/NDX/黄金/配置四段）；改模型同时更新版本号。

## 调度（macOS launchd）

- `com.codex.fund-tracker-update.plist` → 每日 09:10 执行 `scripts/run_scheduled_update.sh`（预建运行目录后跑完整 `--update --report --alert --export`）。
- `com.codex.ndx-shadow-1310.plist` → 每日 13:10（`TZ=Asia/Singapore`）执行 NDX 影子运行；安装/卸载见 `scripts/install_ndx_shadow_launchagent.sh` / `uninstall_*`。

## 文档

设计与交接记录在 `docs/`（如 `ndx-price-temperature-v1-report.md`、`ndx-v1-engineering-final-report.md`、`qdii-carrier-integration-refactor-report.md`、各 `ui-*` 报告）。接手 NDX 或 QDII 相关工作前，先读对应的 handoff/closeout 报告了解既定约束与验收口径。
