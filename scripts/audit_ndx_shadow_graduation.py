#!/usr/bin/env python3
"""Read-only NDX Shadow Graduation daily audit.

This script only inspects local shadow artifacts and appends a concise audit
record. It does not run Shadow, modify the ledger, fetch data, or regenerate
reports.
"""

import datetime as dt
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ndx_shadow_run
from scripts import run_ndx_shadow_daily as daily


SHADOW_ROOT = ROOT / "reports" / "shadow" / "ndx-v1"
SLA_PATH = SHADOW_ROOT / "source-sla.json"
LEDGER_PATH = SHADOW_ROOT / "shadow-ledger.json"
AUDIT_LOG = SHADOW_ROOT / "graduation-audit.log"
DEFAULT_MARKDOWN_DIR = ROOT / "docs" / "ndx-shadow-graduation"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


def _load_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _latest_sla_record(target_date):
    if not SLA_PATH.is_file():
        return None
    payload = _load_json(SLA_PATH)
    matches = [
        row for row in payload.get("records", [])
        if row.get("target_trade_date") == target_date.isoformat()
    ]
    return matches[-1] if matches else None


def _latest_prepared_snapshot(target_date):
    directory = SHADOW_ROOT / "prepared" / target_date.isoformat()
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*canonical-shadow-report.json"), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def _json_status(path):
    if not path or not Path(path).is_file():
        return False, None
    try:
        return True, _load_json(path)
    except (OSError, ValueError, TypeError):
        return False, None


def _today_local(now):
    return now.astimezone(LOCAL_TZ).date()


def _record_is_today(record, now):
    if not record:
        return False
    value = record.get("first_check_at")
    if not value:
        return False
    try:
        checked = dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=LOCAL_TZ)
    return checked.astimezone(LOCAL_TZ).date() == _today_local(now)


def _ledger_summary():
    if not LEDGER_PATH.is_file():
        return {
            "shadow_days_completed": 0,
            "days": [],
            "failures": [],
            "status": "MISSING",
            "decision_status": None,
            "dynamic_cash_pool_status": None,
        }
    ledger = _load_json(LEDGER_PATH)
    return {
        "shadow_days_completed": int(ledger.get("shadow_days_completed", 0) or 0),
        "days": ledger.get("days", []),
        "failures": ledger.get("failures", []),
        "status": ledger.get("status"),
        "decision_status": ledger.get("decision_status"),
        "dynamic_cash_pool_status": ledger.get("dynamic_cash_pool_status"),
    }


def _day_entry(ledger, target_date):
    target = target_date.isoformat()
    for row in ledger.get("days", []):
        if row.get("market_session_date") == target:
            return row
    return None


