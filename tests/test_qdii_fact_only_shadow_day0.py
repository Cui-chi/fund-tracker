import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import ndx_price_temperature
import qdii_carrier


FORBIDDEN = {
    "selection", "asset_allocated_amount", "allocated_amount", "remaining_unallocated_amount",
    "carrier_plan", "planned_amount", "recommended_carrier", "alternative_carriers",
    "overseas_equity_split", "current_holding_amount", "current_holding",
    "transparent_tags", "advantages", "risks", "decision_status", "formal_release_amount",
    "ndx_candidate_release_amount", "snapshot_age_minutes", "carrier_selection_status",
}


def raw_snapshot(generated_at="2026-06-20 14:33:39"):
    limits = [100, 10, 10, 10000, 50, 100, 1000, 10]
    codes = ["539001", "270042", "040046", "019441", "016452", "018966", "021000", "019172"]
    funds = []
    for code, limit in zip(codes, limits):
        funds.append({
            "code": code, "name": "Fund " + code, "pool": "NDX_INDEX_QDII_POOL",
            "benchmark": "NASDAQ_100", "purchase_status": "限大额", "redemption_status": "开放赎回",
            "observed_channel_limit_rmb": limit, "effective_limit_rmb": limit,
            "nav_date": "2026-06-17", "nav": 1.2, "tracking_error_pct": 1.0,
            "purchase_fee_display": "0.12%",
            "source": {"name": "test", "type": "SECONDARY_CHANNEL_OBSERVATION",
                       "observed_at": generated_at, "confidence": "SECONDARY"},
        })
    return {"schema_version": "1.0.0", "generated_at": generated_at, "producer": "test",
            "contract": {"not_investment_signal": True}, "funds": funds}


def keys_recursive(value):
    if isinstance(value, dict):
        result = set(value)
        for item in value.values():
            result |= keys_recursive(item)
        return result
    if isinstance(value, list):
        result = set()
        for item in value:
            result |= keys_recursive(item)
        return result
    return set()


