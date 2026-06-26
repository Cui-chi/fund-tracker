#!/bin/sh
set -eu
LABEL="com.codex.ndx-shadow-1310"
ROOT="/Users/cuichi/Documents/New project/fund_tracker"
SRC="$ROOT/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER_DIR="$HOME/Library/Application Support/fund_tracker_launch"
LOG_DIR="$HOME/Library/Logs/fund_tracker"
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$WRAPPER_DIR" "$LOG_DIR"
cp "$ROOT/scripts/run_ndx_shadow_1310.sh" "$WRAPPER_DIR/run_ndx_shadow_1310.sh"
chmod 755 "$WRAPPER_DIR/run_ndx_shadow_1310.sh"
cp "$SRC" "$DST"
launchctl bootout "gui/$(id -u)" "$DST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null
echo "INSTALLED $LABEL"
