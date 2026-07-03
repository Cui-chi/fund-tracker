"""统一的「每日自动化」中文状态体系（仅用于展示/分类）。

本模块**只做状态映射与展示语义**：读取管线已经产出的原始英文状态（`final_status`、
账本 `status`、`dfii10_lag_status`、载体字段等），返回描述统一中文状态的普通字典，
包含：中文名、颜色、是否异常、是否需人工、是否影响 Graduation、是否影响 Dynamic
Cash Pool。

它**不读文件、不写文件、不改任何 Shadow 核心业务逻辑 / Graduation / Ledger /
自动执行逻辑**——纯函数，给普通 dict/str 进，出普通 dict。日志层仍可保留英文 enum；
本模块负责把英文 enum 翻译成用户能在 5 秒内看懂的中文。

设计原则（对应 spec）：
- 状态具有唯一含义，不把所有异常都归类为「系统异常」。
- 正常的「等待 / 市场限制」（黄/灰）与真正的「系统异常」（红）严格区分。
"""

# ── 语义颜色 token（渲染层再映射到具体 CSS 类，模块本身不含 CSS） ──
GREEN = "green"    # 成功 / 通过
BLUE = "blue"      # 正常进行中 / 冻结（设计使然，非问题）
YELLOW = "yellow"  # 等待 / 市场限制（非异常）
ORANGE = "orange"  # 数据 / 输入异常（需关注，多数可自愈）
RED = "red"        # 系统异常（真故障，需人工）
GRAY = "gray"      # 未开始 / 不适用 / 跳过

# ── 统一中文状态注册表：每个状态携带 spec 要求的完整语义契约 ──
STATES = {
    "EXECUTED": {
        "label": "执行成功",
        "color": GREEN,
        "trigger": "当日影子运行完整通过并计入账本（SHADOW_EXECUTED）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": True,   # 当日计入，+1 天
        "affects_dcp": False,         # 仍保持冻结，不动钱
    },
    "ALREADY_DONE": {
        "label": "今日已完成",
        "color": BLUE,
        "trigger": "目标交易日已计入过，跳过以防重复计数（ALREADY_COMPLETED）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": False,
    },
    "WAIT_SESSION": {
        "label": "等待下一交易日",
        "color": GRAY,
        "trigger": "尚无完整的美股交易日可评估（NO_COMPLETE_SESSION）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": False,
    },
    "WAIT_DATA": {
        "label": "等待数据就绪",
        "color": YELLOW,
        "trigger": "FRED 行情/利率尚未发布到目标日，等待下次刷新（NOT_READY）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": False,
    },
    "MARKET_LIMIT": {
        "label": "市场数据未对齐",
        "color": YELLOW,
        "trigger": "数据源日期领先于目标交易日，等待对齐（AS_OF_MISMATCH）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": False,
    },
    "DATA_ERROR": {
        "label": "数据异常",
        "color": ORANGE,
        "trigger": "本地 CSV 刷新失败或输入数据不一致（LOCAL_REFRESH_FAILED）",
        "is_anomaly": True, "needs_manual": False,  # 多为瞬时/网络，通常次日自愈
        "affects_graduation": False, "affects_dcp": False,
    },
    "SNAPSHOT_NOT_READY": {
        "label": "模型快照未就绪",
        "color": ORANGE,
        "trigger": "报告缺失或 NDX/DFII10 身份无效，未能生成合法预备快照"
                   "（MODEL_SNAPSHOT_NOT_READY / NO_REPORT）",
        "is_anomaly": True, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": False,
    },
    "SYSTEM_ERROR": {
        "label": "系统异常",
        "color": RED,
        "trigger": "出现未预期的崩溃或未知状态，需人工排查",
        "is_anomaly": True, "needs_manual": True,
        "affects_graduation": False, "affects_dcp": False,
    },
    "IN_PROGRESS": {
        "label": "运行进行中",
        "color": BLUE,
        "trigger": "数据已就绪，正在执行影子运行（READY，中间态）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": False,
    },
    # ── 账本 / 毕业维度 ──
    "VALIDATING": {
        "label": "验证进行中",
        "color": BLUE,
        "trigger": "影子验证累计中，尚未满足所需完整交易日数",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": True, "affects_dcp": True,  # 未满足前 DCP 保持冻结
    },
    "GRAD_COMPLETE": {
        "label": "影子验证完成",
        "color": GREEN,
        "trigger": "已累计满所需完整交易日数（SHADOW_COMPLETE）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": True, "affects_dcp": True,
    },
    "LAST_ATTEMPT_FAILED": {
        "label": "上次尝试未计入",
        "color": ORANGE,
        "trigger": "最近一次计入尝试未通过账本闸门（SHADOW_FAILED）；"
                   "属闸门正常拒绝，非系统崩溃，进度不倒退",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": True, "affects_dcp": True,
    },
    "MANUAL_REVIEW": {
        "label": "人工处理中",
        "color": ORANGE,
        "trigger": "已完成影子验证，等待人工激活审查（MANUAL_ACTIVATION_REVIEW）",
        "is_anomaly": False, "needs_manual": True,
        "affects_graduation": True, "affects_dcp": True,
    },
    # ── 治理维度 ──
    "FROZEN": {
        "label": "策略冻结",
        "color": BLUE,
        "trigger": "决策/资金池处于 FREEZE（默认设计使然，模型未激活前不动钱）",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": True,
    },
    "UNKNOWN": {
        "label": "未知状态",
        "color": GRAY,
        "trigger": "无数据或状态缺失",
        "is_anomaly": False, "needs_manual": False,
        "affects_graduation": False, "affects_dcp": False,
    },
}

