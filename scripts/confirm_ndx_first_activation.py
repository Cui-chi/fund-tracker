#!/usr/bin/env python3
"""Explicitly confirm the one-time NDX post-shadow lifecycle gate."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ndx_shadow_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=ROOT / "reports" / "shadow" / "ndx-v1" / "shadow-ledger.json",
    )
    parser.add_argument("--confirmed-by", required=True)
    args = parser.parse_args()
    ledger = ndx_shadow_run.confirm_first_activation_guard(
        args.ledger, confirmed_by=args.confirmed_by,
    )
    print(json.dumps({
        "activation_status": ledger.get("activation_status"),
        "first_activation_guard": ledger.get("first_activation_guard"),
        "first_activation_guard_status": ledger.get("first_activation_guard_status"),
        "decision_status": ledger.get("decision_status"),
        "dynamic_cash_pool_status": ledger.get("dynamic_cash_pool_status"),
        "first_activation_confirmed_at": ledger.get("first_activation_confirmed_at"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
