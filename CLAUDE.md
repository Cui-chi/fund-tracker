# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

资产配置副驾驶 V7（Asset Allocation Copilot V7）/ fund_tracker——个人投资组合的回撤监控、市场温度（Market Temperature）和动态资金配置决策系统。纯 Python + SQLite，无 Web 框架（仅 `http.server`），领域语言为中文。

**重要：`fund_tracker/` 自己就是 git 仓库根目录**（`fund_tracker/.git`，远程 `git@github.com:Cui-chi/fund-tracker.git`），git 命令直接在 `fund_tracker/` 内运行即可，不需要跳到上一级。`../qdii-monitor/` 是同级的独立项目（外部维护、自己的仓库），本仓库只在文件系统层面只读它的快照文件，见下文 QDII 依赖；两者不是同一个 git 仓库，也不是 submodule 关系。

> 历史遗留（已归档）：`fund_tracker/` 所在的上一级目录 `New project/` 曾经存在一个本地 `.git`（无远程），吸收过本项目源码的完整副本。已确认非权威后，该 `.git` 已重命名为 `New project/.git.archived-2026-07-01-non-authoritative`（历史保留，未删除），`New project/` 现在不再是 git 仓库。如遇到 git 操作行为反常，先用 `git rev-parse --show-toplevel` 确认当前在哪个 `.git` 下操作。

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

# 测试（pytest 7.x，约 377 个 unittest 风格用例，必须从项目根运行）
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

### NDX V1 影子运行与激活（已于 2026-07 完成 5/5 影子验证并激活）

- `ndx_shadow_run.py` — 受治理的五会话（5 个完整美股交易日）状态机，`REQUIRED_COMPLETE_DAYS = 5`，账本 schema `ndx-shadow-ledger-v1`。`approve_manual_activation()` 是唯一的人工激活入口：要求账本已 `SHADOW_COMPLETE` 且天数达标，写入 `activation_status=ACTIVE` 与 `activation_audit`，**但同时把 `first_activation_guard=True` / `first_activation_guard_status=PENDING_MANUAL_CONFIRMATION` 写进账本**——激活本身不等于允许正式放钱。
- `fund_tracker.py:ndx_activation_gate_status()` 是正式决策是否能打开的唯一闸门：读账本的 `activation_status` + `first_activation_guard`，只要 guard 处于 `PENDING_MANUAL_CONFIRMATION` 就返回 `allow_formal_decision=False`（`first_activation_confirmation_required=True`）。**这个闸门曾经存在过"只记录不强制"的漏洞（激活后首个快照直接进 EXECUTE 并给出非零释放金额），已在 `525e42e` 修复**——任何改动 NDX 决策链的代码都必须保证 `generate_copilot_snapshot` 走这个函数而不是绕过它。
- `scripts/run_ndx_shadow_daily.py` — 每日 13:10（SGT）编排器：检查 FRED 新鲜度（`NASDAQ100`、`DFII10`）、就绪时刷新本地 CSV、写一条 SLA 账本、再委托 `scripts/run_ndx_shadow.py`。**它不改 NDX 公式。** 影子日执行成功后会额外触发一次轻量 dashboard 重渲染（复用 `local_server.rebuild_outputs()`，不重新抓取 NAV/宏观/估值数据），让首页 Shadow Day X/Y 与 Today's Focus 不必等到次日 09:10 才刷新；该刷新是尽力而为（best-effort），失败不影响已记录的影子成功与账本，且**必须在 SLA 记录写盘之后再触发**（曾经顺序反了，导致刚跑完的当日在自动化历史里被误判成「电脑离线」，已修复）。
- 价格来源遵循 SSOT（单一可信源），DFII10 作为宏观输入需与模型 as-of 日期对齐（见近期 commit）。
- `daily_automation_status.py` — 纯函数、可测的中文状态映射层（「每日自动化」「自动化历史」两个 Tab 用），把 `SHADOW_EXECUTED`/`SHADOW_FAILED`/`FREEZE` 等英文枚举翻译成统一中文状态（含颜色、是否异常、是否影响 Graduation/DCP）。只读账本/SLA/prepared 产物，不做任何计算或改动。这两个 Tab 目前在导航里被 `style="display:none"` 隐藏（见下文本地服务小节），代码与数据均保留。

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

- **持仓管理（Portfolio Management）** 是持仓金额的唯一编辑/新增入口，走 `POST /api/portfolio`：请求体带 `"action": "create"` → `apply_portfolio_create()`（新增，代码查重、必填校验、资产类别限定四选一）；不带则走 `apply_portfolio_update()`（编辑，未知代码直接报错、绝不静默新增）。新增的持仓复用现有 fund JSON 结构（`type`/`max_holding_amount` 等填安全默认值，避免 `sync_funds` 因缺字段崩溃）。
- 一级导航里的「每日自动化」「自动化历史」两个 Tab 当前用 `style="display:none"` 隐藏（页面结构、数据、`daily_automation_status.py` 均未删除），需要排查自动化问题时删掉这行内联样式即可恢复入口。
- **改了 `local_server.py` 后必须重启常驻进程才会生效**（Python 不热加载源码；launchd 配置了 `com.codex.fund-tracker`，`kill` 掉旧 PID 后会自动重启到新代码）。只改 `fund_tracker.py` 的渲染逻辑、需要看到效果时同理——要么等下次 `rebuild_outputs()`，要么重启常驻服务触发一次。

## 关键不变量与约束（非显而易见，改动前务必遵守）

- **执行流水不可变 / 不反推：** `allocation_events`（含 `plan_amount`、`deploy_amount`、`executed_at`）和 `fund_execution_log` 是只增交易记录。`executedAmount` **绝不**从持仓市值或涨跌反推。`CurrentValue`/`TargetValue`/`GapValue`/`Gain/Loss` 可随市值更新，但不得覆盖本月执行金额与原始分配。
- **DynamicCashPool 只由两件事改变：** 月度新增资金、实际释放金额。其它任何路径都不该改它。
- **缺失不插值：** 美股 PE 缺失月份不插值；美股估值评分仅在 Nasdaq-100 与 S&P 500 样本各 ≥ 20 且 `metric_type` 相同时启用。
- **缓存与降级：** 市场温度数据缓存 24 小时，更新失败保留最近一次成功数据。
- **公式版本一致性：** 跨产物的 `formula_version` 必须一致（`model_risk.FORMULA_VERSION` 拼接 A股/NDX/黄金/配置四段）；改模型同时更新版本号。
- **首次激活护栏：** 模型从影子验证转为 `ACTIVE` 后，`first_activation_guard`/`first_activation_guard_status` 必须能真正拦住第一次正式决策（见上文 NDX 小节），不能只是记录在账本里展示。任何新增的"激活/毕业"类治理状态都要遵循同一模式：状态位必须接到真实闸门上，不能只写不判。

## 调度（macOS launchd）

- `com.codex.fund-tracker-update.plist` → 每日 09:10 执行 `scripts/run_scheduled_update.sh`（预建运行目录后跑完整 `--update --report --alert --export`）。
- `com.codex.ndx-shadow-1310.plist` → 每日 13:10（`TZ=Asia/Singapore`）执行 NDX 影子运行；安装/卸载见 `scripts/install_ndx_shadow_launchagent.sh` / `uninstall_*`。

## 文档

设计与交接记录在 `docs/`（如 `ndx-price-temperature-v1-report.md`、`ndx-v1-engineering-final-report.md`、`qdii-carrier-integration-refactor-report.md`、各 `ui-*` 报告）。接手 NDX 或 QDII 相关工作前，先读对应的 handoff/closeout 报告了解既定约束与验收口径。