# 原始 final_status → 统一状态 key。未列出的值一律落到 SYSTEM_ERROR（安全默认：
# 真正没预料到的值应被醒目标为需排查，而不是静默当成正常）。
FINAL_STATUS_MAP = {
    "SHADOW_EXECUTED": "EXECUTED",
    "ALREADY_COMPLETED": "ALREADY_DONE",
    "NO_COMPLETE_SESSION": "WAIT_SESSION",
    "NOT_READY": "WAIT_DATA",
    "AS_OF_MISMATCH": "MARKET_LIMIT",
    "LOCAL_REFRESH_FAILED": "DATA_ERROR",
    "MODEL_SNAPSHOT_NOT_READY": "SNAPSHOT_NOT_READY",
    "NO_REPORT": "SNAPSHOT_NOT_READY",
    "READY": "IN_PROGRESS",
}


def state(key, raw=None):
    """返回某个统一状态的完整字典副本（附带 key 与原始 raw 值）。"""
    base = dict(STATES.get(key, STATES["UNKNOWN"]))
    base["key"] = key if key in STATES else "UNKNOWN"
    base["raw"] = raw
    return base


def classify_final_status(final_status):
    """把每日运行的 final_status 映射成统一中文状态。"""
    if not final_status:
        return state("UNKNOWN", raw=final_status)
    return state(FINAL_STATUS_MAP.get(final_status, "SYSTEM_ERROR"), raw=final_status)


def classify_ledger_status(ledger_status):
    """把账本 status 映射成统一中文状态（用于「毕业 / 账本」维度）。"""
    if not ledger_status:
        return state("UNKNOWN", raw=ledger_status)
    if ledger_status == "SHADOW_COMPLETE":
        key = "GRAD_COMPLETE"
    elif ledger_status == "SHADOW_FAILED":
        key = "LAST_ATTEMPT_FAILED"
    elif ledger_status == "MANUAL_ACTIVATION_REVIEW":
        key = "MANUAL_REVIEW"
    elif ledger_status.endswith("_PASS") or ledger_status.endswith("_PENDING"):
        key = "VALIDATING"
    else:
        key = "VALIDATING"
    return state(key, raw=ledger_status)