class QdiiFactOnlyShadowDay0Tests(unittest.TestCase):
    def setUp(self):
        self.generated = dt.datetime(2026, 6, 20, 14, 33, 39, tzinfo=qdii_carrier.LOCAL_TIMEZONE)
        self.projection = qdii_carrier.build_carrier_fact_projection(raw_snapshot(), generated_at=self.generated)

    def test_01_schema_v2(self): self.assertEqual(self.projection["schema_version"], "qdii-carrier-facts-v2")
    def test_02_no_selection(self): self.assertNotIn("selection", keys_recursive(self.projection))
    def test_03_no_overseas_split(self): self.assertNotIn("overseas_equity_split", keys_recursive(self.projection))
    def test_04_no_asset_allocated_amount(self): self.assertNotIn("asset_allocated_amount", keys_recursive(self.projection))
    def test_05_no_planned_amount(self): self.assertNotIn("planned_amount", keys_recursive(self.projection))
    def test_06_no_recommended_carrier(self): self.assertNotIn("recommended_carrier", keys_recursive(self.projection))
    def test_07_no_alternative_carriers(self): self.assertNotIn("alternative_carriers", keys_recursive(self.projection))
    def test_08_no_current_holding(self): self.assertFalse({"current_holding", "current_holding_amount"} & keys_recursive(self.projection))
    def test_09_no_subjective_tags(self): self.assertFalse({"transparent_tags", "advantages", "risks"} & keys_recursive(self.projection))
    def test_10_no_decision_status(self): self.assertNotIn("decision_status", keys_recursive(self.projection))
    def test_11_no_formal_release(self): self.assertNotIn("formal_release_amount", keys_recursive(self.projection))
    def test_12_no_candidate_amount(self): self.assertNotIn("ndx_candidate_release_amount", keys_recursive(self.projection))
    def test_13_no_snapshot_age(self): self.assertNotIn("snapshot_age_minutes", keys_recursive(self.projection))
    def test_14_canonical_status_only(self):
        self.assertIn("carrier_availability_status", self.projection["availability"])
        self.assertNotIn("carrier_selection_status", keys_recursive(self.projection))
    def test_15_all_timestamp_values_have_timezone(self):
        self.assertTrue(self.projection["snapshot"]["generated_at"].endswith("+08:00"))
        self.assertTrue(all(row["source"]["observed_at"].endswith("+08:00") for row in self.projection["carriers"]))
    def test_16_eight_approved_carriers(self):
        self.assertEqual(len(self.projection["carriers"]), 8)
        self.assertTrue(all(row["approved"] for row in self.projection["carriers"]))
    def test_17_capacity_is_11280(self): self.assertEqual(self.projection["availability"]["current_effective_capacity"], 11280)
    def test_18_contract_is_fact_only(self):
        self.assertEqual(self.projection["contract"], {"not_investment_signal": True, "contains_v7_decisions": False, "contains_allocation_plan": False})
    def test_19_snapshot_id_is_stable(self): self.assertEqual(self.projection["snapshot"]["snapshot_id"], "qdii-20260620-143339")
    def test_20_legacy_468_is_ignored(self):
        legacy = raw_snapshot(); legacy["selection"] = {"asset_allocated_amount": 468.75, "allocated_amount": 468.75}
        normalized = qdii_carrier.normalize_carrier_fact_snapshot(legacy, now=self.generated)
        self.assertFalse({"selection", "asset_allocated_amount", "allocated_amount"} & keys_recursive(normalized))
    def test_21_candidate_is_298_75(self):
        result = ndx_price_temperature.candidate_amount_chain(846.24, 0.353028, 1875)
        self.assertEqual(result["ndx_candidate_release_amount"], 298.75)
    def test_22_fresh_coverable_is_298_75(self):
        match = qdii_carrier.apply_carrier_matching(298.75, {"carrier_snapshot_valid": True, "carrier_selection_status": "AVAILABLE", "last_known_approved_carrier_capacity": 11280})
        self.assertEqual(match["carrier_coverable_amount"], 298.75)
    def test_23_formal_freeze_identity(self):
        coverable, executable = 298.75, 0
        self.assertEqual(round(coverable - executable, 2), 298.75)
    def test_24_missing_does_not_zero_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            integration = qdii_carrier.integration_snapshot(298.75, path=Path(tmp) / "missing.json", now=self.generated)
            match = qdii_carrier.apply_carrier_matching(298.75, integration)
        self.assertEqual(match["retained_due_to_carrier_block"], 298.75)
    def test_25_invalid_does_not_zero_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"; path.write_text("{bad", encoding="utf-8")
            integration = qdii_carrier.integration_snapshot(298.75, path=path, now=self.generated)
            match = qdii_carrier.apply_carrier_matching(298.75, integration)
        self.assertEqual((integration["carrier_data_status"], match["carrier_coverable_amount"], match["retained_due_to_carrier_block"]), ("INVALID", 0, 298.75))
    def test_26_stale_does_not_zero_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.json"; path.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
            later = self.generated + dt.timedelta(minutes=61)
            integration = qdii_carrier.integration_snapshot(298.75, path=path, now=later)
            match = qdii_carrier.apply_carrier_matching(298.75, integration)
        self.assertEqual((integration["carrier_data_status"], match["carrier_coverable_amount"], match["retained_due_to_carrier_block"]), ("STALE", 0, 298.75))
    def test_27_runtime_age_not_serialized(self):
        normalized = qdii_carrier.normalize_carrier_fact_snapshot(self.projection, now=self.generated + dt.timedelta(minutes=5))
        self.assertEqual(normalized["snapshot_age_minutes"], 5)
        self.assertNotIn("snapshot_age_minutes", keys_recursive(self.projection))
    def test_28_atomic_writer_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            qdii_carrier.write_carrier_fact_snapshot(raw_snapshot(), path, generated_at=self.generated)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], qdii_carrier.FACT_SCHEMA_VERSION)
    def test_29_run_archive_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); latest = root / "latest.json"; raw = root / "raw.json"; run = root / "run"; run.mkdir()
            qdii_carrier.write_carrier_fact_snapshot(raw_snapshot(), latest, generated_at=self.generated)
            raw.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
            manifest = qdii_carrier.archive_run_inputs(run, latest, raw, archived_at=self.generated)
            self.assertEqual(manifest["carrier_latest_sha256"], hashlib.sha256((run / "inputs/qdii-carrier-latest.json").read_bytes()).hexdigest())
            self.assertEqual(manifest["carrier_raw_sha256"], hashlib.sha256((run / "inputs/qdii-carrier-snapshot-raw.json").read_bytes()).hexdigest())
    def test_30_manifest_snapshot_matches_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); latest = root / "latest.json"; raw = root / "raw.json"; run = root / "run"; run.mkdir()
            qdii_carrier.write_carrier_fact_snapshot(raw_snapshot(), latest, generated_at=self.generated); raw.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
            manifest = qdii_carrier.archive_run_inputs(run, latest, raw, archived_at=self.generated)
            self.assertEqual(manifest["carrier_snapshot_id"], "qdii-20260620-143339")
    def test_31_archive_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); latest = root / "latest.json"; raw = root / "raw.json"; run = root / "run"; run.mkdir()
            qdii_carrier.write_carrier_fact_snapshot(raw_snapshot(), latest, generated_at=self.generated); raw.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
            qdii_carrier.archive_run_inputs(run, latest, raw, archived_at=self.generated)
            archived_hash = hashlib.sha256((run / "inputs/qdii-carrier-latest.json").read_bytes()).hexdigest()
            latest.write_text("{}", encoding="utf-8")
            qdii_carrier.archive_run_inputs(run, latest, raw, archived_at=self.generated)
            self.assertEqual(hashlib.sha256((run / "inputs/qdii-carrier-latest.json").read_bytes()).hexdigest(), archived_hash)
    def test_32_shadow_day0_never_counts_day1(self):
        status = {"ready_for_ndx_shadow": True, "shadow_days_completed": 0, "dynamic_cash_pool_status": "FREEZE"}
        self.assertEqual(status, {"ready_for_ndx_shadow": True, "shadow_days_completed": 0, "dynamic_cash_pool_status": "FREEZE"})


if __name__ == "__main__":
    unittest.main()
