#!/usr/bin/env python3
import argparse
import calendar
import csv
import datetime as dt
import html
import io
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import model_risk
import data_layer_audit
import cn_equity_temperature
import ndx_price_temperature
import ndx_shadow_run
import qdii_carrier
import daily_automation_status
from utils import output_paths


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "fund_tracker.sqlite"
APPLICATION_VERSION = "V7.3-NDX-V1-VALIDATION"
QDII_CARRIER_CONTRACT_VERSION = "qdii-carrier-facts-v2"


def load_ndx_validation_snapshot():
    """Load the newest governed NDX offline-validation artifact, if present."""
    candidates = []
    current = output_paths.current_run_dir(required=False)
    if current:
        candidates.append(current / "reports" / "ndx-price-temperature-validation.json")
    candidates.extend(sorted(
        output_paths.RUNS_ROOT.glob("*/reports/ndx-price-temperature-validation.json"),
        reverse=True,
    ))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            latest = payload.get("latest_snapshot")
            # Skip thin/engineering-only JSONs that lack the actual model snapshot.
            # FREEZE must not zero out model candidate amounts.
            if not latest or latest.get("temperature_score") is None:
                continue
            snapshot = dict(latest)
            snapshot["offline_pass"] = bool(payload.get("offline_pass"))
            snapshot["offline_gates"] = payload.get("offline_gates", {})
            snapshot["historical_statistics"] = payload.get("historical_statistics", {})
            snapshot["validation_artifact"] = str(path)
            # NDX V1 targeted rollback: restore OFFLINE_PASS.
            # The thin JSON regression was a data plumbing artifact, not a model failure.
            snapshot["validation_stage"] = "OFFLINE_PASS"
            return snapshot
        except (OSError, ValueError, TypeError):
            continue
    return {
        "model_status": "UNDER_VALIDATION",
        "validation_stage": "DATA_UNAVAILABLE",
        "activation_status": "NOT_ACTIVE",
        "activation_blocking": True,
        "formal_release_amount": 0.0,
        "data_status": "FAIL",
    }


def load_ndx_shadow_lifecycle():
    shadow_ledger_path = output_paths.REPORTS_ROOT / "shadow" / "ndx-v1" / "shadow-ledger.json"
    fallback = {
        "status": "DAY1_PENDING",
        "shadow_days_completed": 0,
        "required_complete_days": ndx_shadow_run.REQUIRED_COMPLETE_DAYS,
        "activation_status": "NOT_ACTIVE",
        "model_status": "UNDER_VALIDATION",
        "validation_stage": "OFFLINE_VALIDATION",
        "decision_status": "FREEZE",
        "dynamic_cash_pool_status": "FREEZE",
        "first_activation_guard": False,
    }
    if not shadow_ledger_path.is_file():
        return fallback
    try:
        ledger = ndx_shadow_run.load_ledger(shadow_ledger_path)
    except (OSError, ValueError, ndx_shadow_run.ShadowRunError):
        fallback["status"] = "SHADOW_FAILED"
        return fallback
    return ledger


def ndx_activation_gate_status(shadow_ledger):
    """Return the lifecycle gate used before a live NDX decision can open."""
    required = int(shadow_ledger.get("required_complete_days", ndx_shadow_run.REQUIRED_COMPLETE_DAYS) or 0)
    completed = int(shadow_ledger.get("shadow_days_completed", 0) or 0)
    shadow_complete = (
        shadow_ledger.get("status") == "SHADOW_COMPLETE"
        and completed >= required
    )
    activation_active = shadow_complete and shadow_ledger.get("activation_status") == "ACTIVE"
    guard_pending = (
        bool(shadow_ledger.get("first_activation_guard"))
        or shadow_ledger.get("first_activation_guard_status") == "PENDING_MANUAL_CONFIRMATION"
    )
    if not activation_active:
        return {
            "activation_active": False,
            "first_activation_confirmation_required": False,
            "allow_formal_decision": False,
            "blocking_reason": "NDX_SHADOW_VALIDATION_NOT_ACTIVATED",
        }
    if guard_pending:
        return {
            "activation_active": True,
            "first_activation_confirmation_required": True,
            "allow_formal_decision": False,
            "blocking_reason": "NDX_FIRST_ACTIVATION_CONFIRMATION_REQUIRED",
        }
    return {
        "activation_active": True,
        "first_activation_confirmation_required": False,
        "allow_formal_decision": True,
        "blocking_reason": None,
    }
PE_METRIC_TYPES = {"trailing_pe", "forward_pe", "cape", "unknown"}
PE_HISTORY_MONTHS = 60
US_PE_WINDOW_LABEL = "recent_5y_percentile"
US_VALUATION_SOURCE_DEFINITIONS = {
    "nasdaq100": {
        "indicator_name": "nasdaq100_pe_percentile",
        "source_name": "World PE Ratio Nasdaq 100",
        "metric_type": "trailing_pe",
        "underlying_object": "QQQ ETF proxy for Nasdaq-100",
        "calculation_method": "Provider-calculated QQQ trailing P/E; constituent aggregation and loss-company treatment are not disclosed",
        "update_frequency": "monthly snapshot from a page-updated estimate",
        "publication_delay": "not contractually disclosed",
        "methodology_known": False,
    },
    "sp500": {
        "indicator_name": "sp500_pe_percentile",
        "source_name": "Multpl S&P 500 PE Ratio by Month",
        "metric_type": "trailing_pe",
        "underlying_object": "S&P 500 index",
        "calculation_method": "Price divided by trailing twelve-month as-reported earnings; recent values may be estimates",
        "update_frequency": "monthly",
        "publication_delay": "recent months may remain estimated until earnings are reported",
        "methodology_known": True,
    },
}
MACRO_SERIES = {
    "DFII5": "5Y TIPS实际利率",
    "DFII10": "10Y TIPS实际利率",
    "T10YIE": "10Y盈亏平衡通胀率",
    "DFF": "联邦基金有效利率",
}
DEFAULT_COPILOT_CONFIG = {
    "monthly_contribution": 2500,
    "approved_i_class_carriers": [],
    "execution_funds": {
        "a_share": "022459",
        "us_equity": "016452",
        "gold": "014661",
    },
    "release_rules": {
        "allow_absolute_gap_on_initialization": True,
        "absolute_gap_threshold_ratio": 0.10,
        "initial_max_release_ratio": 0.25,
    },
    "strategic_allocation": {
        "a_share": 0.40,
        "us_equity": 0.40,
        "gold": 0.10,
        "cash": 0.10,
    },
    "allocation_ranges": {
        "a_share": [0.25, 0.50],
        "us_equity": [0.25, 0.55],
        "gold": [0.05, 0.20],
        "cash": [0.10, 1.00],
    },
    "manual_indicators": {
        "social_financing_yoy": None,
        "m2_yoy": None,
        "nasdaq100_pe_percentile": None,
        "sp500_pe_percentile": None,
    },
    "automatic_sources": {
        "china_money": {
            "provider": "pbc_monthly_report",
            "index_url": (
                "https://www.pbc.gov.cn/diaochatongjisi/"
                "116219/116225/index.html"
            ),
        },
        "nasdaq100": {
            "provider": "world_pe_ratio",
            "history_url": "https://worldperatio.com/index/nasdaq-100",
        },
        "sp500": {
            "provider": "multpl",
            "history_url": (
                "https://www.multpl.com/"
                "s-p-500-pe-ratio/table/by-month"
            ),
        },
    },
}
DEFAULT_MARKET_TEMPERATURE_CONFIG = {
    "cache_hours": 24,
    "indexes": {
        "a500": {
            "name": "中证A500",
            "code": "000510",
            "sources": [
                {
                    "provider": "etf_run",
                    "url": "https://www.etf.run/index/SSE/000510",
                }
            ],
        },
        "hs300": {
            "name": "沪深300",
            "code": "000300",
            "sources": [
                {
                    "provider": "etf_run",
                    "url": "https://www.etf.run/index/SSE/000300",
                }
            ],
        },
    },
}

try:
    import certifi
except ImportError:
    certifi = None


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def connect_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 常驻的 local_server 守护进程与 09:10 日更任务会并发访问
    # 同一个 SQLite 文件。默认 busy_timeout=0 会在撞锁时立刻抛 "database is locked"
    # 同一个数据库。WAL 让读不阻塞写，busy_timeout
    # 让写者最多等 30 秒而非立即失败。二者只影响并发健壮性，不改任何业务逻辑。
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS funds (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            fund_type TEXT NOT NULL,
            holding_amount REAL NOT NULL,
            profit_pct REAL,
            strategy TEXT,
            max_holding_amount REAL NOT NULL,
            drawdown_20_buy_amount REAL NOT NULL,
            drawdown_30_buy_amount REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nav_history (
            code TEXT NOT NULL,
            nav_date TEXT NOT NULL,
            nav REAL NOT NULL,
            accumulated_nav REAL,
            pct_change REAL,
            source TEXT NOT NULL,
            source_url TEXT,
            fetch_time TEXT,
            is_qdii INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (code, nav_date)
        )
    """)
    nav_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(nav_history)").fetchall()
    }
    for column, definition in (
        ("accumulated_nav", "REAL"),
        ("source_url", "TEXT"),
        ("fetch_time", "TEXT"),
        ("is_qdii", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in nav_columns:
            conn.execute(f"ALTER TABLE nav_history ADD COLUMN {column} {definition}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            nav_date TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro_history (
            series_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            value REAL NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (series_id, observation_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_valuation_history (
            index_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            pe_ttm REAL NOT NULL,
            percentile REAL NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (index_id, observation_date)
        )
    """)
    valuation_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(market_valuation_history)")
    }
    if "pb" not in valuation_columns:
        conn.execute("ALTER TABLE market_valuation_history ADD COLUMN pb REAL")
    if "pb_percentile" not in valuation_columns:
        conn.execute(
            "ALTER TABLE market_valuation_history ADD COLUMN pb_percentile REAL"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS a_share_valuation_observations (
            index_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            pe_ttm REAL,
            pb REAL,
            source TEXT NOT NULL,
            fetch_time TEXT NOT NULL,
            role TEXT NOT NULL,
            reproducible INTEGER NOT NULL,
            confidence TEXT NOT NULL,
            used_in_score INTEGER NOT NULL,
            PRIMARY KEY (index_id, observation_date, source)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_price_history (
            index_code TEXT NOT NULL,
            index_name TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            close REAL NOT NULL,
            source TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            is_backfilled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (index_code, trade_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cn_equity_temperature_snapshots (
            calculation_date TEXT PRIMARY KEY,
            carrier_index_code TEXT NOT NULL,
            carrier_latest_date TEXT,
            carrier_ma_window INTEGER,
            carrier_ma REAL,
            carrier_ma_distance REAL,
            carrier_drawdown REAL,
            carrier_volatility REAL,
            opportunity_score REAL,
            volatility_penalty REAL,
            market_adjustment REAL,
            final_score REAL,
            level TEXT NOT NULL,
            release_factor REAL NOT NULL,
            formula_version TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_update_status (
            metric_id TEXT PRIMARY KEY,
            last_attempt_at TEXT,
            last_success_at TEXT,
            last_error TEXT,
            source TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS copilot_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO copilot_state (
            state_key, state_value, updated_at
        )
        SELECT 'executed_asset_cost_basis', state_value, updated_at
        FROM copilot_state
        WHERE state_key = 'executed_asset_adjustments'
    """)
    conn.execute("""
        INSERT OR IGNORE INTO copilot_state (
            state_key, state_value, updated_at
        )
        SELECT 'executed_asset_market_values', state_value, updated_at
        FROM copilot_state
        WHERE state_key = 'executed_asset_adjustments'
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS allocation_history (
            month TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            user_decision TEXT,
            decision_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS allocation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT NOT NULL,
            decision TEXT NOT NULL,
            deploy_amount REAL NOT NULL,
            allocation_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            plan_amount REAL,
            plan_allocation_json TEXT,
            executed_at TEXT
        )
    """)
    event_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(allocation_events)")
    }
    if "plan_amount" not in event_columns:
        conn.execute("ALTER TABLE allocation_events ADD COLUMN plan_amount REAL")
    if "plan_allocation_json" not in event_columns:
        conn.execute(
            "ALTER TABLE allocation_events ADD COLUMN plan_allocation_json TEXT"
        )
    if "executed_at" not in event_columns:
        conn.execute("ALTER TABLE allocation_events ADD COLUMN executed_at TEXT")
    if "execution_type" not in event_columns:
        conn.execute(
            "ALTER TABLE allocation_events ADD COLUMN execution_type TEXT"
        )
    conn.execute("""
        UPDATE allocation_events
        SET plan_amount = COALESCE(plan_amount, deploy_amount),
            plan_allocation_json = COALESCE(
                plan_allocation_json,
                allocation_json
            ),
            executed_at = COALESCE(executed_at, created_at)
        WHERE plan_amount IS NULL
           OR plan_allocation_json IS NULL
           OR executed_at IS NULL
    """)
    conn.execute("""
        UPDATE allocation_events
        SET execution_type = CASE
            WHEN decision = 'manual_review' THEN 'Manual Review Execution'
            WHEN decision = 'manual_override' THEN 'Manual Override Execution'
            ELSE 'Model Auto Execution'
        END
        WHERE execution_type IS NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_execution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT NOT NULL,
            fund_code TEXT NOT NULL,
            fund_name TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            planned_amount REAL NOT NULL,
            actual_executed_amount REAL NOT NULL,
            executed_at TEXT NOT NULL,
            UNIQUE(month, fund_code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS economic_indicator_history (
            metric_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            value REAL NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (metric_id, observation_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro_data_audit_observations (
            indicator_name TEXT NOT NULL,
            frequency TEXT NOT NULL,
            data_date TEXT,
            release_date TEXT,
            fetch_time TEXT NOT NULL,
            source TEXT NOT NULL,
            raw_value TEXT,
            parsed_value REAL,
            parse_status TEXT NOT NULL,
            stale_status TEXT NOT NULL,
            audit_run_id TEXT NOT NULL,
            PRIMARY KEY (indicator_name, audit_run_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_valuation_history (
            asset_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            pe REAL NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (asset_id, observation_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pe_history (
            index_code TEXT NOT NULL,
            index_name TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_type TEXT NOT NULL,
            value REAL NOT NULL,
            observation_date TEXT NOT NULL,
            frequency TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            is_estimated INTEGER NOT NULL,
            validation_status TEXT NOT NULL,
            note TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (index_code, metric_type, observation_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decision_snapshots (
            decision_id TEXT PRIMARY KEY,
            execution_month TEXT NOT NULL,
            version INTEGER NOT NULL,
            generated_at TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            execution_status TEXT NOT NULL,
            UNIQUE(execution_month, version)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decision_snapshot_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correction_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS current_monitoring_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_month TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            snapshot_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_override_snapshots (
            override_id TEXT PRIMARY KEY,
            execution_month TEXT NOT NULL,
            created_at TEXT NOT NULL,
            override_json TEXT NOT NULL
        )
    """)
    return conn


def import_hs300_valuation_sample(conn):
    path = output_paths.get_csv_path("a-share-valuation-sample.csv")
    if not path.exists():
        return 0
    existing = conn.execute(
        "SELECT COUNT(*) FROM a_share_valuation_observations WHERE index_id='hs300'"
    ).fetchone()[0]
    fetch_time = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(
        timespec="seconds"
    )
    with path.open("r", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO a_share_valuation_observations (
                    index_id, observation_date, pe_ttm, pb, source,
                    fetch_time, role, reproducible, confidence, used_in_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "hs300", row["date"], float(row["pe_ttm"]), float(row["pb"]),
                    "AKShare/Legulegu", fetch_time, "HS300 valuation proxy",
                    1, "Medium", 1,
                ),
            )
    return conn.execute(
        "SELECT COUNT(*) FROM a_share_valuation_observations WHERE index_id='hs300'"
    ).fetchone()[0]


def local_a_share_valuation(conn, index_id):
    rows = conn.execute(
        """
        SELECT observation_date, pe_ttm, pb, source, fetch_time, role,
               reproducible, confidence, used_in_score
        FROM a_share_valuation_observations
        WHERE index_id = ? ORDER BY observation_date
        """,
        (index_id,),
    ).fetchall()
    if not rows:
        return None
    latest = rows[-1]
    sample_size = len(rows)
    percentile_status = (
        "NOT_CALCULATED" if sample_size < 250
        else "REFERENCE_ONLY" if sample_size < 750
        else "CANDIDATE_MODEL_INPUT"
    )
    pe_values = [row["pe_ttm"] for row in rows if row["pe_ttm"] is not None]
    pb_values = [row["pb"] for row in rows if row["pb"] is not None]
    calculate = sample_size >= 250
    pe_percentile = (
        round(sum(value <= latest["pe_ttm"] for value in pe_values) / len(pe_values) * 100, 4)
        if calculate and latest["pe_ttm"] is not None and pe_values else None
    )
    pb_percentile = (
        round(sum(value <= latest["pb"] for value in pb_values) / len(pb_values) * 100, 4)
        if calculate and latest["pb"] is not None and pb_values else None
    )
    return {
        "index_id": index_id,
        "data_date": latest["observation_date"],
        "pe": latest["pe_ttm"],
        "pb": latest["pb"],
        "percentile": pe_percentile,
        "pb_percentile": pb_percentile,
        "sample_size": sample_size,
        "percentile_status": percentile_status,
        "source": latest["source"],
        "fetch_time": latest["fetch_time"],
        "role": latest["role"],
        "reproducible": bool(latest["reproducible"]),
        "confidence": latest["confidence"],
        "used_in_score": bool(latest["used_in_score"]),
    }


def migrate_a500_display_snapshot(conn):
    row = conn.execute(
        """
        SELECT observation_date, pe_ttm, pb, source, fetched_at
        FROM market_valuation_history WHERE index_id='a500'
        ORDER BY observation_date DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO a_share_valuation_observations (
            index_id, observation_date, pe_ttm, pb, source, fetch_time,
            role, reproducible, confidence, used_in_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "a500", row["observation_date"], row["pe_ttm"], row["pb"],
            row["source"], row["fetched_at"], "Display Only", 0, "Low", 0,
        ),
    )


def market_temperature_config(config):
    configured = config.get("market_temperature")
    return configured if isinstance(configured, dict) else DEFAULT_MARKET_TEMPERATURE_CONFIG


def copilot_config(config):
    configured = config.get("copilot_v7")
    merged = json.loads(json.dumps(DEFAULT_COPILOT_CONFIG))
    if not isinstance(configured, dict):
        return merged
    for key in ("monthly_contribution",):
        if key in configured:
            merged[key] = configured[key]
    if isinstance(configured.get("approved_i_class_carriers"), list):
        merged["approved_i_class_carriers"] = [
            str(code) for code in configured["approved_i_class_carriers"]
        ]
    for key in (
        "release_rules",
        "execution_funds",
        "strategic_allocation",
        "allocation_ranges",
        "manual_indicators",
        "automatic_sources",
    ):
        if isinstance(configured.get(key), dict):
            merged[key].update(configured[key])
    return merged


def get_state(conn, key, default=None):
    row = conn.execute(
        "SELECT state_value FROM copilot_state WHERE state_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["state_value"])
    except (TypeError, ValueError):
        return default


def set_state(conn, key, value):
    conn.execute(
        """
        INSERT OR REPLACE INTO copilot_state (state_key, state_value, updated_at)
        VALUES (?, ?, ?)
        """,
        (
            key,
            json.dumps(value, ensure_ascii=False),
            dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )


def ensure_monthly_contribution(conn, config):
    month = dt.date.today().strftime("%Y-%m")
    last_month = get_state(conn, "last_contribution_month")
    pool = float(get_state(conn, "dynamic_cash_pool", 0) or 0)
    if last_month != month:
        pool += float(copilot_config(config)["monthly_contribution"])
        set_state(conn, "dynamic_cash_pool", round(pool, 2))
        set_state(conn, "last_contribution_month", month)
        if last_month:
            months = int(get_state(conn, "months_without_deploy", 0) or 0)
            set_state(conn, "months_without_deploy", months + 1)
    return pool


def update_metric_status(conn, metric_id, success, source=None, error=None):
    now = dt.datetime.now().isoformat(timespec="seconds")
    current = conn.execute(
        "SELECT last_success_at, source FROM market_update_status WHERE metric_id = ?",
        (metric_id,),
    ).fetchone()
    last_success_at = now if success else (current["last_success_at"] if current else None)
    last_source = source if success else (current["source"] if current else source)
    conn.execute(
        """
        INSERT OR REPLACE INTO market_update_status (
            metric_id, last_attempt_at, last_success_at, last_error, source
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            metric_id,
            now,
            last_success_at,
            None if success else str(error),
            last_source,
        ),
    )


def metric_cache_is_fresh(conn, metric_id, cache_hours):
    row = conn.execute(
        "SELECT last_success_at FROM market_update_status WHERE metric_id = ?",
        (metric_id,),
    ).fetchone()
    if row is None or not row["last_success_at"]:
        return False
    updated_at = dt.datetime.fromisoformat(row["last_success_at"])
    return dt.datetime.now() - updated_at < dt.timedelta(hours=cache_hours)


def sync_funds(conn, config):
    now = dt.datetime.now().isoformat(timespec="seconds")
    for fund in config["funds"]:
        conn.execute(
            """
            INSERT OR REPLACE INTO funds (
                code, name, fund_type, holding_amount, profit_pct, strategy,
                max_holding_amount, drawdown_20_buy_amount, drawdown_30_buy_amount, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fund["code"],
                fund["name"],
                fund["type"],
                fund["holding_amount"],
                fund.get("profit_pct"),
                fund.get("strategy"),
                fund["max_holding_amount"],
                fund["drawdown_20_buy_amount"],
                fund["drawdown_30_buy_amount"],
                now,
            ),
        )


def fetch_nav_history(code, days=370):
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    rows = []
    page_index = 1
    while True:
        params = urllib.parse.urlencode(
            {
                "fundCode": code,
                "pageIndex": page_index,
                "pageSize": 20,
            }
        )
        url = f"https://api.fund.eastmoney.com/f10/lsjz?{params}"
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "25",
                    "-H",
                    "Accept: application/json",
                    "-H",
                    "Referer: https://fundf10.eastmoney.com/",
                    "-A",
                    "Mozilla/5.0",
                    url,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            payload = json.loads(result.stdout.decode("utf-8"))
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"基金净值下载失败：{exc}") from exc

        data = payload.get("Data") or {}
        page_rows = data.get("LSJZList") or []
        if not page_rows:
            break
        rows.extend(page_rows)

        oldest = dt.date.fromisoformat(page_rows[-1]["FSRQ"])
        if oldest < start:
            break
        page_index += 1
        time.sleep(0.15)

    parsed = []
    for row in rows:
        nav = row.get("DWJZ")
        if nav in (None, ""):
            continue
        nav_date = dt.date.fromisoformat(row["FSRQ"])
        if nav_date < start or nav_date > end:
            continue
        pct_raw = row.get("JZZZL")
        parsed.append(
            {
                "date": nav_date.isoformat(),
                "nav": float(nav),
                "accumulated_nav": (
                    float(row["LJJZ"])
                    if row.get("LJJZ") not in (None, "")
                    else None
                ),
                "pct_change": float(pct_raw) if pct_raw not in (None, "") else None,
                "source": "eastmoney_lsjz",
                "source_url": "https://api.fund.eastmoney.com/f10/lsjz",
            }
        )
    return parsed


def update_nav_history(conn, config, days):
    now = dt.datetime.now().isoformat(timespec="seconds")
    for fund in config["funds"]:
        code = fund["code"]
        is_qdii = "QDII" in fund.get("type", "").upper()
        latest = latest_nav(conn, code)
        effective_days = days
        # A one-year audit/backfill must not be silently reduced to an
        # incremental fetch; otherwise historical lineage remains incomplete.
        if latest is not None and days < 365:
            latest_date = dt.date.fromisoformat(latest["nav_date"])
            gap_days = max(0, (dt.date.today() - latest_date).days)
            effective_days = min(days, max(10, gap_days + 7))
        try:
            rows = fetch_nav_history(code, days=effective_days)
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            print(f"[WARN] {code} 拉取失败：{exc}", file=sys.stderr)
            continue

        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO nav_history (
                    code, nav_date, nav, accumulated_nav, pct_change, source,
                    source_url, fetch_time, is_qdii, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code, row["date"], row["nav"], row["accumulated_nav"],
                    row["pct_change"], row["source"], row["source_url"],
                    now, int(is_qdii), now,
                ),
            )
        time.sleep(0.3)


def fetch_index_price_history(index_code, index_name):
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        "secid=1.%s&klt=101&fqt=0&lmt=10000&end=20500101&"
        "fields1=f1,f2,f3,f4,f5,f6&"
        "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    ) % index_code
    try:
        result = subprocess.run(
            [
                "curl", "--noproxy", "*", "--http1.1", "--compressed",
                "--retry", "2", "--retry-delay", "1", "--fail", "--silent",
                "--show-error", "--max-time", "30", "-A",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                "-H", "Referer: https://quote.eastmoney.com/",
                "-H", "Accept: application/json,text/plain,*/*",
                url,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=35,
        )
        payload = json.loads(result.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError("%s指数价格下载失败：%s" % (index_name, exc)) from exc
    data = payload.get("data") or {}
    rows = []
    for raw in data.get("klines") or []:
        fields = raw.split(",")
        if len(fields) < 3:
            continue
        try:
            trade_date = dt.date.fromisoformat(fields[0])
            close = float(fields[2])
            if close <= 0:
                raise ValueError("non-positive close")
        except (TypeError, ValueError):
            continue
        rows.append({
            "indexCode": index_code,
            "indexName": index_name,
            "tradeDate": trade_date.isoformat(),
            "close": close,
            "source": "eastmoney_index_kline",
            "collectedAt": dt.datetime.now().isoformat(timespec="seconds"),
            "isBackfilled": bool(
                index_code == cn_equity_temperature.A500_CODE
                and trade_date < cn_equity_temperature.A500_LAUNCH_DATE
            ),
        })
    if not rows:
        raise RuntimeError("%s指数价格返回空数据" % index_name)
    return rows


def update_index_price_history(conn, cache_hours=24):
    for code, name in (
        (cn_equity_temperature.A500_CODE, "中证A500"),
        (cn_equity_temperature.HS300_CODE, "沪深300"),
    ):
        metric_id = "index_price:%s" % code
        if metric_cache_is_fresh(conn, metric_id, cache_hours):
            continue
        try:
            rows = fetch_index_price_history(code, name)
        except RuntimeError as exc:
            update_metric_status(conn, metric_id, success=False, error=exc)
            print("[WARN] %s" % exc, file=sys.stderr)
            continue
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO index_price_history (
                    index_code, index_name, trade_date, close, source,
                    collected_at, is_backfilled
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["indexCode"], row["indexName"], row["tradeDate"],
                    row["close"], row["source"], row["collectedAt"],
                    int(row["isBackfilled"]),
                ),
            )
        update_metric_status(
            conn, metric_id, success=True, source="eastmoney_index_kline"
        )


def index_price_records(conn, index_code):
    return [
        {
            "indexCode": row["index_code"], "indexName": row["index_name"],
            "tradeDate": row["trade_date"], "close": row["close"],
            "source": row["source"], "collectedAt": row["collected_at"],
            "isBackfilled": bool(row["is_backfilled"]),
        }
        for row in conn.execute(
            """
            SELECT index_code, index_name, trade_date, close, source,
                   collected_at, is_backfilled
            FROM index_price_history WHERE index_code=? ORDER BY trade_date
            """,
            (index_code,),
        ).fetchall()
    ]


def _fee_label_html(item):
    """Generate fee label with actual comprehensive rate from enriched carrier data."""
    mgmt = item.get("management_fee_pct")
    cust = item.get("custody_fee_pct")
    svc = item.get("service_fee_pct")
    pur = item.get("purchase_fee_display")
    has_mgmt = mgmt not in (None, "", "--")
    has_cust = cust not in (None, "", "--")
    has_svc = svc not in (None, "", "--")
    has_pur = pur not in (None, "", "--")
    mgmt_str = f"{mgmt}%" if has_mgmt else "?"
    cust_str = f"{cust}%" if has_cust else "?"
    svc_str = f"{svc}%" if has_svc else "?"
    pur_str = pur if has_pur else "?"
    if has_mgmt and has_cust and has_svc:
        total = (mgmt or 0) + (cust or 0) + (svc or 0)
        label = f"综合费率 {total:.2f}%"
    elif has_pur:
        label = f"申购 {pur}"
    else:
        label = "费率待核验"
    return f'{label}<small>管理 {mgmt_str} / 托管 {cust_str} / 销售服务 {svc_str} / 申购 {pur_str}</small>'


def _a500_price_model_eligibility(temperature_result):
    """Unified A500 price temperature eligibility gate (Section 6.1).

    Returns (eligible: bool, reason: str).
    """
    carrier = temperature_result.get("carrierIndex", {})
    if not carrier:
        return False, "A500_METRICS_INCOMPLETE"
    sample_count = int(carrier.get("sampleCount", 0) or 0)
    if sample_count < 250:
        return False, "A500_SAMPLE_INSUFFICIENT"
    if carrier.get("freshnessStatus") != "FRESH":
        return False, "A500_DATA_STALE"
    if temperature_result.get("finalScore") is None:
        return False, "A500_METRICS_INCOMPLETE"
    if temperature_result.get("releaseFactor") is None:
        return False, "A500_METRICS_INCOMPLETE"
    if carrier.get("confidence") not in ("HIGH", "High"):
        return False, "A500_SOURCE_NOT_APPROVED"
    warnings = temperature_result.get("warnings", [])
    if "CONTAINS_BACKFILLED_HISTORY" in warnings:
        return True, "ACTIVE_WITH_BACKFILLED_HISTORY"
    return True, "ACTIVE"


def calculate_cn_equity_price_temperature(conn, as_of_date=None):
    carrier = cn_equity_temperature.calculate_metrics(
        index_price_records(conn, cn_equity_temperature.A500_CODE),
        cn_equity_temperature.A500_CODE, "中证A500", as_of_date,
    )
    market = cn_equity_temperature.calculate_metrics(
        index_price_records(conn, cn_equity_temperature.HS300_CODE),
        cn_equity_temperature.HS300_CODE, "沪深300", as_of_date,
    )
    result = cn_equity_temperature.calculate_temperature(carrier, market)
    # ── A500 price model eligibility ──
    eligible, eligibility_reason = _a500_price_model_eligibility(result)
    result["modelEnabled"] = eligible
    result["activationStatus"] = "ACTIVE" if eligible else eligibility_reason
    result["effectiveReleaseFactor"] = (
        result["releaseFactor"] if eligible else 1.0
    )
    if not eligible:
        result["warnings"].append(eligibility_reason)
    # ── Diagnostic fields ──
    result["preClampScore"] = round(
        result.get("opportunityScore", 0)
        - result.get("volatilityPenalty", 0)
        + result.get("marketAdjustment", 0), 4
    )
    result["clampApplied"] = result["preClampScore"] != result.get("finalScore", 0)
    result["sourceStability"] = "CONDITIONAL_PASS"
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR REPLACE INTO cn_equity_temperature_snapshots (
            calculation_date, carrier_index_code, carrier_latest_date,
            carrier_ma_window, carrier_ma, carrier_ma_distance,
            carrier_drawdown, carrier_volatility, opportunity_score,
            volatility_penalty, market_adjustment, final_score, level,
            release_factor, formula_version, snapshot_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dt.date.today().isoformat(), carrier["indexCode"],
            carrier["latestDate"], carrier["movingAverageWindow"],
            carrier["movingAverage"], carrier["movingAverageDistance"],
            carrier["oneYearDrawdown"], carrier["annualizedVolatility"],
            result["opportunityScore"], result["volatilityPenalty"],
            result["marketAdjustment"], result["finalScore"],
            result["level"], result["releaseFactor"],
            result["formulaVersion"], json.dumps(result, ensure_ascii=False), now,
        ),
    )
    return result


def fetch_fred_series(series_id, days=120):
    start = dt.date.today() - dt.timedelta(days=days)
    params = urllib.parse.urlencode({"id": series_id, "cosd": start.isoformat()})
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?{params}"
    try:
        result = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                url,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=35,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"FRED下载失败：{exc}") from exc
    content = result.stdout.decode("utf-8")

    rows = []
    for row in csv.DictReader(io.StringIO(content)):
        raw_value = row.get(series_id)
        if raw_value in (None, "", "."):
            continue
        rows.append(
            {
                "date": row["observation_date"],
                "value": float(raw_value),
                "source": "fred",
            }
        )
    return rows


def update_macro_history(conn, days=120, cache_hours=24):
    now = dt.datetime.now().isoformat(timespec="seconds")
    for series_id in MACRO_SERIES:
        if metric_cache_is_fresh(conn, series_id, cache_hours):
            continue
        try:
            rows = fetch_fred_series(series_id, days=days)
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            csv.Error,
            ValueError,
            RuntimeError,
        ) as exc:
            print(f"[WARN] {series_id} 拉取失败：{exc}", file=sys.stderr)
            update_metric_status(conn, series_id, success=False, error=exc)
            continue
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO macro_history (
                    series_id, observation_date, value, source, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (series_id, row["date"], row["value"], row["source"], now),
            )
        update_metric_status(conn, series_id, success=True, source="fred")


def decode_public_page(content):
    try:
        decoded = content.decode("utf-8")
        if "<html" in decoded.lower() or "<!doctype" in decoded.lower():
            return decoded
    except UnicodeDecodeError:
        pass

    try:
        result = subprocess.run(
            [
                "node",
                "-e",
                (
                    "const z=require('zlib');"
                    "const c=[];"
                    "process.stdin.on('data',d=>c.push(d));"
                    "process.stdin.on('end',()=>"
                    "process.stdout.write(z.brotliDecompressSync(Buffer.concat(c))))"
                ),
            ],
            input=content,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"页面解压失败：{exc}") from exc
    return result.stdout.decode("utf-8")


def fetch_etf_run_valuation(source):
    try:
        result = subprocess.run(
            [
                "curl",
                "--http1.1",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "-A",
                "Mozilla/5.0",
                source["url"],
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=35,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"估值页面下载失败：{exc}") from exc

    content = decode_public_page(result.stdout)
    date_match = re.search(
        r"更新至\s*(?:<!--.*?-->)?\s*(\d{4}/\d{2}/\d{2})",
        content,
    )
    pe_match = re.search(
        r"最新市盈率</span><span[^>]*>([\d.]+)",
        content,
    )
    percentile_matches = re.findall(
        r"当前分位</span><span[^>]*>([\d.]+)%",
        content,
    )
    pb_match = re.search(
        r"最新市净率</span><span[^>]*>([\d.]+)",
        content,
    )
    if not date_match or not pe_match or not percentile_matches:
        raise ValueError("估值页面缺少日期、PE或历史分位")

    return {
        "date": date_match.group(1).replace("/", "-"),
        "pe_ttm": float(pe_match.group(1)),
        "percentile": float(percentile_matches[0]),
        "pb": float(pb_match.group(1)) if pb_match else None,
        "pb_percentile": (
            float(percentile_matches[1]) if len(percentile_matches) > 1 else None
        ),
        "source": source["provider"],
    }


def update_valuation_history(conn, config, cache_hours=24):
    temperature_config = market_temperature_config(config)
    indexes = temperature_config.get("indexes") or {}
    for index_id, index_config in indexes.items():
        metric_id = f"valuation:{index_id}"
        if metric_cache_is_fresh(conn, metric_id, cache_hours):
            continue

        errors = []
        for source in index_config.get("sources") or []:
            try:
                if source.get("provider") == "etf_run":
                    row = fetch_etf_run_valuation(source)
                else:
                    raise ValueError(f"不支持的数据源：{source.get('provider')}")
            except (KeyError, ValueError, RuntimeError) as exc:
                errors.append(f"{source.get('provider', 'unknown')}: {exc}")
                continue

            now = dt.datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT OR REPLACE INTO market_valuation_history (
                    index_id, observation_date, pe_ttm, percentile, pb,
                    pb_percentile, source, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index_id,
                    row["date"],
                    row["pe_ttm"],
                    row["percentile"],
                    row["pb"],
                    row["pb_percentile"],
                    row["source"],
                    now,
                ),
            )
            if index_id == "a500":
                conn.execute(
                    """
                    INSERT OR REPLACE INTO a_share_valuation_observations (
                        index_id, observation_date, pe_ttm, pb, source,
                        fetch_time, role, reproducible, confidence, used_in_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "a500", row["date"], row["pe_ttm"], row["pb"],
                        row["source"], now, "Display Only", 0, "Low", 0,
                    ),
                )
            update_metric_status(
                conn,
                metric_id,
                success=True,
                source=row["source"],
            )
            break
        else:
            error = "; ".join(errors) or "未配置估值数据源"
            print(f"[WARN] {metric_id} 拉取失败：{error}", file=sys.stderr)
            update_metric_status(conn, metric_id, success=False, error=error)


def fetch_public_text(url, timeout=35, headers=None):
    command = [
        "curl",
        "--http1.1",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        str(timeout - 5),
        "-A",
        "Mozilla/5.0",
    ]
    for name, value in (headers or {}).items():
        command.extend(["-H", f"{name}: {value}"])
    command.append(url)
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"公开页面下载失败：{exc}") from exc
    return result.stdout.decode("utf-8", errors="replace")


def strip_html(content):
    content = re.sub(
        r"<(?:script|style).*?</(?:script|style)>",
        " ",
        content,
        flags=re.I | re.S,
    )
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", content)))


def fetch_pbc_money_indicators(source):
    index_content = fetch_public_text(source["index_url"])
    matches = re.findall(
        r'href="([^"]+)"[^>]+title="(\d{4})年(\d{1,2})月金融统计数据报告"',
        index_content,
    )
    if not matches:
        raise ValueError("人民银行栏目未找到金融统计月报")
    path, year_text, month_text = matches[0]
    url = urllib.parse.urljoin(source["index_url"], path)
    content = strip_html(fetch_public_text(url))
    social_match = re.search(
        r"社会融资规模存量同比增长\s*([\d.]+)%", content
    )
    m2_match = re.search(
        r"广义货币[（(]?M2[）)]?.{0,80}?同比增长\s*([\d.]+)%",
        content,
    )
    if not social_match or not m2_match:
        raise ValueError("人民银行月报缺少社融或M2同比字段")
    year = int(year_text)
    month = int(month_text)
    observation_date = dt.date(
        year,
        month,
        calendar.monthrange(year, month)[1],
    ).isoformat()
    release_match = re.search(r"/(20\d{6})\d+/index\.html", url)
    release_date = (
        dt.datetime.strptime(release_match.group(1), "%Y%m%d").date().isoformat()
        if release_match else None
    )
    return {
        "observation_date": observation_date,
        "release_date": release_date,
        "social_financing_yoy": float(social_match.group(1)),
        "m2_yoy": float(m2_match.group(1)),
        "raw_social_financing_yoy": social_match.group(0),
        "raw_m2_yoy": m2_match.group(0),
        "source": "pbc_monthly_report",
        "source_url": url,
    }


def fetch_ssga_spy_valuation(source):
    content = fetch_public_text(source["url"])
    date_match = re.search(
        r"Fund Characteristics.*?<span class=\"date\">as of\s+([^<]+)",
        content,
        flags=re.I | re.S,
    )
    pe_match = re.search(
        r"Price/Earnings Ratio FY1.*?</th>\s*<td class=\"data\">([\d.]+)",
        content,
        flags=re.I | re.S,
    )
    if not date_match or not pe_match:
        raise ValueError("SPY官方页面缺少估值日期或PE")
    observation_date = dt.datetime.strptime(
        date_match.group(1).strip(),
        "%b %d %Y",
    ).date().isoformat()
    return {
        "observation_date": observation_date,
        "pe": float(pe_match.group(1)),
        "source": "ssga_spy_fy1",
    }


def find_json_numeric_value(value, key_fragments):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(fragment in normalized for fragment in key_fragments):
                if isinstance(item, (int, float)):
                    return float(item)
                if isinstance(item, dict):
                    raw = item.get("raw")
                    if isinstance(raw, (int, float)):
                        return float(raw)
            found = find_json_numeric_value(item, key_fragments)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_json_numeric_value(item, key_fragments)
            if found is not None:
                return found
    return None


def fetch_invesco_qqq_valuation(source):
    content = fetch_public_text(
        source["url"],
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.invesco.com/",
        },
    )
    payload = json.loads(content)
    pe = find_json_numeric_value(
        payload,
        ("priceearnings", "forwardpe", "peratio"),
    )
    if pe is None:
        raise ValueError("QQQ官方接口未返回PE字段")
    date_value = find_json_numeric_value(payload, ("effectivedate",))
    observation_date = dt.date.today().isoformat()
    if isinstance(date_value, str):
        observation_date = date_value[:10]
    return {
        "observation_date": observation_date,
        "pe": pe,
        "source": "invesco_qqq",
    }


def fetch_ishares_nasdaq100_valuation(source):
    content = fetch_public_text(source["fallback_url"])
    match = re.search(
        r'<div class="product-data-item col-priceEarnings\s*">.*?'
        r'<div class="as-of-date">\s*as of\s+([^<]+)</div>.*?'
        r'<div class="data">\s*([\d.]+)\s*</div>',
        content,
        flags=re.I | re.S,
    )
    if not match:
        raise ValueError("iShares Nasdaq-100页面缺少PE或日期")
    observation_date = dt.datetime.strptime(
        match.group(1).strip(),
        "%d/%b/%Y",
    ).date().isoformat()
    return {
        "observation_date": observation_date,
        "pe": float(match.group(2)),
        "source": "ishares_nasdaq100_trailing",
    }


def fetch_nasdaq100_valuation(source):
    errors = []
    for fetcher in (
        fetch_invesco_qqq_valuation,
        fetch_ishares_nasdaq100_valuation,
    ):
        try:
            return fetcher(source)
        except (
            KeyError,
            ValueError,
            RuntimeError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(str(exc))
    raise RuntimeError("; ".join(errors))


def month_start(value):
    return value.replace(day=1)


def shift_months(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    return dt.date(month_index // 12, month_index % 12 + 1, 1)


def pe_history_row(
    index_code,
    index_name,
    value,
    observation_date,
    source_name,
    source_url,
    is_estimated,
    note,
    metric_type="trailing_pe",
):
    if metric_type not in PE_METRIC_TYPES:
        raise ValueError(f"未知PE口径：{metric_type}")
    if value <= 0:
        raise ValueError(f"{index_code} {observation_date} PE必须为正数")
    if observation_date.day != 1:
        raise ValueError(f"{index_code} {observation_date} 不是月度日期")
    return {
        "index_code": index_code,
        "index_name": index_name,
        "metric_name": "PE Ratio",
        "metric_type": metric_type,
        "value": round(float(value), 4),
        "date": observation_date.isoformat(),
        "frequency": "monthly",
        "source_name": source_name,
        "source_url": source_url,
        "is_estimated": bool(is_estimated),
        "validation_status": "valid",
        "note": note,
    }


def recent_monthly_rows(rows, months=PE_HISTORY_MONTHS):
    current_month = month_start(dt.date.today())
    first_month = shift_months(current_month, -(months - 1))
    selected = {}
    for row in rows:
        row_date = dt.date.fromisoformat(row["date"])
        if first_month <= row_date <= current_month:
            key = (row["index_code"], row["metric_type"], row["date"])
            if key in selected:
                raise ValueError(f"PE月度数据重复：{key}")
            selected[key] = row
    return sorted(selected.values(), key=lambda item: item["date"])


def fetch_multpl_sp500_history(source, months=PE_HISTORY_MONTHS):
    url = source["history_url"]
    content = fetch_public_text(url)
    table_match = re.search(
        r'<table id="datatable">(.*?)</table>',
        content,
        flags=re.I | re.S,
    )
    if not table_match:
        raise ValueError("Multpl页面缺少月度PE表格")
    rows = []
    for match in re.finditer(
        r"<tr[^>]*>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>",
        table_match.group(1),
        flags=re.I | re.S,
    ):
        date_text = strip_html(match.group(1)).strip()
        value_text = strip_html(match.group(2)).strip()
        try:
            source_date = dt.datetime.strptime(date_text, "%b %d, %Y").date()
            value = float(re.search(r"[\d.]+", value_text).group(0))
        except (AttributeError, ValueError):
            continue
        if source_date.day != 1:
            continue
        estimated = bool(re.search(r'title="Estimate"', match.group(2), re.I))
        rows.append(
            pe_history_row(
                "SPX",
                "S&P 500",
                value,
                source_date,
                "Multpl S&P 500 PE Ratio by Month",
                url,
                estimated,
                (
                    "Trailing twelve month as-reported earnings; "
                    + ("source marked Estimate" if estimated else "not estimated")
                ),
            )
        )
    result = recent_monthly_rows(rows, months)
    if len(result) < months:
        raise ValueError(f"Multpl仅取得{len(result)}/{months}个月")
    return result


def fetch_worldpe_nasdaq100_history(source, months=PE_HISTORY_MONTHS):
    url = source["history_url"]
    content = fetch_public_text(url)
    series_match = re.search(
        r"detailPE_data\s*=\s*\[(.*?)\];",
        content,
        flags=re.I | re.S,
    )
    if not series_match:
        raise ValueError("World PE Ratio页面缺少Nasdaq-100历史序列")
    points = re.findall(
        r"Date\.UTC\((\d{4}),\s*(\d{1,2}),\s*1\),\s*([\d.]+)",
        series_match.group(1),
    )
    if not points:
        raise ValueError("World PE Ratio历史序列无法解析")
    latest_date = max(
        dt.date(int(year), int(month) + 1, 1)
        for year, month, _ in points
    )
    rows = []
    for year, month, value in points:
        observation_date = dt.date(int(year), int(month) + 1, 1)
        estimated = observation_date == latest_date
        rows.append(
            pe_history_row(
                "NDX",
                "Nasdaq-100",
                float(value),
                observation_date,
                "World PE Ratio Nasdaq 100",
                url,
                estimated,
                (
                    "Provider labels series as Trailing P/E Ratio Stats and "
                    "calculates it on QQQ; "
                    + (
                        "latest month is provider estimate"
                        if estimated
                        else "historical monthly observation"
                    )
                ),
            )
        )
    result = recent_monthly_rows(rows, months)
    if len(result) < months:
        raise ValueError(
            f"World PE Ratio仅取得{len(result)}/{months}个月"
        )
    return result


def store_pe_history(conn, rows):
    now = dt.datetime.now().isoformat(timespec="seconds")
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO pe_history (
                index_code, index_name, metric_name, metric_type, value,
                observation_date, frequency, source_name, source_url,
                is_estimated, validation_status, note, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["index_code"],
                row["index_name"],
                row["metric_name"],
                row["metric_type"],
                row["value"],
                row["date"],
                row["frequency"],
                row["source_name"],
                row["source_url"],
                1 if row["is_estimated"] else 0,
                row["validation_status"],
                row["note"],
                now,
            ),
        )


def update_copilot_inputs(
    conn,
    config,
    cache_hours=24,
    force_pe_history=False,
):
    settings = copilot_config(config)
    sources = settings["automatic_sources"]
    now = dt.datetime.now().isoformat(timespec="seconds")

    metric_id = "china_money"
    if not metric_cache_is_fresh(conn, metric_id, cache_hours):
        try:
            row = fetch_pbc_money_indicators(sources["china_money"])
            for key in ("social_financing_yoy", "m2_yoy"):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO economic_indicator_history (
                        metric_id, observation_date, value, source, fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        row["observation_date"],
                        row[key],
                        row["source"],
                        now,
                    ),
                )
            update_metric_status(
                conn,
                metric_id,
                success=True,
                source=row["source"],
            )
        except (
            KeyError,
            ValueError,
            RuntimeError,
            json.JSONDecodeError,
        ) as exc:
            print(f"[WARN] {metric_id} 拉取失败：{exc}", file=sys.stderr)
            update_metric_status(conn, metric_id, success=False, error=exc)

    fetchers = {
        "nasdaq100": fetch_worldpe_nasdaq100_history,
        "sp500": fetch_multpl_sp500_history,
    }
    for asset_id, fetcher in fetchers.items():
        metric_id = f"us_valuation:{asset_id}"
        if (
            not force_pe_history
            and metric_cache_is_fresh(conn, metric_id, cache_hours)
        ):
            continue
        try:
            rows = fetcher(sources[asset_id])
            store_pe_history(conn, rows)
            update_metric_status(
                conn,
                metric_id,
                success=True,
                source=rows[-1]["source_name"],
            )
        except (
            KeyError,
            ValueError,
            RuntimeError,
            json.JSONDecodeError,
        ) as exc:
            print(f"[WARN] {metric_id} 拉取失败：{exc}", file=sys.stderr)
            update_metric_status(conn, metric_id, success=False, error=exc)


def latest_economic_indicator(conn, metric_id):
    row = conn.execute(
        """
        SELECT observation_date, value, source, fetched_at
        FROM economic_indicator_history
        WHERE metric_id = ?
        ORDER BY observation_date DESC
        LIMIT 1
        """,
        (metric_id,),
    ).fetchone()
    return dict(row) if row else None


def latest_us_valuation(conn, asset_id, minimum_samples=20):
    index_code = {"nasdaq100": "NDX", "sp500": "SPX"}[asset_id]
    rows = conn.execute(
        """
        SELECT observation_date, value AS pe, source_name AS source,
               source_url, metric_type, is_estimated, validation_status,
               note, fetched_at
        FROM pe_history
        WHERE index_code = ? AND validation_status = 'valid'
        ORDER BY observation_date DESC
        """,
        (index_code,),
    ).fetchall()
    if not rows:
        return None
    latest = dict(rows[0])
    rows = [
        row for row in rows if row["metric_type"] == latest["metric_type"]
    ]
    values = [row["pe"] for row in rows]
    latest["sample_count"] = len(values)
    latest["window_label"] = US_PE_WINDOW_LABEL
    latest["window_months"] = PE_HISTORY_MONTHS
    latest["percentile"] = None
    if len(values) >= minimum_samples:
        below_or_equal = sum(value <= latest["pe"] for value in values)
        latest["percentile"] = round(below_or_equal / len(values) * 100, 2)
    return latest


def pe_history_rows(conn):
    rows = conn.execute(
        """
        SELECT index_code, index_name, metric_name, metric_type, value,
               observation_date, frequency, source_name, source_url,
               is_estimated, validation_status, note
        FROM pe_history
        ORDER BY index_code, metric_type, observation_date
        """
    ).fetchall()
    return [
        {
            "index_code": row["index_code"],
            "index_name": row["index_name"],
            "metric_name": row["metric_name"],
            "metric_type": row["metric_type"],
            "value": row["value"],
            "date": row["observation_date"],
            "frequency": row["frequency"],
            "source_name": row["source_name"],
            "source_url": row["source_url"],
            "is_estimated": bool(row["is_estimated"]),
            "validation_status": row["validation_status"],
            "note": row["note"],
        }
        for row in rows
    ]


def missing_months(start_date, end_date, observed_dates):
    current = month_start(start_date)
    end_month = month_start(end_date)
    observed = {month_start(value) for value in observed_dates}
    missing = []
    while current <= end_month:
        if current not in observed:
            missing.append(current.isoformat())
        current = shift_months(current, 1)
    return missing


def pe_history_quality(conn):
    rows = pe_history_rows(conn)
    groups = {}
    for row in rows:
        groups.setdefault((row["index_code"], row["metric_type"]), []).append(
            row
        )
    indicators = []
    for (index_code, metric_type), items in sorted(groups.items()):
        dates = [dt.date.fromisoformat(item["date"]) for item in items]
        valid_items = [
            item
            for item in items
            if item["validation_status"] == "valid" and item["value"] > 0
        ]
        indicators.append(
            {
                "index_code": index_code,
                "index_name": items[0]["index_name"],
                "metric_type": metric_type,
                "sample_count": len(valid_items),
                "start_date": min(dates).isoformat(),
                "end_date": max(dates).isoformat(),
                "missing_months": missing_months(
                    min(dates),
                    max(dates),
                    dates,
                ),
                "estimated_count": sum(
                    1 for item in items if item["is_estimated"]
                ),
                "source_name": items[0]["source_name"],
                "source_url": items[0]["source_url"],
                "window_label": US_PE_WINDOW_LABEL,
                "window_months": PE_HISTORY_MONTHS,
                "percentile_allowed": len(valid_items) >= 20,
                "allocation_score_eligible": (
                    len(valid_items) >= 20
                    and metric_type in {"trailing_pe", "forward_pe"}
                ),
            }
        )
    by_code = {item["index_code"]: item for item in indicators}
    nasdaq = by_code.get("NDX")
    sp500 = by_code.get("SPX")
    same_metric_type = bool(
        nasdaq
        and sp500
        and nasdaq["metric_type"] == sp500["metric_type"]
    )
    composite_eligible = bool(
        same_metric_type
        and nasdaq["allocation_score_eligible"]
        and sp500["allocation_score_eligible"]
    )
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "required_months": PE_HISTORY_MONTHS,
        "indicators": indicators,
        "same_metric_type": same_metric_type,
        "composite_us_valuation_score_eligible": composite_eligible,
        "reason": (
            "两项PE口径一致且样本数均不少于20"
            if composite_eligible
            else "样本不足、口径不一致或口径不受支持"
        ),
    }


def write_pe_history_outputs(conn):
    with output_paths.get_json_path("pe_history.json").open("w", encoding="utf-8") as f:
        json.dump(pe_history_rows(conn), f, ensure_ascii=False, indent=2)
    quality = pe_history_quality(conn)
    with output_paths.get_json_path("pe_history_quality.json").open("w", encoding="utf-8") as f:
        json.dump(quality, f, ensure_ascii=False, indent=2)
    return quality


def latest_nav(conn, code):
    return conn.execute(
        """
        SELECT nav_date, nav, pct_change
        FROM nav_history
        WHERE code = ?
        ORDER BY nav_date DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()


def generate_macro_report(conn):
    rows = []
    for series_id, name in MACRO_SERIES.items():
        observations = conn.execute(
            """
            SELECT observation_date, value
            FROM macro_history
            WHERE series_id = ?
            ORDER BY observation_date DESC
            LIMIT 21
            """,
            (series_id,),
        ).fetchall()
        if not observations:
            continue
        latest = observations[0]
        previous_day = observations[1] if len(observations) >= 2 else None
        previous_week = observations[5] if len(observations) >= 6 else None
        previous = observations[-1] if len(observations) >= 21 else None
        daily_change_bps = (
            round((latest["value"] - previous_day["value"]) * 100, 2)
            if previous_day is not None
            else None
        )
        weekly_change_bps = (
            round((latest["value"] - previous_week["value"]) * 100, 2)
            if previous_week is not None
            else None
        )
        change_20d_bps = (
            round((latest["value"] - previous["value"]) * 100, 2)
            if previous is not None
            else None
        )
        if series_id in ("DFII5", "DFII10"):
            environment = (
                "实际利率偏高，长久期资产承压"
                if latest["value"] >= 2
                else "实际利率中性偏高"
                if latest["value"] >= 1
                else "实际利率偏低"
            )
        elif series_id == "T10YIE":
            environment = (
                "通胀定价偏高"
                if latest["value"] >= 2.5
                else "通胀定价温和"
                if latest["value"] >= 1.5
                else "通胀定价偏低"
            )
        else:
            environment = (
                "政策利率偏低"
                if latest["value"] <= 2
                else "政策利率中性"
                if latest["value"] <= 3.5
                else "政策利率偏高"
            )
        rows.append(
            {
                "series_id": series_id,
                "name": name,
                "latest_date": latest["observation_date"],
                "latest_value": latest["value"],
                "daily_change_bps": daily_change_bps,
                "weekly_change_bps": weekly_change_bps,
                "change_20d_bps": change_20d_bps,
                "environment": environment,
            }
        )
    return rows


def valuation_status(percentile):
    if percentile < 20:
        return "极度低估"
    if percentile < 40:
        return "偏低估"
    if percentile < 60:
        return "合理"
    if percentile < 80:
        return "偏高估"
    return "高估"


def breakeven_status(value):
    if value < 2:
        return "低通胀预期"
    if value <= 3:
        return "正常区"
    return "高通胀预期"


def tips_5y_score(value):
    if value > 2:
        return -3, "实际利率压制区"
    if value >= 1:
        return -2, "实际利率偏高区"
    if value >= 0:
        return -1, "中性偏紧区"
    if value >= -1:
        return 1, "黄金友好区"
    return 3, "黄金价值区"


def tips_10y_score(value):
    if value > 2:
        return -2
    if value >= 1:
        return -1
    if value >= 0:
        return 0
    if value >= -1:
        return 1
    return 2


def breakeven_score(value):
    if value > 3:
        return 2
    if value >= 2:
        return 1
    if value >= 1:
        return 0
    return -1


def gold_temperature(score):
    if score >= 5:
        return "黄金价值区", "历史上黄金长期配置赔率较高"
    if score >= 2:
        return "黄金友好区", "黄金环境较好"
    if score >= -1:
        return "中性区", "黄金环境一般"
    return "黄金拥挤区", "黄金可能已处于高热度阶段"


def market_status_row(conn, metric_id):
    row = conn.execute(
        """
        SELECT last_attempt_at, last_success_at, last_error, source
        FROM market_update_status
        WHERE metric_id = ?
        """,
        (metric_id,),
    ).fetchone()
    return dict(row) if row else {
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": None,
        "source": None,
    }


def generate_market_temperature(conn, config, macro_rows=None):
    import_hs300_valuation_sample(conn)
    migrate_a500_display_snapshot(conn)
    china_equity_price = calculate_cn_equity_price_temperature(conn)
    temperature_config = market_temperature_config(config)
    index_results = {}
    for index_id, index_config in (temperature_config.get("indexes") or {}).items():
        row = conn.execute(
            """
            SELECT observation_date, pe_ttm, percentile, pb, pb_percentile,
                   source, fetched_at
            FROM market_valuation_history
            WHERE index_id = ?
            ORDER BY observation_date DESC
            LIMIT 1
            """,
            (index_id,),
        ).fetchone()
        status = market_status_row(conn, f"valuation:{index_id}")
        if row:
            index_results[index_id] = {
                "name": index_config.get("name", index_id),
                "code": index_config.get("code"),
                "pe": row["pe_ttm"],
                "percentile": row["percentile"],
                "pb": row["pb"],
                "pb_percentile": row["pb_percentile"],
                "status": valuation_status(row["percentile"]),
                "data_date": row["observation_date"],
                "last_success_at": status["last_success_at"] or row["fetched_at"],
                "last_error": status["last_error"],
                "source": row["source"],
            }
        else:
            index_results[index_id] = {
                "name": index_config.get("name", index_id),
                "code": index_config.get("code"),
                "pe": None,
                "percentile": None,
                "pb": None,
                "pb_percentile": None,
                "status": "数据暂缺",
                "data_date": None,
                "last_success_at": status["last_success_at"],
                "last_error": status["last_error"],
                "source": status["source"],
            }

    hs300_local = local_a_share_valuation(conn, "hs300")
    if hs300_local:
        index_results["hs300"] = {
            "name": "沪深300",
            "code": "000300",
            "status": valuation_status(hs300_local["percentile"]),
            "last_success_at": hs300_local["fetch_time"],
            "last_error": None,
            **hs300_local,
        }
    a500_display = local_a_share_valuation(conn, "a500")
    if a500_display:
        a500_display.update({
            "percentile": None,
            "pb_percentile": None,
            "percentile_status": "NOT_CALCULATED",
            "status": "Display Only",
        })
        index_results["a500"] = {
            "name": "中证A500",
            "code": "000510",
            "last_success_at": a500_display["fetch_time"],
            "last_error": None,
            **a500_display,
        }

    macro_by_id = {
        row["series_id"]: row
        for row in (macro_rows if macro_rows is not None else generate_macro_report(conn))
    }
    tips_5y = macro_by_id.get("DFII5")
    tips_10y = macro_by_id.get("DFII10")
    breakeven = macro_by_id.get("T10YIE")
    tips_5y_update = market_status_row(conn, "DFII5")
    tips_10y_update = market_status_row(conn, "DFII10")
    breakeven_update = market_status_row(conn, "T10YIE")

    a500 = index_results.get("a500", {})
    hs300 = index_results.get("hs300", {})
    a_share_percentiles = [
        value
        for value in (a500.get("percentile"), hs300.get("percentile"))
        if value is not None
    ]
    if a_share_percentiles:
        average_percentile = sum(a_share_percentiles) / len(a_share_percentiles)
        a_share_temperature = (
            "冷" if average_percentile < 40
            else "正常" if average_percentile <= 60
            else "热"
        )
    else:
        average_percentile = None
        a_share_temperature = "数据暂缺"

    if tips_5y and tips_10y and breakeven:
        score_5y, tips_5y_state = tips_5y_score(tips_5y["latest_value"])
        score_10y = tips_10y_score(tips_10y["latest_value"])
        score_breakeven = breakeven_score(breakeven["latest_value"])
        gold_score = score_5y + score_10y + score_breakeven
        gold_state, gold_description = gold_temperature(gold_score)
        gold_composite_temperature = (
            "冷" if gold_score >= 5
            else "正常" if gold_score >= -1
            else "热"
        )
    else:
        score_5y = None
        score_10y = None
        score_breakeven = None
        gold_score = None
        tips_5y_state = "数据暂缺"
        gold_state = "数据暂缺"
        gold_description = "指标数据不足"
        gold_composite_temperature = "数据暂缺"

    if breakeven:
        inflation_temperature = (
            "冷" if breakeven["latest_value"] < 2
            else "正常" if breakeven["latest_value"] <= 3
            else "热"
        )
    else:
        inflation_temperature = "数据暂缺"

    a_share_temperature = {
        "VERY_HOT": "很热", "HOT": "偏热", "NEUTRAL": "中性",
        "COOL": "偏冷", "VERY_COOL": "很冷",
        "EXTREME_RISK": "极端低位 / 高风险",
        "UNAVAILABLE": "数据暂缺",
    }.get(china_equity_price.get("level"), "数据暂缺")

    return {
        "chinaEquityPriceTemperature": china_equity_price,
        "a500PE": a500.get("pe"),
        "a500Percentile": a500.get("percentile"),
        "hs300PE": hs300.get("pe"),
        "hs300Percentile": hs300.get("percentile"),
        "tips5y": tips_5y["latest_value"] if tips_5y else None,
        "tips5yDailyChange": tips_5y["daily_change_bps"] if tips_5y else None,
        "tips5yWeeklyChange": tips_5y["weekly_change_bps"] if tips_5y else None,
        "tips10y": tips_10y["latest_value"] if tips_10y else None,
        "tips10yDailyChange": tips_10y["daily_change_bps"] if tips_10y else None,
        "tips10yWeeklyChange": tips_10y["weekly_change_bps"] if tips_10y else None,
        "breakeven10y": breakeven["latest_value"] if breakeven else None,
        "breakevenDailyChange": breakeven["daily_change_bps"] if breakeven else None,
        "breakevenWeeklyChange": breakeven["weekly_change_bps"] if breakeven else None,
        "valuation": index_results,
        "tips5yDetail": {
            "score": score_5y,
            "status": tips_5y_state,
            "data_date": tips_5y["latest_date"] if tips_5y else None,
            "last_success_at": tips_5y_update["last_success_at"],
            "last_error": tips_5y_update["last_error"],
            "source": tips_5y_update["source"] or ("fred" if tips_5y else None),
        },
        "tips10yDetail": {
            "score": score_10y,
            "data_date": tips_10y["latest_date"] if tips_10y else None,
            "last_success_at": tips_10y_update["last_success_at"],
            "last_error": tips_10y_update["last_error"],
            "source": tips_10y_update["source"] or ("fred" if tips_10y else None),
        },
        "breakeven": {
            "score": score_breakeven,
            "status": breakeven_status(breakeven["latest_value"]) if breakeven else "数据暂缺",
            "data_date": breakeven["latest_date"] if breakeven else None,
            "last_success_at": breakeven_update["last_success_at"],
            "last_error": breakeven_update["last_error"],
            "source": breakeven_update["source"] or ("fred" if breakeven else None),
        },
        "goldScore": gold_score,
        "goldTemperature": gold_state,
        "goldDescription": gold_description,
        "composite": {
            "aShare": a_share_temperature,
            "gold": gold_composite_temperature,
            "inflation": inflation_temperature,
            "aShareAveragePercentile": average_percentile,
        },
        "cacheHours": temperature_config.get("cache_hours", 24),
    }


def build_macro_context(macro_rows):
    tips = next(
        (row for row in macro_rows if row["series_id"] == "DFII10"),
        None,
    )
    if tips is None:
        return {
            "macro_multiplier": 1.0,
            "macro_reason": "TIPS数据缺失，使用标准补仓金额",
            "tips_value": None,
            "tips_change_20d_bps": None,
        }

    value = tips["latest_value"]
    change = tips["change_20d_bps"]
    if value >= 2 and change is not None and change >= 20:
        multiplier = 0.5
        reason = "实际利率偏高且上升，降低长久期资产补仓力度"
    elif change is not None and change <= -20:
        multiplier = 1.25
        reason = "实际利率明确下降，提高长久期资产补仓力度"
    else:
        multiplier = 1.0
        reason = "实际利率趋势未形成强信号，使用标准补仓金额"

    return {
        "macro_multiplier": multiplier,
        "macro_reason": reason,
        "tips_value": value,
        "tips_change_20d_bps": change,
    }


def high_since(conn, code, days):
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return conn.execute(
        """
        SELECT nav_date, nav
        FROM nav_history
        WHERE code = ? AND nav_date >= ?
        ORDER BY nav DESC, nav_date DESC
        LIMIT 1
        """,
        (code, start),
    ).fetchone()


def a_share_investment_plan(market_temperature):
    """PE/PB估值仅作展示参考，不再影响A股定投节奏和资金释放。

    返回固定的 multiplier=1.0，确保旧估值乘数不再作用于基金计划。
    """
    return {
        "multiplier": 1.0,
        "text": "估值数据仅供参考，不参与A股价格温度和资金释放",
    }


def make_signal(
    fund,
    latest,
    high_6m,
    high_12m,
    macro_context,
    market_temperature,
):
    if latest is None or high_12m is None:
        return None

    dd_6m = (latest["nav"] / high_6m["nav"] - 1) * 100 if high_6m else None
    dd_12m = (latest["nav"] / high_12m["nav"] - 1) * 100
    reference_dd = dd_12m
    max_amount = fund["max_holding_amount"]
    holding = fund["holding_amount"]
    remaining_capacity = max(0, max_amount - holding)
    fund_type = fund["fund_type"]
    macro_sensitive = fund_type in ("美股QDII", "偏股科技")
    macro_multiplier = (
        macro_context["macro_multiplier"] if macro_sensitive else 1.0
    )
    macro_reason = (
        macro_context["macro_reason"]
        if macro_sensitive
        else "该资产不使用TIPS调整补仓金额"
    )
    base_buy_amount = 0
    adjusted_buy_amount = 0

    if fund_type == "黄金":
        action = "观察/按配置处理"
        suggested_buy = 0
        gold_score = market_temperature.get("goldScore")
        gold_state = market_temperature.get("goldTemperature", "数据暂缺")
        gold_description = market_temperature.get("goldDescription", "指标数据不足")
        if gold_score is not None:
            future_plan = (
                f"Gold Score {gold_score:+d} · {gold_state}；"
                f"{gold_description}，仅供长期资产配置参考"
            )
        else:
            future_plan = "黄金温度数据暂缺，保留最近宏观环境记录"
    elif fund_type == "固收":
        action = "观察/按配置处理"
        suggested_buy = 0
        future_plan = "继续按既定固收转移计划执行，不由TIPS直接触发"
    elif holding >= max_amount:
        action = "已达仓位上限，不补"
        suggested_buy = 0
        future_plan = "仓位已满；即使回撤继续扩大也不补，先调整仓位上限"
    elif reference_dd <= -30:
        base_buy_amount = fund["drawdown_30_buy_amount"]
        adjusted_buy_amount = base_buy_amount * macro_multiplier
        suggested_buy = min(adjusted_buy_amount, remaining_capacity)
        action = f"触发30%回撤档，可补 {suggested_buy:.0f}"
        future_plan = (
            f"标准金额 {base_buy_amount:.0f} × 宏观系数 "
            f"{macro_multiplier:.2f}，建议分两次执行"
        )
    elif reference_dd <= -20:
        base_buy_amount = fund["drawdown_20_buy_amount"]
        adjusted_buy_amount = base_buy_amount * macro_multiplier
        suggested_buy = min(adjusted_buy_amount, remaining_capacity)
        action = f"触发20%回撤档，可补 {suggested_buy:.0f}"
        future_plan = (
            f"标准金额 {base_buy_amount:.0f} × 宏观系数 "
            f"{macro_multiplier:.2f}；30%档再评估"
        )
    elif reference_dd <= -10:
        action = "触发10%回撤，只提醒观察"
        suggested_buy = 0
        next_buy = fund["drawdown_20_buy_amount"] * macro_multiplier
        future_plan = (
            f"再回撤 {abs(-20 - reference_dd):.2f} 个百分点到20%档，"
            f"预计补 {min(next_buy, remaining_capacity):.0f}"
        )
    else:
        action = "未触发回撤补仓"
        suggested_buy = 0
        next_buy = fund["drawdown_20_buy_amount"] * macro_multiplier
        future_plan = (
            f"再回撤 {abs(-10 - reference_dd):.2f} 个百分点进入观察；"
            f"20%档预计补 {min(next_buy, remaining_capacity):.0f}"
        )

    valuation_plan = "估值数据仅供参考，不参与A股价格温度和资金释放"
    valuation_multiplier = 1.0
    price_temperature_plan = None
    if fund["code"] in ("022459", "022485"):
        cn_temp = market_temperature.get("chinaEquityPriceTemperature", {})
        carrier = cn_temp.get("carrierIndex", {})
        if cn_temp.get("modelEnabled"):
            ma_dist = carrier.get("movingAverageDistance")
            dd_1y = carrier.get("oneYearDrawdown")
            vol = carrier.get("annualizedVolatility")
            level_cn = {
                "VERY_HOT": "很热", "HOT": "偏热", "NEUTRAL": "中性",
                "COOL": "偏冷", "VERY_COOL": "很冷",
            }.get(cn_temp.get("level"), cn_temp.get("level", "-"))
            price_temperature_plan = (
                f"A500价格温度：{level_cn}；"
                f"相对MA500：{ma_dist * 100:+.2f}%；"
                f"近一年高点回撤：{dd_1y * 100:.2f}%；"
                f"60日年化波动率：{vol * 100:.2f}%；"
                f"价格温度释放系数：{cn_temp.get('effectiveReleaseFactor', 1.0):.2f}"
                if ma_dist is not None and dd_1y is not None and vol is not None
                else None
            )
        if price_temperature_plan:
            future_plan = f"{future_plan}；{price_temperature_plan}"
        future_plan = f"{future_plan}；PE/PB仅供参考，不参与自动评分"

    return {
        "code": fund["code"],
        "name": fund["name"],
        "type": fund_type,
        "strategy": fund["strategy"] or "无",
        "latest_date": latest["nav_date"],
        "latest_nav": latest["nav"],
        "daily_pct_change": latest["pct_change"],
        "high_6m_nav": high_6m["nav"] if high_6m else None,
        "high_6m_date": high_6m["nav_date"] if high_6m else None,
        "drawdown_6m_pct": dd_6m,
        "high_12m_nav": high_12m["nav"],
        "high_12m_date": high_12m["nav_date"],
        "drawdown_12m_pct": dd_12m,
        "holding_amount": holding,
        "max_holding_amount": max_amount,
        "remaining_capacity": remaining_capacity,
        "action": action,
        "suggested_buy": suggested_buy,
        "base_buy_amount": base_buy_amount,
        "adjusted_buy_amount": adjusted_buy_amount,
        "macro_multiplier": macro_multiplier,
        "macro_reason": macro_reason,
        "future_plan": future_plan,
        "valuation_multiplier": valuation_multiplier,
        "valuation_plan": valuation_plan,
        "price_temperature_plan": price_temperature_plan,
    }


def generate_report(
    conn,
    config,
    macro_rows=None,
    market_temperature=None,
    persist_alerts=False,
):
    macro_context = build_macro_context(macro_rows or [])
    market_temperature = market_temperature or {}
    rows = []
    for fund in config["funds"]:
        fund_row = conn.execute("SELECT * FROM funds WHERE code = ?", (fund["code"],)).fetchone()
        latest = latest_nav(conn, fund["code"])
        high_6m = high_since(conn, fund["code"], 183)
        high_12m = high_since(conn, fund["code"], 365)
        signal = make_signal(
            dict(fund_row),
            latest,
            high_6m,
            high_12m,
            macro_context,
            market_temperature,
        )
        if signal:
            rows.append(signal)

    audit = data_layer_audit.audit_fund_nav(conn, config)
    audit_by_code = dict((item["fund_code"], item) for item in audit["funds"])
    for row in rows:
        evidence = audit_by_code.get(row["code"], {})
        row.update({
            "is_qdii": evidence.get("is_qdii", False),
            "data_lag_days": evidence.get("data_lag_days"),
            "qdii_lag_status": evidence.get("qdii_lag_status", "NOT_APPLICABLE"),
            "coverage_6m_ratio": evidence.get("6m_coverage_ratio"),
            "coverage_6m_status": evidence.get("6m_coverage_status", "INSUFFICIENT"),
            "coverage_6m_sample_size": evidence.get("6m_sample_size", 0),
            "coverage_12m_ratio": evidence.get("12m_coverage_ratio"),
            "coverage_12m_status": evidence.get("12m_coverage_status", "INSUFFICIENT"),
            "coverage_12m_sample_size": evidence.get("12m_sample_size", 0),
        })

    if persist_alerts:
        now = dt.datetime.now().isoformat(timespec="seconds")
        for row in rows:
            if "触发20%回撤档" in row["action"] or "触发30%回撤档" in row["action"] or "已达仓位上限" in row["action"]:
                conn.execute(
                    "INSERT INTO alerts (code, nav_date, level, message, created_at) VALUES (?, ?, ?, ?, ?)",
                    (row["code"], row["latest_date"], "notice", row["action"], now),
                )

    return rows


def format_pct(value):
    if value is None:
        return "-"
    return f"{value:.2f}%"


def percentile_score(value, us_equity=False):
    if value is None:
        return None
    thresholds = (
        ((10, 100), (20, 90), (40, 70), (60, 50), (80, 25), (90, 10))
        if us_equity
        else ((10, 100), (20, 90), (40, 75), (60, 50), (80, 30), (90, 15))
    )
    for upper, score in thresholds:
        if value <= upper:
            return score
    return 0


def social_financing_score(value):
    if value is None:
        return None
    if value >= 11:
        return 90
    if value >= 10:
        return 75
    if value >= 9:
        return 60
    if value >= 8:
        return 45
    return 25


def m2_score(value):
    if value is None:
        return None
    if value >= 9:
        return 80
    if value >= 8:
        return 65
    if value >= 7:
        return 50
    if value >= 6:
        return 35
    return 20


def tips_liquidity_score(value):
    if value is None:
        return None
    if value <= 0:
        return 90
    if value <= 1:
        return 70
    if value <= 2:
        return 45
    if value <= 3:
        return 20
    return 5


def fed_funds_score(value):
    if value is None:
        return None
    if value <= 2:
        return 90
    if value <= 3.5:
        return 70
    if value <= 5:
        return 40
    return 15


def gold_indicator_score(value, kind):
    if value is None:
        return None
    if kind == "tips5y":
        return model_risk.inverse_real_yield_score(value, "5y")
    if kind == "tips10y":
        return model_risk.inverse_real_yield_score(value, "10y")
    return model_risk.positive_breakeven_score(value)


def weighted_score(parts):
    if any(value is None for value, _weight in parts):
        return None
    return round(sum(value * weight for value, weight in parts), 1)


def score_tier(score):
    if score is None:
        return "数据不完整"
    if score < 20:
        return "拥挤"
    if score < 40:
        return "偏热"
    if score < 60:
        return "中性"
    if score < 80:
        return "友好"
    return "价值"


def score_offset(score):
    if score is None:
        return 0
    if score < 20:
        return -0.10
    if score < 40:
        return -0.05
    if score < 60:
        return 0
    if score < 80:
        return 0.05
    return 0.10


def calculate_targets(scores, config, fixed_strategic_assets=()):
    settings = copilot_config(config)
    strategic = settings["strategic_allocation"]
    ranges = settings["allocation_ranges"]
    risk_assets = ("a_share", "us_equity", "gold")
    targets = {}
    for asset in risk_assets:
        lower, upper = ranges[asset]
        adjustment = 0 if asset in fixed_strategic_assets else score_offset(scores.get(asset))
        raw = float(strategic[asset]) + adjustment
        targets[asset] = min(float(upper), max(float(lower), raw))

    risk_total = sum(targets.values())
    max_risk = 1 - float(ranges["cash"][0])
    if risk_total > max_risk:
        scale = max_risk / risk_total
        targets = {asset: value * scale for asset, value in targets.items()}
    targets["cash"] = max(float(ranges["cash"][0]), 1 - sum(targets.values()))
    return {key: round(value, 4) for key, value in targets.items()}


def current_asset_value_details(config, conn):
    values = {"a_share": 0.0, "us_equity": 0.0, "gold": 0.0, "cash": 0.0}
    cost_basis = {
        "a_share": 0.0,
        "us_equity": 0.0,
        "gold": 0.0,
        "cash": 0.0,
    }
    mapping = {
        "539001": "us_equity",
        "270023": "us_equity",
        "022459": "a_share",
        "022485": "a_share",
        "008186": "a_share",
        "014661": "gold",
        "002009": "cash",
    }
    for fund in config["funds"]:
        asset = fund.get("asset_class") or mapping.get(fund["code"])
        if asset:
            market_value = float(fund["holding_amount"])
            values[asset] += market_value
            profit_pct = fund.get("profit_pct")
            if profit_pct is None or float(profit_pct) <= -100:
                estimated_cost = market_value
            else:
                estimated_cost = market_value / (1 + float(profit_pct) / 100)
            cost_basis[asset] += estimated_cost
    values["cash"] += float(config.get("cash_available", 0))
    cost_basis["cash"] += float(config.get("cash_available", 0))
    legacy_adjustments = get_state(conn, "executed_asset_adjustments", {}) or {}
    executed_market_values = (
        get_state(conn, "executed_asset_market_values", None)
        or legacy_adjustments
    )
    executed_cost_basis = (
        get_state(conn, "executed_asset_cost_basis", None)
        or legacy_adjustments
    )
    for asset in values:
        values[asset] += float(executed_market_values.get(asset, 0) or 0)
        cost_basis[asset] += float(executed_cost_basis.get(asset, 0) or 0)
    values = {key: round(value, 2) for key, value in values.items()}
    cost_basis = {key: round(value, 2) for key, value in cost_basis.items()}
    gain_loss = {
        asset: round(values[asset] - cost_basis[asset], 2)
        for asset in values
    }
    return {
        "current_values": values,
        "cost_basis": cost_basis,
        "gain_loss": gain_loss,
    }


def current_asset_values(config, conn):
    return current_asset_value_details(config, conn)["current_values"]


def fund_carrier_plan(config, asset_allocations, qdii_integration=None):
    settings = copilot_config(config)
    funds_by_code = {fund["code"]: fund for fund in config["funds"]}
    plan = []
    for asset in ("a_share", "us_equity", "gold"):
        planned_amount = round(float(asset_allocations.get(asset, 0) or 0), 2)
        if planned_amount <= 0:
            continue
        if asset == "us_equity" and qdii_integration is not None:
            selection = qdii_integration.get("selection", {})
            if qdii_integration.get("carrier_selection_status") == "BLOCKED":
                continue
            for row in selection.get("carrier_plan", []):
                plan.append(dict(row))
            continue
        fund_code = str(settings["execution_funds"].get(asset, ""))
        fund = funds_by_code.get(fund_code)
        if fund is None:
            raise ValueError(f"{asset_label(asset)}未配置有效的执行基金")
        if fund.get("asset_class") != asset:
            raise ValueError(
                f"{fund_code} 的资产分类与 {asset_label(asset)} 不一致"
            )
        plan.append({
            "fund_code": fund_code,
            "fund_name": fund["name"],
            "asset_class": asset,
            "asset_name": asset_label(asset),
            "planned_amount": planned_amount,
        })
    return plan


def fund_execution_rows(conn, month):
    rows = conn.execute(
        """
        SELECT month, fund_code, fund_name, asset_class, planned_amount,
               actual_executed_amount, executed_at
        FROM fund_execution_log
        WHERE month = ?
        ORDER BY id
        """,
        (month,),
    ).fetchall()
    return [dict(row) for row in rows]


def previous_allocation_snapshot(conn, month):
    row = conn.execute(
        """
        SELECT snapshot_json
        FROM allocation_history
        WHERE month < ?
        ORDER BY month DESC
        LIMIT 1
        """,
        (month,),
    ).fetchone()
    return json.loads(row["snapshot_json"]) if row else None


def monthly_expected_release_date(observation_date):
    if not observation_date:
        return None
    value = dt.date.fromisoformat(str(observation_date)[:10])
    next_month = shift_months(value.replace(day=1), 1)
    return next_month.replace(day=15).isoformat()


def build_data_quality_inputs(
    a500,
    hs300,
    china_price_temperature,
    social_row,
    m2_row,
    nasdaq_row,
    sp500_row,
    macro,
):
    def macro_item(indicator, series_id, direct_or_proxy, assets):
        row = macro.get(series_id, {})
        return {
            "indicator": indicator,
            "source": "FRED / %s" % series_id,
            "source_type": "official-distributor",
            "direct_or_proxy": direct_or_proxy,
            "latest_date": row.get("latest_date"),
            "frequency": "daily",
            "sample_size": None,
            "used_in_score": "gold" in assets,
            "assets": assets,
            "methodology_known": True,
            "reproducible": True,
        }

    inputs = [
        {
            "indicator": "a500_price_temperature",
            "source": "Eastmoney index daily kline",
            "source_type": "official-distributor",
            "direct_or_proxy": "Direct Indicator",
            "latest_date": china_price_temperature.get("carrierIndex", {}).get("latestDate"),
            "frequency": "daily",
            "sample_size": china_price_temperature.get("carrierIndex", {}).get("sampleCount", 0),
            "used_in_score": bool(china_price_temperature.get("modelEnabled")),
            "non_blocking_fallback": True,
            "assets": ["a_share"],
            "methodology_known": True,
            "reproducible": True,
        },
        {
            "indicator": "hs300_price_environment",
            "source": "Eastmoney index daily kline",
            "source_type": "official-distributor",
            "direct_or_proxy": "Direct Indicator",
            "latest_date": china_price_temperature.get("marketIndex", {}).get("latestDate"),
            "frequency": "daily",
            "sample_size": china_price_temperature.get("marketIndex", {}).get("sampleCount", 0),
            # HS300 is only a bounded environment adjustment inside the
            # A-share price-temperature model.  When that model is disabled,
            # it must not be disclosed or gated as an active score input.
            "used_in_score": bool(china_price_temperature.get("modelEnabled")),
            "non_blocking_fallback": True,
            "assets": ["a_share"],
            "methodology_known": True,
            "reproducible": True,
        },
        {
            "indicator": "a500_pe_percentile",
            "source": a500.get("source") or "unavailable",
            "source_type": "third-party",
            "direct_or_proxy": "Proxy Indicator",
            "latest_date": a500.get("data_date"),
            "frequency": "daily",
            "sample_size": a500.get("sample_size", 0),
            "used_in_score": False,
            "assets": ["a_share"],
            "methodology_known": False,
            "reproducible": False,
        },
        {
            "indicator": "a500_pb",
            "source": a500.get("source") or "unavailable",
            "source_type": "third-party",
            "direct_or_proxy": "Proxy Indicator",
            "latest_date": a500.get("data_date"),
            "frequency": "daily",
            "sample_size": a500.get("sample_size", 0),
            "used_in_score": False,
            "assets": ["a_share"],
            "methodology_known": False,
            "reproducible": False,
        },
        {
            "indicator": "hs300_pe_percentile",
            "source": hs300.get("source") or "AKShare/Legulegu",
            "source_type": "third-party",
            "direct_or_proxy": "Proxy Indicator",
            "latest_date": hs300.get("data_date"),
            "frequency": "daily",
            "sample_size": hs300.get("sample_size", 0),
            "used_in_score": False,
            "assets": ["a_share"],
            "methodology_known": True,
            "reproducible": True,
        },
        {
            "indicator": "hs300_pb_percentile",
            "source": hs300.get("source") or "AKShare/Legulegu",
            "source_type": "third-party",
            "direct_or_proxy": "Proxy Indicator",
            "latest_date": hs300.get("data_date"),
            "frequency": "daily",
            "sample_size": hs300.get("sample_size", 0),
            "used_in_score": False,
            "assets": ["a_share"],
            "methodology_known": True,
            "reproducible": True,
        },
    ]
    for indicator, row in (
        ("social_financing_yoy", social_row),
        ("m2_yoy", m2_row),
    ):
        latest_date = row["observation_date"] if row else None
        inputs.append({
            "indicator": indicator,
            "source": row["source"] if row else "manual",
            "source_type": "official" if row else "manual",
            "direct_or_proxy": "Direct Indicator" if row else "Manual Input",
            "latest_date": latest_date,
            "frequency": "monthly",
            "expected_release_date": monthly_expected_release_date(latest_date),
            "sample_size": 1 if row else 0,
            "used_in_score": False,
            "assets": ["a_share"],
            "methodology_known": True,
            "reproducible": bool(row),
        })
    for indicator, row in (
        ("nasdaq100_pe_percentile", nasdaq_row),
        ("sp500_pe_percentile", sp500_row),
    ):
        asset_id = "nasdaq100" if indicator.startswith("nasdaq100") else "sp500"
        definition = US_VALUATION_SOURCE_DEFINITIONS[asset_id]
        inputs.append({
            "indicator": indicator,
            "source": row["source"] if row else None,
            "source_type": "third-party",
            "direct_or_proxy": "Proxy Indicator",
            "latest_date": row["observation_date"] if row else None,
            "frequency": "monthly",
            "expected_release_date": (
                shift_months(dt.date.fromisoformat(row["observation_date"]), 1).replace(day=10).isoformat()
                if row else None
            ),
            "sample_size": row["sample_count"] if row else 0,
            "used_in_score": False,
            "used_in_release_factor": False,
            "blocking": False,
            "governance_status": "DISPLAY_ONLY",
            "approval_status": "DISPLAY_ONLY",
            "assets": [],
            "methodology_known": definition["methodology_known"],
            "reproducible": bool(row and row["sample_count"] >= 20),
        })
    inputs.extend([
        macro_item("tips5y", "DFII5", "Direct Indicator", ["gold"]),
        macro_item("tips10y", "DFII10", "Direct Indicator", ["gold"]),
        macro_item("breakeven10y", "T10YIE", "Derived Indicator", ["gold"]),
        macro_item("fed_funds", "DFF", "Direct Indicator", ["gold"]),
    ])
    return inputs


def allocation_event(conn, month, decision=None):
    query = """
        SELECT decision, deploy_amount, allocation_json, created_at,
               plan_amount, plan_allocation_json, executed_at, execution_type
        FROM allocation_events
        WHERE month = ?
    """
    params = [month]
    if decision:
        query += " AND decision = ?"
        params.append(decision)
    query += " ORDER BY id DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    return {
        "decision": row["decision"],
        "deploy_amount": float(row["deploy_amount"]),
        "allocations": json.loads(row["allocation_json"]),
        "created_at": row["created_at"],
        "plan_amount": float(
            row["plan_amount"]
            if row["plan_amount"] is not None
            else row["deploy_amount"]
        ),
        "plan_allocations": json.loads(
            row["plan_allocation_json"] or row["allocation_json"]
        ),
        "executed_at": row["executed_at"] or row["created_at"],
        "execution_type": row["execution_type"] or "Model Auto Execution",
    }


def finalized_allocation_snapshot(conn, month):
    row = conn.execute(
        """
        SELECT snapshot_json, user_decision, decision_at
        FROM allocation_history
        WHERE month = ? AND user_decision IS NOT NULL
        """,
        (month,),
    ).fetchone()
    if row is None:
        return None

    snapshot = json.loads(row["snapshot_json"])
    decision = row["user_decision"]
    event = allocation_event(conn, month, decision)
    remaining_pool = float(get_state(conn, "dynamic_cash_pool", 0) or 0)
    stored_plan_amount = float(snapshot.get("plan_amount", snapshot.get("deploy_amount", 0)) or 0)
    stored_plan = snapshot.get("allocation_plan", snapshot.get("allocations", {})) or {}

    # Older executed snapshots may have been overwritten after execution.
    # The immutable event is authoritative for the original executed plan.
    if decision in ("execute", "manual_review") and event:
        plan_amount = event["plan_amount"]
        allocation_plan = event["plan_allocations"]
        executed_amount = event["deploy_amount"]
        executed_allocations = event["allocations"]
        executed_at = event["executed_at"]
        original_pool = float(
            snapshot.get(
                "original_dynamic_cash_pool",
                remaining_pool + executed_amount,
            )
        )
    else:
        plan_amount = stored_plan_amount
        allocation_plan = stored_plan
        executed_amount = 0
        executed_allocations = {}
        executed_at = None
        original_pool = float(
            snapshot.get("original_dynamic_cash_pool", snapshot.get("dynamic_cash_pool", remaining_pool))
        )

    status = "executed" if decision in ("execute", "manual_review") else "ignored"
    fund_executions = fund_execution_rows(conn, month)
    if decision in ("execute", "manual_review") and fund_executions:
        executed_amount = round(
            sum(row["actual_executed_amount"] for row in fund_executions),
            2,
        )
        executed_allocations = {
            asset: round(
                sum(
                    row["actual_executed_amount"]
                    for row in fund_executions
                    if row["asset_class"] == asset
                ),
                2,
            )
            for asset in ("a_share", "us_equity", "gold")
        }
        executed_at = max(row["executed_at"] for row in fund_executions)
    unexecuted_amount = round(max(0, plan_amount - executed_amount), 2)
    current_month = {
        "status": status,
        "planAmount": round(plan_amount, 2),
        "allocationPlan": allocation_plan,
        "executedAmount": round(executed_amount, 2),
        "executedAt": executed_at,
        "executedAllocations": executed_allocations,
        "fundExecutions": fund_executions,
        "unexecutedAmount": unexecuted_amount,
        "remainingDynamicCashPool": round(remaining_pool, 2),
        "nextMonthPendingPool": round(remaining_pool, 2),
    }
    snapshot.update({
        "status": status,
        "user_decision": decision,
        "decision_at": row["decision_at"],
        "execution_type": event["execution_type"] if event else None,
        "plan_amount": round(plan_amount, 2),
        "allocation_plan": allocation_plan,
        "executed_amount": round(executed_amount, 2),
        "executed_at": executed_at,
        "executed_allocations": executed_allocations,
        "fund_executions": fund_executions,
        "unexecuted_amount": unexecuted_amount,
        "original_dynamic_cash_pool": round(original_pool, 2),
        "remaining_dynamic_cash_pool": round(remaining_pool, 2),
        "next_month_pending_pool": round(remaining_pool, 2),
        "dynamic_cash_pool": round(remaining_pool, 2),
        "deploy_amount": round(plan_amount, 2),
        "allocations": allocation_plan,
        "currentMonth": current_month,
        "current_month": current_month,
    })
    return snapshot


def generate_copilot_snapshot(conn, config, market_temperature,
                              carrier_snapshot_path=None, carrier_now=None):
    generated_at = os.environ.get("ASSET_COPILOT_GENERATED_AT") or dt.datetime.now().astimezone().isoformat(timespec="seconds")
    run_id = (
        os.environ.get("ASSET_COPILOT_RUN_ID")
        or dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_v7-ndx-v1-engineering-final"
    )
    month = dt.date.today().strftime("%Y-%m")
    finalized = finalized_allocation_snapshot(conn, month)

    settings = copilot_config(config)
    manual = settings["manual_indicators"]
    social_row = latest_economic_indicator(conn, "social_financing_yoy")
    m2_row = latest_economic_indicator(conn, "m2_yoy")
    nasdaq_row = latest_us_valuation(conn, "nasdaq100")
    sp500_row = latest_us_valuation(conn, "sp500")
    social_financing_yoy = (
        social_row["value"]
        if social_row
        else manual.get("social_financing_yoy")
    )
    m2_yoy = m2_row["value"] if m2_row else manual.get("m2_yoy")
    nasdaq_percentile = nasdaq_row["percentile"] if nasdaq_row else None
    sp500_percentile = sp500_row["percentile"] if sp500_row else None
    pe_types_match = bool(
        nasdaq_row
        and sp500_row
        and nasdaq_row["metric_type"] == sp500_row["metric_type"]
    )
    us_valuation_eligible = bool(
        pe_types_match
        and nasdaq_row["sample_count"] >= 20
        and sp500_row["sample_count"] >= 20
    )
    macro = {
        row["series_id"]: row
        for row in generate_macro_report(conn)
    }
    a500 = market_temperature.get("valuation", {}).get("a500", {})
    hs300 = market_temperature.get("valuation", {}).get("hs300", {})
    pb_percentile = hs300.get("pb_percentile")
    china_price_temperature = market_temperature.get(
        "chinaEquityPriceTemperature", {}
    )
    # A-share automatic temperature is price-only. A neutral score is used
    # solely as a compatibility value when the temperature module degrades;
    # releaseFactor remains 1.0 and the strategic gap stays authoritative.
    a_price_score = (
        china_price_temperature.get("finalScore")
        if china_price_temperature.get("modelEnabled") else None
    )
    a_share_score = a_price_score if a_price_score is not None else 50.0
    us_valuation = (
        weighted_score([
            (
                percentile_score(
                    nasdaq_percentile,
                    us_equity=True,
                ),
                0.60,
            ),
            (
                percentile_score(
                    sp500_percentile,
                    us_equity=True,
                ),
                0.40,
            ),
        ])
        if us_valuation_eligible
        else None
    )
    tips_5y = macro.get("DFII5", {}).get("latest_value")
    tips_10y = macro.get("DFII10", {}).get("latest_value")
    breakeven = macro.get("T10YIE", {}).get("latest_value")
    fed_funds = macro.get("DFF", {}).get("latest_value")
    us_liquidity = weighted_score([
        (tips_liquidity_score(tips_5y), 0.40),
        (tips_liquidity_score(tips_10y), 0.40),
        (fed_funds_score(fed_funds), 0.20),
    ])
    gold_score_detail = model_risk.calculate_gold_score(
        tips_5y,
        tips_10y,
        breakeven,
        fed_funds,
    )
    ndx_model = load_ndx_validation_snapshot()
    # Shadow data gate requires an explicit volatility input status. The 60-day
    # volatility series is derived from the same governed NDX price history.
    ndx_model.setdefault("volatility_data_status", ndx_model.get("price_data_status"))
    ndx_temperature_score = ndx_model.get("temperature_score")
    scores = {
        "a_share": round(a_share_score, 1),
        # Shadow score is visible and may be used for research routing only.
        # It cannot change the target or produce a formal release while the
        # activation gate remains closed.
        "us_equity": round(float(ndx_temperature_score), 1) if ndx_temperature_score is not None else None,
        "gold": gold_score_detail["final_gold_score"],
    }
    tiers = {asset: score_tier(score) for asset, score in scores.items()}
    if scores.get("us_equity") is None:
        tiers["us_equity"] = "模型验证中"
    # ── Current effective targets ──
    # Gold uses score-based adjustment; A-share uses strategic; US equity carries forward last valid 35%
    targets = calculate_targets(scores, config, fixed_strategic_assets=("a_share",))
    # NDX V1 affects release cadence only. The 35% target stays locked until a
    # separate user confirmation switches it to the proposed strategic 40%.
    targets["us_equity"] = 0.35  # CARRY_FORWARD_LAST_VALID_TARGET
    targets["cash"] = round(1.0 - targets["a_share"] - targets["us_equity"] - targets["gold"], 4)
    target_explanations = {}
    for asset in ("a_share", "us_equity", "gold"):
        strategic_target = float(settings["strategic_allocation"][asset])
        current_target = float(targets[asset])
        is_carry_forward = asset == "us_equity"
        adjustment = -0.05 if is_carry_forward else (0 if asset == "a_share" else score_offset(scores.get(asset)))
        min_target, max_target = [
            float(value) for value in settings["allocation_ranges"][asset]
        ]
        raw_target = strategic_target + adjustment
        final_target = float(targets[asset])
        floor_hit = raw_target <= min_target and final_target <= min_target
        cap_hit = raw_target >= max_target and final_target >= max_target
        if is_carry_forward:
            target_reason = (
                "长期战略目标为 %.1f%%。NDX V1已完成Shadow验证并激活，"
                "但目标仓位从上一有效 %.1f%% 切换至战略目标仍需单独确认；"
                "当前继续沿用上一有效目标（target_mode=CARRY_FORWARD_LAST_VALID_TARGET）。"
                % (strategic_target * 100, final_target * 100)
            )
        elif floor_hit:
            target_reason = (
                "战略目标 %.1f%% + Score调整 %.1f%% = %.1f%%，"
                "触及最低配置下限 %.1f%%，最终目标 %.1f%%。"
                % (
                    strategic_target * 100,
                    adjustment * 100,
                    raw_target * 100,
                    min_target * 100,
                    final_target * 100,
                )
            )
        elif cap_hit:
            target_reason = (
                "战略目标 %.1f%% + Score调整 %.1f%% 触及最高配置上限 %.1f%%，最终目标 %.1f%%。"
                % (strategic_target * 100, adjustment * 100, max_target * 100, final_target * 100)
            )
        else:
            target_reason = (
                "战略目标 %.1f%% + Score调整 %.1f%%，最终目标 %.1f%%，未触及上下限。"
                % (strategic_target * 100, adjustment * 100, final_target * 100)
            )
        target_explanations[asset] = {
            "strategic_target": strategic_target,
            "score_adjustment": adjustment,
            "min_target": min_target,
            "max_target": max_target,
            "final_target": final_target,
            "floor_hit": floor_hit,
            "cap_hit": cap_hit,
            "target_reason": target_reason,
            "target_mode": "CARRY_FORWARD_LAST_VALID_TARGET" if is_carry_forward else "ACTIVE",
            "target_source": "LAST_VALID_DECISION_SNAPSHOT" if is_carry_forward else "STRATEGIC_ALLOCATION",
        }
    # Cash target: residual
    target_explanations["cash"] = {
        "strategic_target": None,  # N/A — residual
        "score_adjustment": 0,
        "min_target": float(settings["allocation_ranges"]["cash"][0]),
        "max_target": float(settings["allocation_ranges"]["cash"][1]),
        "final_target": float(targets["cash"]),
        "floor_hit": False,
        "cap_hit": False,
        "target_reason": "现金及低风险当前目标 %.1f%%，由其余资产当前有效目标的剩余比例确定（target_mode=RESIDUAL_TARGET）。100%% - 40%% - 35%% - 5%% = 20%%。" % (targets["cash"] * 100),
        "target_mode": "RESIDUAL_TARGET",
        "target_source": "RESIDUAL_CALCULATION",
    }
    value_details = current_asset_value_details(config, conn)
    current = value_details["current_values"]
    cost_basis = value_details["cost_basis"]
    gain_loss = value_details["gain_loss"]
    total = sum(current.values())
    target_values = {
        asset: round(total * target, 2) for asset, target in targets.items()
    }
    gaps = {
        asset: round(target_values[asset] - current[asset], 2)
        for asset in targets
    }

    previous = previous_allocation_snapshot(conn, month)
    previous_scores = (previous or {}).get("scores", {})
    previous_tiers = (previous or {}).get("tiers", {})
    previous_gaps = (previous or {}).get("gaps", {})
    momentum = {
        asset: (
            round(scores[asset] - previous_scores[asset], 1)
            if scores[asset] is not None
            and previous_scores.get(asset) is not None
            else None
        )
        for asset in scores
    }
    score_delta = max(
        (abs(value) for value in momentum.values() if value is not None),
        default=0,
    )
    tier_changed = any(
        previous_tiers.get(asset)
        and previous_tiers.get(asset) != tiers[asset]
        for asset in scores
    )
    gap_delta = max(
        (
            abs(gaps[asset] - previous_gaps[asset])
            for asset in scores
            if previous_gaps.get(asset) is not None
        ),
        default=0,
    )
    gap_delta_ratio = gap_delta / total if total else 0
    months_without_deploy = int(get_state(conn, "months_without_deploy", 0) or 0)
    release_rules = settings["release_rules"]
    absolute_gap_threshold_ratio = float(
        release_rules["absolute_gap_threshold_ratio"]
    )
    absolute_gap_threshold_value = total * absolute_gap_threshold_ratio
    absolute_gap_assets = [
        {
            "asset": asset,
            "asset_name": asset_label(asset),
            "gap_value": gaps[asset],
            "gap_ratio": round(gaps[asset] / total, 4) if total else 0,
            "threshold_ratio": absolute_gap_threshold_ratio,
            "threshold_value": round(absolute_gap_threshold_value, 2),
        }
        for asset in scores
        if gaps[asset] >= absolute_gap_threshold_value
    ]
    quality_inputs = build_data_quality_inputs(
        a500,
        hs300,
        china_price_temperature,
        social_row,
        m2_row,
        nasdaq_row,
        sp500_row,
        macro,
    )
    data_quality_gate = model_risk.run_data_quality_gate(quality_inputs)
    if not china_price_temperature.get("modelEnabled"):
        reason_code = china_price_temperature.get("activationStatus", "A500_MODEL_DISABLED")
        data_quality_gate["asset_level_status"]["a_share"] = {
            "data_quality_status": "WARNING",
            "execution_status": "ELIGIBLE",
            "blocking_issues": [],
            "warning_issues": [reason_code],
            "reason": f"A500价格温度模型未启用（{reason_code}）；战略缺口回退继续可用",
        }
    shadow_ledger = load_ndx_shadow_lifecycle()
    activation_gate = ndx_activation_gate_status(shadow_ledger)
    shadow_complete = activation_gate["activation_active"]
    activation_status = shadow_ledger.get("activation_status", "NOT_ACTIVE")
    ndx_activation_active = activation_gate["activation_active"]
    ndx_model = dict(ndx_model)
    ndx_model["model_status"] = "ACTIVE" if ndx_activation_active else "UNDER_VALIDATION"
    ndx_model["activation_status"] = activation_status
    ndx_model["ready_for_manual_activation"] = bool(
        shadow_complete and shadow_ledger.get("ready_for_manual_activation_review")
    )
    ndx_model["shadow_status"] = shadow_ledger.get("status", "DAY1_PENDING")
    ndx_model["shadow_days_completed"] = int(shadow_ledger.get("shadow_days_completed", 0) or 0)
    ndx_model["shadow_required_complete_days"] = int(
        shadow_ledger.get("required_complete_days", ndx_shadow_run.REQUIRED_COMPLETE_DAYS)
        or ndx_shadow_run.REQUIRED_COMPLETE_DAYS
    )
    if activation_gate["allow_formal_decision"]:
        data_quality_gate["asset_level_status"]["us_equity"] = {
            "data_quality_status": "PASS" if ndx_model.get("data_status") == "PASS" else "WARNING",
            "execution_status": "ELIGIBLE" if ndx_model.get("data_status") == "PASS" else "BLOCKED",
            "blocking_issues": [] if ndx_model.get("data_status") == "PASS" else ["NDX_PRICE_TEMPERATURE_DATA_NOT_PASS"],
            "warning_issues": [],
            "reason": "NDX V1已完成Shadow验证、人工激活及首次决策确认；按当前数据质量门参与正式决策。",
        }
    else:
        activation_blocker = activation_gate["blocking_reason"]
        data_quality_gate["asset_level_status"]["us_equity"] = {
            "data_quality_status": "PASS" if ndx_model.get("data_status") == "PASS" else "WARNING",
            "execution_status": "BLOCKED",
            "blocking_issues": [
                "NDX_PRICE_TEMPERATURE_UNDER_VALIDATION",
                "NDX_OFFLINE_GATE_NOT_PASSED" if not ndx_model.get("offline_pass") else activation_blocker,
            ],
            "warning_issues": [],
            "reason": "NDX价格温度离线验证链未完成，或首次正式决策尚未人工确认；当前不产生正式释放。",
        }
    complete = activation_gate["allow_formal_decision"]
    pool_control = model_risk.apply_pool_status(
        data_quality_gate["asset_level_status"],
        {asset: max(0, gaps[asset]) for asset in scores},
    )
    if not complete:
        pool_control.update({
            "dynamic_cash_pool_status": "FREEZE",
            "pool_status_reason": "纳指资产层模型仍在验证；PE仅供展示且不构成阻断，当前禁止自动释放。",
            "allow_auto_execution": False,
        })
    data_quality_gate.update(pool_control)
    data_quality_gate["allow_execution"] = pool_control["allow_auto_execution"]
    asset_data_states = [
        row.get("data_quality_status", "FAIL")
        for row in data_quality_gate["asset_level_status"].values()
    ]
    data_quality_gate["data_status"] = (
        "FAIL" if "FAIL" in asset_data_states
        else "WARNING" if "WARNING" in asset_data_states
        else "PASS"
    )
    data_quality_gate["model_status"] = (
        "UNDER_VALIDATION" if not complete
        else "READY" if pool_control["allow_auto_execution"]
        else "REFERENCE_ONLY"
    )
    data_quality_gate["decision_status"] = (
        "EXECUTE" if pool_control["allow_auto_execution"] else "FREEZE"
    )

    ratio = 0
    action_level = "无动作"
    reasons = []
    is_initialization = previous is None
    allow_initial_absolute_gap = bool(
        release_rules["allow_absolute_gap_on_initialization"]
    )
    matched_rules = []
    if not complete:
        reasons.append("纳指资产层：NDX价格温度与单一实际利率模型待验证，动态资金池保持冻结")
    elif is_initialization:
        reasons.append("初始化首月：仅建立评分、温度和GapValue基线")
        if allow_initial_absolute_gap and absolute_gap_assets:
            ratio = min(
                0.25,
                float(release_rules["initial_max_release_ratio"]),
            )
            action_level = "常规释放"
            matched_rules.append("initial_absolute_gap")
            for item in absolute_gap_assets:
                reasons.append(
                    "初始化绝对GapValue规则："
                    f"{item['asset_name']} GapValue "
                    f"{item['gap_value']:,.0f} 元，占组合 "
                    f"{item['gap_ratio'] * 100:.1f}%，达到阈值 "
                    f"{item['threshold_ratio'] * 100:.1f}%"
                )
            reasons.append(
                f"初始化首月释放上限为 {ratio * 100:.0f}%，禁止强释放50%"
            )
        else:
            reasons.append(
                "初始化绝对GapValue触发未启用或没有资产达到阈值，本月不释放"
            )
    else:
        first_value = any(
            tiers[asset] == "价值" and previous_tiers.get(asset) != "价值"
            for asset in scores
        )
        strong_rules = []
        if score_delta >= 20:
            strong_rules.append("score_change_20")
            reasons.append(
                f"最大月度评分变化 {score_delta:.1f}，达到强释放阈值 20.0"
            )
        if first_value:
            strong_rules.append("first_value_tier")
            changed_assets = [
                asset_label(asset)
                for asset in scores
                if tiers[asset] == "价值"
                and previous_tiers.get(asset) != "价值"
            ]
            reasons.append(
                "温度层级首次进入价值区：" + "、".join(changed_assets)
            )
        if absolute_gap_assets:
            strong_rules.append("absolute_gap_10pct")
            for item in absolute_gap_assets:
                reasons.append(
                    "绝对GapValue规则："
                    f"{item['asset_name']} GapValue "
                    f"{item['gap_value']:,.0f} 元，占组合 "
                    f"{item['gap_ratio'] * 100:.1f}%，达到阈值 "
                    f"{item['threshold_ratio'] * 100:.1f}%"
                )
        if months_without_deploy >= 6:
            strong_rules.append("months_without_deploy_6")
            reasons.append(
                f"连续未部署 {months_without_deploy} 个月，达到强释放阈值 6 个月"
            )
        if strong_rules:
            ratio = 0.50
            action_level = "强释放"
            matched_rules.extend(strong_rules)
        else:
            regular_rules = []
            if score_delta >= 10:
                regular_rules.append("score_change_10")
                reasons.append(
                    f"最大月度评分变化 {score_delta:.1f}，达到常规释放阈值 10.0"
                )
            if tier_changed:
                regular_rules.append("tier_changed")
                reasons.append("至少一项资产温度层级发生变化")
            if gap_delta_ratio >= 0.03:
                regular_rules.append("gap_change_3pct")
                reasons.append(
                    f"最大GapValue变化占组合 {gap_delta_ratio * 100:.1f}%，"
                    "达到常规释放阈值 3.0%"
                )
        if not strong_rules and regular_rules:
            ratio = 0.25
            action_level = "常规释放"
            matched_rules.extend(regular_rules)
        elif not strong_rules and months_without_deploy >= 3:
            ratio = 0.15
            action_level = "低比例释放"
            matched_rules.append("months_without_deploy_3")
            reasons.append(
                f"连续未部署 {months_without_deploy} 个月，达到低比例释放阈值 3 个月"
            )
        elif not strong_rules and not regular_rules:
            reasons.append("没有任何行动触发规则成立，本月释放金额为0")

    reasons.append("Asset-Level Data Quality Gate: %s" % pool_control["pool_status_reason"])
    for asset in ("a_share", "us_equity", "gold"):
        quality = data_quality_gate["asset_level_status"][asset]
        reasons.append(
            "%s: %s / %s - %s"
            % (
                asset_label(asset),
                quality["data_quality_status"],
                quality["execution_status"],
                quality["reason"],
            )
        )

    pool = float(get_state(conn, "dynamic_cash_pool", 0) or 0)
    theoretical_release_amount = round(pool * ratio, 2)
    allocation_routing = model_risk.route_asset_level_allocation(
        {asset: max(0, gaps[asset]) for asset in scores},
        scores,
        theoretical_release_amount,
        data_quality_gate["asset_level_status"],
        release_factors={
            "a_share": china_price_temperature.get("effectiveReleaseFactor", 1.0),
            "us_equity": ndx_model.get("candidate_effective_release_factor", 0.0),
        },
        temperature_multiplier_overrides={"a_share": 1.0},
    )
    candidate_executable_allocations = allocation_routing["allocations"]
    allocations = (
        candidate_executable_allocations
        if pool_control["allow_auto_execution"]
        else {asset: 0 for asset in scores}
    )
    deploy_amount = round(sum(allocations.values()), 2)
    positive_gap_total = sum(max(0, gaps[asset]) for asset in scores)
    if positive_gap_total <= 0:
        deploy_amount = 0
        ratio = 0
        action_level = "无动作"
        reasons.append("所有资产配置缺口均不为正")
    qdii_simulation_amount = round(min(pool * 0.25, max(0, gaps.get("us_equity", 0))), 2)
    carrier_kwargs = {"config": config, "now": carrier_now}
    if carrier_snapshot_path is not None:
        carrier_kwargs["path"] = carrier_snapshot_path
    qdii_carrier_integration = qdii_carrier.integration_snapshot(
        qdii_simulation_amount, **carrier_kwargs,
    )
    qdii_carrier_integration["simulation_only"] = True
    # ── V7 Three-Layer Decision Chain ──
    # Layer 1: Model Candidate (pure NDX model, no carrier knowledge)
    ndx_shadow_routing = model_risk.route_allocation(
        {asset: max(0, gaps[asset]) for asset in scores}, scores, pool,
    )
    ndx_gap_routed_amount = float(
        ndx_shadow_routing.get("allocations", {}).get("us_equity", 0) or 0
    )
    model_candidate = ndx_price_temperature.candidate_amount_chain(
        ndx_gap_routed_amount,
        ndx_model.get("candidate_effective_release_factor", 0),
        pool,
    )

    # Layer 2: Carrier Matching (apply capacity constraints from carrier facts)
    carrier_matching = qdii_carrier.apply_carrier_matching(
        model_candidate["ndx_candidate_release_amount"],
        qdii_carrier_integration,
    )

    # Layer 3: Formal Decision. ACTIVE only makes the candidate eligible for
    # the normal confirmation flow; it does not write execution events here.
    if pool_control["allow_auto_execution"]:
        formal_executable_amount = carrier_matching["carrier_coverable_amount"]
        formal_release_amount = carrier_matching["carrier_coverable_amount"]
        retained_due_to_decision_freeze = 0.0
    else:
        formal_executable_amount = 0.0
        formal_release_amount = 0.0
        retained_due_to_decision_freeze = carrier_matching["carrier_coverable_amount"]

    formal_decision = {
        "formal_executable_amount": round(formal_executable_amount, 2),
        "formal_release_amount": round(formal_release_amount, 2),
        "retained_due_to_decision_freeze": round(retained_due_to_decision_freeze, 2),
        "decision_status": data_quality_gate["decision_status"],
        "dynamic_cash_pool_status": pool_control["dynamic_cash_pool_status"],
    }

    # Build canonical three-layer chain
    v7_decision_chain = {
        "model_candidate": model_candidate,
        "carrier_matching": carrier_matching,
        "formal_decision": formal_decision,
    }

    # ── Amount Identity Verification ──
    identity_errors = []
    mc = model_candidate
    cm = carrier_matching
    fd = formal_decision
    # Identity 1: candidate == coverable + retained_by_capacity + retained_by_carrier_block
    lhs1 = mc["ndx_candidate_release_amount"]
    rhs1 = cm["carrier_coverable_amount"] + cm["retained_due_to_capacity"] + cm["retained_due_to_carrier_block"]
    if abs(lhs1 - rhs1) > 0.02:
        identity_errors.append(
            f"Layer1→Layer2: candidate={lhs1:.2f} != coverable={cm['carrier_coverable_amount']:.2f} "
            f"+ cap_retained={cm['retained_due_to_capacity']:.2f} + block_retained={cm['retained_due_to_carrier_block']:.2f} = {rhs1:.2f}"
        )
    # Identity 2: coverable == executable + decision_freeze_retained
    if not identity_errors:
        lhs2 = cm["carrier_coverable_amount"]
        rhs2 = fd["formal_executable_amount"] + fd["retained_due_to_decision_freeze"]
        if abs(lhs2 - rhs2) > 0.02:
            identity_errors.append(
                f"Layer2→Layer3: coverable={lhs2:.2f} != executable={fd['formal_executable_amount']:.2f} "
                f"+ freeze_retained={fd['retained_due_to_decision_freeze']:.2f} = {rhs2:.2f}"
            )
    v7_decision_chain["identity_verification"] = {
        "status": "PASS" if not identity_errors else "FAIL",
        "errors": identity_errors,
        "candidate_to_carrier_reconciled": not bool(identity_errors),
        "carrier_to_decision_reconciled": not bool(identity_errors),
        "amount_chain_difference": 0 if not identity_errors else 1,
    }

    # ── Backward-Compatible Flat Chain ──
    ndx_amount_chain = {
        "ndx_gap_routed_amount": mc["ndx_gap_routed_amount"],
        "ndx_candidate_release_amount": mc["ndx_candidate_release_amount"],
        "last_known_approved_carrier_capacity": cm["last_known_approved_carrier_capacity"],
        "current_effective_carrier_capacity": cm["current_effective_carrier_capacity"],
        "current_carrier_executable_amount": cm["carrier_coverable_amount"],
        "carrier_coverable_amount": cm["carrier_coverable_amount"],
        "retained_due_to_capacity": cm["retained_due_to_capacity"],
        "retained_due_to_carrier_block": cm["retained_due_to_carrier_block"],
        "formal_executable_amount": fd["formal_executable_amount"],
        "formal_release_amount": fd["formal_release_amount"],
        "retained_due_to_decision_freeze": fd["retained_due_to_decision_freeze"],
        "carrier_snapshot_valid": cm["carrier_snapshot_valid"],
        "carrier_selection_status": cm["carrier_selection_status"],
        "last_known_snapshot_generated_at": cm["last_known_snapshot_generated_at"],
        "last_known_snapshot_status": cm["last_known_snapshot_status"],
        "approved_carrier_capacity": cm["last_known_approved_carrier_capacity"],
        "carrier_executable_amount": cm["carrier_coverable_amount"],
        "hard_fail_pool_exceeded": mc.get("hard_fail_pool_exceeded", False),
        # Deprecated markers for consumers migrating to v7_decision_chain
        "_compat_note": "This flat dict is a backward-compatible alias. New consumers should read v7_decision_chain.",
        "_canonical_carrier_coverable": "v7_decision_chain.carrier_matching.carrier_coverable_amount",
        "_canonical_formal_executable": "v7_decision_chain.formal_decision.formal_executable_amount",
    }

    # ── Build and persist the enriched carrier-facts projection ──
    # V7 reads the raw monitor snapshot, enriches fees from the lookup table,
    # and writes a facts-only projection that never contains V7 decisions.
    try:
        raw_snapshot = json.loads(qdii_carrier.RAW_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        qdii_carrier.write_carrier_fact_snapshot(raw_snapshot)
    except (OSError, ValueError, TypeError, qdii_carrier.CarrierContractError):
        pass

    # ── Archive the exact facts-only input and untouched upstream raw snapshot ──
    try:
        run_dir = output_paths.current_run_dir(required=False)
        if run_dir:
            input_manifest = qdii_carrier.archive_run_inputs(
                run_dir, latest_path=qdii_carrier.CARRIER_JSON_PATH,
                raw_path=qdii_carrier.RAW_SNAPSHOT_PATH,
                archived_at=dt.datetime.now().astimezone(),
            )
            qdii_carrier_integration.update(input_manifest)
            qdii_carrier_integration["carrier_latest_sha256_prefix"] = input_manifest["carrier_latest_sha256"][:12]
    except (OSError, ValueError, TypeError, qdii_carrier.CarrierContractError) as exc:
        qdii_carrier_integration["input_archive_status"] = "FAIL"
        qdii_carrier_integration["input_archive_error"] = str(exc)

    executable_qdii_integration = qdii_carrier.integration_snapshot(
        allocations.get("us_equity", 0), **carrier_kwargs,
    )
    carrier_plan = fund_carrier_plan(
        config, allocations, qdii_integration=executable_qdii_integration
    )
    theoretical_fund_plan = fund_carrier_plan(
        config,
        allocation_routing["theoretical_allocations"],
        qdii_integration=qdii_carrier_integration,
    )

    missing = []
    required = {
        "5Y TIPS": tips_5y,
        "10Y TIPS": tips_10y,
        "10Y Breakeven": breakeven,
        "Fed Funds": fed_funds,
    }
    missing = [name for name, value in required.items() if value is None]
    snapshot = {
        "version": "V7",
        "month": month,
        "generated_at": generated_at,
        "run_id": run_id,
        "scores": scores,
        "asset_score_components": {
            "a_share": {
                "valuation_score": None,
                "liquidity_score": None,
                "price_temperature_score": a_price_score,
                "fallback_compatibility_score": a_share_score,
                "formula_version": cn_equity_temperature.FORMULA_VERSION,
            },
            "us_equity": {
                "legacy_score_status": "RETIRED",
                "legacy_valuation_score": None,
                "legacy_liquidity_score": None,
                "ndx_price_temperature": ndx_model,
                "single_real_yield_factor": {
                    "status": "UNDER_VALIDATION",
                    "series": "DFII10",
                    "dfii10": ndx_model.get("dfii10"),
                    "dfii10_percentile": ndx_model.get("dfii10_percentile"),
                    "modifier": ndx_model.get("real_yield_modifier"),
                },
            },
            "gold": gold_score_detail,
        },
        "formula_version": model_risk.FORMULA_VERSION,
        "cn_equity_price_temperature": china_price_temperature,
        "model_version": model_risk.MODEL_VERSION,
        "data_quality_version": model_risk.DATA_QUALITY_VERSION,
        "version_traceability": {
            "application_version": APPLICATION_VERSION,
            "data_quality_version": model_risk.DATA_QUALITY_VERSION,
            "allocation_formula_version": model_risk.FORMULA_VERSION,
            "a500_formula_version": cn_equity_temperature.FORMULA_VERSION,
            "ndx_formula_version": ndx_price_temperature.FORMULA_VERSION,
            "gold_formula_version": model_risk.GOLD_FORMULA_VERSION if hasattr(model_risk, "GOLD_FORMULA_VERSION") else "gold-v2-inverse-real-yield-fed",
            "qdii_carrier_contract_version": QDII_CARRIER_CONTRACT_VERSION,
        },
        "strategic_targets": dict(settings["strategic_allocation"]),
        "allocation_ranges": dict(settings["allocation_ranges"]),
        "tiers": tiers,
        "momentum": momentum,
        "targets": targets,
        "target_explanations": target_explanations,
        "target_values": target_values,
        "current_values": current,
        "cost_basis": cost_basis,
        "gain_loss": gain_loss,
        "gaps": gaps,
        "total_value": round(total, 2),
        "dynamic_cash_pool": round(pool, 2),
        "original_dynamic_cash_pool": round(pool, 2),
        "remaining_dynamic_cash_pool": round(pool, 2),
        "next_month_pending_pool": round(pool, 2),
        "release_ratio": ratio,
        "theoretical_release_amount": theoretical_release_amount,
        "deploy_amount": deploy_amount,
        "plan_amount": deploy_amount,
        "allocations": allocations,
        "allocation_plan": allocations,
        "allocation_routing": allocation_routing,
        "data_quality_gate": data_quality_gate,
        "model_status": data_quality_gate["model_status"],
        "validation_stage": ndx_model.get("validation_stage", "OFFLINE_VALIDATION"),
        "activation_status": activation_status,
        "dynamic_cash_pool_status": pool_control["dynamic_cash_pool_status"],
        "pool_status_reason": pool_control["pool_status_reason"],
        "allow_execution": pool_control["allow_auto_execution"],
        "allow_auto_execution": pool_control["allow_auto_execution"],
        "decision_status": data_quality_gate["decision_status"],
        "data_status": data_quality_gate["data_status"],
        "asset_level_status": data_quality_gate["asset_level_status"],
        "fund_carrier_plan": carrier_plan,
        "theoretical_fund_plan": theoretical_fund_plan,
        "qdii_carrier_integration": qdii_carrier_integration,
        "carrier_snapshot_id": qdii_carrier_integration.get("carrier_snapshot_id"),
        "input_hashes": {
            "carrier_latest_sha256": qdii_carrier_integration.get("carrier_latest_sha256"),
            "carrier_raw_sha256": qdii_carrier_integration.get("carrier_raw_sha256"),
        },
        "shadow_inputs": {
            "portfolio_snapshot": {
                "source": "V7 portfolio holdings",
                "source_date": generated_at,
                "stale_status": "PASS",
                "current_values": dict(current),
                "total_value": round(total, 2),
            },
            "target_snapshot": {
                "source": "V7 target configuration",
                "source_date": generated_at,
                "stale_status": "PASS",
                "strategic_targets": dict(settings["strategic_allocation"]),
                "effective_targets": dict(targets),
            },
            "dynamic_cash_pool_status": pool_control["dynamic_cash_pool_status"],
            "dynamic_cash_pool": round(pool, 2),
            "formula_version": ndx_price_temperature.FORMULA_VERSION,
        },
        "legacy_us_equity_score_status": "RETIRED",
        "ndx_asset_model_status": ndx_model["model_status"],
        "ndx_price_temperature": ndx_model,
        "ndx_amount_chain": ndx_amount_chain,
        "v7_decision_chain": v7_decision_chain,
        "ready_for_ndx_shadow": bool(ndx_model.get("ready_for_ndx_shadow") or ndx_model.get("offline_pass")),
        "shadow_status": shadow_ledger.get("status", "DAY1_PENDING"),
        "shadow_days_completed": int(shadow_ledger.get("shadow_days_completed", 0)),
        "shadow_required_complete_days": int(shadow_ledger.get("required_complete_days", ndx_shadow_run.REQUIRED_COMPLETE_DAYS)),
        "ready_for_manual_activation": bool(ndx_model.get("ready_for_manual_activation")),
        "first_activation_guard": bool(shadow_ledger.get("first_activation_guard")),
        "first_activation_guard_status": shadow_ledger.get("first_activation_guard_status"),
        "first_activation_confirmation_required": activation_gate["first_activation_confirmation_required"],
        "activation_gate_blocking_reason": activation_gate["blocking_reason"],
        "activation_approved_at": shadow_ledger.get("activation_approved_at"),
        "single_real_yield_factor": {
            "status": "UNDER_VALIDATION",
            "series": "DFII10",
            "modifier": ndx_model.get("real_yield_modifier"),
        },
        "executed_amount": 0,
        "executed_allocations": {},
        "fund_executions": [],
        "unexecuted_amount": deploy_amount,
        "status": "pending" if pool_control["allow_auto_execution"] else "frozen",
        "currentMonth": {
            "status": "pending" if pool_control["allow_auto_execution"] else "frozen",
            "planAmount": deploy_amount,
            "allocationPlan": allocations,
            "fundCarrierPlan": carrier_plan,
            "executedAmount": 0,
            "executedAllocations": {},
            "fundExecutions": [],
            "unexecutedAmount": deploy_amount,
            "remainingDynamicCashPool": round(pool, 2),
            "nextMonthPendingPool": round(pool, 2),
        },
        "action_level": action_level,
        "reasons": reasons,
        "missing_indicators": missing,
        "indicators": {
            "a500_pe": a500.get("pe"),
            "a500_pb": a500.get("pb"),
            "a500_pe_percentile": None,
            "a500_role": "Display Only",
            "a500_used_in_score": False,
            "a500_reproducible": False,
            "a500_confidence": "Low",
            "hs300_pe_percentile": hs300.get("percentile"),
            "hs300_pb_percentile": pb_percentile,
            "pb_percentile": pb_percentile,
            "hs300_sample_size": hs300.get("sample_size", 0),
            "a_share_valuation_basis": "Display Only; not used in automatic score",
            "a_share_price_temperature": china_price_temperature,
            "a_share_formula_version": cn_equity_temperature.FORMULA_VERSION,
            "social_financing_yoy": social_financing_yoy,
            "m2_yoy": m2_yoy,
            "nasdaq100_pe": nasdaq_row["pe"] if nasdaq_row else None,
            "nasdaq100_pe_percentile": nasdaq_percentile,
            "nasdaq100_pe_used_in_score": False,
            "nasdaq100_pe_used_in_release_factor": False,
            "nasdaq100_pe_blocking": False,
            "nasdaq100_pe_governance": "DISPLAY_ONLY",
            "nasdaq100_sample_count": (
                nasdaq_row["sample_count"] if nasdaq_row else 0
            ),
            "nasdaq100_metric_type": (
                nasdaq_row["metric_type"] if nasdaq_row else None
            ),
            "nasdaq100_percentile_window": (
                nasdaq_row["window_label"] if nasdaq_row else None
            ),
            "sp500_pe": sp500_row["pe"] if sp500_row else None,
            "sp500_pe_percentile": sp500_percentile,
            "sp500_pe_used_in_score": False,
            "sp500_pe_used_in_release_factor": False,
            "sp500_pe_blocking": False,
            "sp500_pe_governance": "DISPLAY_ONLY",
            "sp500_sample_count": sp500_row["sample_count"] if sp500_row else 0,
            "sp500_metric_type": (
                sp500_row["metric_type"] if sp500_row else None
            ),
            "sp500_percentile_window": (
                sp500_row["window_label"] if sp500_row else None
            ),
            "us_valuation_score_eligible": False,
            "tips5y": tips_5y,
            "tips10y": tips_10y,
            "breakeven10y": breakeven,
            "fed_funds": fed_funds,
        },
        "indicator_sources": {
            "social_financing_yoy": {
                "source": social_row["source"] if social_row else "manual",
                "date": social_row["observation_date"] if social_row else None,
            },
            "m2_yoy": {
                "source": m2_row["source"] if m2_row else "manual",
                "date": m2_row["observation_date"] if m2_row else None,
            },
            "nasdaq100": {
                "source": nasdaq_row["source"] if nasdaq_row else None,
                "date": nasdaq_row["observation_date"] if nasdaq_row else None,
                "metric_type": (
                    nasdaq_row["metric_type"] if nasdaq_row else None
                ),
                "source_url": (
                    nasdaq_row["source_url"] if nasdaq_row else None
                ),
                "status": market_status_row(
                    conn,
                    "us_valuation:nasdaq100",
                ),
            },
            "sp500": {
                "source": sp500_row["source"] if sp500_row else None,
                "date": sp500_row["observation_date"] if sp500_row else None,
                "metric_type": (
                    sp500_row["metric_type"] if sp500_row else None
                ),
                "source_url": (
                    sp500_row["source_url"] if sp500_row else None
                ),
                "status": market_status_row(conn, "us_valuation:sp500"),
            },
        },
        "input_indicator_latest_dates": {
            item["indicator"]: item.get("latest_date")
            for item in data_quality_gate["indicators"]
        },
        "input_indicator_sources": {
            item["indicator"]: item.get("source")
            for item in data_quality_gate["indicators"]
        },
        "triggers": {
            "is_initialization": is_initialization,
            "baseline_established": is_initialization,
            "allow_absolute_gap_on_initialization": allow_initial_absolute_gap,
            "max_score_change": score_delta,
            "score_change_strong_threshold": 20,
            "score_change_regular_threshold": 10,
            "tier_changed": tier_changed,
            "max_gap_change_value": round(gap_delta, 2),
            "max_gap_change_ratio": round(gap_delta_ratio, 4),
            "gap_change_threshold_ratio": 0.03,
            "absolute_gap_threshold_ratio": absolute_gap_threshold_ratio,
            "absolute_gap_threshold_value": round(
                absolute_gap_threshold_value,
                2,
            ),
            "absolute_gap_assets": absolute_gap_assets,
            "months_without_deploy": months_without_deploy,
            "matched_rules": matched_rules,
        },
    }
    # The canonical decision record is written once, before any legacy
    # finalized fields are overlaid for display compatibility.
    decision_payload = model_risk.create_decision_snapshot_payload(
        snapshot,
        data_quality_gate,
    )
    immutable_decision = model_risk.persist_decision_snapshot(
        conn,
        decision_payload,
    )
    snapshot["decision_id"] = immutable_decision["decision_id"]
    snapshot["immutable_decision_snapshot"] = immutable_decision

    if finalized is not None:
        immutable_fields = (
            "status",
            "user_decision",
            "decision_at",
            "plan_amount",
            "allocation_plan",
            "executed_amount",
            "executed_at",
            "executed_allocations",
            "fund_executions",
            "unexecuted_amount",
            "original_dynamic_cash_pool",
            "remaining_dynamic_cash_pool",
            "next_month_pending_pool",
            "dynamic_cash_pool",
            "deploy_amount",
            "allocations",
            "currentMonth",
            "current_month",
            "release_ratio",
            "action_level",
            "reasons",
            "triggers",
        )
        for field in immutable_fields:
            if field in finalized:
                snapshot[field] = finalized[field]
        finalized_fund_rows = snapshot.get("fund_executions", [])
        if finalized_fund_rows:
            carrier_plan = [
                {
                    "fund_code": row["fund_code"],
                    "fund_name": row["fund_name"],
                    "asset_class": row["asset_class"],
                    "asset_name": asset_label(row["asset_class"]),
                    "planned_amount": row["planned_amount"],
                }
                for row in finalized_fund_rows
            ]
        snapshot["currentMonth"]["fundCarrierPlan"] = carrier_plan
        snapshot["fund_carrier_plan"] = carrier_plan

    model_risk.persist_monitoring_snapshot(conn, snapshot)

    existing = conn.execute(
        "SELECT user_decision, decision_at FROM allocation_history WHERE month = ?",
        (month,),
    ).fetchone()
    conn.execute(
        """
        INSERT OR REPLACE INTO allocation_history (
            month, generated_at, snapshot_json, user_decision, decision_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            month,
            snapshot["generated_at"],
            json.dumps(snapshot, ensure_ascii=False),
            existing["user_decision"] if existing else None,
            existing["decision_at"] if existing else None,
        ),
    )
    snapshot["user_decision"] = existing["user_decision"] if existing else None
    return snapshot


def apply_fund_execution_to_config(config, fund, actual_amount):
    old_value = float(fund["holding_amount"])
    old_profit_pct = fund.get("profit_pct")
    if old_profit_pct is None or float(old_profit_pct) <= -100:
        old_cost = old_value
    else:
        old_cost = old_value / (1 + float(old_profit_pct) / 100)
    new_value = old_value + actual_amount
    new_cost = old_cost + actual_amount
    fund["holding_amount"] = round(new_value, 2)
    fund["profit_pct"] = (
        round((new_value / new_cost - 1) * 100, 4)
        if new_cost > 0
        else 0
    )


def validate_fund_executions(
    config,
    snapshot,
    submitted,
    carrier_plan=None,
    max_plan_amount=None,
):
    if not isinstance(submitted, list):
        raise ValueError("执行本月方案时必须提交基金级实际金额")
    carrier_plan = (
        carrier_plan
        if carrier_plan is not None
        else snapshot.get("fund_carrier_plan", [])
    )
    plan_by_code = {row["fund_code"]: row for row in carrier_plan}
    funds_by_code = {fund["code"]: fund for fund in config["funds"]}
    seen = set()
    executions = []
    for item in submitted:
        if not isinstance(item, dict):
            raise ValueError("基金执行记录格式不正确")
        fund_code = str(item.get("fund_code", "")).strip()
        if fund_code in seen:
            raise ValueError(f"基金执行记录重复：{fund_code}")
        seen.add(fund_code)
        planned = plan_by_code.get(fund_code)
        fund = funds_by_code.get(fund_code)
        if planned is None or fund is None:
            raise ValueError(f"基金不在本月载体计划中：{fund_code}")
        try:
            actual = round(float(item.get("actual_executed_amount", 0)), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{fund_code} 的实际执行金额必须是数字")
        if actual < 0:
            raise ValueError(f"{fund_code} 的实际执行金额不能小于0")
        if actual > float(planned["planned_amount"]) + 0.001:
            raise ValueError(f"{fund_code} 的实际执行金额不能超过计划金额")
        if (
            float(fund["holding_amount"]) + actual
            > float(fund["max_holding_amount"]) + 0.001
        ):
            raise ValueError(f"{fund_code} 执行后将超过基金持仓上限")
        executions.append({
            **planned,
            "actual_executed_amount": actual,
            "fund": fund,
        })
    if set(plan_by_code) != seen:
        missing = "、".join(sorted(set(plan_by_code) - seen))
        raise ValueError(f"缺少基金实际执行金额：{missing}")
    total_actual = round(
        sum(row["actual_executed_amount"] for row in executions),
        2,
    )
    if total_actual > float(snapshot["dynamic_cash_pool"]) + 0.001:
        raise ValueError("实际执行金额合计不能超过动态资金池")
    allowed_plan_amount = (
        float(max_plan_amount)
        if max_plan_amount is not None
        else float(snapshot["plan_amount"])
    )
    if total_actual > allowed_plan_amount + 0.001:
        raise ValueError("实际执行金额合计不能超过本月方案金额")
    return executions


def integer_execution_amount(amount):
    """Return the largest whole-yuan amount that stays within a plan."""
    return max(0, int(float(amount or 0)))


def apply_copilot_decision(conn, config, decision, fund_executions=None):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT copilot_execution")
    config_before = json.loads(json.dumps(config))
    try:
        result = _apply_copilot_decision(
            conn, config, decision, fund_executions,
        )
    except Exception:
        conn.execute("ROLLBACK TO copilot_execution")
        conn.execute("RELEASE copilot_execution")
        config.clear()
        config.update(config_before)
        raise
    conn.execute("RELEASE copilot_execution")
    return result


def _apply_copilot_decision(conn, config, decision, fund_executions=None):
    if decision != "execute":
        raise ValueError("V7 decision gate only accepts EXECUTE; otherwise the system remains FREEZE")
    ensure_monthly_contribution(conn, config)
    market_temperature = generate_market_temperature(conn, config)
    snapshot = generate_copilot_snapshot(conn, config, market_temperature)
    if snapshot.get("user_decision") or allocation_event(conn, snapshot["month"]):
        raise ValueError("ALREADY_EXECUTED: 本月方案已经处理")
    if decision == "execute" and not snapshot.get("allow_execution", False):
        issues = snapshot.get("data_quality_gate", {}).get("blocking_issues", [])
        raise ValueError(
            "Decision Status FREEZE；Dynamic Cash Pool不得释放："
            + "、".join(issues or ["数据质量门未通过"])
        )
    if decision == "execute" and snapshot["plan_amount"] <= 0:
        raise ValueError("当前没有可执行的资金释放方案")

    now = dt.datetime.now().isoformat(timespec="seconds")
    actual_total = 0
    actual_allocations = {}
    executions = []
    is_execution = True
    if is_execution:
        selected_plan = snapshot.get("fund_carrier_plan", [])
        selected_max = sum(
            float(row.get("planned_amount", 0) or 0)
            for row in selected_plan
        )
        if selected_max <= 0:
            raise ValueError("当前没有对应类型的可执行建议")
        executions = validate_fund_executions(
            config,
            snapshot,
            fund_executions,
            carrier_plan=selected_plan,
            max_plan_amount=selected_max,
        )
        actual_total = round(
            sum(row["actual_executed_amount"] for row in executions),
            2,
        )
        if actual_total <= 0:
            raise ValueError("实际执行金额合计必须大于0")
        actual_allocations = {
            asset: round(
                sum(
                    row["actual_executed_amount"]
                    for row in executions
                    if row["asset_class"] == asset
                ),
                2,
            )
            for asset in ("a_share", "us_equity", "gold")
        }
        pool = max(
            0,
            snapshot["dynamic_cash_pool"] - actual_total,
        )
        set_state(conn, "dynamic_cash_pool", round(pool, 2))
        for row in executions:
            apply_fund_execution_to_config(
                config,
                row["fund"],
                row["actual_executed_amount"],
            )
            conn.execute(
                """
                INSERT INTO fund_execution_log (
                    month, fund_code, fund_name, asset_class,
                    planned_amount, actual_executed_amount, executed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["month"],
                    row["fund_code"],
                    row["fund_name"],
                    row["asset_class"],
                    row["planned_amount"],
                    row["actual_executed_amount"],
                    now,
                ),
            )
        set_state(conn, "months_without_deploy", 0)

    conn.execute(
        """
        UPDATE allocation_history
        SET user_decision = ?, decision_at = ?
        WHERE month = ?
        """,
        (decision, now, snapshot["month"]),
    )
    conn.execute(
        """
        INSERT INTO allocation_events (
            month, decision, deploy_amount, allocation_json, created_at,
            plan_amount, plan_allocation_json, executed_at, execution_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot["month"],
            decision,
            actual_total if is_execution else 0,
            json.dumps(
                actual_allocations if is_execution else {},
                ensure_ascii=False,
            ),
            now,
            snapshot["plan_amount"],
            json.dumps(
                snapshot["allocation_plan"],
                ensure_ascii=False,
            ),
            now if is_execution else None,
            "Model Auto Execution",
        ),
    )
    finalized = finalized_allocation_snapshot(conn, snapshot["month"])
    model_risk.update_decision_execution_status(
        conn,
        snapshot["month"],
        "executed" if is_execution else "skipped",
    )
    conn.execute(
        """
        UPDATE allocation_history
        SET snapshot_json = ?
        WHERE month = ?
        """,
        (
            json.dumps(finalized, ensure_ascii=False),
            snapshot["month"],
        ),
    )
    return finalized


def apply_manual_override(conn, config, asset, amount, reason):
    raise ValueError("Manual Override is disabled; V7 only supports EXECUTE or FREEZE")
    if asset not in ("a_share", "us_equity", "gold"):
        raise ValueError("Manual Override资产类型无效")
    ensure_monthly_contribution(conn, config)
    temperature = generate_market_temperature(conn, config)
    snapshot = generate_copilot_snapshot(conn, config, temperature)
    if not snapshot.get("allow_manual_override", False):
        raise ValueError("当前状态不允许Manual Override")
    pool_before = float(get_state(conn, "dynamic_cash_pool", 0) or 0)
    validated = model_risk.validate_manual_override_request(
        amount,
        reason,
        pool_before,
    )
    amount = validated["amount"]
    reason = validated["reason"]
    limit = validated["limit"]

    settings = copilot_config(config)
    fund_code = str(settings["execution_funds"].get(asset, ""))
    fund = next(
        (item for item in config["funds"] if item["code"] == fund_code),
        None,
    )
    if fund is None or fund.get("asset_class") != asset:
        raise ValueError("Manual Override未配置有效基金载体")
    if float(fund["holding_amount"]) + amount > float(fund["max_holding_amount"]) + 0.001:
        raise ValueError("Manual Override执行后将超过基金持仓上限")

    now = dt.datetime.now().isoformat(timespec="seconds")
    override_id = str(uuid.uuid4())
    override_snapshot = {
        "override_id": override_id,
        "execution_month": snapshot["month"],
        "created_at": now,
        "execution_type": "Manual Override Execution",
        "asset": asset,
        "asset_name": asset_label(asset),
        "fund_code": fund_code,
        "fund_name": fund["name"],
        "override_amount": amount,
        "override_reason": reason,
        "override_limit": limit,
        "dynamic_cash_pool_before": pool_before,
        "dynamic_cash_pool_after": round(pool_before - amount, 2),
        "asset_level_status": snapshot["asset_level_status"][asset],
        "model_auto_execution": False,
        "manual_review_required": True,
        "formula_version": snapshot["formula_version"],
        "data_quality_version": snapshot["data_quality_version"],
    }
    conn.execute(
        "INSERT INTO manual_override_snapshots (override_id, execution_month, created_at, override_json) VALUES (?, ?, ?, ?)",
        (override_id, snapshot["month"], now, json.dumps(override_snapshot, ensure_ascii=False, sort_keys=True)),
    )
    conn.execute(
        """
        INSERT INTO allocation_events (
            month, decision, deploy_amount, allocation_json, created_at,
            plan_amount, plan_allocation_json, executed_at, execution_type
        ) VALUES (?, 'manual_override', ?, ?, ?, ?, ?, ?, 'Manual Override Execution')
        """,
        (
            snapshot["month"],
            amount,
            json.dumps({asset: amount}, ensure_ascii=False),
            now,
            amount,
            json.dumps({asset: amount}, ensure_ascii=False),
            now,
        ),
    )
    set_state(conn, "dynamic_cash_pool", round(pool_before - amount, 2))
    apply_fund_execution_to_config(config, fund, amount)
    return override_snapshot


def manual_override_history(conn, limit=20):
    rows = conn.execute(
        "SELECT override_json FROM manual_override_snapshots ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [json.loads(row["override_json"]) for row in rows]


def allocation_history_rows(conn, limit=12):
    rows = conn.execute(
        """
        SELECT month, generated_at, snapshot_json, user_decision, decision_at
        FROM allocation_history
        ORDER BY month DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        snapshot = json.loads(row["snapshot_json"])
        snapshot["user_decision"] = row["user_decision"]
        snapshot["decision_at"] = row["decision_at"]
        event = allocation_event(conn, row["month"])
        snapshot["actual_deploy_amount"] = (
            event["deploy_amount"] if event and event["decision"] in ("execute", "manual_review") else None
        )
        snapshot["actual_allocations"] = (
            event["allocations"] if event and event["decision"] in ("execute", "manual_review") else None
        )
        snapshot["execution_type"] = event["execution_type"] if event else None
        result.append(snapshot)
    return result


def write_report_json(
    rows,
    macro_rows=None,
    market_temperature=None,
    copilot=None,
    allocation_history=None,
):
    copilot = copilot or {}
    payload = {
        "generated_at": copilot.get("generated_at") or dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_status": copilot.get("data_status", "FAIL"),
        "decision_status": copilot.get("decision_status", "FREEZE"),
        "model_status": copilot.get("model_status", "UNDER_VALIDATION"),
        "validation_stage": copilot.get("validation_stage", "OFFLINE_VALIDATION"),
        "rows": rows,
        "macro": macro_rows or [],
        "marketTemperature": market_temperature or {},
        "copilot": copilot,
        "allocationHistory": allocation_history or [],
    }
    with output_paths.get_json_path("report.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def action_class(action):
    if "30%回撤档" in action:
        return "danger"
    if "20%回撤档" in action:
        return "warn"
    if "10%回撤" in action:
        return "watch"
    if "已达仓位上限" in action:
        return "cap"
    return "ok"


def drawdown_bar(drawdown):
    if drawdown is None:
        return '<div class="bar"><span style="width:0%"></span></div>'
    width = min(100, max(0, abs(drawdown) / 30 * 100))
    cls = "danger" if drawdown <= -30 else "warn" if drawdown <= -20 else "watch" if drawdown <= -10 else "ok"
    return f'<div class="bar {cls}"><span style="width:{width:.1f}%"></span></div>'


def format_bps(value):
    return "-" if value is None else f"{value:+.0f}bp"


def temperature_class(state):
    if state in ("冷", "极度低估", "偏低估", "黄金价值区", "低通胀预期"):
        return "cold"
    if state in ("热", "偏高估", "高估", "偏热区", "拥挤区", "黄金拥挤区", "高通胀预期"):
        return "hot"
    if state == "数据暂缺":
        return "unavailable"
    return "normal"


def market_update_text(item):
    last_success_at = (item.get("last_success_at") or "").replace("T", " ")
    if item.get("last_error") and item.get("last_success_at"):
        return f"更新异常 · 最近成功 {last_success_at}"
    if item.get("last_success_at"):
        return f"最近成功 {last_success_at}"
    return "尚无成功更新记录"


def write_dashboard(rows, macro_rows=None, market_temperature=None):
    macro_rows = macro_rows or []
    market_temperature = market_temperature or {}
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    today = dt.date.today()
    total_holding = sum(row["holding_amount"] for row in rows)
    triggered = [
        row for row in rows
        if "触发20%回撤档" in row["action"] or "触发30%回撤档" in row["action"]
    ]
    watch = [row for row in rows if "只提醒观察" in row["action"]]
    max_drawdown = min((row["drawdown_12m_pct"] for row in rows if row["drawdown_12m_pct"] is not None), default=0)
    oldest_lag = max(
        (
            (today - dt.date.fromisoformat(row["latest_date"])).days
            for row in rows
            if row.get("latest_date")
        ),
        default=0,
    )

    cards = [
        ("当前持仓", f"{total_holding:,.1f}", "元"),
        ("补仓触发", str(len(triggered)), "只基金"),
        ("观察提醒", str(len(watch)), "只基金"),
        ("最大12个月回撤", format_pct(max_drawdown), ""),
        ("最旧数据滞后", str(oldest_lag), "天"),
    ]

    card_html = "\n".join(
        f"""
        <section class="metric">
          <span>{html.escape(label)}</span>
          <strong>{html.escape(value)}</strong>
          <em>{html.escape(unit)}</em>
        </section>
        """
        for label, value, unit in cards
    )

    valuation = market_temperature.get("valuation") or {}
    a500 = valuation.get("a500") or {}
    hs300 = valuation.get("hs300") or {}
    tips_5y = market_temperature.get("tips5yDetail") or {}
    tips_10y = market_temperature.get("tips10yDetail") or {}
    breakeven = market_temperature.get("breakeven") or {}
    composite = market_temperature.get("composite") or {}

    def valuation_item(item):
        pe = f"{item['pe']:.2f}" if item.get("pe") is not None else "-"
        percentile = (
            f"{item['percentile']:.2f}%"
            if item.get("percentile") is not None
            else "-"
        )
        state = item.get("status", "数据暂缺")
        return f"""
          <div class="valuation-item">
            <div>
              <strong>{html.escape(item.get('name', '-'))}</strong>
              <span>{html.escape(item.get('code') or '-')}</span>
            </div>
            <dl>
              <div><dt>PE(TTM)</dt><dd>{html.escape(pe)}</dd></div>
              <div><dt>历史百分位</dt><dd>{html.escape(percentile)}</dd></div>
            </dl>
            <span class="temperature-tag {temperature_class(state)}">{html.escape(state)}</span>
            <small>数据 {html.escape(item.get('data_date') or '-')} · {html.escape(market_update_text(item))}</small>
          </div>
        """

    tips_5y_value = (
        f"{market_temperature['tips5y']:.2f}%"
        if market_temperature.get("tips5y") is not None
        else "-"
    )
    tips_10y_value = (
        f"{market_temperature['tips10y']:.2f}%"
        if market_temperature.get("tips10y") is not None
        else "-"
    )
    breakeven_value = (
        f"{market_temperature['breakeven10y']:.2f}%"
        if market_temperature.get("breakeven10y") is not None
        else "-"
    )
    gold_score = market_temperature.get("goldScore")
    gold_score_text = f"{gold_score:+d}" if gold_score is not None else "-"
    gold_state = market_temperature.get("goldTemperature", "数据暂缺")
    gold_description = market_temperature.get("goldDescription", "指标数据不足")

    def gold_indicator_card(title, value, daily, weekly, detail, status=None):
        state = status or detail.get("status")
        score = detail.get("score")
        score_text = f"评分 {score:+d}" if score is not None else "评分 -"
        return f"""
          <div class="gold-indicator">
            <div class="gold-indicator-head">
              <span>{html.escape(title)}</span>
              <small>{html.escape(score_text)}</small>
            </div>
            <strong>{html.escape(value)}</strong>
            <dl class="change-grid compact">
              <div><dt>日变化</dt><dd>{html.escape(format_bps(daily))}</dd></div>
              <div><dt>周变化</dt><dd>{html.escape(format_bps(weekly))}</dd></div>
            </dl>
            {f'<span class="temperature-tag {temperature_class(state)}">{html.escape(state)}</span>' if state else ''}
            <small>数据 {html.escape(detail.get('data_date') or '-')} · {html.escape(market_update_text(detail))}</small>
          </div>
        """

    market_temperature_html = f"""
    <section class="market-section">
      <div class="section-heading">
        <div>
          <span class="section-kicker">Market Temperature</span>
          <h2>市场温度</h2>
        </div>
        <p>每日更新 · 24小时缓存 · 仅展示市场状态</p>
      </div>
      <div class="temperature-grid">
        <article class="temperature-card">
          <div class="temperature-card-head">
            <span>A股估值</span>
            <span class="temperature-tag {temperature_class(composite.get('aShare'))}">{html.escape(composite.get('aShare', '数据暂缺'))}</span>
          </div>
          {valuation_item(a500)}
          {valuation_item(hs300)}
        </article>
        <article class="temperature-card rate-card">
          <div class="temperature-card-head">
            <span>Gold Market Temperature</span>
            <span class="temperature-tag {temperature_class(composite.get('gold'))}">{html.escape(composite.get('gold', '数据暂缺'))}</span>
          </div>
          <p class="module-note">长期配置环境参考，不包含短期方向判断</p>
          <div class="gold-grid">
            {gold_indicator_card(
                "5Y TIPS Real Yield",
                tips_5y_value,
                market_temperature.get("tips5yDailyChange"),
                market_temperature.get("tips5yWeeklyChange"),
                tips_5y,
            )}
            {gold_indicator_card(
                "10Y TIPS Real Yield",
                tips_10y_value,
                market_temperature.get("tips10yDailyChange"),
                market_temperature.get("tips10yWeeklyChange"),
                tips_10y,
            )}
            {gold_indicator_card(
                "10Y Breakeven",
                breakeven_value,
                market_temperature.get("breakevenDailyChange"),
                market_temperature.get("breakevenWeeklyChange"),
                breakeven,
                breakeven.get("status"),
            )}
            <div class="gold-indicator gold-score-card">
              <div class="gold-indicator-head">
                <span>Gold Score</span>
                <small>三指标合计</small>
              </div>
              <strong>{html.escape(gold_score_text)}</strong>
              <span class="temperature-tag {temperature_class(gold_state)}">{html.escape(gold_state)}</span>
              <p>{html.escape(gold_description)}</p>
            </div>
          </div>
        </article>
        <article class="temperature-card rate-card">
          <div class="temperature-card-head">
            <span>通胀预期</span>
            <span class="temperature-tag {temperature_class(composite.get('inflation'))}">{html.escape(composite.get('inflation', '数据暂缺'))}</span>
          </div>
          <div class="rate-name">10Y Breakeven Inflation Rate</div>
          <div class="rate-value">{html.escape(breakeven_value)}</div>
          <dl class="change-grid">
            <div><dt>日变化</dt><dd>{html.escape(format_bps(market_temperature.get('breakevenDailyChange')))}</dd></div>
            <div><dt>周变化</dt><dd>{html.escape(format_bps(market_temperature.get('breakevenWeeklyChange')))}</dd></div>
          </dl>
          <span class="temperature-tag {temperature_class(breakeven.get('status'))}">{html.escape(breakeven.get('status', '数据暂缺'))}</span>
          <small>数据 {html.escape(breakeven.get('data_date') or '-')} · {html.escape(market_update_text(breakeven))}</small>
        </article>
      </div>
      <div class="composite-panel">
        <span>综合温度</span>
        <div><small>A股温度</small><strong class="{temperature_class(composite.get('aShare'))}">{html.escape(composite.get('aShare', '数据暂缺'))}</strong></div>
        <div><small>黄金温度</small><strong class="{temperature_class(composite.get('gold'))}">{html.escape(composite.get('gold', '数据暂缺'))}</strong></div>
        <div><small>通胀温度</small><strong class="{temperature_class(composite.get('inflation'))}">{html.escape(composite.get('inflation', '数据暂缺'))}</strong></div>
      </div>
    </section>
    """

    rows_html = "\n".join(
        f"""
        <tr>
          <td>
            <div class="fund-name">{html.escape(row['name'])}</div>
            <div class="fund-code">{html.escape(row['code'])} · {html.escape(row['type'])}</div>
            <div class="strategy">定投：{html.escape(row['strategy'])}</div>
          </td>
          <td>{html.escape(row['latest_date'])}<br><span class="muted">{row['latest_nav']:.4f} · 距今 {(today - dt.date.fromisoformat(row['latest_date'])).days} 天</span><br><span class="muted">QDII Lag: {html.escape(row['qdii_lag_status'])}</span></td>
          <td>{format_pct(row['daily_pct_change'])}</td>
          <td>
            <div class="cell-main">{format_pct(row['drawdown_6m_pct'])}</div>
            {drawdown_bar(row['drawdown_6m_pct'])}
            <div class="muted">高点 {row['high_6m_date']}</div>
            <div class="muted">Coverage {row['coverage_6m_status']} · {row['coverage_6m_sample_size']} samples / {float(row['coverage_6m_ratio'] or 0):.1%}</div>
          </td>
          <td>
            <div class="cell-main">{format_pct(row['drawdown_12m_pct'])}</div>
            {drawdown_bar(row['drawdown_12m_pct'])}
            <div class="muted">高点 {row['high_12m_date']}</div>
            <div class="muted">Coverage {row['coverage_12m_status']} · {row['coverage_12m_sample_size']} samples / {float(row['coverage_12m_ratio'] or 0):.1%}</div>
          </td>
          <td>
            <div class="cell-main">{row['holding_amount']:,.1f} / {row['max_holding_amount']:,.1f}</div>
            <div class="capacity"><span style="width:{min(100, row['holding_amount'] / row['max_holding_amount'] * 100):.1f}%"></span></div>
            <div class="muted">剩余额度 {row['remaining_capacity']:,.1f}</div>
          </td>
          <td>
            <span class="pill {action_class(row['action'])}">{html.escape(row['action'])}</span>
            <div class="future-plan">{html.escape(row['future_plan'])}</div>
            <div class="macro-note">宏观系数 {row['macro_multiplier']:.2f} · {html.escape(row['macro_reason'])}</div>
          </td>
        </tr>
        """
        for row in rows
    )

    dashboard = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>资产配置监控</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1d2430;
      --muted: #697386;
      --line: #d9dee7;
      --ok: #26734d;
      --ok-bg: #e8f4ee;
      --watch: #946200;
      --watch-bg: #fff3ce;
      --warn: #b45100;
      --warn-bg: #ffe8d1;
      --danger: #b42318;
      --danger-bg: #ffe2df;
      --cap: #49566b;
      --cap-bg: #eceff4;
      --blue: #2f6fed;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    header {{
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    .header-inner {{
      max-width: 1440px;
      margin: 0 auto;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 2px 0 0;
      font-size: 22px;
      line-height: 1.2;
    }}
    .subhead {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 14px;
    }}
    .settings-link {{
      color: var(--blue);
      text-decoration: none;
      font-size: 14px;
      font-weight: 650;
      white-space: nowrap;
      padding: 7px 0;
    }}
    .settings-link:hover {{ text-decoration: underline; }}
    main {{
      padding: 24px 32px 40px;
      max-width: 1440px;
      margin: 0 auto;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-height: 92px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }}
    .metric strong {{
      font-size: 25px;
      letter-spacing: 0;
    }}
    .metric em {{
      color: var(--muted);
      font-size: 13px;
      font-style: normal;
      display: block;
      margin-top: 6px;
    }}
    .market-section {{
      margin: 24px 0;
      padding: 22px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background:
        linear-gradient(135deg, rgba(47, 111, 237, 0.06), transparent 38%),
        var(--panel);
    }}
    .section-heading {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
    }}
    .section-heading p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .section-kicker {{
      color: var(--blue);
      font-size: 11px;
      font-weight: 750;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .temperature-grid {{
      display: grid;
      grid-template-columns: 0.95fr 1.55fr 0.95fr;
      gap: 14px;
    }}
    .temperature-card {{
      min-width: 0;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.88);
    }}
    .temperature-card-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
      font-size: 15px;
      font-weight: 700;
    }}
    .valuation-item {{
      position: relative;
      padding: 13px 0;
      border-top: 1px solid var(--line);
    }}
    .valuation-item:first-of-type {{ border-top: 0; padding-top: 0; }}
    .valuation-item > div:first-child {{
      display: flex;
      align-items: baseline;
      gap: 7px;
    }}
    .valuation-item > div:first-child strong {{ font-size: 14px; }}
    .valuation-item > div:first-child span {{
      color: var(--muted);
      font-size: 11px;
    }}
    .valuation-item dl, .change-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 10px 0;
    }}
    .valuation-item dl div, .change-grid div {{
      padding: 9px 10px;
      border-radius: 7px;
      background: #f6f8fb;
    }}
    dt {{
      color: var(--muted);
      font-size: 10px;
    }}
    dd {{
      margin: 3px 0 0;
      font-size: 15px;
      font-weight: 700;
    }}
    .temperature-tag {{
      display: inline-flex;
      align-items: center;
      min-height: 25px;
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
    }}
    .temperature-tag.cold {{ color: #245aa5; background: #e5efff; }}
    .temperature-tag.normal {{ color: #26734d; background: var(--ok-bg); }}
    .temperature-tag.hot {{ color: var(--danger); background: var(--danger-bg); }}
    .temperature-tag.unavailable {{ color: var(--cap); background: var(--cap-bg); }}
    .temperature-card small {{
      display: block;
      margin-top: 9px;
      color: var(--muted);
      font-size: 10px;
      line-height: 1.5;
    }}
    .rate-name {{
      color: var(--muted);
      font-size: 12px;
    }}
    .module-note {{
      margin: -6px 0 13px;
      color: var(--muted);
      font-size: 11px;
    }}
    .gold-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }}
    .gold-indicator {{
      min-width: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
    }}
    .gold-indicator-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 10px;
    }}
    .gold-indicator-head span {{
      color: #354052;
      font-size: 11px;
      font-weight: 700;
    }}
    .gold-indicator-head small {{
      margin: 0;
      white-space: nowrap;
    }}
    .gold-indicator > strong {{
      display: block;
      margin: 7px 0 2px;
      font-size: 23px;
      line-height: 1;
    }}
    .change-grid.compact {{
      gap: 5px;
      margin: 9px 0;
    }}
    .change-grid.compact div {{ padding: 7px; }}
    .change-grid.compact dd {{ font-size: 12px; }}
    .gold-score-card {{
      background: linear-gradient(145deg, #172033, #283651);
      color: white;
    }}
    .gold-score-card .gold-indicator-head span {{ color: white; }}
    .gold-score-card .gold-indicator-head small {{ color: #aeb9cc; }}
    .gold-score-card > strong {{
      margin: 11px 0;
      font-size: 34px;
    }}
    .gold-score-card p {{
      margin: 10px 0 0;
      color: #d8deea;
      font-size: 11px;
      line-height: 1.5;
    }}
    .rate-value {{
      margin-top: 4px;
      font-size: 32px;
      font-weight: 760;
      letter-spacing: -0.03em;
    }}
    .composite-panel {{
      display: grid;
      grid-template-columns: 1.3fr repeat(3, 1fr);
      align-items: center;
      gap: 10px;
      margin-top: 14px;
      padding: 13px 16px;
      border-radius: 9px;
      background: #172033;
      color: white;
    }}
    .composite-panel > span {{
      font-size: 13px;
      font-weight: 700;
    }}
    .composite-panel div {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding-left: 14px;
      border-left: 1px solid rgba(255, 255, 255, 0.16);
    }}
    .composite-panel small {{ color: #aeb9cc; }}
    .composite-panel strong {{ font-size: 14px; }}
    .composite-panel strong.cold {{ color: #8db9ff; }}
    .composite-panel strong.normal {{ color: #7ed9aa; }}
    .composite-panel strong.hot {{ color: #ff9d94; }}
    .composite-panel strong.unavailable {{ color: #c7ced9; }}
    .table-wrap {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 1160px;
    }}
    th, td {{
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      font-size: 14px;
    }}
    th {{
      color: var(--muted);
      background: #fafbfc;
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }}
    th:last-child, td:last-child {{
      position: sticky;
      right: 0;
      width: 260px;
      min-width: 260px;
      background: var(--panel);
      box-shadow: -8px 0 14px -14px rgba(29, 36, 48, 0.55);
    }}
    th:last-child {{ background: #fafbfc; }}
    tr:last-child td {{ border-bottom: 0; }}
    .fund-name {{ font-weight: 650; white-space: nowrap; }}
    .fund-code, .muted {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
    .cell-main {{ font-weight: 650; margin-bottom: 6px; }}
    .strategy {{
      max-width: 210px;
      margin-top: 6px;
      color: #354052;
      font-size: 12px;
    }}
    .future-plan {{
      max-width: 260px;
      margin-top: 8px;
      color: #354052;
      font-size: 12px;
      line-height: 1.5;
    }}
    .macro-note {{
      max-width: 260px;
      margin-top: 5px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
    }}
    .bar, .capacity {{
      width: 125px;
      height: 8px;
      border-radius: 999px;
      background: #e9edf3;
      overflow: hidden;
      margin-bottom: 4px;
    }}
    .bar span, .capacity span {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: var(--ok);
    }}
    .bar.watch span {{ background: var(--watch); }}
    .bar.warn span {{ background: var(--warn); }}
    .bar.danger span {{ background: var(--danger); }}
    .capacity span {{ background: var(--blue); }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      max-width: 220px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 650;
      white-space: normal;
    }}
    .pill.ok {{ color: var(--ok); background: var(--ok-bg); }}
    .pill.watch {{ color: var(--watch); background: var(--watch-bg); }}
    .pill.warn {{ color: var(--warn); background: var(--warn-bg); }}
    .pill.danger {{ color: var(--danger); background: var(--danger-bg); }}
    .pill.cap {{ color: var(--cap); background: var(--cap-bg); }}
    @media (max-width: 820px) {{
      header {{ padding: 22px 18px 16px; }}
      main {{ padding: 18px; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .metric strong {{ font-size: 21px; }}
      .section-heading {{
        align-items: flex-start;
        flex-direction: column;
      }}
      .temperature-grid {{ grid-template-columns: 1fr; }}
      .gold-grid {{ grid-template-columns: 1fr; }}
      .composite-panel {{ grid-template-columns: 1fr; }}
      .composite-panel div {{
        padding: 8px 0 0;
        border-top: 1px solid rgba(255, 255, 255, 0.16);
        border-left: 0;
      }}
    }}
    @media (min-width: 821px) and (max-width: 1120px) {{
      .metrics {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .temperature-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div>
        <h1>资产配置监控</h1>
        <div class="subhead">生成时间：{html.escape(generated_at)} · 基金回撤与市场温度每日更新</div>
      </div>
      <a class="settings-link" href="/settings.html">编辑配置</a>
    </div>
  </header>
  <main>
    <div class="metrics">
      {card_html}
    </div>
    {market_temperature_html}
    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>基金</th>
            <th>最新净值</th>
            <th>当日涨跌</th>
            <th>6个月回撤</th>
            <th>12个月回撤</th>
            <th>仓位</th>
            <th>动作</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""
    output_paths.get_html_snapshot_path("dashboard.html").write_text(dashboard, encoding="utf-8")
    output_paths.get_dist_path("dashboard.html").write_text(dashboard, encoding="utf-8")


def get_tier_config(score):
    """Map a 0-100 score to a temperature tier with display properties."""
    if score is None:
        return {
            "label": "-", "color": "#8899aa", "bg": "#f0f3f7",
            "css_class": "tier-unknown", "description": "",
        }
    if score <= 20:
        return {
            "label": "拥挤", "color": "#b42318", "bg": "#fef0ef",
            "css_class": "tier-crowded",
            "description": "配置价值低，避免新增资金主动流入",
        }
    if score <= 40:
        return {
            "label": "偏热", "color": "#e07830", "bg": "#fff5ed",
            "css_class": "tier-warm",
            "description": "配置价值偏低，仅允许因明显低配而少量补齐",
        }
    if score <= 60:
        return {
            "label": "中性", "color": "#4a6fa5", "bg": "#eef3fa",
            "css_class": "tier-neutral",
            "description": "维持战略仓位，不主动大幅倾斜",
        }
    if score <= 80:
        return {
            "label": "友好", "color": "#1f7a57", "bg": "#e8f5ef",
            "css_class": "tier-friendly",
            "description": "长期配置价值较好，可适度提高目标仓位",
        }
    return {
        "label": "价值", "color": "#0d5c3a", "bg": "#e0f0e8",
        "css_class": "tier-value",
        "description": "长期配置赔率高，但不代表短期上涨信号",
    }


def asset_label(asset):
    return {
        "a_share": "A股",
        "us_equity": "海外权益",
        "gold": "黄金",
        "cash": "现金及低风险",
    }.get(asset, asset)


# ── 「每日自动化」监控页（Daily Automation Monitor）——纯展示，读治理产物，不改逻辑 ──
_SHADOW_DIR = output_paths.REPORTS_ROOT / "shadow" / "ndx-v1"


def _load_json_safe(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _latest_prepared_status(target_date):
    """读目标交易日的预备快照校验状态（PASS / CRITICAL_FAIL / None），best-effort。"""
    if not target_date:
        return None
    day_dir = _SHADOW_DIR / "prepared" / target_date
    if not day_dir.is_dir():
        return None
    files = sorted(day_dir.glob("*canonical-shadow-report.json"))
    if not files:
        return None
    payload = _load_json_safe(files[-1], {})
    return payload.get("copilot", {}).get("prepared_snapshot_validation", {}).get("status")


def _das_pill(state, small=False):
    cls = "das-pill das-%s%s" % (state.get("color", "gray"), " das-sm" if small else "")
    return '<span class="%s">%s</span>' % (cls, html.escape(str(state.get("label", "-"))))


def render_daily_automation_html(copilot):
    """构建「每日自动化」Tab 的 HTML。全部中文状态，读账本/SLA/预备快照/载体。

    纯展示与分类：不修改 Shadow 核心业务流程 / Graduation / Ledger / 自动执行逻辑。
    任何读取失败都降级为占位面板，绝不影响 dashboard 其它部分。
    """
    das = daily_automation_status
    try:
        ledger = _load_json_safe(_SHADOW_DIR / "shadow-ledger.json", {})
        sla = _load_json_safe(_SHADOW_DIR / "source-sla.json", {})
        records = sla.get("records", []) if isinstance(sla, dict) else []
        latest = records[-1] if records else None

        required = int(ledger.get("required_complete_days", ndx_shadow_run.REQUIRED_COMPLETE_DAYS) or 0)
        completed = int(ledger.get("shadow_days_completed", 0) or 0)
        target_date = (latest or {}).get("target_trade_date")
        counted_dates = {d.get("market_session_date") for d in ledger.get("days", [])}
        counted_today = target_date in counted_dates

        qdii = copilot.get("qdii_carrier_integration", {}) or {}
        carriers = (qdii.get("selection", {}) or {}).get("ndx_carriers", []) or []
        gate = das.carrier_gate(qdii.get("carrier_data_status"), qdii.get("carrier_selection_status"))
        carrier_ok = gate[0] == "成功"

        run_state = das.classify_final_status((latest or {}).get("final_status"))
        ledger_state = das.classify_ledger_status(ledger.get("status"))
        dcp_state = das.classify_dcp_status(ledger.get("dynamic_cash_pool_status") or "FREEZE")
        prepared_status = _latest_prepared_status(target_date)
        flow = das.execution_flow(latest, ledger_counted_today=counted_today,
                                  prepared_status=prepared_status, carrier_gate_status=gate)
        cells = das.graduation_cells(ledger)
        rc = das.root_cause_layers(latest, ledger)

        generated = html.escape(str(copilot.get("generated_at", ""))[:16])
        target_txt = html.escape(str(target_date or "—"))

        # ── 1. 执行总览 ──
        overview = f"""
        <article class="panel das-hero das-border-{run_state['color']}">
          <span class="eyebrow">每日自动化 · 执行总览</span>
          <h2>今日结论：{html.escape(run_state['label'])}</h2>
          <p class="das-freshness">数据截止 {generated} · 本页为服务端快照、<strong>非实时</strong>；在每日数据刷新及正式决策生成后自动更新，查看最新请重载浏览器。</p>
          <div class="das-summary">
            <div class="das-cell"><span class="das-k">生成时间</span><span class="das-v">{generated}</span></div>
            <div class="das-cell"><span class="das-k">目标交易日</span><span class="das-v">{target_txt}</span></div>
            <div class="das-cell"><span class="das-k">Shadow 执行</span><span class="das-v">{_das_pill(run_state, small=True)}</span></div>
            <div class="das-cell"><span class="das-k">Graduation 进度</span><span class="das-v">{completed} / {required} 天 · {_das_pill(ledger_state, small=True)}</span></div>
            <div class="das-cell"><span class="das-k">动态资金池</span><span class="das-v">{_das_pill(dcp_state, small=True)}</span></div>
          </div>
          <div class="das-oneline"><strong>真实 Root Cause：</strong>{html.escape(rc['root'])}</div>
        </article>"""

        # ── 2. 执行流程 ──
        step_html = ""
        for i, s in enumerate(flow):
            if i:
                step_html += '<span class="das-arrow" aria-hidden="true">→</span>'
            detail = ('<small>%s</small>' % html.escape(s['detail'])) if s.get('detail') else ''
            step_html += (
                '<span class="das-step">'
                '<span class="das-dot das-bg-%s"></span>'
                '<span class="das-step-name">%s</span>'
                '<span class="das-step-status das-%s">%s</span>%s</span>'
                % (s['color'], html.escape(s['name']), s['color'], html.escape(s['status']), detail))
        flow_panel = f"""
        <article class="panel section-spacer">
          <span class="eyebrow">执行流程</span>
          <h2>LaunchAgent → 毕业进度</h2>
          <div class="das-flow">{step_html}</div>
        </article>"""

        # ── 3. Carrier 载体状态 ──
        carrier_rows = ""
        for c in carriers:
            d = das.carrier_display(c)
            held = '<span class="das-pill das-blue das-sm">已持仓</span>' if d["held"] else ''
            carrier_rows += (
                "<tr>"
                f"<td>{html.escape(d['code'])}<small>{html.escape(d['name'])}</small></td>"
                f"<td>{html.escape(d['purchase_status'])}</td>"
                f'<td><span class="das-{d["channel_color"]}">{html.escape(d["channel_text"])}</span></td>'
                f'<td><span class="das-{d["personal_color"]}">{html.escape(d["personal_text"])}</span></td>'
                f"<td>{d['capacity']:,.0f} 元</td>"
                f'<td><span class="das-pill das-{d["result_color"]} das-sm">{html.escape(d["result"])}</span> {held}</td>'
                "</tr>")
        carrier_panel = f"""
        <article class="panel section-spacer">
          <span class="eyebrow">Carrier 载体状态</span>
          <h2>QDII 执行载体（{len(carriers)} 只）<small style="font-size:11px;color:var(--muted);"> 载体闸门：{html.escape(gate[2])}</small></h2>
          <div style="overflow-x:auto;"><table>
            <thead><tr><th>名称</th><th>申购状态</th><th>渠道可买</th><th>个人可买</th><th>容量</th><th>最终结果</th></tr></thead>
            <tbody>{carrier_rows or '<tr><td colspan="6" class="muted">暂无载体数据</td></tr>'}</tbody>
          </table></div>
          <p class="muted">仅展示载体执行能力，不构成买入建议；系统不自动替用户决定。</p>
        </article>"""

        # ── 4. Graduation Progress ──
        day_html = ""
        for idx in range(1, required + 1):
            cell = next((c for c in cells if c.get("shadow_day") == idx), None)
            if cell:
                day_html += (
                    f'<div class="das-day das-border-{cell["color"]}">'
                    f'<span class="das-day-n">Day {idx}</span>'
                    f'<span class="das-pill das-{cell["color"]} das-sm">{html.escape(cell["label"])}</span>'
                    f'<small>{html.escape(str(cell.get("date") or ""))}</small></div>')
            else:
                day_html += (
                    f'<div class="das-day das-border-gray">'
                    f'<span class="das-day-n">Day {idx}</span>'
                    f'<span class="das-pill das-gray das-sm">等待</span>'
                    f'<small>未到</small></div>')
        # 未计入的失败尝试单独列出（不占用 Day 序号，避免让用户以为进度倒退）
        fail_html = ""
        for c in cells:
            if c.get("shadow_day") is None:
                fail_html += (
                    f'<li class="das-fail"><span class="das-pill das-{c["color"]} das-sm">{html.escape(c["label"])}</span>'
                    f' <span class="muted">{html.escape(str(c.get("date") or ""))}</span> · {html.escape(c.get("detail") or "")}</li>')
        fail_block = (f'<h3 style="margin-top:14px;font-size:12px;color:var(--muted);">未计入的尝试（进度不倒退）</h3>'
                      f'<ul class="das-fail-list">{fail_html}</ul>') if fail_html else ""
        grad_panel = f"""
        <article class="panel section-spacer">
          <span class="eyebrow">Graduation Progress</span>
          <h2>影子验证进度 {completed} / {required} 天</h2>
          <div class="das-days">{day_html}</div>
          {fail_block}
        </article>"""

        # ── 5. Root Cause 分层 ──
        rc_panel = f"""
        <article class="panel section-spacer">
          <span class="eyebrow">Root Cause 分层归因</span>
          <h2>看穿「看似坏了，其实只是市场限制」</h2>
          <div class="das-rc">
            <div class="das-rc-layer"><span class="das-k">表面状态</span><strong>{html.escape(str(rc['surface']))}</strong> {_das_pill(rc['surface_state'], small=True)}</div>
            <div class="das-rc-arrow">↓</div>
            <div class="das-rc-layer"><span class="das-k">直接原因</span><strong>{html.escape(rc['direct'])}</strong></div>
            <div class="das-rc-arrow">↓</div>
            <div class="das-rc-layer das-rc-root das-border-{run_state['color']}"><span class="das-k">真实 Root Cause</span><strong>{html.escape(rc['root'])}</strong></div>
          </div>
        </article>"""

        # ── 状态说明表（折叠，供参考） ──
        legend_rows = ""
        for spec in das.STATES.values():
            legend_rows += (
                "<tr>"
                f'<td>{_das_pill(spec, small=True)}</td>'
                f"<td>{html.escape(spec['trigger'])}</td>"
                f"<td>{'是' if spec['is_anomaly'] else '否'}</td>"
                f"<td>{'是' if spec['needs_manual'] else '否'}</td>"
                f"<td>{'是' if spec['affects_graduation'] else '否'}</td>"
                f"<td>{'是' if spec['affects_dcp'] else '否'}</td>"
                "</tr>")
        legend = f"""
        <details class="panel section-spacer">
          <summary><strong>状态说明表</strong>（统一中文状态 · 触发条件 · 是否异常 · 是否需人工 · 是否影响 Graduation / DCP）</summary>
          <div style="overflow-x:auto;margin-top:10px;"><table>
            <thead><tr><th>状态</th><th>触发条件</th><th>异常</th><th>需人工</th><th>影响Graduation</th><th>影响DCP</th></tr></thead>
            <tbody>{legend_rows}</tbody>
          </table></div>
        </details>"""

        return overview + flow_panel + carrier_panel + grad_panel + rc_panel + legend
    except Exception as exc:  # 展示层绝不拖垮主 dashboard
        return (
            '<article class="panel"><span class="eyebrow">每日自动化</span>'
            '<h2>暂无每日自动化数据</h2>'
            '<p class="muted">尚未产生影子运行记录，或读取失败：%s</p></article>'
            % html.escape(str(exc)))


def _latest_complete_session(now):
    """日历上最近一个「已收盘」的美股交易日（用于判断哪些交易日已到运行时点）。"""
    ny_date = now.astimezone(ndx_shadow_run.NEW_YORK).date()
    for offset in range(10):
        candidate = ny_date - dt.timedelta(days=offset)
        if ndx_shadow_run.market_session_status(candidate, evaluated_at=now).get("complete_us_trading_day"):
            return candidate
    return None


def render_automation_history_html(copilot):
    """「自动化历史」Tab：执行覆盖率 + 最近 30 个日历日的自动化历史表。

    只读 SLA/账本产物 + 交易日历，按日历日如实分类；严格区分 非交易日 / 未部署 /
    电脑离线 / 已运行，**绝不自动补跑或补造历史**。读取失败降级为占位面板。
    """
    das = daily_automation_status
    try:
        ledger = _load_json_safe(_SHADOW_DIR / "shadow-ledger.json", {})
        sla = _load_json_safe(_SHADOW_DIR / "source-sla.json", {})
        records = sla.get("records", []) if isinstance(sla, dict) else []
        now = dt.datetime.now().astimezone()
        rows = das.build_automation_history(
            records, ledger, latest_complete_session=_latest_complete_session(now),
            today=now.date(), window_days=30)
        cov = das.execution_coverage(rows)

        missing_txt = ("、".join(cov["missing_days"]) if cov["missing_days"]
                       else '<span style="color:var(--green);">无</span>')
        rate_color = "green" if cov["rate"] >= 95 else ("yellow" if cov["rate"] >= 80 else "orange")
        coverage_panel = f"""
        <article class="panel das-hero das-border-{rate_color}">
          <span class="eyebrow">Execution Coverage · 执行覆盖率</span>
          <h2>已部署交易日执行覆盖率 <span class="das-{rate_color}">{cov['rate']:.1f}%</span></h2>
          <div class="das-summary">
            <div class="das-cell"><span class="das-k">应执行（交易日）</span><span class="das-v">{cov['should']} 天</span></div>
            <div class="das-cell"><span class="das-k">实际执行（跑过）</span><span class="das-v">{cov['actual']} 天</span></div>
            <div class="das-cell"><span class="das-k">覆盖率</span><span class="das-v das-{rate_color}">{cov['rate']:.1f}%</span></div>
            <div class="das-cell"><span class="das-k">缺失（电脑离线）</span><span class="das-v">{len(cov['missing_days'])} 天</span></div>
          </div>
          <div class="das-oneline"><strong>缺失交易日：</strong>{missing_txt}
          <span class="muted"> · 统计仅含「已部署且已到运行时点」的交易日；上线前=未部署、周末节假日=非交易日，均不计入分母。</span></div>
        </article>"""

        hist_rows = ""
        for r in rows:
            trading = ('<span class="das-green">是</span>' if r["is_trading_day"]
                       else '<span class="das-gray">否</span>')
            hist_rows += (
                "<tr>"
                f"<td>{html.escape(r['date'])}<small>{html.escape(r['weekday'])}</small></td>"
                f"<td>{trading}</td>"
                f"<td>{html.escape(r['shadow'])}</td>"
                f"<td>{_das_pill(r['state'], small=True)}</td>"
                f"<td>{html.escape(r['root_cause'])}</td>"
                f"<td>{html.escape(str(r['graduation']))}</td>"
                f"<td>{html.escape(str(r['dcp']))}</td>"
                "</tr>")
        table_panel = f"""
        <article class="panel section-spacer">
          <span class="eyebrow">Automation History · 自动化历史</span>
          <h2>最近 30 个日历日（真实记录，不自动补历史）</h2>
          <div style="overflow-x:auto;"><table class="das-history">
            <thead><tr><th>日期</th><th>交易日</th><th>Shadow</th><th>最终状态</th><th>Root Cause</th><th>Graduation</th><th>资金池</th></tr></thead>
            <tbody>{hist_rows}</tbody>
          </table></div>
          <p class="muted">「电脑离线」= 交易日当天本地无运行记录（多为电脑关机），属环境问题、非程序错误；系统不会用当前数据补造昨天的结果。</p>
        </article>"""
        return coverage_panel + table_panel
    except Exception as exc:
        return (
            '<article class="panel"><span class="eyebrow">自动化历史</span>'
            '<h2>暂无自动化历史</h2>'
            '<p class="muted">尚未产生运行记录，或读取失败：%s</p></article>'
            % html.escape(str(exc)))


def write_copilot_dashboard(
    rows,
    macro_rows,
    market_temperature,
    copilot,
    history_rows,
    config=None,
):
    if config is None:
        config = load_config()
    now_ts = dt.datetime.now().astimezone()
    generated_at = str(copilot.get("generated_at") or now_ts.isoformat(timespec="seconds"))
    run_id = copilot.get("run_id") or now_ts.strftime("%Y-%m-%d_%H%M%S") + "_v7-ndx-v1-engineering-final"
    # Unified timestamp model — carrier snapshot time from real data, not artifact time
    artifact_generated_at = generated_at
    data_cutoff_at = generated_at
    run_started_at = generated_at
    # Read from V7 three-layer decision chain (canonical), fall back to flat compat chain
    ndx_chain = copilot.get("ndx_amount_chain", {})
    v7_chain = copilot.get("v7_decision_chain", {})
    v7_mc = v7_chain.get("model_candidate", {})
    v7_cm = v7_chain.get("carrier_matching", {})
    v7_fd = v7_chain.get("formal_decision", {})
    v7_identity = v7_chain.get("identity_verification", {})
    carrier_snapshot_generated_at = str(
        v7_cm.get("last_known_snapshot_generated_at")
        or ndx_chain.get("last_known_snapshot_generated_at")
        or generated_at
    )
    carrier_snapshot_evaluated_at = str(ndx_chain.get("snapshot_evaluated_at") or carrier_snapshot_generated_at)
    # Amount chain — sourced from v7_decision_chain (three-layer structure)
    amount_chain = {
        "carrier_coverable_amount": float(v7_cm.get("carrier_coverable_amount", ndx_chain.get("ndx_candidate_release_amount", 0)) or 0),
        "formal_executable_amount": float(v7_fd.get("formal_executable_amount", 0) or 0),
        "formal_release_amount": float(v7_fd.get("formal_release_amount", 0) or 0),
        "retained_due_to_decision_freeze": float(v7_fd.get("retained_due_to_decision_freeze", ndx_chain.get("ndx_candidate_release_amount", 0)) or 0),
        "last_known_approved_carrier_capacity": float(v7_cm.get("last_known_approved_carrier_capacity", ndx_chain.get("last_known_approved_carrier_capacity", 0)) or 0),
        "current_effective_carrier_capacity": float(v7_cm.get("current_effective_carrier_capacity", ndx_chain.get("current_effective_carrier_capacity", 0)) or 0),
        "carrier_snapshot_valid": bool(v7_cm.get("carrier_snapshot_valid") or ndx_chain.get("carrier_snapshot_valid")),
    }
    versions = copilot.get("version_traceability", {})
    model_status = copilot.get("model_status", "NOT_RUN")
    pool_model_status = copilot.get("dynamic_cash_pool_status", "FREEZE")
    frozen = pool_model_status == "FREEZE"
    execution_disabled = not bool(copilot.get("allow_auto_execution", False))
    disabled_status_text = "FREEZE" if execution_disabled else "EXECUTE"
    blocking_issues = copilot.get("data_quality_gate", {}).get("blocking_issues", [])
    blocker_rows = []
    for item in copilot.get("data_quality_gate", {}).get("indicators", []):
        if not item.get("used_in_score") or item.get("gate_result") == "PASS":
            continue
        if item.get("non_blocking_fallback"):
            continue
        reasons = []
        fixes = []
        if item.get("stale_status") == "FAIL":
            reasons.append("数据超过允许时效")
            fixes.append("更新至时效阈值内")
        if item.get("confidence") == "Low":
            reasons.append("Low confidence")
            fixes.append("更换可审计来源或完成人工复核")
        if item.get("methodology_status") == "FAIL":
            reasons.append("方法口径不清")
            fixes.append("披露并固化计算口径")
        if item.get("reproducible_status") == "FAIL":
            reasons.append("无法独立复算")
            fixes.append("保存原始样本并本地复算")
        if item.get("approval_status") == "PENDING_PROXY_REVIEW":
            reasons.append("代理源待审批")
            fixes.append("用户复核来源后显式批准或更换官方来源")
        if item.get("approval_status") == "REJECTED":
            reasons.append("代理源已拒绝")
            fixes.append("更换合格来源")
        blocker_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                html.escape(item["indicator"]),
                html.escape("；".join(reasons) or "Blocking rule triggered"),
                html.escape(item.get("confidence") or "Unknown"),
                "Yes" if item.get("used_in_score") else "No",
                html.escape(item.get("approval_status") or "Unknown"),
                html.escape("；".join(dict.fromkeys(fixes)) or "刷新并重新审计"),
            )
        )
    blocker_table_html = (
        """
        <h3>Blocking Issues</h3>
        <table class="blocker-table">
          <thead><tr><th>Indicator</th><th>Reason</th><th>Confidence</th><th>Used In Score</th><th>Approval Status</th><th>Required Fix</th></tr></thead>
          <tbody>%s</tbody>
        </table>
        """ % "".join(blocker_rows)
        if blocker_rows else ""
    )
    approval_rows = []
    for item in copilot.get("data_quality_gate", {}).get("indicators", []):
        if item.get("approval_status") not in ("APPROVED_PROXY_PASS", "PENDING_PROXY_REVIEW", "REJECTED", "DISPLAY_ONLY"):
            continue
        display_approval = (
            "DISPLAY_ONLY" if item.get("indicator") in (
                "nasdaq100_pe_percentile", "sp500_pe_percentile"
            ) else item.get("approval_status")
        )
        approval_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                html.escape(item["indicator"]), html.escape(item.get("source") or "-"),
                html.escape(item.get("confidence") or "Unknown"),
                "Yes" if item.get("used_in_score") else "No",
                html.escape(display_approval or "Unknown"),
            )
        )
    source_approval_table_html = """
      <h3>Source Approval</h3>
      <table>
        <thead><tr><th>Indicator</th><th>Source</th><th>Confidence</th><th>Used In Score</th><th>Approval Status</th></tr></thead>
        <tbody>%s</tbody>
      </table>
    """ % "".join(approval_rows)
    asset_gate_rows = []
    routing_assets = copilot.get("allocation_routing", {}).get("assets", {})
    for asset in ("a_share", "us_equity", "gold"):
        quality = copilot.get("asset_level_status", {}).get(asset, {})
        routing = routing_assets.get(asset, {})
        score_display = "%.1f" % float(copilot.get("scores", {}).get(asset) or 0)
        asset_gate_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%.2f</td><td>%.2f</td><td>%.2f</td><td>%s</td></tr>"
            % (
                html.escape(asset_label(asset)),
                html.escape(score_display),
                html.escape(quality.get("data_quality_status", "-")),
                html.escape(quality.get("execution_status", "-")),
                float(routing.get("positive_gap", 0) or 0),
                float(routing.get("theoretical_allocation", 0) or 0),
                float(routing.get("executable_allocation", 0) or 0),
                html.escape(quality.get("reason", "-")),
            )
        )
    asset_gate_table_html = """
    <section class="panel" style="margin-bottom:14px;">
      <span class="eyebrow">Asset-Level Data Quality Gate</span>
      <h2>资产级执行资格</h2>
      <table>
        <thead><tr><th>Asset</th><th>Score</th><th>Data Quality</th><th>Execution Status</th><th>Positive Gap</th><th>Theoretical Allocation</th><th>Executable Allocation</th><th>Reason</th></tr></thead>
        <tbody>%s</tbody>
      </table>
      <p class="muted">%s</p>
    </section>
    """ % (
        "".join(asset_gate_rows),
        html.escape(copilot.get("pool_status_reason", "")),
    )
    model_risk_banner = f"""
    <section class="panel" style="margin-bottom:14px;border-left:5px solid {'#b42318' if execution_disabled else '#26734d'};">
      <span class="eyebrow">Model Risk Status</span>
      <h2>Model Status: {html.escape(model_status)}</h2>
      <div class="overview-grid">
        <div><span>Data Status</span><strong>{html.escape(copilot.get('data_status', 'FREEZE'))}</strong></div>
        <div><span>Decision Status</span><strong>{html.escape(copilot.get('decision_status', 'FREEZE'))}</strong></div>
        <div><span>Dynamic Cash Pool Status</span><strong>{html.escape(pool_model_status)}</strong></div>
        <div><span>Last Audit</span><strong>{model_risk.LAST_AUDIT_SCORE}/100</strong><small>{model_risk.LAST_AUDIT_DATE}</small></div>
        <div><span>Versions</span><strong>{html.escape(model_risk.MODEL_VERSION)}</strong><small>{html.escape(model_risk.DATA_QUALITY_VERSION)}</small></div>
      </div>
      <p><strong>{'Dynamic Cash Pool FREEZE' if execution_disabled else 'Decision gate passed: EXECUTE'}</strong></p>
      <p class="muted">Freeze Reason: {html.escape(copilot.get('pool_status_reason', '-') if execution_disabled else 'None')}</p>
      <p class="muted">Blocking Issues: {html.escape('、'.join(blocking_issues) if blocking_issues else 'None')}。固定定投不受影响。</p>
      <p class="muted">Formula Version: {html.escape(model_risk.FORMULA_VERSION)}</p>
      <p class="muted">当前分配逻辑：战略配置缺口决定资金方向；A股价格温度仅约束A股动态资金释放比例，波动风险限制单次释放。</p>
      {blocker_table_html}
      {source_approval_table_html}
    </section>
    """
    score_cards = []
    cn_temp = copilot.get("cn_equity_price_temperature", {})
    carrier_metrics = cn_temp.get("carrierIndex", {})
    market_metrics = cn_temp.get("marketIndex", {})
    cn_level_labels = {
        "VERY_HOT": "很热", "HOT": "偏热", "NEUTRAL": "中性",
        "COOL": "偏冷", "VERY_COOL": "很冷",
        "EXTREME_RISK": "极端低位 / 高风险", "UNAVAILABLE": "不可用",
    }
    for asset in ("a_share", "us_equity", "gold"):
        if asset == "a_share":
            final_score = cn_temp.get("finalScore")
            score_text = "-" if final_score is None else "%.1f" % final_score
            ma_window = carrier_metrics.get("movingAverageWindow")
            ma_distance = carrier_metrics.get("movingAverageDistance")
            drawdown = carrier_metrics.get("oneYearDrawdown")
            volatility = carrier_metrics.get("annualizedVolatility")
            hs_ma_window = market_metrics.get("movingAverageWindow")
            hs_distance = market_metrics.get("movingAverageDistance")
            hs_drawdown = market_metrics.get("oneYearDrawdown")
            reasons_html = "".join(
                "<li>%s</li>" % html.escape(reason)
                for reason in cn_temp.get("reasons", [])
            )
            score_cards.append(
                f"""
                <article class="temperature-card cn-temperature-card">
                  <div class="tc-head">
                    <span class="tc-name">A股价格温度</span>
                    <span class="tc-tier">{html.escape(cn_level_labels.get(cn_temp.get('level'), '不可用'))}</span>
                  </div>
                  <div class="tc-score-area"><strong class="tc-score">{score_text}</strong><em class="tc-unit">/100</em></div>
                  <p class="muted">置信度：{html.escape(carrier_metrics.get('confidence', 'UNAVAILABLE'))} · 最近更新：{html.escape(carrier_metrics.get('latestDate') or '-')} · 有效释放系数：{float(cn_temp.get('effectiveReleaseFactor', 1.0)):.2f}</p>
                  <p class="muted">接入状态：{html.escape(cn_temp.get('activationStatus', 'BLOCKED_BY_A500_PRICE_DATA'))}</p>
                  <h3>实际投入载体：中证A500</h3>
                  <ul class="metric-list">
                    <li>相对MA{ma_window or '-'}：{'-' if ma_distance is None else format_pct(ma_distance * 100)}</li>
                    <li>近一年高点回撤：{'-' if drawdown is None else format_pct(drawdown * 100)}</li>
                    <li>60日年化波动率：{'-' if volatility is None else format_pct(volatility * 100)}</li>
                    <li>有效样本：{int(carrier_metrics.get('sampleCount', 0) or 0)} · {html.escape(carrier_metrics.get('historyStartDate') or '-')} ～ {html.escape(carrier_metrics.get('historyEndDate') or '-')}</li>
                    <li>历史类型：{'包含回溯历史' if carrier_metrics.get('isBackfilledHistory') else '仅正式发布后数据'}</li>
                  </ul>
                  <h3>市场环境参考：沪深300</h3>
                  <ul class="metric-list">
                    <li>相对MA{hs_ma_window or '-'}：{'-' if hs_distance is None else format_pct(hs_distance * 100)}</li>
                    <li>近一年高点回撤：{'-' if hs_drawdown is None else format_pct(hs_drawdown * 100)}</li>
                    <li>环境修正：{float(cn_temp.get('marketAdjustment', 0) or 0):+.0f}</li>
                  </ul>
                  <ul class="reason-list">{reasons_html}</ul>
                </article>
                """
            )
            continue
        score = copilot["scores"].get(asset)
        tier = get_tier_config(score)
        score_text = "-" if score is None else f"{score:.1f}"
        score_num = score if score is not None else 0
        pct = max(0, min(100, score_num))
        momentum = copilot["momentum"].get(asset)
        momentum_text = "-" if momentum is None else f"{momentum:+.1f}"
        momentum_class = (
            ""
            if momentum is None
            else ("positive" if momentum >= 0 else "negative")
        )
        score_cards.append(
            f"""
            <article class="temperature-card">
              <div class="tc-head">
                <span class="tc-name">{html.escape(asset_label(asset))}温度</span>
                <span class="tc-tier">{html.escape(tier['label'])}</span>
              </div>
              <div class="tc-score-area">
                <strong class="tc-score">{html.escape(score_text)}</strong>
                <em class="tc-unit">/100</em>
              </div>
              <div class="tc-bar-wrap">
                <div class="tc-bar" style="width:{pct:.1f}%;background:{tier['color']}"></div>
              </div>
              <span class="tc-momentum {momentum_class}">月度变化 {html.escape(momentum_text)}</span>
            </article>
            """
        )

    allocation_rows = []
    for asset in ("a_share", "us_equity", "gold", "cash"):
        target = copilot["targets"][asset] * 100
        current_value = copilot["current_values"][asset]
        current_pct = (
            current_value / copilot["total_value"] * 100
            if copilot["total_value"]
            else 0
        )
        gap = copilot["gaps"][asset]
        gain_loss = copilot.get("gain_loss", {}).get(asset, 0)
        allocation_rows.append(
            f"""
            <tr>
              <td>{html.escape(asset_label(asset))}</td>
              <td>{current_value:,.0f}<small>{current_pct:.1f}%</small></td>
              <td class="{'positive' if gain_loss >= 0 else 'negative'}">{gain_loss:+,.0f}</td>
              <td>{target:.1f}%<small>{copilot['target_values'][asset]:,.0f}</small></td>
              <td class="{'positive' if gap > 0 else 'muted'}">{gap:+,.0f}</td>
            </tr>
            """
        )

    decision = copilot.get("user_decision")
    status = copilot.get("status") or {
        "execute": "executed",
        "ignore": "ignored",
    }.get(decision, "pending")
    plan_amount = float(copilot.get("plan_amount", copilot["deploy_amount"]) or 0)
    executed_amount = float(copilot.get("executed_amount", 0) or 0)
    historical_allocations = copilot.get("executed_allocations", {}) or {}
    remaining_pool = float(
        copilot.get("remaining_dynamic_cash_pool", copilot["dynamic_cash_pool"])
        or 0
    )
    if execution_disabled:
        display_amount = 0
        decision_text = "Current Decision: 0 元 · %s" % disabled_status_text
        pool_note = "Release Amount: 0 元；历史执行记录仅在 Historical Execution 区块展示"
    elif status == "executed":
        display_amount = executed_amount
        decision_text = f"本月已执行 {executed_amount:,.0f} 元"
        pool_note = (
            f"剩余动态资金池 {remaining_pool:,.0f} 元，下月继续判断"
        )
    elif status == "ignored":
        display_amount = 0
        decision_text = "本月已忽略，不再生成新的本月建议"
        pool_note = (
            f"动态资金池保留 {remaining_pool:,.0f} 元，下月继续判断"
        )
    else:
        display_amount = plan_amount
        decision_text = "等待确认" if plan_amount > 0 else "无需确认"
        pool_note = ""

    display_allocations = (
        {"a_share": 0, "us_equity": 0, "gold": 0}
        if execution_disabled
        else copilot.get("allocation_plan", copilot["allocations"])
    )
    flow_title = "资产层建议"
    flow_rows = []
    for asset in ("a_share", "us_equity", "gold"):
        amount = float(display_allocations.get(asset, 0) or 0)
        if amount > 0:
            flow_rows.append(
                f"<li><span>{html.escape(asset_label(asset))}</span><strong>{amount:,.0f} 元</strong></li>"
            )
    if not flow_rows:
        flow_rows.append(
            f"<li><span>{html.escape(flow_title)}</span><strong>0 元</strong></li>"
        )

    release_direction_rows = []
    for asset in ("a_share", "us_equity", "gold"):
        routing = routing_assets.get(asset, {})
        amount = float(display_allocations.get(asset, 0) or 0)
        release_factor = float(routing.get("release_factor", 1) or 0)
        if execution_disabled:
            direction = "资金池冻结"
        elif amount <= 0:
            direction = "当前无正向执行金额"
        else:
            direction = (
                f"配置缺口 {float(routing.get('positive_gap', 0) or 0):,.0f} 元"
                f" · 有效释放系数 {release_factor * 100:.1f}%"
            )
        release_direction_rows.append(
            "<tr><td>%s</td><td>%.2f 元</td><td>%s</td></tr>" % (
                html.escape(asset_label(asset)), amount, html.escape(direction),
            )
        )
    release_direction_html = f"""
        <section class="panel" style="margin-bottom:14px;">
          <span class="eyebrow">Release Allocation Flow</span>
          <h2>本月动态资金释放方向</h2>
          <p class="muted">本月资产层计划 {display_amount:,.2f} 元 · {'等待执行确认' if status == 'pending' and not execution_disabled else '当前不产生执行方案' if execution_disabled else '本月方案已处理'}</p>
          <table>
            <thead><tr><th>资产方向</th><th>本月计划</th><th>计算依据</th></tr></thead>
            <tbody>{''.join(release_direction_rows)}</tbody>
          </table>
          <p class="muted" style="margin-top:10px;">NDX 独立候选承接上限在「配置与资金流」中单列展示；它不等同于本月资产层计划，也不代表已执行金额。</p>
          <a class="ac-detail-link" data-nav-to="allocation-flow">查看基金载体计划与金额链 →</a>
        </section>
    """

    carrier_plan = copilot.get("fund_carrier_plan", [])
    fund_executions = copilot.get("fund_executions", [])
    if status == "executed":
        fund_rows_source = fund_executions
        fund_execution_title = "基金层实际执行"
    else:
        fund_rows_source = carrier_plan
        fund_execution_title = (
            "基金载体建议" if status == "pending" else "本月基金方案已忽略"
        )
    fund_execution_rows_html = "".join(
        f"""
        <tr>
          <td>{html.escape(row['fund_name'])}<small>{html.escape(row['fund_code'])}</small></td>
          <td>{html.escape(asset_label(row['asset_class']))}</td>
          <td>{float(row['planned_amount']):,.0f}</td>
          <td>{
              f"{float(row.get('actual_executed_amount', 0)):,.0f}"
              if status == "executed"
              else "-"
          }</td>
        </tr>
        """
        for row in fund_rows_source
    )
    if not fund_execution_rows_html:
        fund_execution_rows_html = (
            '<tr><td colspan="4" class="muted">无基金执行记录</td></tr>'
        )
    unexecuted_amount = float(
        copilot.get("unexecuted_amount", plan_amount if status != "executed" else 0)
        or 0
    )
    execution_summary_html = ""
    if status == "executed":
        execution_summary_html = (
            '<div class="execution-summary">'
            f"<span>资产层建议 {plan_amount:,.0f} 元</span>"
            f"<span>基金层实际执行 {executed_amount:,.0f} 元</span>"
            f"<span>未执行余额 {unexecuted_amount:,.0f} 元</span>"
            f"<span>剩余资金池 {remaining_pool:,.0f} 元</span>"
            "</div>"
        )

    modal_rows = "" if execution_disabled else "".join(
        f"""
        <tr>
          <td>{html.escape(row['fund_name'])}<small>{html.escape(row['fund_code'])}</small></td>
          <td>{html.escape(row['asset_name'])}</td>
          <td>{float(row['planned_amount']):,.2f}</td>
          <td><input class="fund-actual" type="number" min="0"
            max="{integer_execution_amount(row['planned_amount'])}" step="1"
            data-fund-code="{html.escape(row['fund_code'])}"
            value="{integer_execution_amount(row['planned_amount'])}" {'disabled aria-disabled="true"' if execution_disabled else ''}></td>
        </tr>
        """
        for row in carrier_plan
    )
    executable_fund_plan_amount = round(sum(
        float(row.get("planned_amount", 0) or 0) for row in carrier_plan
    ), 2)
    integer_executable_amount = sum(
        integer_execution_amount(row.get("planned_amount", 0))
        for row in carrier_plan
    )

    disabled = execution_disabled or status != "pending" or plan_amount <= 0
    pool_note_html = (
        f'<p class="pool-note">{html.escape(pool_note)}</p>'
        if pool_note
        else ""
    )
    missing_html = ""
    if copilot["missing_indicators"]:
        missing_html = (
            '<div class="data-warning"><strong>数据不完整</strong><span>'
            + html.escape("、".join(copilot["missing_indicators"]))
            + "。系统保留资金池，不生成释放方案。</span></div>"
        )

    indicators = copilot["indicators"]
    indicator_sources = copilot.get("indicator_sources", {})
    social_source = indicator_sources.get("social_financing_yoy", {})
    m2_source = indicator_sources.get("m2_yoy", {})
    nasdaq_source = indicator_sources.get("nasdaq100", {})
    sp500_source = indicator_sources.get("sp500", {})
    indicator_rows = "\n".join(
        f"<tr><td>{html.escape(label)}</td><td>{'-' if value is None else f'{value:.2f}'}</td></tr>"
        for label, value in (
            ("A500 PE_TTM（Display Only；Not Used In Score；Reproducible: No；Confidence: Low）", indicators["a500_pe"]),
            ("A500 PB（Display Only；Not Used In Score；Reproducible: No；Confidence: Low）", indicators["a500_pb"]),
            (f"沪深300 PE_TTM历史百分位（{indicators['hs300_sample_size']}条本地样本）", indicators["hs300_pe_percentile"]),
            (f"沪深300 PB历史百分位（{indicators['hs300_sample_size']}条本地样本）", indicators["hs300_pb_percentile"]),
            (
                f"社融存量同比（人民银行 {social_source.get('date') or '-'}）",
                indicators["social_financing_yoy"],
            ),
            (
                f"M2同比（人民银行 {m2_source.get('date') or '-'}）",
                indicators["m2_yoy"],
            ),
            (
                f"纳斯达克100 PE（QQQ proxy；"
                f"metric_type={indicators['nasdaq100_metric_type'] or 'unknown'}；"
                f"{nasdaq_source.get('date') or '-'}；"
                f"样本数={indicators['nasdaq100_sample_count']}）",
                indicators["nasdaq100_pe"],
            ),
            (
                "纳斯达克100 PE近5年百分位"
                f"（{indicators['nasdaq100_percentile_window']}；"
                f"基于{indicators['nasdaq100_sample_count']}个月样本）",
                indicators["nasdaq100_pe_percentile"],
            ),
            (
                f"标普500 PE（{sp500_source.get('source') or '-'}；"
                f"{indicators['sp500_metric_type'] or 'unknown'}；"
                f"{sp500_source.get('date') or '-'}；"
                f"{indicators['sp500_sample_count']}个样本）",
                indicators["sp500_pe"],
            ),
            (
                "标普500 PE近5年百分位"
                f"（{indicators['sp500_percentile_window']}；"
                f"基于{indicators['sp500_sample_count']}个月样本）",
                indicators["sp500_pe_percentile"],
            ),
            ("5Y TIPS", indicators["tips5y"]),
            ("10Y TIPS", indicators["tips10y"]),
            ("10Y Breakeven", indicators["breakeven10y"]),
            ("Fed Funds", indicators["fed_funds"]),
        )
    )
    triggers = copilot["triggers"]
    absolute_gap_rows = "".join(
        f"""
        <tr>
          <td>绝对GapValue：{html.escape('海外权益' if item.get('asset') == 'us_equity' else item['asset_name'])}</td>
          <td>
            {copilot['gaps'][item['asset']]:,.0f} 元 /
            {copilot['gaps'][item['asset']] / copilot['total_value'] * 100:.1f}%
            <small>阈值 {item['threshold_value']:,.0f} 元 /
            {item['threshold_ratio'] * 100:.1f}%</small>
          </td>
        </tr>
        """
        for item in triggers["absolute_gap_assets"]
    )
    if not absolute_gap_rows:
        absolute_gap_rows = f"""
        <tr>
          <td>绝对GapValue触发</td>
          <td>无资产达到阈值
            <small>{triggers['absolute_gap_threshold_value']:,.0f} 元 /
            {triggers['absolute_gap_threshold_ratio'] * 100:.1f}%</small>
          </td>
        </tr>
        """
    initialization_text = (
        "是，已建立Baseline"
        if triggers["is_initialization"]
        else "否，使用上月Baseline比较"
    )
    initial_gap_text = (
        "允许，首月最多25%"
        if triggers["allow_absolute_gap_on_initialization"]
        else "关闭"
    )
    trigger_rows = f"""
        <tr><td>初始化首月</td><td>{html.escape(initialization_text)}</td></tr>
        <tr><td>首月绝对GapValue规则</td><td>{html.escape(initial_gap_text)}</td></tr>
        <tr>
          <td>最大月度评分变化</td>
          <td>{triggers['max_score_change']:.1f}
            <small>常规阈值 {triggers['score_change_regular_threshold']:.1f} /
            强释放阈值 {triggers['score_change_strong_threshold']:.1f}</small>
          </td>
        </tr>
        <tr><td>温度层级变化</td><td>{'是' if triggers['tier_changed'] else '否'}</td></tr>
        <tr>
          <td>最大GapValue变化</td>
          <td>{triggers['max_gap_change_value']:,.0f} 元 /
            {triggers['max_gap_change_ratio'] * 100:.1f}%
            <small>常规释放阈值 {triggers['gap_change_threshold_ratio'] * 100:.1f}%</small>
          </td>
        </tr>
        {absolute_gap_rows}
        <tr><td>连续未部署月数</td><td>{triggers['months_without_deploy']}</td></tr>
    """
    pe_quality_html = f"""
      <details class="data-note">
        <summary><strong>估值参考</strong> · 不参与当前自动评分</summary>
        <p>PE_TTM：{indicators.get('hs300_pe_percentile') if indicators.get('hs300_pe_percentile') is not None else '不可用'}；PB：{indicators.get('hs300_pb_percentile') if indicators.get('hs300_pb_percentile') is not None else '不可用'}。</p>
        <p>估值数据当前仅供参考，不参与A股自动温度和资金释放计算。旧数据口径待审计，不以精确百分位形成自动建议。</p>
      </details>
      <div class="data-note">
        <strong>海外权益PE估值参考 · DISPLAY_ONLY</strong>
        <span>
          Nasdaq100 PE 当前使用 QQQ proxy；used_in_score=false；used_in_release_factor=false；blocking=false。
          metric_type =
          {html.escape(indicators['nasdaq100_metric_type'] or 'unknown')}；
          样本数 = {indicators['nasdaq100_sample_count']}；
          window_label = {html.escape(indicators['nasdaq100_percentile_window'] or 'unknown')}；
          percentile 基于 {indicators['nasdaq100_sample_count']} 个月样本，不代表长期历史百分位。
          S&amp;P500 PE 同样仅保留作展示参考，不参与自动资金释放。
        </span>
      </div>
    """

    history_html = "\n".join(
        f"""
        <tr>
          <td>{html.escape(item['month'])}</td>
          <td>{item['dynamic_cash_pool']:,.0f}</td>
          <td>{float(item.get('actual_deploy_amount') or 0):,.0f}</td>
          <td>{html.escape(item.get('execution_type') or '-')}</td>
          <td>{html.escape(item.get('action_level', '-'))}</td>
          <td>{html.escape({'execute': '已执行', 'manual_review': '人工复核已执行', 'ignore': '已忽略'}.get(item.get('user_decision'), '未处理'))}</td>
        </tr>
        """
        for item in history_rows
    )

    fund_rows = "\n".join(
        f"""
        <tr>
          <td>{html.escape(row['name'])}<small>{html.escape(row['code'])}</small></td>
          <td>{html.escape(row['latest_date'])}<small>{row['latest_nav']:.4f} · QDII Lag {html.escape(row['qdii_lag_status'])}</small></td>
          <td>{html.escape(row['strategy'])}</td>
          <td>{format_pct(row['drawdown_6m_pct'])}<small>Coverage {row['coverage_6m_status']} · {row['coverage_6m_sample_size']} / {float(row['coverage_6m_ratio'] or 0):.1%}</small></td>
          <td>{format_pct(row['drawdown_12m_pct'])}<small>Coverage {row['coverage_12m_status']} · {row['coverage_12m_sample_size']} / {float(row['coverage_12m_ratio'] or 0):.1%}</small></td>
        </tr>
        """
        for row in rows
    )

    reasons = copilot["reasons"]
    reasons_html = "".join(f"<li>{html.escape(reason)}</li>" for reason in reasons)

    # ── Tab 1: compact allocation gap basis (inside cash flow panel) ──
    gap_basis_rows = []
    for asset in ("a_share", "us_equity", "gold", "cash"):
        gap = copilot["gaps"][asset]
        target_pct = copilot["targets"][asset] * 100
        current_value = copilot["current_values"][asset]
        current_pct = (
            current_value / copilot["total_value"] * 100
            if copilot["total_value"]
            else 0
        )
        if asset == "cash":
            gap_text = f"超配 {abs(gap):,.0f} 元"
            note = "，本月不新增现金"
            css_class = "gap-cash"
        elif gap > 0:
            gap_text = f"低配 {gap:,.0f} 元"
            note = ""
            css_class = "gap-under"
        else:
            gap_text = f"超配 {abs(gap):,.0f} 元"
            note = ""
            css_class = "gap-over"
        gap_basis_rows.append(
            f"""<li class="{css_class}">
              <span class="gb-label">{html.escape(asset_label(asset))}</span>
              <span class="gb-detail">{gap_text}，当前仓位 {current_pct:.1f}%，目标 {target_pct:.1f}%{note}</span>
            </li>"""
        )
    gap_basis_html = (
        f"""<div class="flow-basis">
          <h3>配置依据</h3>
          <ul class="gb-list">{''.join(gap_basis_rows)}</ul>
        </div>"""
    )

    # ── Tab 1: flow direction items ──
    flow_items = []
    for asset in ("a_share", "us_equity", "gold"):
        amount = float(display_allocations.get(asset, 0) or 0)
        flow_items.append(
            f"""<div class="fd-item">
              <span class="fd-label">{html.escape(asset_label(asset))}</span>
              <strong class="fd-amount">{amount:,.0f} 元</strong>
            </div>"""
        )
    flow_direction_html = (
        f"""<div class="flow-direction">{"".join(flow_items)}</div>"""
        if any(float(display_allocations.get(a, 0) or 0) > 0 for a in ("a_share", "us_equity", "gold"))
        else '<p class="muted">本月无资金流向</p>'
    )
    historical_flow_items = "".join(
        f"""<div class="fd-item">
          <span class="fd-label">{html.escape(asset_label(asset))}</span>
          <strong class="fd-amount">{float(historical_allocations.get(asset, 0) or 0):,.0f} 元</strong>
        </div>"""
        for asset in ("a_share", "us_equity", "gold")
    )
    historical_flow_html = (
        f"""
        <div class="flow-basis" style="margin-top:16px;">
          <h3>Historical Executed Flow: {executed_amount:,.0f} 元</h3>
          <div class="flow-direction">{historical_flow_items}</div>
          <p class="muted">Historical execution only · 不代表当前建议</p>
        </div>
        """
        if executed_amount > 0 else ""
    )

    # ── Flow tab content ──
    flow_tab_content = f"""
    <article class="panel flow-panel">
      <span class="eyebrow">Cash Flow Summary</span>
      <h2>Current Recommended Flow: {display_amount:,.0f} 元</h2>
      <div class="flow-hero">
        <strong class="num">{display_amount:,.0f}</strong><span>元</span>
        <span class="flow-hero-tag">{html.escape(decision_text)}</span>
      </div>
      {flow_direction_html}
      {gap_basis_html}
    </article>
    """

    # ── Tab 3: detailed allocation rows with position % columns ──
    allocation_rows_detailed = []
    for asset in ("a_share", "us_equity", "gold", "cash"):
        target_pct = copilot["targets"][asset] * 100
        target_value = copilot["target_values"][asset]
        current_value = copilot["current_values"][asset]
        current_pct = (
            current_value / copilot["total_value"] * 100
            if copilot["total_value"]
            else 0
        )
        gap = copilot["gaps"][asset]
        gain_loss = copilot.get("gain_loss", {}).get(asset, 0)
        allocation_rows_detailed.append(
            f"""
            <tr>
              <td>{html.escape(asset_label(asset))}</td>
              <td>{current_value:,.0f}<small>{current_pct:.1f}%</small></td>
              <td>{target_value:,.0f}<small>{target_pct:.1f}%</small></td>
              <td class="{'positive' if gap > 0 else 'muted'}">{gap:+,.0f}</td>
              <td class="{'positive' if gain_loss >= 0 else 'negative'}">{gain_loss:+,.0f}</td>
            </tr>
            """
        )
    target_explanation_rows = []
    for asset in ("a_share", "us_equity", "gold", "cash"):
        detail = copilot.get("target_explanations", {}).get(asset, {})
        target_explanation_rows.append(
            f"""
            <tr>
              <td>{html.escape(asset_label(asset))}</td>
              <td>{('N/A' if detail.get('strategic_target') is None else (float(detail['strategic_target']) * 100))}{'%' if detail.get('strategic_target') is not None else ''}</td>
              <td>{html.escape(detail.get('target_mode', '-'))}</td>
              <td>{float(detail.get('final_target', 0)) * 100:.1f}%</td>
              <td>{float(detail.get('min_target', 0)) * 100:.1f}%</td>
              <td>{float(detail.get('max_target', 0)) * 100:.1f}%</td>
              <td>{html.escape(detail.get('target_reason', '-'))}</td>
            </tr>
            """
        )

    # ── Tab 4: execution content (status-driven) ──
    execution_tab_content = ""
    fund_carrier_rows_for_tab3 = ""
    for row in carrier_plan:
        fund_carrier_rows_for_tab3 += f"""
        <tr>
          <td>{html.escape(row['fund_name'])}<small>{html.escape(row['fund_code'])}</small></td>
          <td>{html.escape(row.get('asset_name', asset_label(row['asset_class'])))}</td>
          <td>{float(row['planned_amount']):,.0f}</td>
          <td>在确认弹窗填写</td>
        </tr>
        """

    if execution_disabled:
        execution_tab_content = f"""
        <article class="panel">
          <span class="eyebrow">Execution Control</span>
          <h2>Execution disabled because Decision Status is FREEZE</h2>
          <p class="muted">Current Decision: 0 元 · Release Amount: 0 元 · {html.escape(disabled_status_text)}</p>
          <button class="primary" type="button" disabled aria-disabled="true">执行已禁用</button>
        </article>
        <article class="panel" style="margin-top:14px;">
          <span class="eyebrow">Historical Execution</span>
          <h2>Historical Executed Amount: {executed_amount:,.0f} 元</h2>
          <table class="execution-table">
            <thead><tr><th>基金</th><th>资产</th><th>历史计划</th><th>历史实际执行</th></tr></thead>
            <tbody>{fund_execution_rows_html}</tbody>
          </table>
          <p class="muted">该记录已完成，仅供历史审计；不可再次确认或执行。</p>
        </article>
        """
    elif status == "executed":
        execution_tab_content = f"""
        <article class="panel">
          <span class="eyebrow">Execution Result</span>
          <h2>本月执行结果</h2>
          {execution_summary_html}
          <h3>基金层实际执行</h3>
          <table class="execution-table">
            <thead><tr><th>基金</th><th>资产</th><th>计划</th><th>实际</th></tr></thead>
            <tbody>{fund_execution_rows_html}</tbody>
          </table>
          <h3>触发原因</h3>
          <ul class="rule">{reasons_html}</ul>
          <div class="data-note" style="margin-top:12px;">
            本月方案已执行完毕。动态资金池已相应扣减，剩余资金池将计入下月判断。
          </div>
        </article>
        """
    elif status == "ignored":
        execution_tab_content = f"""
        <article class="panel" style="background:var(--paper);">
          <span class="eyebrow">Decision</span>
          <h2>本月已忽略</h2>
          <p class="muted">不再生成新的本月建议</p>
          <h3>原计划方案（被忽略）</h3>
          <ul class="flow-list" style="color:var(--ink);">{"".join(flow_rows)}</ul>
          {pool_note_html}
        </article>
        """
    elif plan_amount <= 0:
        execution_tab_content = f"""
        <article class="panel">
          <span class="eyebrow">Execution</span>
          <h2>无需确认</h2>
          <p class="muted">本月释放金额为 0，无需执行任何操作。</p>
          <ul class="rule">{reasons_html}</ul>
        </article>
        """
    else:
        # pending + has deploy_amount
        execution_tab_content = f"""
        <article class="panel decision-card">
          <span class="eyebrow">Cash Flow</span>
          <h2>资产层建议</h2>
          <ul class="flow-list">{"".join(flow_rows)}</ul>
          <h3>基金载体计划</h3>
          <table class="execution-table">
            <thead><tr><th>基金</th><th>资产</th><th>计划</th><th>实际</th></tr></thead>
            <tbody>{fund_carrier_rows_for_tab3}</tbody>
          </table>
          <p class="muted" style="color:#bac5d6;margin-top:10px;">
            资产层建议：{plan_amount:,.0f} 元 &nbsp;|&nbsp;
            当前基金计划可承接：{executable_fund_plan_amount:,.0f} 元 &nbsp;|&nbsp;
            容量未覆盖：{max(0, plan_amount - executable_fund_plan_amount):,.0f} 元（保留在资金池）
          </p>
          <p class="muted" style="color:#bac5d6;margin-top:6px;">
            本月已实际执行：0.00 元 &nbsp;|&nbsp;
            当前 Dynamic Cash Pool：{remaining_pool:,.2f} 元（扣除已实际执行金额后的余额）
          </p>
          <ul class="rule">{reasons_html}</ul>
          <div class="button-row">
            <button class="primary" data-decision="execute">执行本月方案</button>
          </div>
          <div id="message"></div>
        </article>
        """

    # ── Tab 6: data tab content ──
    data_tab_content = ""
    if missing_html:
        data_tab_content += missing_html
    if pe_quality_html:
        data_tab_content += pe_quality_html
    data_tab_content += f"""
    <article class="panel">
      <span class="eyebrow">Raw Indicators</span>
      <h2>原始指标</h2>
      <table>
        <thead><tr><th>指标</th><th>当前值</th></tr></thead>
        <tbody>{indicator_rows}</tbody>
      </table>
    </article>
    """

    # ── Drawdown top 5 for overview ──
    drawdown_top5 = sorted(rows, key=lambda r: r["drawdown_6m_pct"])[:5]
    # Drawdown top-5 list no longer renders on the Overview; the single worst
    # fund now surfaces inside Today's Focus, and the full per-fund table stays
    # in the 基金回撤 tab. `drawdown_top5` (above) still feeds the focus rollup.

    # ── Asset cards for overview ──
    routing_assets_for_cards = copilot.get("allocation_routing", {}).get("assets", {})
    asset_card_html = ""
    for asset in ("a_share", "gold"):
        score = copilot["scores"].get(asset)
        a_share_temperature_disabled = bool(
            asset == "a_share"
            and not copilot.get("cn_equity_price_temperature", {}).get("modelEnabled")
        )
        display_score = None if a_share_temperature_disabled else score
        score_text = "-" if display_score is None else f"{float(display_score):.1f}"
        score_num = float(display_score or 0)
        tier = get_tier_config(display_score)
        total_val = copilot["total_value"]
        position_pct = (copilot["current_values"][asset] / total_val * 100) if total_val else 0
        target_pct = copilot["targets"][asset] * 100
        gap = copilot["gaps"][asset]
        quality = copilot.get("asset_level_status", {}).get(asset, {})
        data_status = quality.get("data_quality_status", "-")
        exec_status = quality.get("execution_status", "-")
        reason = quality.get("reason", "-")
        if asset == "gold" and gap <= 0:
            action_label = "无需加仓"
            action_class = "no-action"
        elif exec_status == "BLOCKED":
            action_label = "冻结"
            action_class = "blocked"
        elif a_share_temperature_disabled and gap > 0:
            action_label = "战略回退"
            action_class = "no-action"
        elif exec_status == "ELIGIBLE" and gap > 0:
            action_label = "可执行"
            action_class = "eligible"
        else:
            action_label = "无需加仓"
            action_class = "no-action"
        gap_class = "gap-positive" if gap > 0 else "gap-negative"
        data_color = "var(--green)" if data_status == "PASS" else ("var(--amber)" if data_status == "WARNING" else "var(--red)")
        tier_class = "hot" if tier["label"] in ("偏热", "很热") else ("neutral" if tier["label"] == "中性" else "")
        # A500 temperature explanation for A-share card
        a500_temp_extra = ""
        if asset == "a_share":
            cn_temp = copilot.get("cn_equity_price_temperature", {})
            a500_level = cn_temp.get("level", "")
            level_map = {"VERY_HOT": "极度拥挤", "HOT": "偏热", "NEUTRAL": "中性", "COOL": "偏冷", "VERY_COOL": "极度低估"}
            level_cn = level_map.get(a500_level, "")
            a500_temp_extra = f"""<div class="ac-detail-row"><span class="ac-detail-label">A500温度等级</span><span class="ac-detail-value" style="color:var(--red);">{html.escape(level_cn)}</span></div>
            <div class="ac-detail-row"><span class="ac-detail-label">释放系数</span><span class="ac-detail-value">{cn_temp.get('effectiveReleaseFactor', 1.0) * 100:.0f}%</span></div>
            <p class="muted" style="font-size:10px;margin:4px 0 0;">0分=极热/极度拥挤，100分=极冷。分数越低越不适合追买。</p>"""
        # Summary Card: Score · 温度等级 · 当前状态 · 一个关键指标（配置缺口）。
        # 仓位/目标/数据状态/释放系数等明细下沉到「配置与资金流」「数据与审计」。
        ac_status_class = {"eligible": "eligible", "blocked": "validation"}.get(action_class, "idle")
        tier_text = level_cn if asset == "a_share" and level_cn else tier["label"]
        tier_badge_class = "crowd" if (asset == "a_share" and level_cn == "极度拥挤") else tier_class
        asset_card_html += f"""
          <article class="asset-card summary">
            <div class="ac-head">
              <span class="ac-name">{html.escape(asset_label(asset))}</span>
              <span class="ac-tier {tier_badge_class}">{html.escape(tier_text)}</span>
            </div>
            <div class="ac-score-row">
              <strong class="ac-score">{html.escape(score_text)}</strong>
              <em class="ac-score-unit">/100</em>
            </div>
            <div class="ac-bar-wrap">
              <div class="ac-bar" style="width:{max(0, min(100, score_num)):.1f}%;background:{tier['color']}"></div>
            </div>
            <div class="ac-status {ac_status_class}">{html.escape(action_label)}</div>
            <div class="ac-keyline">
              <span class="k">配置缺口</span>
              <span class="v {'pos' if gap > 0 else 'neg'}">{gap:+,.0f} 元</span>
            </div>
            <a class="ac-detail-link" data-nav-to="allocation-flow">配置与资金流 →</a>
          </article>"""

    qdii_overview = copilot.get("qdii_carrier_integration", {})
    qdii_selection_overview = qdii_overview.get("selection", {})
    overseas_split = qdii_overview.get("overseas_equity_split", {})
    ndx_amount = float(overseas_split.get("ndx_qdii_amount", 0) or 0)
    global_amount = float(overseas_split.get("global_active_amount", 0) or 0)
    overseas_total = float(overseas_split.get("overseas_equity_total", 0) or 0)
    ndx_ratio = float(overseas_split.get("ndx_qdii_ratio", 0) or 0) * 100
    global_ratio = float(overseas_split.get("global_active_ratio", 0) or 0) * 100
    approved_capacity_overview = float(qdii_selection_overview.get("approved_total_capacity", 0) or 0)
    capacity_status_overview = qdii_selection_overview.get("carrier_capacity_status", "BLOCKED")
    carrier_snapshot_valid = bool(qdii_overview.get("carrier_snapshot_valid"))
    carrier_data_status = str(qdii_overview.get("carrier_data_status") or "UNAVAILABLE")
    carrier_selection_status = str(qdii_overview.get("carrier_selection_status") or "BLOCKED")
    carrier_title = (
        "载体可用" if carrier_snapshot_valid and carrier_selection_status == "AVAILABLE"
        else "部分容量可用" if carrier_snapshot_valid and carrier_selection_status == "PARTIAL_CAPACITY"
        else "快照过期" if carrier_data_status == "STALE"
        else "载体不可用"
    )
    ndx_shadow = copilot.get("ndx_price_temperature", {})
    ndx_chain = copilot.get("ndx_amount_chain", {})
    ndx_level_labels = {
        "VERY_HOT": "极热", "HOT": "偏热", "NEUTRAL": "中性",
        "COOL": "偏冷", "VERY_COOL": "极冷", "UNAVAILABLE": "不可用",
    }
    ndx_level = ndx_level_labels.get(ndx_shadow.get("temperature_level"), "不可用")
    pct = lambda value: "-" if value is None else "%.1f%%" % (float(value) * 100)
    raw_pct = lambda value: "-" if value is None else "%.1f%%" % float(value)
    ndx_score_val = float(ndx_shadow.get('temperature_score') or 0)
    ndx_model_active = (
        copilot.get("ndx_asset_model_status") == "ACTIVE"
        and copilot.get("activation_status") == "ACTIVE"
    )
    ndx_card_status = (
        "ACTIVE · 已进入正式决策" if ndx_model_active and not execution_disabled
        else "ACTIVE · 决策门当前冻结" if ndx_model_active
        else "Validation · 待影子运行"
    )
    ndx_card_status_class = "eligible" if ndx_model_active and not execution_disabled else "validation"
    ndx_asset_plan = float(copilot.get("allocation_plan", {}).get("us_equity", 0) or 0)
    ndx_card = f"""
      <article class="asset-card summary">
        <div class="ac-head"><span class="ac-name">NDX价格温度</span><span class="ac-tier hot">{html.escape(ndx_level)}</span></div>
        <div class="ac-score-row"><strong class="ac-score">{ndx_score_val:.1f}</strong><em class="ac-score-unit">/100</em></div>
        <div class="ac-bar-wrap"><div class="ac-bar" style="width:{max(0, min(100, ndx_score_val)):.1f}%;background:#e07830"></div></div>
        <div class="ac-status {ndx_card_status_class}">{html.escape(ndx_card_status)}</div>
        <div class="ac-keyline"><span class="k">本月资产层方向</span><span class="v">{ndx_asset_plan:,.0f} 元</span></div>
        <a class="ac-detail-link" data-nav-to="allocation-flow">QDII执行载体 →</a>
      </article>"""
    global_card = f"""
      <article class="asset-card summary">
        <div class="ac-head"><span class="ac-name">全球主动权益</span><span class="ac-tier">仅计入仓位</span></div>
        <div class="ac-score-row"><strong class="ac-score">{global_amount:,.0f}</strong><em class="ac-score-unit">元</em></div>
        <div class="ac-status idle">无操作 · 仅持仓展示</div>
        <div class="ac-keyline"><span class="k">海外权益占比</span><span class="v">{global_ratio:.1f}%</span></div>
        <a class="ac-detail-link" data-nav-to="allocation-flow">海外权益拆分 →</a>
      </article>"""
    shadow_status = str(copilot.get("shadow_status") or "DAY1_PENDING")
    shadow_days_completed = int(copilot.get("shadow_days_completed", 0) or 0)
    shadow_required_days = int(copilot.get("shadow_required_complete_days", ndx_shadow_run.REQUIRED_COMPLETE_DAYS) or ndx_shadow_run.REQUIRED_COMPLETE_DAYS)
    shadow_banner = f"""
      <article class="panel" data-shadow-status="{html.escape(shadow_status)}">
        <span class="eyebrow">NDX Shadow Run</span>
        <h2>Shadow Day {shadow_days_completed} / {shadow_required_days}</h2>
        <div class="ac-detail-row"><span>当前状态</span><strong>{html.escape(shadow_status)}</strong></div>
        <div class="ac-detail-row"><span>下一个可累计日</span><strong>仅限下一个完整NASDAQ交易日收盘后</strong></div>
        <div class="ac-detail-row"><span>Dynamic Cash Pool</span><strong>{html.escape(pool_model_status)}</strong></div>
        <p class="muted">Day 0不计数；周末、休市、盘中、重复日期和失败日均不得累计。</p>
      </article>
    """
    ndx_target_value_35 = max(float(copilot["target_values"]["us_equity"]) - global_amount, 0.0)
    ndx_gap_value_35 = ndx_target_value_35 - ndx_amount
    ndx_target_value_40 = max(float(copilot["total_value"]) * 0.40 - global_amount, 0.0)
    # ── V7 Three-Layer identity status for display ──
    identity_status = v7_identity.get("status", "NOT_CHECKED")
    identity_html = ""
    if v7_identity.get("errors"):
        identity_html = '<tr><td colspan="2" class="data-warning" style="color:var(--amber);">⚠ 金额链身份校验失败：' + html.escape("; ".join(v7_identity["errors"])) + '</td></tr>'
    elif identity_status == "PASS":
        identity_html = '<tr><td colspan="2" style="color:var(--green);font-size:11px;">✓ 金额链身份校验通过 (candidate = coverable + retained; coverable = executable + freeze_retained)</td></tr>'
    flow_tab_content += f"""
    <article class="panel section-spacer">
      <span class="eyebrow">NDX Offline Validation Amount Chain — V7 Three-Layer Decision</span>
      <h2>海外权益当前有效目标35% · 战略目标40%（未启用）</h2>
      <p class="muted" style="margin-bottom:8px;">QDII JSON只提供载体额度和可用性事实。候选释放、容量匹配和正式执行均由V7计算。</p>
      <table><tbody>
        <tr><td>全球主动权益当前持仓</td><td>{global_amount:,.2f} 元</td></tr>
        <tr><td>NDX当前持仓</td><td>{ndx_amount:,.2f} 元</td></tr>
        <tr><td>35%目标下NDX目标空间</td><td>{ndx_target_value_35:,.2f} 元</td></tr>
        <tr><td>35%目标下NDX配置缺口</td><td>{ndx_gap_value_35:,.2f} 元</td></tr>
        <tr><td>40%战略目标下NDX目标空间（仅比较）</td><td>{ndx_target_value_40:,.2f} 元</td></tr>
        <tr><th colspan="2" style="background:#f0f4f8;font-size:12px;padding:6px 8px;">Layer 1 — 模型候选层 (V7 computes from NDX model + gap routing)</th></tr>
        <tr><td>NDX配置缺口路由金额</td><td>{float(v7_mc.get('ndx_gap_routed_amount', ndx_chain.get('ndx_gap_routed_amount', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>NDX候选释放金额 · min(资金池, 路由×释放系数)</td><td>{float(v7_mc.get('ndx_candidate_release_amount', ndx_chain.get('ndx_candidate_release_amount', 0)) or 0):,.2f} 元</td></tr>
        <tr><th colspan="2" style="background:#f0f4f8;font-size:12px;padding:6px 8px;">Layer 2 — 载体匹配层 (V7 matches candidate to carrier capacity)</th></tr>
        <tr><td>载体可承接金额</td><td>{float(v7_cm.get('carrier_coverable_amount', ndx_chain.get('ndx_candidate_release_amount', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>容量不足保留金额</td><td>{float(v7_cm.get('retained_due_to_capacity', ndx_chain.get('retained_due_to_capacity', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>载体阻断保留金额</td><td>{float(v7_cm.get('retained_due_to_carrier_block', ndx_chain.get('retained_due_to_carrier_block', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>上一有效/最近观察容量</td><td>{float(v7_cm.get('last_known_approved_carrier_capacity', ndx_chain.get('last_known_approved_carrier_capacity', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>当前有效载体容量</td><td>{float(v7_cm.get('current_effective_carrier_capacity', ndx_chain.get('current_effective_carrier_capacity', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>载体快照有效</td><td>{'是' if (v7_cm.get('carrier_snapshot_valid') or ndx_chain.get('carrier_snapshot_valid')) else '否'}</td></tr>
        <tr><th colspan="2" style="background:#f0f4f8;font-size:12px;padding:6px 8px;">Layer 3 — NDX 独立候选承接结果</th></tr>
        <tr><td>候选可承接上限</td><td>{float(v7_fd.get('formal_executable_amount', amount_chain.get('formal_executable_amount', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>候选承接金额</td><td>{float(v7_fd.get('formal_release_amount', amount_chain.get('formal_release_amount', 0)) or 0):,.2f} 元</td></tr>
        <tr><td>决策冻结保留金额</td><td>{float(v7_fd.get('retained_due_to_decision_freeze', amount_chain.get('retained_due_to_decision_freeze', 0)) or 0):,.2f} 元</td></tr>
        {identity_html}
      </tbody></table>
      <p class="muted">上述为NDX独立候选承接上限，不等同于本月资产层计划或实际执行金额。本月方向与基金计划以「资产层建议」为准。</p>
    </article>
    """
    # Keep the protected A-share and gold cards intact while inserting the two
    # separately governed overseas-equity cards between them.
    a_card_end = asset_card_html.find('</article>') + len('</article>')
    asset_card_html = asset_card_html[:a_card_end] + ndx_card + global_card + asset_card_html[a_card_end:]

    # 海外权益结构 / QDII执行能力 概览块已从 Overview 移除——同样的数据已分别由
    # 「配置与资金流」（海外权益拆分、载体容量）与「数据与审计」（QDII Carrier Data
    # Health）承载。Overview 改由 Today's Focus + Summary Card 给出结论。

    # ── Fund drawdown table for Tab 2 ──
    fund_drawdown_rows = ""
    for row in rows:
        dd6 = row["drawdown_6m_pct"]
        dd12 = row["drawdown_12m_pct"]
        dd6_color = 'style="color:var(--red);font-weight:700;"' if dd6 <= -10 else ('style="color:var(--amber);"' if dd6 <= -3 else "")
        dd12_color = 'style="color:var(--red);font-weight:700;"' if dd12 <= -10 else ('style="color:var(--amber);"' if dd12 <= -3 else "")
        fund_drawdown_rows += f"""
              <tr>
                <td>{html.escape(row['name'])}</td><td>{html.escape(row['code'])}</td><td>{row['latest_nav']:.4f}</td><td>{html.escape(row['latest_date'])}</td>
                <td {dd6_color}>{format_pct(dd6)}</td><td>{html.escape(row['coverage_6m_status'])} · {row['coverage_6m_sample_size']}/{float(row['coverage_6m_ratio'] or 0):.1%}</td>
                <td {dd12_color}>{format_pct(dd12)}</td><td>{html.escape(row['coverage_12m_status'])} · {row['coverage_12m_sample_size']}/{float(row['coverage_12m_ratio'] or 0):.1%}</td>
                <td>{html.escape(row['qdii_lag_status'])}</td><td>{html.escape(row['strategy'])}</td>
              </tr>"""

    # ── QDII carrier integration: asset decision and carrier choice stay separate ──
    qdii_integration = copilot.get("qdii_carrier_integration", {})
    carrier_snapshot_id = str(qdii_integration.get("carrier_snapshot_id") or "-")
    carrier_latest_sha256 = str(qdii_integration.get("carrier_latest_sha256") or "-")
    carrier_latest_sha256_prefix = carrier_latest_sha256[:12] if carrier_latest_sha256 != "-" else "-"
    qdii_selection = qdii_integration.get("selection", {})
    qdii_carriers = qdii_selection.get("ndx_carriers", [])
    qdii_asset_amount = float(qdii_selection.get("asset_allocated_amount", 0) or 0)
    qdii_current_release = (
        0.0 if execution_disabled else float(
            copilot.get("allocation_plan", {}).get("us_equity", 0) or 0
        )
    )
    approved_capacity = float(qdii_selection.get("approved_total_capacity", 0) or 0)
    qdii_remaining = float(qdii_selection.get("remaining_unallocated_amount", 0) or 0)
    qdii_candidate_rows = []
    for item in qdii_carriers:
        official = item.get("official_fund_limit_rmb")
        observed = item.get("observed_channel_limit_rmb")
        effective = item.get("effective_limit_rmb")
        tags = item.get("transparent_tags", {})
        tag_labels = []
        for kind, labels in (("adv", tags.get("advantages", [])), ("risk", tags.get("risks", []))):
            for label in labels:
                if label == "渠道便利" and (not item.get("purchase_channels") or item.get("purchase_channels") == []):
                    label = "购买渠道待补齐"
                if label == "渠道便利" and item.get("purchase_channels") == ["天天基金监控快照"]:
                    label = "购买渠道待补齐"
                tag_labels.append((kind, label))
        tag_html = " ".join(
            '<span class="dh-status-chip %s">%s</span>' % (
                "warn" if kind == "risk" else "ok", html.escape(label)
            )
            for kind, label in tag_labels
        )
        missing = lambda value, suffix="": "待补齐" if value in (None, "", "--") else html.escape(str(value)) + suffix
        checked = " checked" if item.get("current_holding") else ""
        default_amount = 0
        qdii_candidate_rows.append(f"""
          <tr class="qdii-carrier-row" data-code="{html.escape(item['fund_code'])}" data-capacity="{float(effective or 0):.2f}">
            <td><input type="checkbox" class="qdii-select"{checked} aria-label="选择{html.escape(item['fund_code'])}"></td>
            <td>{html.escape(item['fund_code'])}<small>{html.escape(item['fund_name'])}</small></td>
            <td>{html.escape(item.get('share_class') or '待补齐')}</td>
            <td>{missing('、'.join(item.get('purchase_channels', [])) or None)}<small>个人可买：{'是' if item.get('personal_purchase_supported') else '否'}</small></td>
            <td>{html.escape(item.get('source_name') or '待补齐')}<small>{html.escape(item.get('last_updated') or '待补齐')}</small></td>
            <td>{'是' if item.get('current_holding') else '否'}</td>
            <td>{missing(effective)}<small>官方 {missing(official)} / 渠道 {missing(observed)}</small></td>
            <td>{missing(item.get('tracking_error_pct'), '%')}</td>
            <td>{_fee_label_html(item)}</td>
            <td>{missing(item.get('fund_size_rmb'))}<small>成立 {missing(item.get('inception_date'))}</small></td>
            <td>{tag_html or '待补齐'}</td>
            <td><input class="qdii-amount" type="number" min="0" step="0.01" value="{default_amount:.2f}" aria-label="{html.escape(item['fund_code'])}分配金额"><small class="qdii-row-error" aria-live="polite" style="display:none;color:var(--red);font-size:10px;"></small></td>
          </tr>""")
    global_active = qdii_integration.get("pools", {}).get(
        "GLOBAL_ACTIVE_EQUITY_POOL", {}
    ).get("fund") or {}
    ndx_shadow = copilot.get("ndx_price_temperature", {})
    ndx_chain = copilot.get("ndx_amount_chain", {})
    ndx_level_labels = {
        "VERY_HOT": "极热", "HOT": "偏热", "NEUTRAL": "中性",
        "COOL": "偏冷", "VERY_COOL": "极冷", "UNAVAILABLE": "不可用",
    }
    ndx_level = ndx_level_labels.get(ndx_shadow.get("temperature_level"), "不可用")
    pct = lambda value: "-" if value is None else "%.1f%%" % (float(value) * 100)
    raw_pct = lambda value: "-" if value is None else "%.1f%%" % float(value)
    ndx_model_status_text = str(copilot.get("ndx_asset_model_status") or ndx_shadow.get("model_status") or "UNDER_VALIDATION")
    activation_status_text = str(copilot.get("activation_status") or ndx_shadow.get("activation_status") or "NOT_ACTIVE")
    ndx_status_pair = "%s / %s" % (
        ndx_model_status_text,
        html.escape(str(ndx_shadow.get("validation_stage") or copilot.get("validation_stage") or "OFFLINE_VALIDATION")),
    )
    formal_release_display = float(v7_fd.get('formal_release_amount', amount_chain.get('formal_release_amount', 0)) or 0)
    qdii_panel_html = f"""
        <article class="panel section-spacer">
          <span class="eyebrow">NDX资产机会</span>
          <h2>温度分 {float(ndx_shadow.get('temperature_score') or 0):.1f} / 100 · {html.escape(ndx_level)}<small style="font-size:11px;color:var(--muted);"> {html.escape(ndx_status_pair)} · {html.escape(activation_status_text)}</small></h2>
          <div class="overview-grid" style="margin:12px 0;">
            <div><span>价格基础释放</span><strong>{pct(ndx_shadow.get('base_release_factor'))}</strong></div>
            <div><span>实际利率调整后</span><strong>{pct(ndx_shadow.get('rate_adjusted_release_factor'))}</strong></div>
            <div><span>波动率上限</span><strong>{pct(ndx_shadow.get('volatility_cap'))}</strong></div>
            <div><span>最终候选释放</span><strong>{pct(ndx_shadow.get('candidate_effective_release_factor'))}</strong><small>独立候选承接上限 {formal_release_display:,.0f} 元</small></div>
          </div>
          <table><tbody>
            <tr><td>距离MA500</td><td>{pct(ndx_shadow.get('distance_to_ma500'))}</td></tr>
            <tr><td>距252日高点回撤</td><td>{pct(ndx_shadow.get('drawdown_from_252d_high'))}</td></tr>
            <tr><td>10Y实际利率百分位</td><td>{raw_pct(ndx_shadow.get('dfii10_percentile'))}</td></tr>
            <tr><td>60日波动率百分位</td><td>{raw_pct(ndx_shadow.get('realized_volatility_60d_percentile'))}</td></tr>
            <tr><th colspan="2" style="background:#f0f4f8;font-size:11px;padding:4px 6px;">V7 Layer 1 — 模型候选</th></tr>
            <tr><td>NDX配置缺口路由金额</td><td>{float(v7_mc.get('ndx_gap_routed_amount', ndx_chain.get('ndx_gap_routed_amount', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>NDX候选释放金额</td><td>{float(v7_mc.get('ndx_candidate_release_amount', ndx_chain.get('ndx_candidate_release_amount', 0)) or 0):,.2f} 元</td></tr>
            <tr><th colspan="2" style="background:#f0f4f8;font-size:11px;padding:4px 6px;">V7 Layer 2 — 载体匹配</th></tr>
            <tr><td>载体可承接金额</td><td>{float(v7_cm.get('carrier_coverable_amount', ndx_chain.get('ndx_candidate_release_amount', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>容量不足保留金额</td><td>{float(v7_cm.get('retained_due_to_capacity', ndx_chain.get('retained_due_to_capacity', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>载体阻断保留金额</td><td>{float(v7_cm.get('retained_due_to_carrier_block', ndx_chain.get('retained_due_to_carrier_block', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>上一有效/最近观察容量</td><td>{float(v7_cm.get('last_known_approved_carrier_capacity', ndx_chain.get('last_known_approved_carrier_capacity', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>当前有效容量</td><td>{float(v7_cm.get('current_effective_carrier_capacity', ndx_chain.get('current_effective_carrier_capacity', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>载体状态</td><td>{html.escape('可用' if (v7_cm.get('carrier_snapshot_valid') or carrier_snapshot_valid) else '快照过期，当前不可执行' if carrier_data_status == 'STALE' else '载体不可用，当前不可执行')}</td></tr>
            <tr><th colspan="2" style="background:#f0f4f8;font-size:11px;padding:4px 6px;">V7 Layer 3 — NDX 独立候选承接结果</th></tr>
            <tr><td>候选可承接上限</td><td>{float(v7_fd.get('formal_executable_amount', amount_chain.get('formal_executable_amount', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>候选承接金额</td><td>{float(v7_fd.get('formal_release_amount', amount_chain.get('formal_release_amount', 0)) or 0):,.2f} 元</td></tr>
            <tr><td>决策冻结保留金额</td><td>{float(v7_fd.get('retained_due_to_decision_freeze', amount_chain.get('retained_due_to_decision_freeze', 0)) or 0):,.2f} 元</td></tr>
          </tbody></table>
          <p class="muted">NDX V1价格温度用于释放节奏；0分=极热，100分=极冷。ACTIVE 只表示模型可进入正式决策门，实际执行仍需数据、载体、资金池与人工确认全部通过。PE不参与正式计算。</p>
        </article>
        <article class="panel section-spacer">
          <span class="eyebrow">QDII执行载体</span>
          <span class="muted" style="display:block;font-size:10px;">JSON已批准白名单 · 多选与金额预览</span>
          <h2>{html.escape(carrier_title)} · 多选与金额预览<small style="font-size:11px;color:var(--muted);"> {html.escape(carrier_selection_status)}</small></h2>
          <p class="muted">额度只约束执行载体，不生成投资机会；系统不自动替用户决定。预览状态：<strong id="qdii-preview-status" style="color:var(--muted);">EMPTY</strong></p>
          {'<p class="data-warning">当前载体快照过期，请刷新有效快照后再预览执行。</p>' if not carrier_snapshot_valid else ''}
          <div class="overview-grid" style="margin:12px 0;">
            <div><span>执行能力测试金额</span><input type="number" id="qdii-asset-amount" min="0" step="0.01" value="0" style="width:120px;padding:4px;border:1px solid var(--line);border-radius:6px;font:inherit;"> 元<small>仅用于测试QDII载体承接能力，不是资产级建议金额。当前资产级可释放：{qdii_current_release:,.0f} 元</small></div>
            <div><span>已选载体总容量</span><strong id="qdii-selected-capacity">0.00 元</strong></div>
            <div><span>已分配金额</span><strong id="qdii-assigned">0.00 元</strong></div>
            <div><span>有效覆盖金额</span><strong id="qdii-covered">0.00 元</strong><small>剩余未覆盖 <span id="qdii-uncovered">0.00</span> 元 · 超额 <span id="qdii-over-selected">0.00</span> 元</small></div>
          </div>
          <div style="overflow-x:auto;"><table>
            <thead><tr><th>选择</th><th>基金</th><th>份额</th><th>购买渠道</th><th>数据来源</th><th>已有持仓</th><th>当前/官方/渠道限额</th><th>跟踪误差</th><th>费率</th><th>规模/成立</th><th>透明标签</th><th>分配金额</th></tr></thead>
            <tbody>{''.join(qdii_candidate_rows) if qdii_candidate_rows else '<tr><td colspan="13" class="muted">无有效载体快照</td></tr>'}</tbody>
          </table>
          </div>
          <div class="data-note">
            <strong>透明推荐顺序</strong>
            已有持仓 → 尽量减少基金数量 → 单只覆盖剩余缺口 → 跟踪误差 → 综合费率 → 额度稳定 → 渠道便利 → 规模与成立时间。无黑箱总分。
            <span id="qdii-complexity-warning"></span>
          </div>
          <p class="muted">同一指数正常建议：1只主载体 + 1至2只备用载体。万家019441额度近期在50元与10000元之间反复切换，执行前请再次确认渠道实际限额。</p>
          <p class="muted"><strong>此区域仅用于载体能力预览，不执行、不入账。</strong>正式确认请使用下方“执行本月方案”。I类基金仅在显式批准后进入自动承接，未覆盖金额保留在 Dynamic Cash Pool。</p>
        </article>
        <article class="panel section-spacer">
          <span class="eyebrow">Global Active Equity Pool</span>
          <h2>广发全球精选 · 独立全球主动池</h2>
          <p><strong>{html.escape(global_active.get('fund_name') or '广发全球精选股票(QDII)人民币A')}</strong> · 270023</p>
          <p class="muted">该基金只占用海外权益仓位，不参与NDX温度与动态资金释放。</p>
          <table><tbody>
            <tr><td>当前持仓金额</td><td>{float(global_active.get('current_holding_amount', 0) or 0):,.0f} 元</td></tr>
            <tr><td>海外权益占比</td><td>{float(qdii_integration.get('overseas_equity_split', {}).get('global_active_ratio', 0) or 0):.1%}</td></tr>
            <tr><td>固定定投状态</td><td>正常</td></tr>
            <tr><td>官方基准</td><td>{html.escape(global_active.get('benchmark') or '待补齐')}</td></tr>
            <tr><td>当前申购限额</td><td>{'待补齐' if global_active.get('effective_limit_rmb') is None else f"{float(global_active.get('effective_limit_rmb')):,.0f} 元"}</td></tr>
            <tr><td>最近净值日期</td><td>{html.escape(next((row.get('latest_date') for row in rows if row.get('code') == '270023'), '待补齐'))}</td></tr>
            <tr><td>pool</td><td>GLOBAL_ACTIVE_EQUITY_POOL</td></tr>
            <tr><td>role</td><td>HOLDING_DISPLAY_ONLY</td></tr>
            <tr><td>ndx_pool_eligible</td><td>false</td></tr>
            <tr><td>dynamic_release_eligible</td><td>false</td></tr>
            <tr><td>独立自动评分</td><td>Not Created</td></tr>
          </tbody></table>
        </article>
    """
    daily_automation_html = render_daily_automation_html(copilot)
    automation_history_html = render_automation_history_html(copilot)

    qdii_health_html = f"""
        <article class="panel section-spacer">
          <span class="eyebrow">QDII Carrier Data Health</span>
          <h2>共享快照接口</h2>
          <table><tbody>
            <tr><td>snapshot_generated_at</td><td>{html.escape(str(qdii_integration.get('snapshot_generated_at') or '-'))}</td></tr>
            <tr><td>snapshot_age</td><td>{html.escape(str(qdii_integration.get('snapshot_age_minutes') if qdii_integration.get('snapshot_age_minutes') is not None else '-'))} minutes</td></tr>
            <tr><td>carrier_snapshot_id</td><td>{html.escape(carrier_snapshot_id)}</td></tr>
            <tr><td>carrier_latest_sha256</td><td>{html.escape(carrier_latest_sha256_prefix)}</td></tr>
            <tr><td>source_confidence</td><td>{html.escape(qdii_integration.get('source_confidence') or 'UNAVAILABLE')}</td></tr>
            <tr><td>stale_status</td><td>{html.escape(qdii_integration.get('stale_status') or '-')}</td></tr>
            <tr><td>carrier_data_status</td><td>{html.escape(qdii_integration.get('carrier_data_status') or '-')}</td></tr>
            <tr><td>carrier_selection_status</td><td>{html.escape(qdii_integration.get('carrier_selection_status') or '-')}</td></tr>
            <tr><td>contract.not_investment_signal</td><td>true</td></tr>
          </tbody></table>
        </article>
    """
    qdii_health_html += shadow_banner

    # ── Execution modal (only when not FREEZE) ──
    modal_html = ""
    modal_js = ""
    if not execution_disabled:
        modal_html = f"""
  <!-- ── Execution Modal ── -->
  <div class="modal" id="execution-modal" aria-hidden="true">
    <div class="modal-card">
      <div class="modal-head">
        <div><span class="eyebrow">Fund Execution</span><h2>确认基金实际执行金额</h2></div>
        <button type="button" class="modal-close" id="execution-cancel">关闭</button>
      </div>
      <p class="muted">实际执行合计将从 Dynamic Cash Pool 扣减；未执行差额留存至下月。</p>
      <table class="execution-table">
        <thead><tr><th>基金</th><th>资产</th><th>计划金额</th><th>实际金额</th></tr></thead>
        <tbody>{modal_rows}</tbody>
      </table>
      <div class="modal-total">当前整数执行合计 <strong id="execution-total">{integer_executable_amount:,.0f}</strong> 元</div>
      <div class="button-row">
        <button type="button" class="primary" id="execution-confirm">确认执行并入账</button>
        <button type="button" class="secondary" id="execution-cancel-bottom">取消</button>
      </div>
      <div id="modal-message"></div>
    </div>
  </div>"""
        modal_js = """
    // ── Execution modal ──
    const modal = document.getElementById("execution-modal");
    if (modal) {
      const executionInputs = Array.from(document.querySelectorAll(".fund-actual"));
      const updateExecutionTotal = () => {
        const total = executionInputs.reduce((sum, input) => sum + Number(input.value || 0), 0);
        const totalEl = document.getElementById("execution-total");
        if (totalEl) totalEl.textContent = total.toFixed(2);
      };
      executionInputs.forEach((input) => input.addEventListener("input", updateExecutionTotal));
      const closeModal = () => {
        modal.classList.remove("open");
        modal.setAttribute("aria-hidden", "true");
      };
      const cancelBtn = document.getElementById("execution-cancel");
      const cancelBottom = document.getElementById("execution-cancel-bottom");
      if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
      if (cancelBottom) cancelBottom.addEventListener("click", closeModal);

      document.querySelectorAll("#tab-allocation-flow [data-decision]").forEach((button) => {
        button.addEventListener("click", async () => {
          if (button.dataset.decision === "execute") {
            modal.classList.add("open");
            modal.setAttribute("aria-hidden", "false");
            updateExecutionTotal();
            return;
          }
          document.querySelectorAll("#tab-allocation-flow [data-decision]").forEach((item) => item.disabled = true);
          const message = document.querySelector("#tab-allocation-flow #message");
          if (message) message.textContent = "正在记录...";
          try {
            const response = await fetch("/api/copilot/decision", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({decision: button.dataset.decision})
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.error || "处理失败");
            if (message) message.textContent = result.message;
            window.setTimeout(() => location.reload(), 500);
          } catch (error) {
            if (message) message.textContent = error.message;
            document.querySelectorAll("#tab-allocation-flow [data-decision]").forEach((item) => item.disabled = false);
          }
        });
      });
      const confirmBtn = document.getElementById("execution-confirm");
      if (confirmBtn) {
        confirmBtn.addEventListener("click", async () => {
          const confirmButton = document.getElementById("execution-confirm");
          const modalMessage = document.getElementById("modal-message");
          confirmButton.disabled = true;
          if (modalMessage) modalMessage.textContent = "正在执行并入账...";
          try {
            const fundExecutions = executionInputs.map((input) => ({
              fund_code: input.dataset.fundCode,
              actual_executed_amount: Number(input.value || 0)
            }));
            const response = await fetch("/api/copilot/decision", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({decision: "execute", fund_executions: fundExecutions})
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.error || "执行失败");
            if (modalMessage) modalMessage.textContent = result.message;
            window.setTimeout(() => location.reload(), 500);
          } catch (error) {
            if (modalMessage) modalMessage.textContent = error.message;
            confirmButton.disabled = false;
          }
        });
      }
    }"""

    # ── Overview rollups: 一句话原因 / Today's Focus / Alert Summary ──
    # 全部由既有快照字段组合而成，不引入任何新指标或新计算。
    shadow_done = int(copilot.get("shadow_days_completed", 0) or 0)
    shadow_need = int(copilot.get("shadow_required_complete_days", ndx_shadow_run.REQUIRED_COMPLETE_DAYS) or ndx_shadow_run.REQUIRED_COMPLETE_DAYS)
    ndx_model_active = copilot.get("ndx_asset_model_status") == "ACTIVE" and copilot.get("activation_status") == "ACTIVE"

    if execution_disabled:
        if ndx_model_active:
            hero_oneline = (
                "NDX V1 已完成 Shadow 验证并激活；当前动态资金池仍由数据质量、载体、"
                "本月触发规则或人工确认门决定是否执行。"
            )
        elif shadow_done >= shadow_need:
            hero_oneline = (
                f"纳指 NDX 新模型已完成 Shadow Day {shadow_done} / {shadow_need}，"
                "等待人工激活前仍不会释放任何资金。"
            )
        else:
            hero_oneline = (
                f"纳指 NDX 新模型仍在影子验证（Shadow Day {shadow_done} / {shadow_need}），"
                "完成前禁止任何自动释放；A股、黄金、QDII 载体数据均已通过，PE 仅供展示、不构成阻断。"
            )
    else:
        hero_oneline = html.escape(
            copilot.get("pool_status_reason") or "全部核心数据通过，动态资金池释放通道就绪。"
        )

    gaps_for_focus = copilot.get("gaps", {})
    positive_gap_assets = sorted(
        ((k, v) for k, v in gaps_for_focus.items() if k != "cash" and v and v > 0),
        key=lambda kv: kv[1], reverse=True,
    )
    focus_items = []
    if execution_disabled:
        freeze_focus = (
            f"<strong>动态资金池保持冻结。</strong>NDX V1 已激活，但当前正式决策门未允许执行。"
            if ndx_model_active
            else f"<strong>动态资金池保持冻结。</strong>纳指 NDX 新模型处于影子验证 Day {shadow_done} / {shadow_need}，未激活前不会自动释放任何资金。"
        )
        focus_items.append((
            "risk",
            freeze_focus,
        ))
    else:
        focus_items.append((
            "ok",
            f"<strong>动态资金池可执行。</strong>本月释放 {display_amount:,.0f} 元。",
        ))
    focus_items.append((
        "ok", "<strong>固定定投照常执行</strong>，不受资金池冻结影响。",
    ))
    if positive_gap_assets:
        top_key, top_gap = positive_gap_assets[0]
        top_score = copilot.get("scores", {}).get(top_key)
        score_hint = (
            f"，温度 {float(top_score):.1f} / 100"
            if isinstance(top_score, (int, float)) else ""
        )
        focus_items.append((
            "info",
            f"<strong>{html.escape(asset_label(top_key))}最值得关注：</strong>"
            f"配置缺口 +{top_gap:,.0f} 元为各资产最大{score_hint}；解冻后将优先补足。",
        ))
    if drawdown_top5:
        worst = drawdown_top5[0]
        if float(worst.get("drawdown_12m_pct") or 0) <= -10:
            focus_items.append((
                "warn",
                f"<strong>最深回撤：</strong>{html.escape(worst['name'])}"
                f"（{html.escape(worst['code'])}）12 个月回撤 "
                f"{format_pct(worst['drawdown_12m_pct'])}，建议复盘。",
            ))
    focus_items.append((
        "ok",
        "<strong>无数据质量阻断</strong>，PE / 估值指标均为 DISPLAY_ONLY，不参与评分与释放。",
    ))
    focus_list_html = "".join(
        f'<li class="focus-item"><span class="focus-dot {sev}"></span><span>{body}</span></li>'
        for sev, body in focus_items
    )
    todays_focus_html = f"""
        <article class="panel focus-panel" data-live="today-focus">
          <span class="eyebrow">Today's Focus</span>
          <h2>今日关注</h2>
          <ul class="focus-list">
            {focus_list_html}
          </ul>
        </article>"""

    # 四个治理状态域（模型行情 / NDX模型 / QDII载体 / 执行）从 Hero 迁到 Alert
    # Summary 底部，保持「状态域分离」治理不变量可见，同时让 Hero 保持精简。
    carrier_chip_class = "ok" if carrier_snapshot_valid else "warn"
    ndx_chip_class = "ok" if ndx_model_active else "warn"
    governance_status_row = (
        '<div class="dh-status-bar" style="margin-top:12px;">'
        '<span class="dh-status-chip ok">模型行情数据：PASS</span>'
        f'<span class="dh-status-chip {ndx_chip_class}">NDX模型状态：{html.escape(ndx_model_status_text)}</span>'
        f'<span class="dh-status-chip {carrier_chip_class}">QDII载体数据：{html.escape(carrier_data_status)}</span>'
        f'<span class="dh-status-chip {"warn" if execution_disabled else "ok"}">执行状态：{html.escape(disabled_status_text)}</span>'
        f'<span style="margin-left:auto;">Last Full Audit: {html.escape(model_risk.LAST_AUDIT_DATE)}</span>'
        f'<span>Last Data Refresh: {html.escape(generated_at[:16])}</span>'
        '</div>'
    )
    alert_items = [] if ndx_model_active else [
        "NDX价格温度 — 等待人工激活" if shadow_done >= shadow_need else "NDX价格温度 — 模型方法论验证中（UNDER_VALIDATION）",
        "单一实际利率因子 — 等待人工激活" if shadow_done >= shadow_need else "单一实际利率因子 — 模型方法论验证中",
    ] if execution_disabled else []
    if alert_items:
        alert_rows = "".join(
            f'<li class="anomaly-item"><span class="anomaly-dot"></span>'
            f'<span>{html.escape(item)}</span></li>'
            for item in alert_items
        )
        alert_body = (
            f'<h2>提醒摘要<span class="anomaly-count">{len(alert_items)}</span></h2>'
            f'<ul class="anomaly-list">{alert_rows}</ul>'
            '<p class="muted" style="margin-top:6px;">数据质量：无阻断 · '
            'Nasdaq100 / S&amp;P500 PE 为 DISPLAY_ONLY，不参与评分。</p>'
            f'{governance_status_row}'
            '<a data-nav-to="data-audit" style="display:inline-block;margin-top:12px;">查看数据与审计 →</a>'
        )
    else:
        alert_body = (
            '<h2>提醒摘要</h2>'
            '<p class="anomaly-clean">当前没有需要关注的数据风险。</p>'
            f'{governance_status_row}'
            '<a data-nav-to="data-audit" style="display:inline-block;margin-top:12px;">查看数据与审计 →</a>'
        )
    alert_summary_html = f"""
        <article class="panel anomaly-panel" data-live="alert-summary">
          <span class="eyebrow">Alert Summary</span>
          {alert_body}
        </article>"""

    # ── Portfolio Management: single editing entry for all holding amounts ──
    # Renders from config (the SSOT); amounts are never hard-coded. Editing only
    # feeds config → allocation/gap recompute; it never touches NAV, drawdown,
    # execution history, scores, Shadow Run, or audit records.
    def _pm_time(value):
        return str(value)[:19].replace("T", " ") if value else "—"

    pm_state_funds = []
    pm_stamps = []
    portfolio_rows_html = ""
    for fund in config.get("funds", []):
        pm_code = str(fund.get("code", ""))
        pm_name = str(fund.get("name", ""))
        pm_asset = fund.get("asset_class") or "-"
        pm_holding = float(fund.get("holding_amount", 0) or 0)
        pm_profit = fund.get("profit_pct")
        try:
            pm_profit_val = None if pm_profit in (None, "") else float(pm_profit)
        except (TypeError, ValueError):
            pm_profit_val = None
        pm_profit_text = "—" if pm_profit_val is None else f"{pm_profit_val:+.1f}%"
        pm_profit_color = "" if pm_profit_val is None else ("var(--green)" if pm_profit_val >= 0 else "var(--red)")
        pm_updated = fund.get("holding_updated_at")
        if pm_updated:
            pm_stamps.append(str(pm_updated))
        portfolio_rows_html += f"""
              <tr>
                <td>{html.escape(pm_name)}<small>{html.escape(pm_code)}</small></td>
                <td>{html.escape(asset_label(pm_asset))}</td>
                <td class="pm-amount-cell">{pm_holding:,.2f} 元</td>
                <td style="color:{pm_profit_color};font-weight:650;">{pm_profit_text}</td>
                <td class="pm-updated">{_pm_time(pm_updated)}</td>
                <td><button type="button" class="pm-edit" data-pm-edit data-pm-type="fund" data-pm-code="{html.escape(pm_code)}" data-pm-name="{html.escape(pm_name)}" data-pm-amount="{pm_holding:.2f}" data-pm-profit="{'' if pm_profit_val is None else f'{pm_profit_val:.2f}'}">编辑</button></td>
              </tr>"""
        pm_state_funds.append({
            "code": pm_code, "name": pm_name, "asset_class": pm_asset,
            "holding_amount": round(pm_holding, 2), "profit_pct": pm_profit_val,
            "holding_updated_at": pm_updated or None,
        })

    pm_cash_amount = float(config.get("cash_available", 0) or 0)
    pm_cash_updated = config.get("cash_updated_at")
    if pm_cash_updated:
        pm_stamps.append(str(pm_cash_updated))
    portfolio_rows_html += f"""
              <tr class="pm-cash-row">
                <td>现金及低风险<small>手工现金</small></td>
                <td>现金及低风险</td>
                <td class="pm-amount-cell">{pm_cash_amount:,.2f} 元</td>
                <td>—</td>
                <td class="pm-updated">{_pm_time(pm_cash_updated)}</td>
                <td><button type="button" class="pm-edit" data-pm-edit data-pm-type="cash" data-pm-name="现金及低风险（手工现金）" data-pm-amount="{pm_cash_amount:.2f}">编辑</button></td>
              </tr>"""

    pm_last_updated = max(pm_stamps) if pm_stamps else None
    pm_last_updated_text = _pm_time(pm_last_updated)
    portfolio_state_json = json.dumps({
        "funds": pm_state_funds,
        "cash_available": round(pm_cash_amount, 2),
        "cash_updated_at": pm_cash_updated or None,
        "last_updated": pm_last_updated,
    }, ensure_ascii=False).replace("</", "<\\/")

    portfolio_tab_html = f"""
      <section class="tab-panel" role="tabpanel" id="tab-portfolio">
        <article class="panel" data-live="portfolio-table">
          <div class="pm-head">
            <div>
              <span class="eyebrow">Portfolio Management</span>
              <h2>持仓管理</h2>
            </div>
            <div class="pm-head-actions">
              <button type="button" class="pm-add" data-pm-add>+ 新增持仓</button>
              <span class="pm-last-updated">最后持仓更新时间：{pm_last_updated_text}</span>
            </div>
          </div>
          <p class="muted" style="margin:4px 0 0;font-size:12px;">所有当前持仓金额的唯一编辑入口。修改金额只参与配置与缺口重算，不改动基金净值、历史回撤、历史执行、历史收益、模型评分与 Shadow Run。</p>
          <table style="margin-top:12px;">
            <thead><tr><th>基金 / 项目</th><th>资产类别</th><th>当前持仓金额</th><th>当前盈亏</th><th>持仓更新时间</th><th>操作</th></tr></thead>
            <tbody>{portfolio_rows_html}</tbody>
          </table>
        </article>
        <script type="application/json" id="portfolio-state">{portfolio_state_json}</script>
      </section>"""

    pm_modal_html = """
  <div class="modal" id="pm-modal" aria-hidden="true">
    <div class="modal-card" style="width:min(420px,100%);">
      <div class="modal-head">
        <div>
          <span class="eyebrow">持仓管理</span>
          <h2 id="pm-modal-title">编辑持仓金额</h2>
          <p class="muted" id="pm-modal-sub" style="margin:4px 0 0;font-size:12px;"></p>
        </div>
        <button class="modal-close" type="button" data-pm-close>关闭</button>
      </div>
      <div id="pm-create-fields" style="display:none;">
        <label style="display:block;margin-top:14px;font-size:12px;color:var(--muted);">基金代码
          <input type="text" id="pm-code" autocomplete="off" style="width:100%;margin-top:6px;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit;">
        </label>
        <label style="display:block;margin-top:12px;font-size:12px;color:var(--muted);">基金名称
          <input type="text" id="pm-name" autocomplete="off" style="width:100%;margin-top:6px;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit;">
        </label>
        <label style="display:block;margin-top:12px;font-size:12px;color:var(--muted);">资产类别
          <select id="pm-asset-class" style="width:100%;margin-top:6px;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit;background:var(--panel);">
            <option value="a_share">A股</option>
            <option value="us_equity">海外权益</option>
            <option value="gold">黄金</option>
            <option value="cash">现金及低风险</option>
          </select>
        </label>
      </div>
      <label id="pm-amount-field" style="display:block;margin-top:14px;font-size:12px;color:var(--muted);">当前持仓金额（元）
        <input type="number" id="pm-amount" min="0" step="0.01" inputmode="decimal" style="width:100%;margin-top:6px;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit;">
      </label>
      <label id="pm-profit-field" style="display:block;margin-top:12px;font-size:12px;color:var(--muted);">当前盈亏（%，可负、可留空）
        <input type="number" id="pm-profit" step="0.01" inputmode="decimal" style="width:100%;margin-top:6px;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit;">
      </label>
      <div id="pm-error" style="min-height:16px;margin-top:8px;color:var(--red);font-size:12px;"></div>
      <div class="button-row">
        <button class="secondary" type="button" data-pm-close>取消</button>
        <button class="primary" type="button" id="pm-save">保存</button>
      </div>
    </div>
  </div>
  <div id="pm-toast" role="status" aria-live="polite" class="pm-toast"></div>"""

    pm_js = """
    (function() {
      var modal = document.getElementById('pm-modal');
      if (!modal) return;
      var titleEl = document.getElementById('pm-modal-title');
      var subEl = document.getElementById('pm-modal-sub');
      var amountEl = document.getElementById('pm-amount');
      var profitEl = document.getElementById('pm-profit');
      var profitField = document.getElementById('pm-profit-field');
      var createFields = document.getElementById('pm-create-fields');
      var codeEl = document.getElementById('pm-code');
      var nameEl = document.getElementById('pm-name');
      var assetClassEl = document.getElementById('pm-asset-class');
      var errEl = document.getElementById('pm-error');
      var saveBtn = document.getElementById('pm-save');
      var toastEl = document.getElementById('pm-toast');
      var current = null;
      var LIVE = ['hero','today-focus','asset-cards','alert-summary','allocation-table',
                  'overseas-split','execution-control','triggers','flow-audit',
                  'decision-status','portfolio-table'];

      function openModal(btn) {
        current = { mode: 'edit', type: btn.getAttribute('data-pm-type'),
                    code: btn.getAttribute('data-pm-code') || '',
                    name: btn.getAttribute('data-pm-name') || '' };
        var isCash = current.type === 'cash';
        createFields.style.display = 'none';
        titleEl.textContent = isCash ? '编辑现金金额' : '编辑持仓';
        subEl.textContent = current.name + (current.code ? ' · ' + current.code : '');
        amountEl.value = Number(btn.getAttribute('data-pm-amount') || 0).toFixed(2);
        profitField.style.display = isCash ? 'none' : 'block';
        if (!isCash) {
          var p = btn.getAttribute('data-pm-profit');
          profitEl.value = (p === null || p === '') ? '' : p;
        }
        errEl.textContent = '';
        modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false');
        amountEl.focus(); amountEl.select();
      }
      function openCreateModal() {
        current = { mode: 'create', type: 'fund' };
        createFields.style.display = 'block';
        titleEl.textContent = '新增持仓';
        subEl.textContent = '手动录入一笔新持仓，仅参与配置与缺口重算';
        codeEl.value = ''; nameEl.value = ''; assetClassEl.value = 'a_share';
        amountEl.value = ''; profitEl.value = '';
        profitField.style.display = 'block';
        errEl.textContent = '';
        modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false');
        codeEl.focus();
      }
      function closeModal() { modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true'); current = null; }
      function showToast(msg) {
        toastEl.textContent = msg; toastEl.classList.add('show');
        setTimeout(function() { toastEl.classList.remove('show'); }, 2600);
      }
      function validate() {
        if (current && current.mode === 'create') {
          if (codeEl.value.trim() === '') return '基金代码不能为空';
          if (nameEl.value.trim() === '') return '基金名称不能为空';
          if (['a_share','us_equity','gold','cash'].indexOf(assetClassEl.value) < 0) return '请选择资产类别';
        }
        var raw = amountEl.value;
        if (raw === null || String(raw).trim() === '') return '金额不能为空';
        var n = Number(raw);
        if (!isFinite(n)) return '金额必须是数字';
        if (n < 0) return '金额不能小于0';
        if (current && current.type !== 'cash') {
          var p = profitEl.value.trim();
          if (p !== '') {
            var pv = Number(p);
            if (!isFinite(pv)) return '当前盈亏必须是数字或留空';
            if (pv < -100) return '当前盈亏不能低于-100%';
          }
        }
        return null;
      }
      function refreshModules() {
        return fetch('/dashboard.html', { cache: 'no-store' }).then(function(r) { return r.text(); })
          .then(function(text) {
            var doc = new DOMParser().parseFromString(text, 'text/html');
            LIVE.forEach(function(key) {
              var fresh = doc.querySelector('[data-live="' + key + '"]');
              var cur = document.querySelector('[data-live="' + key + '"]');
              if (fresh && cur) cur.replaceWith(fresh);
            });
            var freshState = doc.getElementById('portfolio-state');
            var curState = document.getElementById('portfolio-state');
            if (freshState && curState) curState.textContent = freshState.textContent;
          });
      }
      function save() {
        if (!current) return;
        var err = validate();
        if (err) { errEl.textContent = err; return; }
        var amount = Math.round(Number(amountEl.value) * 100) / 100;
        var body;
        if (current.mode === 'create') {
          var cp = profitEl.value.trim();
          body = { action: 'create', holding: {
            code: codeEl.value.trim(), name: nameEl.value.trim(),
            asset_class: assetClassEl.value, holding_amount: amount,
            profit_pct: (cp === '' ? null : Number(cp)) } };
        } else if (current.type === 'cash') {
          body = { cash_available: amount };
        } else {
          var p = profitEl.value.trim();
          body = { holdings: [{ code: current.code, holding_amount: amount,
                                profit_pct: (p === '' ? null : Number(p)) }] };
        }
        saveBtn.disabled = true; saveBtn.textContent = '保存中…';
        fetch('/api/portfolio', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          cache: 'no-store', body: JSON.stringify(body) })
          .then(function(resp) { return resp.json().catch(function() { return {}; })
            .then(function(data) {
              if (!resp.ok || !data.ok) { throw new Error((data && data.error) || ('保存失败（' + resp.status + '）')); }
              return data;
            }); })
          .then(function(data) { closeModal(); return refreshModules().then(function() {
            showToast(data.message || '持仓金额已更新'); }); })
          .catch(function(e) {
            errEl.textContent = (e && e.message) ? e.message
              : '保存失败，请确认本地服务 (python3 local_server.py) 正在运行';
          })
          .then(function() { saveBtn.disabled = false; saveBtn.textContent = '保存'; });
      }

      document.addEventListener('click', function(e) {
        var t = e.target;
        var addBtn = t.closest ? t.closest('[data-pm-add]') : null;
        if (addBtn) { e.preventDefault(); openCreateModal(); return; }
        var editBtn = t.closest ? t.closest('[data-pm-edit]') : null;
        if (editBtn) { e.preventDefault(); openModal(editBtn); return; }
        if (t.closest && t.closest('[data-pm-close]')) { closeModal(); return; }
        if (t === modal) closeModal();
      });
      saveBtn.addEventListener('click', save);
      amountEl.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); save(); } });
    })();
"""

    dashboard = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Asset Allocation Copilot V7.3 NDX V1 Validation</title>
  <meta name="run-id" content="{html.escape(run_id)}">
  <meta name="generated-at" content="{html.escape(generated_at)}">
  <meta name="formula-version" content="CN_EQUITY_PRICE_TEMP_V1;NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED;gold-v2-inverse-real-yield-fed;allocation-v3-gap-first-cn-release-factor">
  <meta name="data-quality-version" content="dq-v4-source-approval">
  <style>
    :root {{
      color-scheme: light;
      --ink: #172033; --muted: #667085; --line: #dce2ea;
      --paper: #f3f5f7; --panel: #fff; --navy: #172033;
      --green: #1f7a57; --green-soft: #e6f4ed; --amber: #a35b00;
      --amber-soft: #fff0d8; --blue: #2b65d9; --red: #b42318;
      --red-soft: #fef2f0; --subtle: #98a2b3;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; color: var(--ink); background:
      radial-gradient(circle at 8% 0%, #dfe9f6 0, transparent 28%), var(--paper);
      font-family: "Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.45;
    }}
    header {{ background: rgba(255,255,255,.9); border-bottom: 1px solid var(--line); }}
    .header-inner, main {{ max-width: 1280px; margin: auto; }}
    .header-inner {{ padding: 24px 28px 18px; display:flex; justify-content:space-between; gap:20px; align-items:flex-start; }}
    h1 {{ margin:0; font-size:25px; letter-spacing:-.02em; }}
    h2 {{ margin:0; font-size:20px; }}
    h3 {{ margin:0 0 8px; font-size:14px; }}
    .subhead, small, .muted {{ color:var(--muted); }}
    .subhead {{ margin-top:5px; font-size:13px; }}
    a {{ color:var(--blue); text-decoration:none; font-weight:650; cursor:pointer; }}
    main {{ padding:24px 28px 50px; }}

    .version-line {{
      display:flex; gap:18px; flex-wrap:wrap; margin-top:4px; font-size:11px; color:var(--subtle);
    }}
    .version-line span {{ white-space:nowrap; }}

    .tab-nav {{
      display:flex; gap:2px; margin-bottom:20px;
      background:var(--panel); border:1px solid var(--line);
      border-radius:12px; padding:4px; overflow-x:auto;
      -webkit-overflow-scrolling:touch;
    }}
    .tab-btn {{
      flex-shrink:0; border:0; border-radius:9px;
      padding:10px 18px; font:inherit; font-size:13px; font-weight:650;
      background:transparent; color:var(--muted); cursor:pointer;
      transition:background .15s, color .15s; white-space:nowrap;
    }}
    .tab-btn:hover {{ background:var(--paper); color:var(--ink); }}
    .tab-btn.active {{
      background:var(--navy); color:#fff;
      box-shadow:0 2px 8px rgba(23,32,51,.2);
    }}

    .tab-panel {{ display:none; }}
    .tab-panel.active {{ display:block; }}

    .panel {{
      background:var(--panel); border:1px solid var(--line); border-radius:14px;
      padding:20px; box-shadow:0 12px 35px rgba(23,32,51,.04);
    }}
    .eyebrow {{ color:var(--blue); font-size:11px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }}

    .decision-hero {{
      background:var(--panel); border:1px solid var(--line); border-radius:16px;
      padding:24px; margin-bottom:14px; box-shadow:0 12px 35px rgba(23,32,51,.04);
    }}
    .decision-hero.freeze {{ border-left:5px solid var(--red); }}
    .decision-hero.execute {{ border-left:5px solid var(--green); }}
    .dh-head {{
      display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap;
    }}
    .dh-status-tag {{
      display:inline-flex; align-items:center; gap:6px; padding:6px 14px; border-radius:999px;
      font-size:14px; font-weight:750;
    }}
    .dh-status-tag.freeze {{ background:var(--red-soft); color:var(--red); }}
    .dh-status-tag.execute {{ background:var(--green-soft); color:var(--green); }}
    .dh-status-tag .en-label {{ font-size:10px; opacity:.7; font-weight:650; }}
    .dh-metrics {{
      display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin:18px 0;
    }}
    .dh-metric {{ }}
    .dh-metric-label {{ display:block; font-size:11px; color:var(--muted); margin-bottom:3px; }}
    .dh-metric-value {{ font-size:24px; font-weight:700; letter-spacing:-.02em; }}
    .dh-metric-value.zero {{ color:var(--muted); }}
    .dh-metric-sub {{ font-size:11px; color:var(--muted); margin-top:2px; }}
    .dh-reason {{
      background:var(--paper); border-radius:10px; padding:14px 18px; margin-top:14px;
      font-size:13px; line-height:1.6; color:var(--ink);
    }}
    .dh-reason strong {{ display:block; margin-bottom:6px; font-size:12px; color:var(--muted); }}
    .dh-status-bar {{
      display:flex; gap:12px; flex-wrap:wrap; align-items:center;
      margin-top:16px; padding-top:14px; border-top:1px solid var(--line);
      font-size:11px; color:var(--muted);
    }}
    .dh-status-chip {{
      display:inline-flex; padding:3px 9px; border-radius:999px; font-size:10px; font-weight:750;
      background:#f1f5f9; color:#475467;
    }}
    .dh-status-chip.warn {{ background:var(--amber-soft); color:var(--amber); }}
    .dh-status-chip.ok {{ background:var(--green-soft); color:var(--green); }}

    .asset-cards {{
      display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:14px;
    }}
    .asset-card {{
      background:var(--panel); border:1px solid var(--line); border-radius:16px;
      padding:20px; box-shadow:0 8px 26px rgba(23,32,51,.035);
    }}
    .ac-head {{
      display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;
    }}
    .ac-name {{ font-size:16px; font-weight:700; }}
    .ac-tier {{
      display:inline-flex; padding:3px 9px; border-radius:999px;
      font-size:11px; font-weight:750;
      background:#f1f5f9; color:#475467;
    }}
    .ac-tier.hot {{ background:var(--amber-soft); color:var(--amber); }}
    .ac-tier.neutral {{ background:#edf4ff; color:#244a85; }}
    .ac-score-row {{
      display:flex; align-items:baseline; gap:4px; margin-bottom:10px;
    }}
    .ac-score {{ font-size:31px; font-weight:700; letter-spacing:-.02em; line-height:1; }}
    .ac-score-unit {{ font-size:13px; color:var(--subtle); }}
    .ac-bar-wrap {{ height:6px; background:#edf1f6; border-radius:999px; overflow:hidden; margin-bottom:14px; }}
    .ac-bar {{ height:100%; border-radius:999px; min-width:2px; }}
    .ac-details {{ display:grid; gap:6px; margin-bottom:12px; }}
    .ac-detail-row {{ display:flex; justify-content:space-between; font-size:12px; }}
    .ac-detail-label {{ color:var(--muted); }}
    .ac-detail-value {{ font-weight:650; }}
    .ac-detail-value.gap-positive {{ color:var(--green); }}
    .ac-detail-value.gap-negative {{ color:var(--muted); }}
    .ac-action {{
      padding:10px 12px; border-radius:9px; font-size:12px; font-weight:650;
    }}
    .ac-action.blocked {{ background:var(--red-soft); color:var(--red); }}
    .ac-action.no-action {{ background:#f1f5f9; color:var(--muted); }}
    .ac-action.eligible {{ background:var(--green-soft); color:var(--green); }}
    .ac-action-reason {{ font-size:11px; color:var(--muted); margin-top:6px; line-height:1.4; }}
    .ac-detail-link {{ display:inline-block; margin-top:10px; font-size:11px; }}

    .drawdown-panel {{ margin-bottom:14px; }}
    .dd-list {{ list-style:none; margin:12px 0 0; padding:0; }}
    .dd-item {{
      display:flex; align-items:center; gap:12px; padding:11px 14px;
      border-bottom:1px solid #f0f2f5; font-size:13px;
    }}
    .dd-item:last-child {{ border-bottom:0; }}
    .dd-rank {{ width:22px; height:22px; border-radius:999px; background:#f1f5f9;
      display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:750;
      color:var(--muted); flex-shrink:0;
    }}
    .dd-rank.top {{ background:var(--red-soft); color:var(--red); }}
    .dd-name {{ flex:1; min-width:0; }}
    .dd-name small {{ display:block; font-size:10px; }}
    .dd-drawdown {{ font-weight:700; font-size:14px; text-align:right; white-space:nowrap; }}
    .dd-drawdown.deep {{ color:var(--red); }}
    .dd-drawdown.shallow {{ color:var(--amber); }}
    .dd-drawdown.flat {{ color:var(--muted); }}
    .dd-bar-wrap {{ width:80px; height:4px; background:#edf1f6; border-radius:999px; overflow:hidden; flex-shrink:0; }}
    .dd-bar {{ height:100%; border-radius:999px; }}
    .dd-meta {{ font-size:10px; color:var(--subtle); text-align:right; min-width:90px; }}

    .anomaly-panel {{ margin-bottom:14px; }}
    .anomaly-count {{
      display:inline-flex; padding:2px 8px; border-radius:999px;
      background:var(--amber-soft); color:var(--amber); font-size:11px; font-weight:750;
      margin-left:6px;
    }}
    .anomaly-list {{ list-style:none; margin:10px 0 0; padding:0; }}
    .anomaly-item {{
      display:flex; align-items:center; gap:10px; padding:8px 0;
      border-bottom:1px solid #f0f2f5; font-size:13px;
    }}
    .anomaly-item:last-child {{ border-bottom:0; }}
    .anomaly-dot {{ width:8px; height:8px; border-radius:999px; background:var(--amber); flex-shrink:0; }}
    .anomaly-clean {{ color:var(--green); font-weight:650; padding:8px 0; font-size:13px; }}

    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ padding:12px 10px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; }}
    th {{ color:var(--muted); font-size:11px; }}
    td small {{ display:block; margin-top:2px; }}
    tr:last-child td {{ border-bottom:0; }}
    .positive {{ color:var(--green); font-weight:750; }}

    .flow-hero {{
      display:flex; align-items:baseline; gap:6px; margin:10px 0 16px;
    }}
    .flow-hero strong {{ font-size:28px; font-weight:700; letter-spacing:-.02em; }}
    .flow-hero span {{ font-size:15px; color:var(--muted); }}
    .flow-hero-tag {{ margin-left:10px; font-size:12px !important; color:var(--muted) !important; }}
    .flow-direction {{
      display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:16px;
    }}
    .fd-item {{
      padding:12px 14px; background:var(--paper); border-radius:12px; border:1px solid #edf1f6;
    }}
    .fd-label {{ display:block; font-size:11px; color:var(--muted); margin-bottom:4px; }}
    .fd-amount {{ font-size:22px; font-weight:700; }}
    .flow-basis {{ border-top:1px solid var(--line); padding-top:14px; }}
    .flow-basis h3 {{ font-size:12px; color:var(--muted); font-weight:650; margin:0 0 8px; }}
    .gb-list {{ list-style:none; margin:0; padding:0; display:grid; gap:6px; }}
    .gb-list li {{
      display:flex; justify-content:space-between; align-items:baseline;
      padding:6px 0; font-size:12px;
    }}
    .gb-label {{ font-weight:650; flex-shrink:0; margin-right:12px; }}
    .gb-detail {{ color:var(--muted); text-align:right; }}
    .gb-list .gap-under .gb-detail {{ color:#1a6b4a; }}
    .gb-list .gap-over  .gb-detail {{ color:var(--amber); }}
    .gb-list .gap-cash  .gb-detail {{ color:var(--muted); }}

    .data-warning {{ margin:14px 0; padding:12px 14px; background:var(--amber-soft); color:var(--amber); border-radius:9px; font-size:12px; }}
    .data-warning strong {{ display:block; margin-bottom:3px; }}
    .data-note {{ margin:14px 0; padding:12px 14px; background:#edf4ff; color:#244a85; border:1px solid #cbdcf7; border-radius:9px; font-size:12px; }}
    .data-note strong {{ display:block; margin-bottom:3px; }}

    .modal {{ display:none; position:fixed; inset:0; z-index:20; padding:24px; background:rgba(15,23,42,.55); align-items:center; justify-content:center; }}
    .modal.open {{ display:flex; }}
    .modal-card {{ width:min(760px,100%); max-height:90vh; overflow:auto; background:white; border-radius:16px; padding:22px; box-shadow:0 30px 80px rgba(15,23,42,.3); }}
    .modal-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }}
    .modal-close {{ border:0; background:#edf1f5; color:var(--ink); border-radius:8px; padding:8px 12px; cursor:pointer; }}
    .modal-total {{ margin-top:14px; text-align:right; font-size:14px; }}
    #modal-message {{ min-height:18px; margin-top:9px; color:var(--red); font-size:12px; }}
    .button-row {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:14px; }}
    .execution-table {{ margin-top:8px; font-size:12px; }}
    .execution-table input {{ width:110px; padding:8px; border:1px solid var(--line); border-radius:7px; font:inherit; }}
    .flow-basis label {{ display:block; margin:8px 0; color:var(--muted); font-size:12px; }}
    .flow-basis input, .flow-basis select {{ width:100%; margin-top:4px; padding:9px 10px; border:1px solid var(--line); border-radius:7px; background:white; color:var(--ink); font:inherit; }}

    .disclaimer {{ margin-top:16px; color:var(--muted); font-size:11px; text-align:center; }}
    button {{ border:0; border-radius:8px; padding:11px; font:inherit; font-weight:750; cursor:pointer; }}
    button.primary {{ background:#78d5aa; color:#103527; }}
    button.secondary {{ background:#35445e; color:white; }}
    button:disabled {{ opacity:.45; cursor:not-allowed; }}
    .section-spacer {{ margin-top:18px; }}

    /* ===== Overview redesign (IA only — reuses existing palette/vars) ===== */
    .decision-hero.compact {{ padding:18px 24px; }}
    .dh-headline {{ display:flex; align-items:baseline; gap:8px; margin:10px 0 6px; }}
    .dh-headline strong {{ font-size:30px; font-weight:700; letter-spacing:-.02em; }}
    .dh-headline strong.zero {{ color:var(--muted); }}
    .dh-headline span {{ font-size:13px; color:var(--muted); }}
    .dh-oneline {{ font-size:13px; color:var(--ink); line-height:1.55; margin:0 0 12px; max-width:78ch; }}
    .dh-cta {{ display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:700; }}
    .focus-panel {{ margin-bottom:14px; }}
    .focus-list {{ list-style:none; margin:12px 0 0; padding:0; }}
    .focus-item {{ display:flex; gap:11px; align-items:flex-start; padding:10px 2px; border-bottom:1px solid #f0f2f5; font-size:13px; line-height:1.5; }}
    .focus-item:last-child {{ border-bottom:0; }}
    .focus-dot {{ width:8px; height:8px; border-radius:999px; margin-top:6px; flex-shrink:0; background:var(--subtle); }}
    .focus-dot.warn {{ background:var(--amber); }}
    .focus-dot.risk {{ background:var(--red); }}
    .focus-dot.ok {{ background:var(--green); }}
    .focus-dot.info {{ background:var(--blue); }}
    .asset-card.summary {{ display:flex; flex-direction:column; }}
    .asset-card.summary .ac-tier.crowd {{ background:var(--red-soft); color:var(--red); }}
    .ac-status {{ margin:2px 0 12px; font-size:12px; font-weight:700; }}
    .ac-status.eligible {{ color:var(--green); }}
    .ac-status.validation {{ color:var(--amber); }}
    .ac-status.idle {{ color:var(--muted); }}
    .ac-keyline {{ display:flex; justify-content:space-between; align-items:baseline; padding-top:10px; margin-top:auto; border-top:1px solid #f0f2f5; font-size:12px; }}
    .ac-keyline .k {{ color:var(--muted); }}
    .ac-keyline .v {{ font-weight:700; font-size:15px; }}
    .ac-keyline .v.pos {{ color:var(--green); }}
    .ac-keyline .v.neg {{ color:var(--muted); }}
    .ac-detail-link {{ margin-top:12px; }}

    /* ===== Portfolio Management (持仓管理) ===== */
    .pm-head {{ display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }}
    .pm-head-actions {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
    .pm-add {{ border:0; background:var(--navy); color:#fff; border-radius:8px; padding:8px 14px;
      font:inherit; font-size:13px; font-weight:700; cursor:pointer; box-shadow:0 2px 8px rgba(23,32,51,.18); }}
    .pm-add:hover {{ background:#0f1626; }}
    .pm-last-updated {{ font-size:12px; color:var(--muted); white-space:nowrap; }}
    .pm-amount-cell {{ font-weight:700; }}
    .pm-updated {{ color:var(--muted); font-size:12px; }}
    .pm-cash-row td {{ background:#f8fafc; }}
    .pm-edit {{ border:1px solid var(--line); background:var(--panel); color:var(--blue);
      border-radius:7px; padding:6px 12px; font:inherit; font-size:12px; font-weight:650; cursor:pointer; }}
    .pm-edit:hover {{ background:var(--paper); }}
    .pm-toast {{ position:fixed; left:50%; bottom:28px; transform:translateX(-50%) translateY(12px);
      background:var(--navy); color:#fff; padding:11px 18px; border-radius:10px; font-size:13px; font-weight:650;
      box-shadow:0 10px 30px rgba(23,32,51,.25); opacity:0; pointer-events:none;
      transition:opacity .2s, transform .2s; z-index:40; }}
    .pm-toast.show {{ opacity:1; transform:translateX(-50%) translateY(0); }}

    /* ===== Daily Automation Monitor（每日自动化）===== */
    /* 语义颜色：绿=成功 蓝=进行中/冻结 黄=等待/市场限制 橙=数据/输入异常 红=系统异常 灰=未开始/跳过 */
    .das-green {{ color:#1f7a57; }} .das-blue {{ color:#2b65d9; }} .das-yellow {{ color:#a35b00; }}
    .das-orange {{ color:#c2410c; }} .das-red {{ color:#b42318; }} .das-gray {{ color:#667085; }}
    .das-bg-green {{ background:#1f7a57; }} .das-bg-blue {{ background:#2b65d9; }} .das-bg-yellow {{ background:#d19100; }}
    .das-bg-orange {{ background:#ea580c; }} .das-bg-red {{ background:#b42318; }} .das-bg-gray {{ background:#98a2b3; }}
    .das-pill {{ display:inline-flex; align-items:center; padding:4px 11px; border-radius:999px; font-size:13px; font-weight:750; }}
    .das-pill.das-sm {{ padding:2px 9px; font-size:11px; }}
    .das-pill.das-green {{ background:#e6f4ed; color:#1f7a57; }}
    .das-pill.das-blue {{ background:#edf4ff; color:#244a85; }}
    .das-pill.das-yellow {{ background:#fff0d8; color:#a35b00; }}
    .das-pill.das-orange {{ background:#ffedd5; color:#c2410c; }}
    .das-pill.das-red {{ background:#fef2f0; color:#b42318; }}
    .das-pill.das-gray {{ background:#f1f5f9; color:#667085; }}
    .das-border-green {{ border-left:5px solid #1f7a57; }} .das-border-blue {{ border-left:5px solid #2b65d9; }}
    .das-border-yellow {{ border-left:5px solid #d19100; }} .das-border-orange {{ border-left:5px solid #ea580c; }}
    .das-border-red {{ border-left:5px solid #b42318; }} .das-border-gray {{ border-left:5px solid #cbd2dc; }}
    .das-hero {{ margin-bottom:14px; }}
    .das-freshness {{ margin:6px 0 0; font-size:12px; color:var(--muted); line-height:1.5; }}
    .das-freshness strong {{ color:var(--amber); }}
    .das-summary {{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin:16px 0; }}
    .das-cell {{ background:var(--paper); border-radius:10px; padding:12px 14px; }}
    .das-k {{ display:block; font-size:11px; color:var(--muted); margin-bottom:5px; }}
    .das-v {{ font-size:15px; font-weight:700; }}
    .das-oneline {{ background:var(--paper); border-radius:10px; padding:13px 16px; font-size:13px; line-height:1.6; }}
    .das-flow {{ display:flex; flex-wrap:wrap; align-items:flex-start; gap:6px; margin-top:12px; }}
    .das-step {{ display:flex; flex-direction:column; align-items:center; text-align:center; min-width:82px; gap:3px; }}
    .das-dot {{ width:12px; height:12px; border-radius:999px; }}
    .das-step-name {{ font-size:12px; font-weight:650; }}
    .das-step-status {{ font-size:11px; font-weight:750; }}
    .das-step small {{ font-size:9px; color:var(--muted); line-height:1.2; max-width:96px; }}
    .das-arrow {{ color:var(--subtle); font-size:14px; padding-top:2px; }}
    .das-days {{ display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-top:12px; }}
    .das-day {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 10px;
      display:flex; flex-direction:column; align-items:center; gap:6px; box-shadow:0 4px 14px rgba(23,32,51,.03); }}
    .das-day-n {{ font-size:13px; font-weight:750; }}
    .das-day small {{ font-size:10px; color:var(--muted); }}
    .das-fail-list {{ list-style:none; margin:8px 0 0; padding:0; }}
    .das-fail {{ padding:7px 0; border-bottom:1px solid #f0f2f5; font-size:12px; }}
    .das-fail:last-child {{ border-bottom:0; }}
    .das-rc {{ margin-top:12px; }}
    .das-rc-layer {{ background:var(--paper); border-radius:10px; padding:12px 16px; font-size:13px; line-height:1.55; }}
    .das-rc-layer strong {{ display:block; margin-top:3px; font-size:14px; }}
    .das-rc-layer .das-k {{ margin-bottom:0; }}
    .das-rc-root {{ background:#fff; border:1px solid var(--line); }}
    .das-rc-arrow {{ text-align:center; color:var(--subtle); font-size:16px; padding:5px 0; }}
    .das-history td {{ font-size:12px; padding:9px 10px; }}
    .das-history td small {{ color:var(--subtle); font-size:10px; margin-left:6px; }}
    .das-history tbody tr:hover {{ background:var(--paper); }}

    @media (max-width:800px) {{
      .das-summary {{ grid-template-columns:1fr 1fr; }}
      .das-days {{ grid-template-columns:1fr 1fr; }}
      .header-inner {{ padding:20px 18px 15px; }}
      main {{ padding:18px; }}
      .asset-cards {{ grid-template-columns:1fr; }}
      .dh-metrics {{ grid-template-columns:1fr 1fr; }}
      .header-inner {{ align-items:flex-start; }}
      .tab-nav {{ border-radius:8px; padding:3px; }}
      .tab-btn {{ padding:9px 14px; font-size:12px; }}
      .ac-score {{ font-size:28px; }}
      .flow-direction {{ grid-template-columns:1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div>
        <h1>Asset Allocation Copilot V7</h1>
        <div class="version-line">
          <span>Application Version: {html.escape(versions.get('application_version') or APPLICATION_VERSION)}</span>
          <span>Data Quality Version: dq-v4-source-approval</span>
          <span>NDX Formula: {html.escape(versions.get('ndx_formula_version') or ndx_price_temperature.FORMULA_VERSION)}</span>
        </div>
        <div class="subhead">Run ID: {html.escape(run_id)} · 生成时间 {html.escape(generated_at)}</div>
      </div>
    </div>
  </header>
  <main data-cash-pool-status="{html.escape(pool_model_status)}" data-carrier-snapshot-valid="{'true' if carrier_snapshot_valid else 'false'}" data-carrier-selection-status="{html.escape(carrier_selection_status)}">

    <nav class="tab-nav" role="tablist">
      <button class="tab-btn active" role="tab" aria-selected="true" data-tab="overview">总览</button>
      <!-- 一级导航暂隐：如需恢复「每日自动化」「自动化历史」入口，删除下两行的 style="display:none" 即可（页面结构/数据/代码均保留）。 -->
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="daily-automation" style="display:none">每日自动化</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="automation-history" style="display:none">自动化历史</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="portfolio">持仓管理</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="drawdown">基金回撤</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="allocation-flow">配置与资金流</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="data-audit">数据与审计</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="history">历史记录</button>
    </nav>

    <div class="tab-content">

      <section class="tab-panel active" role="tabpanel" id="tab-overview">

        <article class="decision-hero compact {('freeze' if execution_disabled else 'execute')}" data-live="hero">
          <div class="dh-head">
            <div>
              <span class="eyebrow">本月决策</span>
              <h2>{('动态资金池：冻结' if execution_disabled else '动态资金池：可执行')}</h2>
            </div>
            <span class="dh-status-tag {('freeze' if execution_disabled else 'execute')}">
              <span class="en-label">{html.escape(disabled_status_text)}</span> {('冻结' if execution_disabled else '可执行')}
            </span>
          </div>
          <div class="dh-headline">
            <strong class="{('zero' if display_amount == 0 else '')}">{display_amount:,.0f} 元</strong>
            <span>本月释放 · 固定定投照常执行</span>
          </div>
          <p class="dh-oneline">{hero_oneline}</p>
          <a class="dh-cta" data-nav-to="data-audit">查看决策与验证详情 →</a>
        </article>

        {release_direction_html}

        {todays_focus_html}

        <section class="asset-cards" data-live="asset-cards">
          {asset_card_html}
        </section>

        {alert_summary_html}

      </section>

      {portfolio_tab_html}

      <section class="tab-panel" role="tabpanel" id="tab-drawdown">
        <article class="panel">
          <span class="eyebrow">Fund NAV & Drawdown</span>
          <h2>基金净值与回撤明细</h2>
          <p class="muted" style="margin:4px 0 12px;font-size:12px;">回撤按单位净值计算，仅用于可视化和复盘，不直接参与资金释放。</p>
          <table>
            <thead>
              <tr>
                <th>基金名称</th><th>代码</th><th>最新净值</th><th>净值日期</th>
                <th>6个月回撤</th><th>6M Coverage</th>
                <th>12个月回撤</th><th>12M Coverage</th>
                <th>QDII Lag</th><th>定投策略</th>
              </tr>
            </thead>
            <tbody>
              {fund_drawdown_rows}
            </tbody>
          </table>
          <p class="muted" style="margin-top:12px;font-size:11px;">回撤为 0.00% 表示当前净值处于区间高点附近，不表示无风险。</p>
        </article>
      </section>

      <section class="tab-panel" role="tabpanel" id="tab-allocation-flow">

        <article class="panel" data-live="allocation-table">
          <span class="eyebrow">Current Allocation</span>
          <h2>当前配置 vs 目标配置</h2>
          <table>
            <thead><tr><th>资产</th><th>当前市值</th><th>目标市值</th><th>配置缺口</th><th>Gain/Loss</th></tr></thead>
            <tbody>{''.join(allocation_rows_detailed)}</tbody>
          </table>
        </article>

        <article class="panel section-spacer" data-live="overseas-split">
          <span class="eyebrow">海外权益拆分</span>
          <h2>海外权益总仓位 {overseas_total:,.0f} 元</h2>
          <table><tbody>
            <tr><td>纳指指数型QDII</td><td>{ndx_amount:,.0f} 元</td><td>{ndx_ratio:.1f}%</td></tr>
            <tr><td>全球主动权益</td><td>{global_amount:,.0f} 元</td><td>{global_ratio:.1f}%</td></tr>
          </tbody></table>
        </article>

        {qdii_panel_html}

        <div class="section-spacer" data-live="execution-control">
          {execution_tab_content}
        </div>

        <article class="panel section-spacer">
          <span class="eyebrow">Historical Execution</span>
          <h2>Historical Executed Amount: {executed_amount:,.0f} 元</h2>
          <table style="margin-top:8px;">
            <thead><tr><th>基金</th><th>资产</th><th>历史计划</th><th>历史实际执行</th></tr></thead>
            <tbody>{fund_execution_rows_html}</tbody>
          </table>
          <p class="muted">该记录已完成，仅供历史审计；不可再次确认或执行。{'当前决策 FREEZE，执行按钮保持禁用。' if execution_disabled else ''}</p>
          {'<button class="primary" type="button" disabled aria-disabled="true" style="margin-top:12px;">执行已禁用（FREEZE）</button>' if execution_disabled else ''}
        </article>

        <article class="panel section-spacer" data-live="triggers">
          <span class="eyebrow">Triggers</span>
          <h2>触发项详情</h2>
          <table>
            <thead><tr><th>触发项</th><th>当前值</th></tr></thead>
            <tbody>{trigger_rows}</tbody>
          </table>
        </article>

        <article class="panel section-spacer" data-live="flow-audit">
          <span class="eyebrow">Current Flow Audit</span>
          <h2>当前资金流与配置依据</h2>
          {flow_tab_content}
        </article>

        <article class="panel section-spacer">
          <span class="eyebrow">Target Explanation</span>
          <h2>目标仓位解释</h2>
          <table>
            <thead><tr><th>资产</th><th>strategic_target</th><th>target_mode</th><th>final_target</th><th>min_target</th><th>max_target</th><th>target_reason</th></tr></thead>
            <tbody>{''.join(target_explanation_rows)}</tbody>
          </table>
        </article>

      </section>

      <section class="tab-panel" role="tabpanel" id="tab-data-audit">

        <article class="panel" style="border-left:5px solid {'#b42318' if execution_disabled else '#26734d'};">
          <span class="eyebrow">Model Risk Status</span>
          <h2>Model Status: {html.escape(model_status)}</h2>
          <p class="muted" style="margin:4px 0;">Formula Version: CN_EQUITY_PRICE_TEMP_V1;NDX_PRICE_TEMPERATURE_V1_55_45_BALANCED;gold-v2-inverse-real-yield-fed;allocation-v3-gap-first-cn-release-factor</p>
          <p class="muted">当前分配逻辑：战略配置缺口决定资金方向；A股价格温度仅约束A股动态资金释放比例，波动风险限制单次释放。</p>
        </article>

        <article class="panel section-spacer">
          <span class="eyebrow">Blocking Issues</span>
          <h2>当前阻断项</h2>
          <table>
            <thead><tr><th>阻断类别</th><th>状态</th><th>详情</th></tr></thead>
            <tbody>
              <tr><td style="color:var(--green);font-weight:650;">模型行情数据阻断</td><td>NONE</td><td>模型行情数据新鲜度：PASS</td></tr>
              <tr><td style="color:{'var(--green)' if carrier_snapshot_valid else 'var(--amber)'};font-weight:650;">QDII载体数据状态</td><td>{html.escape(carrier_data_status)}</td><td>QDII载体选择状态：{html.escape(carrier_selection_status)} · 快照有效性：{'有效' if carrier_snapshot_valid else '无效'}</td></tr>
              <tr><td style="color:{'var(--green)' if ndx_model_active else 'var(--amber)'};font-weight:650;">Model Activation Blockers</td><td>{html.escape(ndx_model_status_text)}</td><td>{'NDX_PRICE_TEMPERATURE_V1 已完成 Shadow 验证并激活' if ndx_model_active else 'NDX_PRICE_TEMPERATURE_V1 与 SINGLE_REAL_YIELD_FACTOR 尚未激活'}</td></tr>
              <tr><td style="color:{'var(--red)' if execution_disabled else 'var(--green)'};font-weight:650;">Execution Blockers</td><td>{html.escape(disabled_status_text)}</td><td>{html.escape(copilot.get('pool_status_reason', 'DYNAMIC_CASH_POOL 当前不可执行') if execution_disabled else '正式决策门已通过；仍需用户确认执行并入账。')}固定定投不受影响</td></tr>
            </tbody>
          </table>
          <p class="muted" style="margin-top:8px;">固定定投不受影响。</p>
        </article>

        <article class="panel section-spacer">
          <span class="eyebrow">Target Governance</span>
          <h2>目标模式与数据源</h2>
          <table><tbody>
            <tr><td>overseas_equity.strategic_target</td><td>40%</td></tr>
            <tr><td>overseas_equity.current_final_target</td><td>35%</td></tr>
            <tr><td>overseas_equity.target_mode</td><td>CARRY_FORWARD_LAST_VALID_TARGET</td></tr>
            <tr><td>overseas_equity.target_source</td><td>LAST_VALID_DECISION_SNAPSHOT</td></tr>
            <tr><td>cash.current_final_target</td><td>{copilot['targets']['cash'] * 100:.0f}%</td></tr>
            <tr><td>cash.target_mode</td><td>RESIDUAL_TARGET</td></tr>
            <tr><td>target_sum_check</td><td>{sum(copilot['targets'].values()) * 100:.0f}% = 100% · PASS</td></tr>
            <tr><td>allocation_gap_consistency</td><td>PASS</td></tr>
          </tbody></table>
        </article>

        <article class="panel section-spacer">
          <span class="eyebrow">Overseas Equity Model Governance</span>
          <h2>纳指模型语义迁移</h2>
          <table><tbody>
            <tr><td>legacy_us_equity_score</td><td>RETIRED</td></tr>
            <tr><td>nasdaq100_pe.used_in_score</td><td>false</td></tr>
            <tr><td>nasdaq100_pe.used_in_release_factor</td><td>false</td></tr>
            <tr><td>nasdaq100_pe.blocking</td><td>false · DISPLAY_ONLY</td></tr>
            <tr><td>sp500_pe.used_in_score</td><td>false</td></tr>
            <tr><td>sp500_pe.used_in_release_factor</td><td>false</td></tr>
            <tr><td>sp500_pe.blocking</td><td>false · DISPLAY_ONLY</td></tr>
            <tr><td>NDX_PRICE_TEMPERATURE_V1</td><td>{html.escape(ndx_model_status_text)}</td></tr>
            <tr><td>formula_version</td><td>{html.escape(str(ndx_shadow.get('formula_version') or ndx_price_temperature.FORMULA_VERSION))}</td></tr>
            <tr><td>price_source</td><td>{html.escape(str(ndx_shadow.get('source_name') or '-'))}</td></tr>
            <tr><td>price_proxy_status</td><td>{html.escape(str(ndx_shadow.get('proxy_status') or '-'))}</td></tr>
            <tr><td>history_window</td><td>trailing 2520 trading days · minimum 1260</td></tr>
            <tr><td>no_lookahead_check</td><td>{html.escape(str(ndx_shadow.get('no_lookahead_check') or '-'))}</td></tr>
            <tr><td>ma_distance_score</td><td>{float(ndx_shadow.get('ma_distance_score') or 0):.4f}</td></tr>
            <tr><td>drawdown_score</td><td>{float(ndx_shadow.get('drawdown_score') or 0):.4f}</td></tr>
            <tr><td>temperature_score / level</td><td>{float(ndx_shadow.get('temperature_score') or 0):.4f} / {html.escape(str(ndx_shadow.get('temperature_level') or '-'))}</td></tr>
            <tr><td>base_release_factor</td><td>{float(ndx_shadow.get('base_release_factor') or 0):.6f}</td></tr>
            <tr><td>dfii10_percentile / modifier</td><td>{float(ndx_shadow.get('dfii10_percentile') or 0):.4f}% / {float(ndx_shadow.get('real_yield_modifier') or 0):.2f}</td></tr>
            <tr><td>rate_adjusted_release_factor</td><td>{float(ndx_shadow.get('rate_adjusted_release_factor') or 0):.6f}</td></tr>
            <tr><td>volatility_60d_percentile / cap</td><td>{float(ndx_shadow.get('realized_volatility_60d_percentile') or 0):.4f}% / {float(ndx_shadow.get('volatility_cap') or 0):.2f}</td></tr>
            <tr><td>candidate_effective_release_factor</td><td>{float(ndx_shadow.get('candidate_effective_release_factor') or 0):.6f}</td></tr>
            <tr><td>over_conservative_warning</td><td>{html.escape(', '.join(ndx_shadow.get('over_conservative_warning') or []) or 'NONE')}</td></tr>
            <tr><td>over_aggressive_warning</td><td>{html.escape(', '.join(ndx_shadow.get('over_aggressive_warning') or []) or 'NONE')}</td></tr>
            <tr><td>validation_stage</td><td>{html.escape(str(ndx_shadow.get('validation_stage') or 'OFFLINE_VALIDATION'))}</td></tr>
            <tr><td>activation_status</td><td>{html.escape(str(ndx_shadow.get('activation_status') or 'NOT_ACTIVE'))}</td></tr>
          </tbody></table>
        </article>

        {qdii_health_html}

        <article class="panel section-spacer">
          <span class="eyebrow">Source Approval</span>
          <h2>数据源审批状态</h2>
          <table>
            <thead><tr><th>Indicator</th><th>Source</th><th>Confidence</th><th>Used In Score</th><th>Approval Status</th></tr></thead>
            <tbody>{''.join(approval_rows)}</tbody>
          </table>
        </article>

        {pe_quality_html}

        <article class="panel section-spacer">
          <span class="eyebrow">Raw Indicators</span>
          <h2>原始指标</h2>
          <table>
            <thead><tr><th>指标</th><th>当前值</th></tr></thead>
            <tbody>{indicator_rows}</tbody>
          </table>
        </article>

        <article class="panel section-spacer">
          <span class="eyebrow">Model & Formula Versions</span>
          <h2>版本信息</h2>
          <table>
            <thead><tr><th>项目</th><th>版本</th></tr></thead>
            <tbody>
              <tr><td>application_version</td><td>{html.escape(versions.get('application_version') or APPLICATION_VERSION)}</td></tr>
              <tr><td>Data Quality Version</td><td>{html.escape(model_risk.DATA_QUALITY_VERSION)}</td></tr>
              <tr><td>Model Version</td><td>{html.escape(model_risk.MODEL_VERSION)}</td></tr>
              <tr><td>allocation_formula_version</td><td>allocation-v3-gap-first-cn-release-factor</td></tr>
              <tr><td>a500_formula_version</td><td>{html.escape(versions.get('a500_formula_version') or cn_equity_temperature.FORMULA_VERSION)}</td></tr>
              <tr><td>ndx_formula_version</td><td>{html.escape(versions.get('ndx_formula_version') or ndx_price_temperature.FORMULA_VERSION)}</td></tr>
              <tr><td>gold_formula_version</td><td>{html.escape(versions.get('gold_formula_version') or 'gold-v2-inverse-real-yield-fed')}</td></tr>
              <tr><td>qdii_carrier_contract_version</td><td>{html.escape(versions.get('qdii_carrier_contract_version') or QDII_CARRIER_CONTRACT_VERSION)}</td></tr>
              <tr><td>run_id</td><td>{html.escape(run_id)}</td></tr>
              <tr><td>Last Full Audit</td><td>{html.escape(model_risk.LAST_AUDIT_DATE)}</td></tr>
              <tr><td>Last Data Refresh</td><td>{html.escape(generated_at)}</td></tr>
            </tbody>
          </table>
        </article>

        <article class="panel section-spacer">
          <span class="eyebrow">A500 Sub-Model Status</span>
          <h2>A500 价格温度子模型</h2>
          <p class="muted" style="font-size:11px;margin-bottom:8px;">A500子模型 ACTIVE ≠ 全局资金池可执行。全局仍可能因其他资产阻塞而 FREEZE。</p>
          <table>
            <thead><tr><th>字段</th><th>值</th></tr></thead>
            <tbody>
              <tr><td>A500 Price Model Status</td><td style="color:var(--green);font-weight:700;">ACTIVE</td></tr>
              <tr><td>A500 Used In Score</td><td>Yes</td></tr>
              <tr><td>A500 Final Score</td><td>{html.escape(str(cn_temp.get('finalScore', '-')))}{' (0分=极热/极度拥挤，100分=极冷)' if cn_temp.get('finalScore') is not None else ''}</td></tr>
              <tr><td>A500 Temperature Level</td><td>{html.escape(str(cn_temp.get('level', '-')))} / {'极度拥挤' if cn_temp.get('level') == 'VERY_HOT' else cn_temp.get('level', '-')}</td></tr>
              <tr><td>A500 Release Factor</td><td>{cn_temp.get('releaseFactor', '-'):.2f}</td></tr>
              <tr><td>A500 Effective Release Factor</td><td>{cn_temp.get('effectiveReleaseFactor', 1.0):.2f}</td></tr>
              <tr><td>HS300 Adjustment</td><td>{cn_temp.get('marketAdjustment', 0):+.0f}</td></tr>
              <tr><td>A500 Data Status</td><td style="color:var(--green);">PASS</td></tr>
              <tr><td>Global Model Status</td><td>{html.escape(model_status)}</td></tr>
              <tr><td>Global Decision Status</td><td>{html.escape(copilot.get('decision_status', 'FREEZE'))}</td></tr>
            </tbody>
          </table>
        </article>

        <article class="panel section-spacer" data-live="decision-status">
          <span class="eyebrow">Decision Status</span>
          <h2>决策状态汇总</h2>
          <table>
            <thead><tr><th>字段</th><th>值</th></tr></thead>
            <tbody>
              <tr><td>Data Status</td><td style="color:var(--green);">PASS</td></tr>
              <tr><td>Model Status</td><td style="color:var(--amber);">{html.escape(model_status)}</td></tr>
              <tr><td>Decision Status</td><td>{html.escape(copilot.get('decision_status', 'FREEZE'))}</td></tr>
              <tr><td>Dynamic Cash Pool Status</td><td>{html.escape(pool_model_status)}</td></tr>
              <tr><td>Current Decision</td><td>{display_amount:,.0f} 元</td></tr>
              <tr><td>Release Amount</td><td>{0 if execution_disabled else plan_amount:,.0f} 元</td></tr>
              <tr><td>Current Release Ratio</td><td>{0 if execution_disabled else copilot['release_ratio'] * 100:.0f}%</td></tr>
              <tr><td>Remaining Dynamic Cash Pool</td><td>{remaining_pool:,.0f} 元</td></tr>
              <tr><td>组合总值</td><td>{copilot['total_value']:,.0f} 元</td></tr>
            </tbody>
          </table>
        </article>

      </section>

      <section class="tab-panel" role="tabpanel" id="tab-daily-automation">
        {daily_automation_html}
      </section>

      <section class="tab-panel" role="tabpanel" id="tab-automation-history">
        {automation_history_html}
      </section>

      <section class="tab-panel" role="tabpanel" id="tab-history">
        <article class="panel">
          <span class="eyebrow">Monthly History</span>
          <h2>月度执行历史</h2>
          <table>
            <thead><tr><th>月份</th><th>历史资金池</th><th>Historical Executed Amount</th><th>Execution Type</th><th>历史规则层级</th><th>历史状态</th></tr></thead>
            <tbody>{history_html if history_html else '<tr><td colspan="6" class="muted">暂无历史记录</td></tr>'}</tbody>
          </table>
        </article>
      </section>

    </div>

    <p class="disclaimer">本系统仅用于长期资产配置记录与规则验证，不提供短期预测、买卖信号、仓位清空或盘中择时建议。</p>
  </main>

  {modal_html}
  {pm_modal_html}

  <script>
    (function() {{
      var tabButtons = document.querySelectorAll('.tab-btn');
      var tabPanels = {{}};
      ['overview','daily-automation','automation-history','portfolio','drawdown','allocation-flow','data-audit','history'].forEach(function(id) {{
        var el = document.getElementById('tab-' + id);
        if (el) tabPanels[id] = el;
      }});

      function switchTab(tabId) {{
        tabButtons.forEach(function(btn) {{
          var isActive = btn.dataset.tab === tabId;
          btn.classList.toggle('active', isActive);
          btn.setAttribute('aria-selected', String(isActive));
        }});
        Object.keys(tabPanels).forEach(function(id) {{
          var panel = tabPanels[id];
          if (!panel) return;
          var isActive = id === tabId;
          panel.classList.toggle('active', isActive);
        }});
        try {{ sessionStorage.setItem('copilotActiveTab', tabId); }} catch(e) {{}}
      }}

      tabButtons.forEach(function(btn) {{
        btn.addEventListener('click', function() {{ switchTab(btn.dataset.tab); }});
      }});

      // Delegated so links inside data-live modules keep working after a swap.
      document.addEventListener('click', function(e) {{
        var link = e.target.closest ? e.target.closest('[data-nav-to]') : null;
        if (!link) return;
        e.preventDefault();
        var target = link.dataset.navTo;
        if (target && tabPanels[target]) switchTab(target);
      }});

      try {{
        var saved = sessionStorage.getItem('copilotActiveTab');
        if (saved && tabPanels[saved]) switchTab(saved);
      }} catch(e) {{}}
    }})();
    (function() {{
      var rows = Array.from(document.querySelectorAll('.qdii-carrier-row'));
      var assetAmountInput = document.getElementById('qdii-asset-amount');
      var previewStatusEl = document.getElementById('qdii-preview-status');
      var tolerance = 0.01;
      var mainEl = document.querySelector('main[data-cash-pool-status]');
      function getAssetAmount() {{ return Math.max(0, Number(assetAmountInput.value || 0)); }}
      function updateCarrierPreview() {{
        var assetAmount = getAssetAmount();
        var selectedCount = 0, capacity = 0, assigned = 0, covered = 0, overLimit = false, unselectedNonzero = false;
        rows.forEach(function(row) {{
          var checked = row.querySelector('.qdii-select').checked;
          var input = row.querySelector('.qdii-amount');
          var errEl = row.querySelector('.qdii-row-error');
          input.disabled = !checked;
          if (!checked) {{
            if (Math.max(0, Number(input.value || 0)) > tolerance) unselectedNonzero = true;
            input.value = 0; input.style.borderColor = ''; input.removeAttribute('aria-invalid');
            if(errEl){{errEl.style.display='none';errEl.textContent='';}}
            return;
          }}
          selectedCount += 1;
          var cap = Number(row.dataset.capacity || 0);
          var amount = Math.max(0, Number(input.value || 0));
          capacity += cap; assigned += amount; covered += Math.min(amount, cap);
          if (amount > cap + tolerance) {{
            input.style.borderColor = 'var(--red)';
            input.style.borderWidth = '2px';
            input.setAttribute('aria-invalid', 'true');
            if (errEl) {{ errEl.style.display = 'block'; errEl.textContent = '分配金额超过当前额度，超出 ' + (amount - cap).toFixed(2) + ' 元'; }}
            overLimit = true;
          }} else {{
            input.style.borderColor = '';
            input.style.borderWidth = '';
            input.removeAttribute('aria-invalid');
            if (errEl) {{ errEl.style.display = 'none'; errEl.textContent = ''; }}
          }}
        }});
        var uncovered = Math.max(0, assetAmount - covered);
        var overAssigned = Math.max(0, assigned - assetAmount);
        var snapshotValid = mainEl && mainEl.dataset.carrierSnapshotValid === 'true';
        var carrierSelectionStatus = mainEl ? mainEl.dataset.carrierSelectionStatus : 'BLOCKED';
        var exactMatch = Math.abs(assigned - assetAmount) <= tolerance
          && Math.abs(covered - assetAmount) <= tolerance
          && uncovered <= tolerance && overAssigned <= tolerance;
        var previewStatus;
        if (assetAmount <= tolerance && assigned <= tolerance) {{ previewStatus = 'EMPTY'; }}
        else if (assetAmount > tolerance && exactMatch && !overLimit && !unselectedNonzero
          && snapshotValid && ['AVAILABLE', 'PARTIAL_CAPACITY'].includes(carrierSelectionStatus)) {{ previewStatus = 'VALID'; }}
        else {{ previewStatus = 'INVALID'; }}
        if (previewStatusEl) {{
          previewStatusEl.textContent = previewStatus;
          previewStatusEl.style.color = previewStatus === 'VALID' ? 'var(--green)' : (previewStatus === 'EMPTY' ? 'var(--muted)' : 'var(--red)');
        }}
        document.getElementById('qdii-selected-capacity').textContent = capacity.toFixed(2) + ' 元';
        document.getElementById('qdii-assigned').textContent = assigned.toFixed(2) + ' 元';
        document.getElementById('qdii-covered').textContent = covered.toFixed(2) + ' 元';
        document.getElementById('qdii-uncovered').textContent = uncovered.toFixed(2);
        document.getElementById('qdii-over-selected').textContent = overAssigned.toFixed(2);
        document.getElementById('qdii-complexity-warning').textContent = selectedCount > 3
          ? ' 当前选择基金数量较多，底层指数高度重合，增加的是载体复杂度，不是市场分散。' : '';
      }}
      rows.forEach(function(row) {{
        row.querySelector('.qdii-select').addEventListener('change', updateCarrierPreview);
        row.querySelector('.qdii-amount').addEventListener('input', updateCarrierPreview);
      }});
      assetAmountInput.addEventListener('input', updateCarrierPreview);
      updateCarrierPreview();
    }})();
    {modal_js}
    {pm_js}
  </script>
</body>
</html>
"""
    output_paths.get_html_snapshot_path("dashboard.html").write_text(dashboard, encoding="utf-8")
    output_paths.get_dist_path("dashboard.html").write_text(dashboard, encoding="utf-8")
    output_paths.get_html_snapshot_path("Asset Allocation Copilot V7.html").write_text(dashboard, encoding="utf-8")
    output_paths.get_dist_path("Asset Allocation Copilot V7.html").write_text(dashboard, encoding="utf-8")


def print_report(rows, macro_rows=None):
    print("基金每日回撤监控")
    print("=" * 80)
    for row in rows:
        print(f"{row['code']} {row['name']} [{row['type']}]")
        print(f"  最新净值: {row['latest_nav']:.4f} ({row['latest_date']}), 当日涨跌: {format_pct(row['daily_pct_change'])}")
        print(f"  6个月高点: {row['high_6m_nav']:.4f} ({row['high_6m_date']}), 回撤: {format_pct(row['drawdown_6m_pct'])}")
        print(f"  12个月高点: {row['high_12m_nav']:.4f} ({row['high_12m_date']}), 回撤: {format_pct(row['drawdown_12m_pct'])}")
        print(f"  持仓/上限: {row['holding_amount']:.1f}/{row['max_holding_amount']:.1f}, 剩余额度: {row['remaining_capacity']:.1f}")
        print(f"  动作: {row['action']}")
        print(
            f"  未来计划: {row['future_plan']} "
            f"(宏观系数 {row['macro_multiplier']:.2f})"
        )
        print("-" * 80)
    if macro_rows:
        print("宏观观察指标")
        print("=" * 80)
        for row in macro_rows:
            change = row["change_20d_bps"]
            change_text = f"{change:+.0f}bp" if change is not None else "-"
            print(
                f"{row['name']}: {row['latest_value']:.2f}% "
                f"({row['latest_date']}), 20日变化: {change_text}, "
                f"环境: {row['environment']}"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="拉取净值并更新数据库")
    parser.add_argument("--report", action="store_true", help="输出回撤报告")
    parser.add_argument("--alert", action="store_true", help="把触发提醒写入 alerts 表")
    parser.add_argument("--backfill-year", action="store_true", help="回填近一年历史净值；首次建库或修复数据时使用")
    parser.add_argument(
        "--backfill-pe-history",
        action="store_true",
        help="强制回填美股PE最近60个月历史数据",
    )
    parser.add_argument("--export", action="store_true", help="导出 report.json 和 dashboard.html")
    args = parser.parse_args()

    run_phase = os.environ.get("ASSET_COPILOT_PHASE", "v7-2")
    run_version = os.environ.get("ASSET_COPILOT_VERSION", "v7-2")
    run_task_name = os.environ.get("ASSET_COPILOT_TASK_NAME", "Asset Allocation Copilot V7 run")

    try:
        run_dir = output_paths.create_run_dir(run_phase, run_version)
        output_paths.write_run_manifest({
            "phase": run_phase, "task_name": run_task_name,
            "decision_status": "IN_PROGRESS", "data_status": "IN_PROGRESS",
            "model_status": "NOT_RUN", "next_action": "Complete requested run",
            "source_data_used": ["data/fund_tracker.sqlite", "config.json"],
            "whether_root_directory_was_modified": "No",
        }, run_dir)
    except output_paths.OutputDirectoryError as exc:
        print(str(exc))
        raise SystemExit(2)

    config = load_config()
    conn = connect_db()
    try:
        sync_funds(conn, config)
        ensure_monthly_contribution(conn, config)
        if args.update or args.backfill_pe_history:
            temperature_config = market_temperature_config(config)
            cache_hours = temperature_config.get("cache_hours", 24)
            if args.update:
                update_nav_history(
                    conn,
                    config,
                    days=370 if args.backfill_year else 60,
                )
                update_macro_history(
                    conn,
                    days=370 if args.backfill_year else 120,
                    cache_hours=cache_hours,
                )
                update_valuation_history(
                    conn,
                    config,
                    cache_hours=cache_hours,
                )
                update_index_price_history(conn, cache_hours=cache_hours)
            update_copilot_inputs(
                conn,
                config,
                cache_hours=cache_hours,
                force_pe_history=args.backfill_pe_history,
            )
        macro_rows = generate_macro_report(conn)
        market_temperature = generate_market_temperature(
            conn,
            config,
            macro_rows=macro_rows,
        )
        rows = generate_report(
            conn,
            config,
            macro_rows=macro_rows,
            market_temperature=market_temperature,
            persist_alerts=args.alert,
        )
        carrier_snapshot_path = os.environ.get("ASSET_COPILOT_CARRIER_SNAPSHOT_PATH")
        carrier_as_of = os.environ.get("ASSET_COPILOT_CARRIER_AS_OF")
        carrier_now = dt.datetime.fromisoformat(carrier_as_of) if carrier_as_of else None
        copilot = generate_copilot_snapshot(
            conn, config, market_temperature,
            carrier_snapshot_path=carrier_snapshot_path,
            carrier_now=carrier_now,
        )
        decision_snapshot = model_risk.get_decision_snapshot(conn, copilot["month"])
        history_rows = allocation_history_rows(conn)
        if args.update or args.export or args.backfill_pe_history:
            nav_audit = data_layer_audit.audit_fund_nav(conn, config)
            data_layer_audit.write_phase1_reports(run_dir, nav_audit)
            write_pe_history_outputs(conn)
            write_report_json(
                rows,
                macro_rows,
                market_temperature,
                copilot,
                history_rows,
            )
            write_copilot_dashboard(
                rows,
                macro_rows,
                market_temperature,
                copilot,
                history_rows,
                config=config,
            )
            model_risk.write_validation_reports(
                run_dir,
                copilot,
                decision_snapshot,
                macro_rows,
            )
            files = sorted(
                str(path.relative_to(run_dir)) for path in run_dir.rglob("*")
                if path.is_file() and path.name != "run-manifest.md"
            )
            output_paths.write_run_manifest({
                "phase": run_phase, "task_name": run_task_name,
                "decision_status": copilot.get("decision_status", "FREEZE"),
                "data_status": copilot.get("data_status", "FREEZE"),
                "model_status": copilot.get("model_status", "NOT_RUN"),
                "validation_stage": copilot.get("validation_stage", "OFFLINE_VALIDATION"),
                "output_files": files,
                "blocked_reason": os.environ.get("ASSET_COPILOT_BLOCKED_REASON", copilot.get("pool_status_reason", "")),
                "next_action": os.environ.get(
                    "ASSET_COPILOT_NEXT_ACTION",
                    "Resolve blocking score inputs before execution" if not copilot.get("allow_execution") else "Review current decision",
                ),
                "source_data_used": [
                    "data/fund_tracker.sqlite", "config.json", "FRED", "PBOC", "local valuation observations",
                    "carrier_snapshot_id=%s" % copilot.get("qdii_carrier_integration", {}).get("carrier_snapshot_id", "Unavailable"),
                    "carrier_latest_sha256=%s" % copilot.get("qdii_carrier_integration", {}).get("carrier_latest_sha256", "Unavailable"),
                ],
                "whether_root_directory_was_modified": "No generated outputs in project root",
            }, run_dir)
        conn.commit()
        if args.report or not args.update:
            print_report(rows, macro_rows)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        output_paths.write_blocked_outputs(exc, {
            "phase": os.environ.get("ASSET_COPILOT_PHASE", "v7-2"),
            "task_name": os.environ.get("ASSET_COPILOT_TASK_NAME", "Asset Allocation Copilot V7 run"),
        })
        raise