def classify_dcp_status(dcp_status):
    """动态资金池状态 → 统一中文状态。目前只会是 FREEZE（设计使然）。"""
    if dcp_status in (None, "", "FREEZE"):
        return state("FROZEN", raw=dcp_status or "FREEZE")
    # 非 FREEZE 属于模型已激活后的世界，当前不应出现；如出现则标为需排查。
    return state("SYSTEM_ERROR", raw=dcp_status)


# ── 载体展示：把 true/false 翻成中文 ──
def carrier_display(carrier):
    """单个 QDII 载体 → 全中文展示字段 + 一个「最终结果」判定。"""
    personal = bool(carrier.get("personal_purchase_supported"))
    channel = bool(carrier.get("channel_available"))
    capacity = float(carrier.get("effective_limit_rmb") or 0)
    raw_purchase = carrier.get("purchase_status")
    purchase = str(raw_purchase) if raw_purchase not in (None, "", "--") else "待补齐"

    # 最终结果：能不能真正作为可执行载体（仅展示判定，不参与任何自动决策）。
    if not personal:
        result, result_color = "个人不可买", GRAY
    elif not channel:
        result, result_color = "渠道待补齐", YELLOW
    elif "暂停" in purchase or "限制" in purchase:
        result, result_color = "申购受限", ORANGE
    elif capacity <= 0:
        result, result_color = "额度不足", ORANGE
    else:
        result, result_color = "可执行", GREEN

    return {
        "code": carrier.get("fund_code", ""),
        "name": carrier.get("fund_name", ""),
        "purchase_status": purchase,
        "channel_text": "可买" if channel else "不可买",
        "channel_color": GREEN if channel else GRAY,
        "personal_text": "可买" if personal else "不可买",
        "personal_color": GREEN if personal else GRAY,
        "capacity": capacity,
        "held": bool(carrier.get("current_holding")),
        "result": result,
        "result_color": result_color,
    }


# ── 执行流程步骤（LaunchAgent → … → Graduation），每步成功/失败/跳过/等待 ──
STEP_OK = ("成功", GREEN)
STEP_FAIL = ("失败", RED)
STEP_SKIP = ("跳过", GRAY)
STEP_WAIT = ("等待", YELLOW)


def carrier_gate(data_status, selection_status):
    """载体闸门 → (中文步骤状态, 颜色, 说明)。区分「额度不足」与「真正阻断」。"""
    if str(data_status) != "ACTIVE":
        return ("失败", RED, "载体快照不可用")
    if selection_status == "AVAILABLE":
        return ("成功", GREEN, "载体数据可用")
    if selection_status == "PARTIAL_CAPACITY":
        return ("等待", YELLOW, "载体可用但额度不足")
    return ("失败", RED, "载体选择被阻断")


def _step(name, status_tuple, detail=""):
    label, color = status_tuple
    return {"name": name, "status": label, "color": color, "detail": detail}


