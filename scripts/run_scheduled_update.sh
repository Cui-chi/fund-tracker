#!/bin/sh
set -eu

PROJECT_ROOT='/Users/cuichi/Documents/New project/fund_tracker'
RUN_ID="$(date '+%Y-%m-%d_%H%M%S')_daily-update"
RUN_DIR="$PROJECT_ROOT/reports/runs/$RUN_ID"

if ! mkdir -p "$RUN_DIR/reports" "$RUN_DIR/json" "$RUN_DIR/html" "$RUN_DIR/csv" "$RUN_DIR/logs"; then
  echo 'BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED' >&2
  exit 2
fi

export ASSET_COPILOT_RUN_ID="$RUN_ID"
export ASSET_COPILOT_RUN_DIR="$RUN_DIR"
cd "$PROJECT_ROOT"
exec /usr/local/bin/python3 fund_tracker.py --update --report --alert --export >"$RUN_DIR/logs/audit-run.log" 2>"$RUN_DIR/logs/error.log"