def _audit(now=None):
    now = now or daily.now_sgt()
    target_date = daily.latest_complete_us_session(now)
    ledger = _ledger_summary()
    if not target_date:
        return _result("NO_NEW_TRADING_DAY", None, None, ledger, "NO_COMPLETE_US_SESSION")

    record = _latest_sla_record(target_date)
    if not _record_is_today(record, now):
        return _result("NO_NEW_TRADING_DAY", target_date, record, ledger, "NO_TODAY_1310_RUN")

    prepared_path = _latest_prepared_snapshot(target_date)
    prepared_ok, prepared = _json_status(prepared_path)
    prepared_validation = (prepared or {}).get("copilot", {}).get("prepared_snapshot_validation", {})
    ndx_match = prepared_validation.get("ndx_input_match") is True
    macro_match = prepared_validation.get("macro_input_match") is True
    prepared_pass = prepared_ok and prepared_validation.get("status") == "PASS"

    day_dir = SHADOW_ROOT / target_date.isoformat()
    shadow_path = day_dir / "shadow-run.json"
    manifest_path = day_dir / "inputs" / "input-manifest.json"
    shadow_ok, shadow = _json_status(shadow_path)
    manifest_ok, manifest = _json_status(manifest_path)
    shadow_eval = (shadow or {}).get("shadow_evaluation", {})
    canonical_hash = shadow_eval.get("canonical_input_hash") or (shadow or {}).get("canonical_input_hash")
    manifest_hash = (manifest or {}).get("canonical_input_hash")
    hash_match = bool(manifest_ok and canonical_hash and canonical_hash == manifest_hash and manifest.get("hash_match") is True)
    failures = shadow_eval.get("failures") or []
    no_critical = not failures and not _contains_critical_fail(shadow)
    day_entry = _day_entry(ledger, target_date)
    ledger_pass = bool(day_entry and day_entry.get("result") == "PASS")
    data_ready = bool(record and record.get("ready_attempt") in ("FIRST", "RETRY"))
    shadow_runner_pass = bool(
        record
        and record.get("final_status") in ("SHADOW_EXECUTED", "ALREADY_COMPLETED")
        and record.get("shadow_executed") is True
        and shadow_ok
        and manifest_ok
    )
    pass_all = all([
        data_ready,
        prepared_pass,
        ndx_match,
        macro_match,
        hash_match,
        shadow_runner_pass,
        ledger_pass,
        no_critical,
    ])
    blocker = "NONE" if pass_all else _first_blocker(
        data_ready,
        prepared_pass,
        ndx_match,
        macro_match,
        hash_match,
        shadow_runner_pass,
        ledger_pass,
        no_critical,
        record,
    )
    result = _result("PASS" if pass_all else "NEED_FIX", target_date, record, ledger, blocker)
    result.update({
        "prepared_snapshot": str(prepared_path) if prepared_path else None,
        "prepared_json_valid": prepared_ok,
        "ndx_input_match": ndx_match,
        "macro_input_match": macro_match,
        "canonical_input_hash": canonical_hash,
        "manifest_canonical_input_hash": manifest_hash,
        "hash_match": hash_match,
        "shadow_run_json": str(shadow_path) if shadow_ok else None,
        "input_manifest_json": str(manifest_path) if manifest_ok else None,
        "no_critical_fail": no_critical,
        "formal_release_amount": _formal_release_amount(shadow, day_entry),
        "graduation_day_added": ledger_pass and record.get("final_status") == "SHADOW_EXECUTED",
    })
    return result


def _contains_critical_fail(payload):
    if payload is None:
        return False
    return "CRITICAL_FAIL" in json.dumps(payload, ensure_ascii=False)


def _formal_release_amount(shadow, day_entry):
    if shadow:
        formal = (shadow.get("v7_decision_chain") or {}).get("formal_decision") or {}
        if formal.get("formal_release_amount") is not None:
            return formal.get("formal_release_amount")
    if day_entry:
        return day_entry.get("formal_release_amount")
    return None


def _first_blocker(data_ready, prepared_pass, ndx_match, macro_match, hash_match,
                   shadow_runner_pass, ledger_pass, no_critical, record):
    if not data_ready:
        return "DATA_NOT_READY"
    if not prepared_pass:
        return "PREPARED_SNAPSHOT_NOT_PASS"
    if not ndx_match:
        return "NDX_INPUT_MISMATCH"
    if not macro_match:
        return "MACRO_INPUT_MISMATCH"
    if not hash_match:
        return "CANONICAL_HASH_MISMATCH"
    if not shadow_runner_pass:
        return "SHADOW_RUNNER_NOT_PASS:%s" % ((record or {}).get("final_status") or "NO_STATUS")
    if not ledger_pass:
        return "LEDGER_NOT_COUNTED"
    if not no_critical:
        return "CRITICAL_FAIL_PRESENT"
    return "UNKNOWN"


def _result(status, target_date, record, ledger, blocker):
    completed = ledger.get("shadow_days_completed", 0)
    required = ndx_shadow_run.REQUIRED_COMPLETE_DAYS
    return {
        "audited_at": daily.now_sgt().isoformat(timespec="seconds"),
        "commit_hash": _commit_hash(),
        "target_trade_date": target_date.isoformat() if target_date else None,
        "audit_status": status,
        "final_status": (record or {}).get("final_status"),
        "shadow_executed": bool((record or {}).get("shadow_executed")),
        "shadow_days_completed": completed,
        "graduation_progress": "%d / %d" % (completed, required),
        "ndx_input_match": False,
        "macro_input_match": False,
        "hash_match": False,
        "ledger_status": ledger.get("status"),
        "ledger_days": ledger.get("days", []),
        "ledger_failures": ledger.get("failures", []),
        "decision_status": ledger.get("decision_status"),
        "dynamic_cash_pool_status": ledger.get("dynamic_cash_pool_status"),
        "formal_release_amount": None,
        "blocker": blocker,
        "blocking_reason": blocker,
        "next_action": _next_action(status, blocker),
    }


