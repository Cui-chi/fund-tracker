#!/usr/bin/env python3
"""Initialize or inspect the governed NDX three-session shadow ledger."""

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ndx_shadow_run


def _lag_text(primary_date, validator_date):
    if not primary_date or not validator_date:
        return "unavailable"
    primary = ndx_shadow_run._parse_date(primary_date)
    validator = ndx_shadow_run._parse_date(validator_date)
    days = abs((primary - validator).days)
    return "lag %d day%s" % (days, "" if days == 1 else "s")


def print_shadow_summary(result):
    data = result.get("ndx_data_layer") or {}
    primary = data.get("price_primary") or data.get("primary") or {}
    proxies = {row.get("source"): row for row in data.get("proxy_validators", [])}
    macros = {row.get("source"): row for row in data.get("macro_inputs", [])}
    primary_date = primary.get("date")
    primary_ok = result.get("shadow_evaluation", {}).get("primary_gate", {}).get("decision") == "READY"
    decision = result.get("shadow_evaluation", {}).get("decision") or result.get("shadow_evaluation", {}).get("primary_gate", {}).get("decision")
    if result.get("shadow_evaluation", {}).get("failures") and decision == "READY":
        decision = "FAIL"
    reason = result.get("shadow_evaluation", {}).get("primary_gate", {}).get("reason") or result.get("reason") or "ok"
    print("PRIMARY: %s %s %s" % (primary.get("source") or "QQQ", primary_date or "missing", "OK" if primary_ok else "NOT_OK"))
    print("QQQ_PROXY: %s" % _lag_text(primary_date, proxies.get("QQQ_PROXY", {}).get("date")))
    print("DFII10: %s" % _lag_text(primary_date, macros.get("DFII10", {}).get("date")))
    print("DECISION: %s" % (decision or "NOT_READY"))
    print("REASON: %s" % reason)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=str(ROOT / "reports/shadow/ndx-v1/shadow-ledger.json"))
    parser.add_argument("--day0-report")
    parser.add_argument("--initialize-day0", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--run-session")
    parser.add_argument("--report")
    parser.add_argument("--shadow-root", default=str(ROOT / "reports/shadow/ndx-v1"))
    parser.add_argument("--qdii-latest", default=str(ROOT / "data/qdii-carrier-latest.json"))
    parser.add_argument("--qdii-raw", default=str(ROOT.parent / "qdii-monitor/carrier_snapshot.json"))
    parser.add_argument("--browser-verified", action="store_true")
    parser.add_argument("--evaluated-at")
    args = parser.parse_args()
    ledger_path = Path(args.ledger)
    evaluated = dt.datetime.fromisoformat(args.evaluated_at) if args.evaluated_at else dt.datetime.now().astimezone()
    if args.run_session:
        if not args.report:
            parser.error("--report is required for --run-session")
        result = ndx_shadow_run.run_shadow_session(
            args.report, ledger_path, args.shadow_root, dt.date.fromisoformat(args.run_session),
            evaluated, args.qdii_latest, args.qdii_raw, browser_verified=args.browser_verified,
        )
        print_shadow_summary(result)
        return
    if args.initialize_day0:
        if not args.day0_report:
            parser.error("--day0-report is required")
        ledger = ndx_shadow_run.initialize_ledger(args.day0_report, ledger_path, generated_at=evaluated)
    else:
        ledger = ndx_shadow_run.load_ledger(ledger_path)
    print(json.dumps(ndx_shadow_run.pending_status(ledger, evaluated_at=evaluated), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
