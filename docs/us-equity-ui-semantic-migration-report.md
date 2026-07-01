# US Equity UI Semantic Migration Report

## Executive Verdict

本次迁移完成。`LEGACY_US_EQUITY_SCORE = RETIRED`，Nasdaq100 PE 与 S&P500 PE 均为 `DISPLAY_ONLY`、`used_in_score=false`、`used_in_release_factor=false`、`blocking=false`。NDX 资产模型维持 `UNDER_VALIDATION`，Dynamic Cash Pool 维持 `FREEZE`，当前释放金额为 0 元。

## Homepage Redesign

首页资产卡调整为 A股、纳指指数型QDII、全球主动权益、黄金四类。顶部决策卡直接披露 A股资产层、纳指资产层、黄金资产层和 QDII 载体层状态。新增海外权益结构与 QDII 执行能力摘要，首页不再展示旧美股 Score。

## Overseas Equity Split

海外权益拆为：

- NDX 指数型 QDII 池：14,824 元，占海外权益 66.2%。
- 全球主动权益池：7,565 元，占海外权益 33.8%。
- 海外权益合计：22,389 元。

金额由 `config.json` 实际持仓与 `qdii_carrier_snapshot.json` 基准分类动态计算，未硬编码。

## Legacy US Score Retirement

旧 `60% × PE估值 + 40% × 利率流动性` 的美股 Score 不再写入 `scores.us_equity`。海外权益战略目标保持 40%，临时不使用 Score 调整。NDX 价格温度与单一实际利率因子均处于验证状态，不生成自动释放。

## PE Display-Only Migration

Nasdaq100 PE 和 S&P500 PE 原始值及近 60 个月百分位保留在估值参考区。两者退出数据质量阻断项、Score 和 release factor。五年窗口百分位仍明确标注不代表长期历史百分位。

## QDII Multi-Select Carrier Selector

`qdii_carrier_snapshot.json` 内基金均视为人工批准白名单。V7 不再包含 `DISCOVERED`、`WATCHLIST`、`APPROVED_CARRIER` 或 `ACTIVE_CARRIER` 状态流，也不再提供手动添加或状态升级接口。

选择器支持：

- 多选任意 NDX 白名单基金；
- 手动调整各载体金额；
- 实时显示已选容量、已分配金额、未覆盖金额和超额选择金额；
- 超过三只时提示指数重合导致的载体复杂度；
- 当前 FREEZE 时只允许预览，执行按钮禁用。

## Carrier Comparison Rules

透明排序顺序为：已有持仓、尽量减少基金数量、单只覆盖剩余缺口、跟踪误差、费率、额度稳定性、渠道便利、基金规模与成立时间。页面只显示字段和标签，不生成黑箱 Carrier Score。

缺失的管理费、托管费、销售服务费、基金规模和成立时间显示“待补齐”，不以 0 替代。万家 019441 明确标注额度波动与执行前复核要求。

## I-Class Handling

021000 被识别为 I 类。当监控快照显示渠道可用且有效限额大于 0 时，`personal_purchase_supported=true`，正常进入白名单、多选与透明排序，不因 I 类身份默认排除。

## Guangfa Display-Only Treatment

270023 广发全球精选属于 `GLOBAL_ACTIVE_EQUITY_POOL`，角色为 `HOLDING_DISPLAY_ONLY`。它计入总资产、海外权益仓位和收益偏离，固定定投保持正常；不使用 Nasdaq-100 温度、不进入 NDX 载体列表、不参与动态释放。

## UI Screens

浏览器验收确认：

- 首页四卡、结构摘要和 QDII 容量摘要正常渲染；
- 配置页顺序为当前配置、海外权益拆分、NDX 机会、QDII 多选、全球主动权益、历史执行、触发审计；
- 多选 021000 并输入 100 元后，已选容量从 150 元更新为 1,150 元，已分配金额更新为 100 元，超额选择金额更新为 100 元；
- FREEZE 执行按钮保持 disabled。

## Test Results

单元和回归测试覆盖 JSON 白名单、海外权益拆分、I 类渠道逻辑、多选容量、透明标签、额度波动、缺失字段、复杂度提示、FREEZE 控制和受保护模型。最终结果：99 项通过，0 项失败。

## Regression Results

- A500 价格温度逻辑未改动。
- 黄金 Score 公式与当前结果未改动。
- 战略目标配置未改动。
- 固定定投未改动。
- 历史 625 元执行记录未改动，只在历史区展示。
- 未解除 Dynamic Cash Pool FREEZE。

## Remaining Risks

1. NDX 价格温度与单一实际利率因子尚未完成方法论验证，不能用于自动释放。
2. 多数基金管理费、托管费、销售服务费、规模和成立时间在当前快照中缺失，透明排序只能使用已有字段。
3. 当前渠道额度来自监控快照，万家 019441 波动明显，未来执行前必须重新核验。
4. 多选结果当前为前端预览；只有 NDX 资产模型正式通过并解除相关阻断后，才能接入执行确认写回。

## Change Log

- 重写 `qdii_carrier.py` 为只读 JSON 白名单与透明容量模块。
- 删除 QDII 注册表文件、手动添加接口和状态升级接口。
- 退役旧美股 Score 并将 PE 调整为 Display Only、Non-Blocking。
- 新增海外权益动态拆分、首页四卡和执行能力摘要。
- 新增 QDII 多选、金额调整、实时容量与透明标签。
- 修正数据审计页和 validation result 的海外权益模型治理字段。
- 新增专项回归测试。