def execution_flow(sla_record, *, ledger_counted_today=False,
                   prepared_status=None, carrier_gate_status=None):
    """根据一次每日运行的 SLA 记录 + 账本/预备快照/载体状态，推导流程各步状态。

    sla_record: run_ndx_shadow_daily.run_once() 落库的一条记录（或 None）。
    """
    r = sla_record or {}
    final = r.get("final_status")
    reached_execute = final == "SHADOW_EXECUTED"
    dfii = r.get("dfii10_lag_status")

    if not r:
        # 今天还没有任何运行记录：整条流程处于等待。
        return [_step(n, STEP_WAIT) for n in (
            "定时触发", "单一可信源", "NDX价格输入", "宏观利率输入",
            "预备快照", "规范哈希", "影子运行", "载体闸门", "账本记录", "毕业进度")]

    steps = []
    steps.append(_step("定时触发", STEP_OK, "LaunchAgent 已按时触发"))
    steps.append(_step("单一可信源", STEP_OK if r.get("fred_ndx_date") else STEP_FAIL,
                       "FRED 为唯一价格源"))

    # NDX 价格输入
    if r.get("local_ndx_date") and r.get("local_ndx_date") == r.get("target_trade_date"):
        steps.append(_step("NDX价格输入", STEP_OK, "已对齐目标交易日"))
    elif final in ("NOT_READY",):
        steps.append(_step("NDX价格输入", STEP_WAIT, "FRED 尚未发布目标日行情"))
    elif final == "AS_OF_MISMATCH":
        steps.append(_step("NDX价格输入", STEP_WAIT, "数据源日期领先，等待对齐"))
    else:
        steps.append(_step("NDX价格输入", STEP_OK if reached_execute else STEP_SKIP))

    # 宏观利率输入（DFII10）
    if dfii in ("FRESH", "ACCEPTABLE_LAG"):
        steps.append(_step("宏观利率输入", STEP_OK,
                           "DFII10 %s" % ("当日" if dfii == "FRESH" else "可接受滞后")))
    elif dfii == "AS_OF_MISMATCH":
        steps.append(_step("宏观利率输入", STEP_WAIT, "DFII10 日期领先，等待对齐"))
    elif dfii == "NOT_READY":
        steps.append(_step("宏观利率输入", STEP_WAIT, "DFII10 尚未发布"))
    else:
        steps.append(_step("宏观利率输入", STEP_SKIP))

    # 预备快照
    if prepared_status == "PASS" or reached_execute:
        steps.append(_step("预备快照", STEP_OK, "字段校验通过"))
    elif final in ("MODEL_SNAPSHOT_NOT_READY", "NO_REPORT"):
        steps.append(_step("预备快照", STEP_FAIL, "报告缺失或身份无效"))
    else:
        steps.append(_step("预备快照", STEP_SKIP))

    # 规范哈希 / 影子运行 / 载体闸门 / 账本
    steps.append(_step("规范哈希", STEP_OK if reached_execute else STEP_SKIP))
    if reached_execute:
        steps.append(_step("影子运行", STEP_OK, "执行成功"))
    elif final in ("MODEL_SNAPSHOT_NOT_READY", "NO_REPORT", "LOCAL_REFRESH_FAILED"):
        steps.append(_step("影子运行", STEP_FAIL))
    else:
        steps.append(_step("影子运行", STEP_SKIP))
    if carrier_gate_status is None:
        carrier_gate_status = ("成功", GREEN, "载体数据可用")
    steps.append(_step("载体闸门", (carrier_gate_status[0], carrier_gate_status[1]),
                       carrier_gate_status[2] if len(carrier_gate_status) > 2 else ""))
    steps.append(_step("账本记录", STEP_OK if ledger_counted_today else STEP_SKIP,
                       "已计入当日" if ledger_counted_today else "未计入"))
    steps.append(_step("毕业进度", STEP_OK if reached_execute else STEP_WAIT))
    return steps


# ── 毕业进度：把账本 days（成功）与 failures（未计入尝试）都翻成中文单元 ──
def graduation_cells(ledger):
    """返回按日期排序的毕业进度单元列表，每个 {date, label, color, detail}。"""
    cells = []
    for day in (ledger or {}).get("days", []):
        cells.append({
            "date": day.get("market_session_date"),
            "shadow_day": day.get("shadow_day"),
            "label": "成功", "color": GREEN,
            "detail": "温度 %.1f · %s" % (
                float(day.get("temperature_score") or 0),
                day.get("temperature_level") or "-"),
        })
    for fail in (ledger or {}).get("failures", []):
        # 归因：区分「数据异常 / 市场限制 / 系统异常」，避免全归为失败。
        gates = fail.get("failures", [])
        causes = " · ".join(g.get("root_cause", "") for g in gates if g.get("root_cause"))
        label, color = _classify_failure_cause(gates)
        cells.append({
            "date": fail.get("market_session_date"),
            "shadow_day": None,
            "label": label, "color": color,
            "detail": causes or "未计入",
        })
    cells.sort(key=lambda c: (c.get("date") or ""))
    return cells


