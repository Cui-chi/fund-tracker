#!/bin/sh
set -eu
exec env TZ=Asia/Singapore PYTHONUNBUFFERED=1 python3 "/Users/cuichi/Documents/New project/fund_tracker/scripts/run_ndx_shadow_daily.py"