def _commit_hash():
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            universal_newlines=True,
            timeout=5,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _next_action(status, blocker):
    if status == "PASS":
        return "Continue next scheduled shadow day."
    if status == "NO_NEW_TRADING_DAY":
        return "No action."
    return "Inspect blocker: %s." % blocker


def _append_log(result):
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


def _markdown_path(result, markdown_dir):
    audited_date = str(result.get("audited_at") or daily.now_sgt().isoformat())[:10]
    target = result.get("target_trade_date") or "no-new-trading-day"
    name = "%s_%s.md" % (audited_date, target)
    return Path(markdown_dir) / name


def _write_markdown(result, markdown_dir=DEFAULT_MARKDOWN_DIR):
    path = _markdown_path(result, markdown_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# NDX Shadow Graduation Audit",
        "",
        "- audited_at: `%s`" % result.get("audited_at"),
        "- commit_hash: `%s`" % result.get("commit_hash"),
        "- target_trade_date: `%s`" % (result.get("target_trade_date") or "-"),
        "- audit_status: `%s`" % result.get("audit_status"),
        "- final_status: `%s`" % (result.get("final_status") or "-"),
        "- shadow_executed: `%s`" % str(result.get("shadow_executed")).lower(),
        "- shadow_days_completed: `%s`" % result.get("shadow_days_completed"),
        "- graduation_progress: `%s`" % result.get("graduation_progress"),
        "- ndx_input_match: `%s`" % str(result.get("ndx_input_match")).lower(),
        "- macro_input_match: `%s`" % str(result.get("macro_input_match")).lower(),
        "- hash_match: `%s`" % str(result.get("hash_match")).lower(),
        "- decision_status: `%s`" % (result.get("decision_status") or "-"),
        "- dynamic_cash_pool_status: `%s`" % (result.get("dynamic_cash_pool_status") or "-"),
        "- formal_release_amount: `%s`" % result.get("formal_release_amount"),
        "- blocking_reason: `%s`" % result.get("blocking_reason"),
        "- next_action: `%s`" % result.get("next_action"),
        "",
        "## Local Artifact Evidence",
        "",
        "- prepared_snapshot: `%s`" % (result.get("prepared_snapshot") or "-"),
        "- shadow_run_json: `%s`" % (result.get("shadow_run_json") or "-"),
        "- input_manifest_json: `%s`" % (result.get("input_manifest_json") or "-"),
        "- canonical_input_hash: `%s`" % (result.get("canonical_input_hash") or "-"),
        "- manifest_canonical_input_hash: `%s`" % (result.get("manifest_canonical_input_hash") or "-"),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _print_summary(result):
    print("target_trade_date: %s" % (result.get("target_trade_date") or "-"))
    print("audit_status: %s" % result["audit_status"])
    print("shadow_executed: %s" % str(result["shadow_executed"]).lower())
    print("shadow_days_completed: %s" % result["shadow_days_completed"])
    print("graduation_progress: %s" % result["graduation_progress"])
    print("DCP: decision_status=%s dynamic_cash_pool_status=%s formal_release_amount=%s" % (
        result.get("decision_status") or "-",
        result.get("dynamic_cash_pool_status") or "-",
        result.get("formal_release_amount"),
    ))
    print("blocker: %s" % result["blocker"])
    print("next_action: %s" % result["next_action"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-markdown", action="store_true")
    parser.add_argument("--markdown-dir", default=str(DEFAULT_MARKDOWN_DIR))
    args = parser.parse_args()
    result = _audit()
    _append_log(result)
    if args.write_markdown:
        result["markdown_path"] = str(_write_markdown(result, args.markdown_dir))
    _print_summary(result)
    return 0 if result["audit_status"] in ("PASS", "NO_NEW_TRADING_DAY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