def _classify_failure_cause(gates):
    """账本失败明细 → (中文归因标签, 颜色)。不把闸门拒绝一律当系统异常。"""
    causes = [str(g.get("root_cause") or "").lower() for g in gates]
    fields = [str(g.get("failed_field") or "").lower() for g in gates]
    joined = " ".join(causes + fields)
    if "hash" in joined or "canonical" in joined or "mismatch" in joined:
        return "数据异常", ORANGE      # 输入/一致性问题，非崩溃
    if "session" in joined or "market" in joined or "trading" in joined:
        return "市场限制", YELLOW
    if "exception" in joined or "crash" in joined or "traceback" in joined:
        return "系统异常", RED
    return "未计入", ORANGE


# ── Root Cause 分层：表面状态 → 直接原因 → 真实 Root Cause ──
def root_cause_layers(sla_record, ledger):
    """三层归因，帮助用户看穿「看似坏了其实只是市场限制/闸门拒绝」。"""
    r = sla_record or {}
    final = r.get("final_status")
    run_state = classify_final_status(final)
    ledger_state = classify_ledger_status((ledger or {}).get("status"))

    # 表面状态：用户第一眼可能看到的原始英文（账本 status 常最吓人）。
    surface = (ledger or {}).get("status") or final or "无数据"

    # 直接原因：这次运行到底发生了什么（中文一句话）。
    if final == "SHADOW_EXECUTED":
        direct = "今日影子运行执行成功"
    elif final in ("NOT_READY",):
        direct = "FRED 数据尚未发布到目标交易日，本次未执行"
    elif final == "AS_OF_MISMATCH":
        direct = "数据源日期领先于目标交易日，等待对齐"
    elif final == "ALREADY_COMPLETED":
        direct = "目标交易日已计入，跳过以防重复"
    elif final == "NO_COMPLETE_SESSION":
        direct = "尚无完整美股交易日可评估"
    elif final in ("MODEL_SNAPSHOT_NOT_READY", "NO_REPORT"):
        direct = "未能生成合法预备快照（报告缺失或身份无效）"
    elif final == "LOCAL_REFRESH_FAILED":
        direct = "本地行情/利率 CSV 刷新失败"
    else:
        direct = "运行结果：%s" % (final or "无记录")

    # 真实 Root Cause：翻成「是否系统坏了」，并解释账本状态与当日结果的关系。
    latest_fail = ((ledger or {}).get("failures") or [None])[-1]
    root_parts = []
    if run_state["key"] == "EXECUTED":
        root_parts.append("系统运行正常，当日已成功计入毕业进度。")
    elif run_state["is_anomaly"] and run_state["key"] == "SYSTEM_ERROR":
        root_parts.append("确为系统异常，需人工排查。")
    elif run_state["key"] in ("WAIT_DATA", "MARKET_LIMIT", "WAIT_SESSION"):
        root_parts.append("并非系统故障，只是市场数据/交易日时序限制，等待下次刷新即可。")
    elif run_state["key"] in ("DATA_ERROR", "SNAPSHOT_NOT_READY"):
        root_parts.append("属数据/输入层面问题（多为瞬时或一致性差异），通常次日自愈，非核心逻辑损坏。")

    if ledger_state["key"] == "LAST_ATTEMPT_FAILED":
        cause_txt = ""
        if latest_fail:
            gates = latest_fail.get("failures", [])
            cause_txt = "；".join(g.get("root_cause", "") for g in gates if g.get("root_cause"))
        root_parts.append(
            "账本显示 SHADOW_FAILED 仅表示最近一次计入尝试被闸门拒绝"
            + ("（%s）" % cause_txt if cause_txt else "")
            + "，已通过的天数不会倒退。")

    return {
        "surface": surface,
        "surface_state": ledger_state,
        "direct": direct,
        "root": " ".join(root_parts) or "暂无进一步归因。",
    }
