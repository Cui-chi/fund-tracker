#!/usr/bin/env python3
import datetime as dt
import json
import os
import re
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fund_tracker
import audit_scheduler
from utils import output_paths


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EDITABLE_NUMERIC_FIELDS = {
    "holding_amount",
    "profit_pct",
    "daily_auto_invest",
    "weekly_auto_invest",
    "max_holding_amount",
    "drawdown_20_buy_amount",
    "drawdown_30_buy_amount",
}
NON_NEGATIVE_FIELDS = EDITABLE_NUMERIC_FIELDS - {"profit_pct"}
ASSET_CLASSES = {"a_share", "us_equity", "gold", "cash"}


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_payload(payload, current):
    if not isinstance(payload, dict) or not isinstance(payload.get("funds"), list):
        raise ValueError("配置格式不正确")

    current_by_code = {fund["code"]: fund for fund in current["funds"]}
    updated_funds = []
    seen_codes = set()
    for update in payload["funds"]:
        if not isinstance(update, dict):
            raise ValueError("基金配置格式不正确")
        code = str(update.get("code", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("基金代码必须是6位数字")
        if code in seen_codes:
            raise ValueError(f"基金代码重复：{code}")
        seen_codes.add(code)

        name = str(update.get("name", "")).strip()
        fund_type = str(update.get("type", "")).strip()
        asset_class = str(update.get("asset_class", "")).strip()
        if not name or len(name) > 100:
            raise ValueError(f"{code} 的基金名称不能为空且不能超过100个字符")
        if not fund_type or len(fund_type) > 30:
            raise ValueError(f"{code} 的基金类型不能为空且不能超过30个字符")
        if asset_class not in ASSET_CLASSES:
            raise ValueError(f"{code} 的V7资产分类不正确")

        strategy = update.get("strategy", "")
        if not isinstance(strategy, str) or len(strategy.strip()) > 100:
            raise ValueError(f"{code} 的定投策略不能超过100个字符")

        clean = {
            "code": code,
            "name": name,
            "type": fund_type,
            "asset_class": asset_class,
            "strategy": strategy.strip() or "无",
        }
        current_fund = current_by_code.get(code, {})
        for field in EDITABLE_NUMERIC_FIELDS:
            # Numeric fields are optional: amounts now live in Portfolio
            # Management, so a config save that omits a field (e.g. holding_amount)
            # must preserve the existing value rather than reset it to 0.
            if field not in update:
                clean[field] = round(float(current_fund.get(field, 0) or 0), 2)
                continue
            value = update.get(field, 0)
            if isinstance(value, bool):
                raise ValueError(f"{code} 的 {field} 必须是数字")
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{code} 的 {field} 必须是数字")
            if field in NON_NEGATIVE_FIELDS and value < 0:
                raise ValueError(f"{code} 的 {field} 不能小于0")
            clean[field] = round(value, 2)

        if clean["max_holding_amount"] < clean["holding_amount"]:
            raise ValueError(f"{code} 的持仓上限不能低于当前持仓")
        merged = dict(current_by_code.get(code, {}))
        merged.update(clean)
        updated_funds.append(merged)

    if not updated_funds:
        raise ValueError("基金列表不能为空")

    updated = dict(current)
    if "cash_available" in payload:
        try:
            cash_available = float(payload["cash_available"])
        except (TypeError, ValueError):
            raise ValueError("手工现金必须是数字")
        if cash_available < 0:
            raise ValueError("手工现金不能小于0")
        updated["cash_available"] = round(cash_available, 2)

    if "copilot_v7" in payload:
        incoming = payload["copilot_v7"]
        if not isinstance(incoming, dict):
            raise ValueError("V7配置格式不正确")
        current_copilot = fund_tracker.copilot_config(current)
        try:
            monthly = float(
                incoming.get(
                    "monthly_contribution",
                    current_copilot["monthly_contribution"],
                )
            )
        except (TypeError, ValueError):
            raise ValueError("每月动态资金必须是数字")
        if monthly < 0:
            raise ValueError("每月动态资金不能小于0")
        release_rules = dict(current_copilot["release_rules"])
        incoming_release_rules = incoming.get("release_rules", {})
        if not isinstance(incoming_release_rules, dict):
            raise ValueError("动态资金释放规则格式不正确")
        allow_initial_gap = incoming_release_rules.get(
            "allow_absolute_gap_on_initialization",
            release_rules["allow_absolute_gap_on_initialization"],
        )
        if not isinstance(allow_initial_gap, bool):
            raise ValueError("首月绝对GapValue开关必须为布尔值")
        release_rules["allow_absolute_gap_on_initialization"] = allow_initial_gap
        manual = incoming.get("manual_indicators", {})
        if not isinstance(manual, dict):
            raise ValueError("验证指标格式不正确")
        clean_manual = dict(current_copilot["manual_indicators"])
        for key in clean_manual:
            value = manual.get(key)
            if value in (None, ""):
                clean_manual[key] = None
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是数字或留空")
            if key.endswith("_percentile") and not 0 <= value <= 100:
                raise ValueError(f"{key} 必须在0到100之间")
            clean_manual[key] = round(value, 4)
        updated["copilot_v7"] = {
            "monthly_contribution": round(monthly, 2),
            "execution_funds": current_copilot["execution_funds"],
            "release_rules": release_rules,
            "strategic_allocation": current_copilot["strategic_allocation"],
            "allocation_ranges": current_copilot["allocation_ranges"],
            "manual_indicators": clean_manual,
            "automatic_sources": current_copilot["automatic_sources"],
        }

    updated["funds"] = updated_funds
    return updated


def save_config(config):
    temp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(str(temp_path), str(CONFIG_PATH))


def now_local_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _coerce_amount(raw, label):
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        raise ValueError(f"{label}不能为空")
    if isinstance(raw, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字")
    if value < 0:
        raise ValueError(f"{label}不能小于0")
    return round(value, 2)


def _coerce_profit(raw):
    """Current P/L percent: signed, optional (blank -> None, meaning unknown)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    if isinstance(raw, bool):
        raise ValueError("当前盈亏必须是数字或留空")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("当前盈亏必须是数字或留空")
    if value < -100:
        raise ValueError("当前盈亏不能低于-100%")
    return round(value, 2)


def portfolio_last_updated(config):
    """Most recent holding-edit timestamp across funds and manual cash.

    All stamps share a fixed local offset, so lexicographic max equals
    chronological max.
    """
    stamps = [fund.get("holding_updated_at") for fund in config.get("funds", [])]
    stamps.append(config.get("cash_updated_at"))
    stamps = [stamp for stamp in stamps if stamp]
    return max(stamps) if stamps else None


def apply_portfolio_update(current, payload, now_iso=None):
    """Amounts-only update from Portfolio Management.

    Updates only ``holding_amount`` per submitted fund and the manual
    ``cash_available``; stamps ``holding_updated_at`` / ``cash_updated_at`` on
    every submitted item; leaves every other fund and every non-amount field
    (profit_pct, max_holding_amount, strategy, …) untouched. Returns
    ``(updated_config, last_updated_iso)`` and raises ``ValueError`` on bad input.
    """
    if not isinstance(payload, dict):
        raise ValueError("请求格式不正确")
    now_iso = now_iso or now_local_iso()
    updated = dict(current)
    funds = [dict(fund) for fund in current.get("funds", [])]
    by_code = {fund.get("code"): fund for fund in funds}
    touched = 0

    holdings = payload.get("holdings", [])
    if not isinstance(holdings, list):
        raise ValueError("持仓列表格式不正确")
    for item in holdings:
        if not isinstance(item, dict):
            raise ValueError("持仓项格式不正确")
        code = str(item.get("code", "")).strip()
        fund = by_code.get(code)
        if fund is None:
            raise ValueError(f"未知基金代码：{code}")
        amount = _coerce_amount(item.get("holding_amount"), f"{code} 的持仓金额")
        max_holding = float(fund.get("max_holding_amount", 0) or 0)
        if max_holding and amount > max_holding:
            raise ValueError(
                f"{code} 的持仓金额不能超过持仓上限 {max_holding:.2f}，请在配置页调整上限"
            )
        fund["holding_amount"] = amount
        if "profit_pct" in item:
            fund["profit_pct"] = _coerce_profit(item.get("profit_pct"))
        fund["holding_updated_at"] = now_iso
        touched += 1

    if "cash_available" in payload:
        updated["cash_available"] = _coerce_amount(payload.get("cash_available"), "现金金额")
        updated["cash_updated_at"] = now_iso
        touched += 1

    if touched == 0:
        raise ValueError("没有需要更新的持仓金额")

    updated["funds"] = funds
    return updated, portfolio_last_updated(updated)


def rebuild_outputs(config):
    run_dir = output_paths.create_run_dir("local-rebuild", force_new=True)
    conn = fund_tracker.connect_db()
    try:
        fund_tracker.sync_funds(conn, config)
        fund_tracker.ensure_monthly_contribution(conn, config)
        macro_rows = fund_tracker.generate_macro_report(conn)
        market_temperature = fund_tracker.generate_market_temperature(
            conn,
            config,
            macro_rows=macro_rows,
        )
        rows = fund_tracker.generate_report(
            conn,
            config,
            macro_rows=macro_rows,
            market_temperature=market_temperature,
        )
        copilot = fund_tracker.generate_copilot_snapshot(
            conn,
            config,
            market_temperature,
        )
        history_rows = fund_tracker.allocation_history_rows(conn)
        fund_tracker.write_pe_history_outputs(conn)
        fund_tracker.write_report_json(
            rows,
            macro_rows,
            market_temperature,
            copilot,
            history_rows,
        )
        fund_tracker.write_copilot_dashboard(
            rows,
            macro_rows,
            market_temperature,
            copilot,
            history_rows,
            config=config,
        )
        decision_snapshot = fund_tracker.model_risk.get_decision_snapshot(
            conn,
            copilot["month"],
        )
        fund_tracker.model_risk.write_validation_reports(
            run_dir,
            copilot,
            decision_snapshot,
            macro_rows,
        )
        output_paths.write_run_manifest({
            "phase": "local-rebuild", "task_name": "rebuild current dashboard",
            "decision_status": copilot.get("decision_status", "FREEZE"),
            "data_status": copilot.get("data_status", "FREEZE"),
            "model_status": copilot.get("model_status", "NOT_RUN"),
            "output_files": sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file() and path.name != "run-manifest.md"),
            "blocked_reason": copilot.get("pool_status_reason", ""),
            "next_action": "Review current dashboard",
            "source_data_used": ["data/fund_tracker.sqlite", "config.json"],
            "whether_root_directory_was_modified": "No",
        }, run_dir)
        conn.commit()
    finally:
        conn.close()


class Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        original = self.path
        if self.path == "/dashboard.html":
            self.path = "/dist/dashboard.html"
        elif self.path == "/Asset%20Allocation%20Copilot%20V7.html":
            self.path = "/dist/Asset%20Allocation%20Copilot%20V7.html"
        try:
            return super().send_head()
        finally:
            self.path = original

    def end_headers(self):
        if self.path in ("/", "/dashboard.html", "/report.json", "/settings.html"):
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/audit/status":
            self.send_json({"ok": True, **audit_scheduler.latest_audit_status()})
            return
        if self.path == "/api/config":
            self.send_json(load_config())
            return
        if self.path == "/api/qdii/carriers":
            snapshot = fund_tracker.qdii_carrier.read_snapshot()
            self.send_json({
                "ok": True,
                "snapshot": snapshot,
                "carriers": fund_tracker.qdii_carrier.whitelist_carriers(
                    snapshot, config=load_config()
                ),
                "governance": "JSON_APPROVED_WHITELIST_READ_ONLY",
            })
            return
        if self.path == "/api/copilot":
            try:
                config = load_config()
                conn = fund_tracker.connect_db()
                fund_tracker.ensure_monthly_contribution(conn, config)
                temperature = fund_tracker.generate_market_temperature(conn, config)
                snapshot = fund_tracker.generate_copilot_snapshot(
                    conn,
                    config,
                    temperature,
                )
                conn.commit()
                self.send_json(
                    {
                        "ok": True,
                        "copilot": snapshot,
                        "history": fund_tracker.allocation_history_rows(conn),
                    }
                )
            except Exception as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            finally:
                if "conn" in locals():
                    conn.close()
            return
        if self.path == "/":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        if self.path not in (
            "/api/config",
            "/api/portfolio",
            "/api/copilot/decision",
            "/api/audit/run",
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 100_000:
                raise ValueError("请求内容大小不正确")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if self.path == "/api/audit/run":
                audit_scheduler.trigger_background("manual")
                self.send_json({
                    "ok": True,
                    "message": "Data audit queued",
                    "status_url": "/api/audit/status",
                })
                return
            if self.path == "/api/copilot/decision":
                config = load_config()
                conn = fund_tracker.connect_db()
                try:
                    fund_tracker.apply_copilot_decision(
                        conn,
                        config,
                        payload.get("decision"),
                        payload.get("fund_executions"),
                    )
                    save_config(config)
                    fund_tracker.sync_funds(conn, config)
                    conn.commit()
                finally:
                    conn.close()
                rebuild_outputs(config)
                self.send_json(
                    {"ok": True, "message": "本月决策已记录，页面正在刷新"}
                )
                return
            if self.path == "/api/portfolio":
                current = load_config()
                updated, last_updated = apply_portfolio_update(current, payload)
                save_config(updated)
                rebuild_outputs(updated)
                self.send_json({
                    "ok": True,
                    "message": "持仓金额已更新",
                    "last_updated": last_updated,
                })
                return
            current = load_config()
            updated = validate_payload(payload, current)
            save_config(updated)
            rebuild_outputs(updated)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            try:
                output_paths.write_blocked_outputs(exc, {
                    "phase": "local-rebuild", "task_name": "rebuild current dashboard",
                    "source_data_used": ["data/fund_tracker.sqlite", "config.json"],
                })
            except Exception:
                pass
            self.send_json({"ok": False, "error": f"保存失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_json({"ok": True, "message": "配置已保存，监控页面已刷新"})

    def log_message(self, format_string, *args):
        print(f"[local] {self.address_string()} {format_string % args}")


def main():
    os.chdir(str(BASE_DIR))
    audit_scheduler.start_scheduler()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("基金监控服务：http://127.0.0.1:8765/dashboard.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
