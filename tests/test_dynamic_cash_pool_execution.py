import copy
import json
import sqlite3
import unittest

import fund_tracker
from unittest import mock

import fund_tracker


OPENING_POOL = 4375.0
PROPOSED_RELEASE = 674.75


def schema(conn):
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE copilot_state (
            state_key TEXT PRIMARY KEY, state_value TEXT, updated_at TEXT
        );
        CREATE TABLE allocation_history (
            month TEXT PRIMARY KEY, generated_at TEXT, snapshot_json TEXT,
            user_decision TEXT, decision_at TEXT
        );
        CREATE TABLE allocation_events (
            id INTEGER PRIMARY KEY, month TEXT, decision TEXT,
            deploy_amount REAL, allocation_json TEXT, created_at TEXT,
            plan_amount REAL, plan_allocation_json TEXT,
            executed_at TEXT, execution_type TEXT
        );
        CREATE TABLE fund_execution_log (
            id INTEGER PRIMARY KEY, month TEXT, fund_code TEXT,
            fund_name TEXT, asset_class TEXT, planned_amount REAL,
            actual_executed_amount REAL, executed_at TEXT
        );
    """)


def config():
    return {
        "funds": [
            {"code": "022459", "name": "A500", "asset_class": "a_share",
             "holding_amount": 1000.0, "profit_pct": 0.0,
             "max_holding_amount": 10000.0},
            {"code": "021000", "name": "NDX I", "asset_class": "us_equity",
             "holding_amount": 0.0, "profit_pct": None,
             "max_holding_amount": 22000.0},
            {"code": "016452", "name": "NDX A1", "asset_class": "us_equity",
             "holding_amount": 1000.0, "profit_pct": 0.0,
             "max_holding_amount": 22000.0},
            {"code": "539001", "name": "NDX A2", "asset_class": "us_equity",
             "holding_amount": 1000.0, "profit_pct": 0.0,
             "max_holding_amount": 22000.0},
        ],
    }


def snapshot():
    plan = [
        {"fund_code": "022459", "fund_name": "A500",
         "asset_class": "a_share", "asset_name": "A股",
         "planned_amount": 250.54},
        {"fund_code": "021000", "fund_name": "NDX I",
         "asset_class": "us_equity", "asset_name": "纳指指数型QDII",
         "planned_amount": 424.21},
    ]
    return {
        "month": "2026-07",
        "generated_at": "2026-07-13T14:00:00+08:00",
        "user_decision": None,
        "allow_execution": True,
        "plan_amount": PROPOSED_RELEASE,
        "deploy_amount": PROPOSED_RELEASE,
        "allocation_plan": {
            "a_share": 250.54, "us_equity": 424.21, "gold": 0.0,
        },
        "allocations": {
            "a_share": 250.54, "us_equity": 424.21, "gold": 0.0,
        },
        "allocation_routing": {
            "allocations": {
                "a_share": 250.54, "us_equity": 424.21, "gold": 0.0,
            },
            "assets": {
                "a_share": {
                    "positive_gap": 1000.0,
                    "release_factor": 0.2,
                    "executable_allocation": 250.54,
                },
                "us_equity": {
                    "positive_gap": 1500.0,
                    "release_factor": 0.453801,
                    "executable_allocation": 424.21,
                },
            },
        },
        "dynamic_cash_pool": OPENING_POOL,
        "original_dynamic_cash_pool": OPENING_POOL,
        "fund_carrier_plan": plan,
        "currentMonth": {"fundCarrierPlan": plan},
        "data_quality_gate": {"blocking_issues": []},
    }


class DynamicCashPoolExecutionTests(unittest.TestCase):
    def test_execution_amount_defaults_to_whole_yuan_without_exceeding_plan(self):
        self.assertEqual(fund_tracker.integer_execution_amount(250.54), 250)
        self.assertEqual(fund_tracker.integer_execution_amount(424.21), 424)
        self.assertEqual(
            fund_tracker.integer_execution_amount(250.54)
            + fund_tracker.integer_execution_amount(424.21),
            674,
        )

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        schema(self.conn)
        fund_tracker.set_state(self.conn, "dynamic_cash_pool", OPENING_POOL)
        fund_tracker.set_state(self.conn, "last_contribution_month", "2026-07")
        self.snapshot = snapshot()
        self.conn.execute(
            "INSERT INTO allocation_history VALUES (?, ?, ?, NULL, NULL)",
            ("2026-07", self.snapshot["generated_at"],
             json.dumps(self.snapshot, ensure_ascii=False)),
        )
        self.conn.commit()
        self.config = config()

    def tearDown(self):
        self.conn.close()

    def run_execution(self, a_share=250.54, ndx=424.21):
        submitted = [
            {"fund_code": "022459", "actual_executed_amount": a_share},
            {"fund_code": "021000", "actual_executed_amount": ndx},
        ]
        with mock.patch.object(fund_tracker, "ensure_monthly_contribution"), \
             mock.patch.object(fund_tracker, "generate_market_temperature", return_value={}), \
             mock.patch.object(fund_tracker, "generate_copilot_snapshot", return_value=self.snapshot), \
             mock.patch.object(fund_tracker.model_risk, "update_decision_execution_status"):
            return fund_tracker.apply_copilot_decision(
                self.conn, self.config, "execute", submitted,
            )

    def use_partial_a_class_plan(self):
        self.snapshot["fund_carrier_plan"] = [
            {"fund_code": "022459", "fund_name": "A500",
             "asset_class": "a_share", "asset_name": "A股",
             "planned_amount": 250.54},
            {"fund_code": "016452", "fund_name": "NDX A1",
             "asset_class": "us_equity", "asset_name": "纳指指数型QDII",
             "planned_amount": 10.0},
            {"fund_code": "539001", "fund_name": "NDX A2",
             "asset_class": "us_equity", "asset_name": "纳指指数型QDII",
             "planned_amount": 100.0},
        ]
        self.snapshot["currentMonth"]["fundCarrierPlan"] = self.snapshot["fund_carrier_plan"]

    def run_partial_execution(self, a_share=250.54, first=10.0, second=100.0):
        self.use_partial_a_class_plan()
        submitted = [
            {"fund_code": "022459", "actual_executed_amount": a_share},
            {"fund_code": "016452", "actual_executed_amount": first},
            {"fund_code": "539001", "actual_executed_amount": second},
        ]
        with mock.patch.object(fund_tracker, "ensure_monthly_contribution"), \
             mock.patch.object(fund_tracker, "generate_market_temperature", return_value={}), \
             mock.patch.object(fund_tracker, "generate_copilot_snapshot", return_value=self.snapshot), \
             mock.patch.object(fund_tracker.model_risk, "update_decision_execution_status"):
            return fund_tracker.apply_copilot_decision(
                self.conn, self.config, "execute", submitted,
            )

    def pool(self):
        return float(fund_tracker.get_state(
            self.conn, "dynamic_cash_pool", 0,
        ))

    def test_confirmed_amount_is_the_only_pool_debit(self):
        result = self.run_partial_execution()
        self.assertEqual(result["executed_amount"], 360.54)
        self.assertEqual(result["unexecuted_amount"], 314.21)
        self.assertEqual(self.pool(), 4014.46)
        event = self.conn.execute(
            "SELECT deploy_amount, plan_amount FROM allocation_events"
        ).fetchone()
        self.assertEqual(event["deploy_amount"], 360.54)
        self.assertEqual(event["plan_amount"], PROPOSED_RELEASE)

    def test_execution_freezes_decision_basis_separately_from_actual_amounts(self):
        result = self.run_execution(a_share=249.0, ndx=424.0)
        basis = result["execution_decision_snapshot"]
        self.assertEqual(basis["snapshot_type"], "EXECUTION_DECISION_BASIS")
        self.assertEqual(basis["routing_status"], "FROZEN_AT_DECISION")
        self.assertEqual(basis["plan_amount"], PROPOSED_RELEASE)
        self.assertEqual(
            basis["allocation_routing"]["allocations"],
            self.snapshot["allocation_plan"],
        )
        self.assertEqual(result["plan_amount"], PROPOSED_RELEASE)
        self.assertEqual(result["executed_amount"], 673.0)
        self.assertEqual(result["unexecuted_amount"], 1.75)

    def test_legacy_recalculation_is_not_presented_as_execution_basis(self):
        self.snapshot["allocation_routing"]["allocations"] = {
            "a_share": 213.25, "us_equity": 356.12, "gold": 0.0,
        }
        self.conn.execute(
            "UPDATE allocation_history SET snapshot_json = ? WHERE month = ?",
            (json.dumps(self.snapshot, ensure_ascii=False), "2026-07"),
        )
        result = self.run_execution(a_share=249.0, ndx=424.0)
        basis = result["execution_decision_snapshot"]
        self.assertEqual(
            basis["routing_status"],
            "UNAVAILABLE_LEGACY_EXECUTION_BASIS",
        )
        self.assertEqual(
            basis["allocation_routing"]["allocations"],
            self.snapshot["allocation_plan"],
        )

    def test_user_reduction_debits_only_confirmed_total(self):
        result = self.run_partial_execution(second=50.0)
        self.assertEqual(result["executed_amount"], 310.54)
        self.assertEqual(result["unexecuted_amount"], 364.21)
        self.assertEqual(self.pool(), 4064.46)

    def test_approved_i_class_can_cover_current_ndx_plan(self):
        result = self.run_execution()
        self.assertEqual(result["executed_amount"], PROPOSED_RELEASE)
        self.assertEqual(self.pool(), 3700.25)

    def test_duplicate_request_does_not_debit_twice(self):
        self.run_execution()
        self.conn.commit()
        pool_after_first = self.pool()
        with self.assertRaisesRegex(ValueError, "ALREADY_EXECUTED"):
            self.run_execution()
        self.assertEqual(self.pool(), pool_after_first)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM allocation_events").fetchone()[0],
            1,
        )

    def test_ledger_failure_rolls_back_pool_and_fund_rows(self):
        self.conn.execute("""
            CREATE TRIGGER fail_allocation_event
            BEFORE INSERT ON allocation_events
            BEGIN SELECT RAISE(ABORT, 'injected ledger failure'); END
        """)
        self.conn.commit()
        before_config = copy.deepcopy(self.config)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected ledger failure"):
            self.run_execution()
        self.assertEqual(self.pool(), OPENING_POOL)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM fund_execution_log").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM allocation_events").fetchone()[0],
            0,
        )
        self.assertIsNone(self.conn.execute(
            "SELECT user_decision FROM allocation_history WHERE month='2026-07'"
        ).fetchone()[0])
        self.assertEqual(self.config, before_config)

    def test_zero_execution_is_rejected_without_side_effects(self):
        with self.assertRaisesRegex(ValueError, "必须大于0"):
            self.run_execution(a_share=0, ndx=0)
        self.assertEqual(self.pool(), OPENING_POOL)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM allocation_events").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
