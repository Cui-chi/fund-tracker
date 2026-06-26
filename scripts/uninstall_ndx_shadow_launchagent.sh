#!/bin/sh
set -eu
LABEL="com.codex.ndx-shadow-1310"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER="$HOME/Library/Application Support/fund_tracker_launch/run_ndx_shadow_1310.sh"
launchctl bootout "gui/$(id -u)" "$DST" >/dev/null 2>&1 || true
rm -f "$DST"
rm -f "$WRAPPER"
echo "UNINSTALLED $LABEL"
