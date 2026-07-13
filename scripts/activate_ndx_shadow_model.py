#!/usr/bin/env python3
"""Approve NDX V1 after completed shadow validation.

This is an explicit lifecycle transition only. It records activation approval
in the shadow ledger and keeps Dynamic Cash Pool execution subject to the normal
decision and user-confirmation flow.
"""

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ndx_shadow_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=str(ROOT / "reports/shadow/ndx-v1/shadow-ledger.json"))
    parser.add_argument("--approved-by", default="manual_codex_review")
    parser.add_argument("--approved-at")
    args = parser.parse_args()
    approved_at = dt.datetime.fromisoformat(args.approved_at) if args.approved_at else None
    ledger = ndx_shadow_run.approve_manual_activation(
        args.ledger, approved_by=args.approved_by, generated_at=approved_at
    )
    print(json.dumps({
        "status": ledger.get("status"),
        "activation_status": ledger.get("activation_status"),
        "model_status": ledger.get("model_status"),
        "decision_status": ledger.get("decision_status"),
        "dynamic_cash_pool_status": ledger.get("dynamic_cash_pool_status"),
        "shadow_days_completed": ledger.get("shadow_days_completed"),
        "required_complete_days": ledger.get("required_complete_days"),
        "activation_approved_at": ledger.get("activation_approved_at"),
        "first_activation_guard": ledger.get("first_activation_guard"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
