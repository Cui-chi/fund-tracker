import copy
import json
import sqlite3
import unittest
from unittest import mock

import fund_tracker
import model_risk


def schema(conn):
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE decision_snapshots (decision_id TEXT PRIMARY KEY, execution_month TEXT, version INTEGER, generated_at TEXT, decision_json TEXT, execution_status TEXT, UNIQUE(execution_month, version))")
    conn.execute("CREATE TABLE decision_snapshot_corrections (id INTEGER PRIMARY KEY, decision_id TEXT, created_at TEXT, correction_json TEXT)")
    conn.execute("CREATE TABLE current_monitoring_snapshots (id INTEGER PRIMARY KEY, execution_month TEXT, generated_at TEXT, snapshot_json TEXT)")


def decision_payload():
    scores = {"a_share": 33.6, "us_equity": 22.6, "gold": 40.5}
    current = {"a_share": 24000.0, "us_equity": 22000.0, "gold": 8000.0, "cash": 46000.0}
    strategic = {"a_share": 0.4, "us_equity": 0.4, "gold": 0.1, "cash": 0.1}
    ranges = {"a_share": [0.25, 0.5], "us_equity": [0.25, 0.55], "gold": [0.05, 0.2], "cash": [0.1, 1.0]}
    targets = {"a_share": 0.35, "us_equity": 0.35, "gold": 0.1, "cash": 0.2}
    total = sum(current.values())
    target_values = {a: round(total * v, 2) for a, v in targets.items()}
    gaps = {a: round(target_values[a] - current[a], 2) for a in targets}
    routing = model_risk.route_allocation(gaps, scores, 250.0)
    return {
        "decision_id": "decision-2026-06-v1",
        "execution_month": "2026-06",
        "generated_at": "2026-06-18T09:00:00",
        "formula_version": model_risk.FORMULA_VERSION,
        "model_version": model_risk.MODEL_VERSION,
        "data_quality_version": model_risk.DATA_QUALITY_VERSION,
        "dynamic_cash_pool_before": 1000.0,
        "release_ratio": 0.25,
        "release_amount": 250.0,
        "release_reason": ["test"],
        "asset_scores": scores,
        "asset_score_components": {
            "a_share": {"valuation_score": 30.0, "liquidity_score": 42.0},
            "us_equity": {"valuation_score": 15.0, "liquidity_score": 34.0},
            "gold": {"final_gold_score": 40.5},
        },
        "target_allocation_before_score_adjustment": strategic,
        "allocation_ranges": ranges,
        "target_allocation_after_score_adjustment": targets,
        "current_asset_values": current,
        "target_asset_values": target_values,
        "gap_values": gaps,
        "positive_gap_values": {a: max(0, gaps[a]) for a in scores},
        "allocation_priority_formula": routing["formula"],
        "allocation_priority_values": routing["assets"],
        "asset_allocation_amounts": routing["allocations"],
        "fund_level_recommendations": [],
        "input_indicator_values": {"tips5y": 1.8},
        "input_indicator_latest_dates": {"tips5y": "2026-06-16"},
        "input_indicator_sources": {"tips5y": "FRED"},
        "input_indicator_confidence": {"tips5y": "High"},
        "input_indicator_sample_size": {"tips5y": 88},
        "input_indicator_data_lag": {"tips5y": 2},
        "risk_warnings": [],
        "blocking_issues": [],
        "allow_execution": True,
        "execution_status": "pending",
    }


class DecisionSnapshotTests(unittest.TestCase):
    def test_decision_snapshot_is_immutable_when_monitoring_changes(self):
        conn = sqlite3.connect(":memory:")
        schema(conn)
        original = decision_payload()
        model_risk.persist_decision_snapshot(conn, copy.deepcopy(original))
        monitoring = {"month": "2026-06", "current_values": {"gold": 99999}}
        model_risk.persist_monitoring_snapshot(conn, monitoring)
        loaded = model_risk.get_decision_snapshot(conn, "2026-06")
        self.assertEqual(loaded["decision_id"], original["decision_id"])
        self.assertEqual(loaded["asset_allocation_amounts"], original["asset_allocation_amounts"])
        self.assertEqual(loaded["input_indicator_values"], original["input_indicator_values"])
        self.assertEqual(loaded["formula_version"], original["formula_version"])
        self.assertEqual(loaded["current_asset_values"], original["current_asset_values"])
        self.assertEqual(loaded["gap_values"], original["gap_values"])

    def test_decision_snapshot_reproduces_scores_gaps_and_allocations(self):
        result = model_risk.recompute_decision(decision_payload())
        self.assertLessEqual(result["max_amount_difference"], 0.01)
        self.assertLessEqual(result["max_score_difference"], 0.01)
        self.assertTrue(result["pass"])

    def test_manual_override_is_disabled(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE copilot_state (state_key TEXT PRIMARY KEY, state_value TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE manual_override_snapshots (override_id TEXT PRIMARY KEY, execution_month TEXT, created_at TEXT, override_json TEXT)")
        conn.execute("CREATE TABLE allocation_events (id INTEGER PRIMARY KEY, month TEXT, decision TEXT, deploy_amount REAL, allocation_json TEXT, created_at TEXT, plan_amount REAL, plan_allocation_json TEXT, executed_at TEXT, execution_type TEXT)")
        fund_tracker.set_state(conn, "dynamic_cash_pool", 1875.0)
        config = {
            "copilot_v7": {"execution_funds": {"a_share": "F1"}},
            "funds": [{
                "code": "F1", "name": "Test Fund", "asset_class": "a_share",
                "holding_amount": 1000.0, "max_holding_amount": 5000.0,
                "profit_pct": 0.0,
            }],
        }
        with self.assertRaises(ValueError):
            fund_tracker.apply_manual_override(
                conn, config, "a_share", 400, "人工复核后的小额override"
            )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM manual_override_snapshots").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM allocation_events").fetchone()[0], 0)

    def test_existing_monthly_event_blocks_duplicate_execution(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE allocation_events (id INTEGER PRIMARY KEY, month TEXT, decision TEXT, deploy_amount REAL, allocation_json TEXT, created_at TEXT, plan_amount REAL, plan_allocation_json TEXT, executed_at TEXT, execution_type TEXT)")
        conn.execute(
            "INSERT INTO allocation_events VALUES (1, '2026-07', 'execute', 100, '{}', '2026-07-01', 200, '{}', '2026-07-01', 'Model Auto Execution')"
        )
        snapshot = {"month": "2026-07", "user_decision": None}
        with mock.patch.object(fund_tracker, "ensure_monthly_contribution"), \
             mock.patch.object(fund_tracker, "generate_market_temperature", return_value={}), \
             mock.patch.object(fund_tracker, "generate_copilot_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(ValueError, "ALREADY_EXECUTED"):
                fund_tracker.apply_copilot_decision(conn, {}, "execute", [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM allocation_events").fetchone()[0], 1)
