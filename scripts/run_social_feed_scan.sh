#!/usr/bin/env bash
# jobs_social_feed_scan: run the X and LinkedIn feed monitors back to back and
# hand both JSON results to the cron agent for formatting.
#
# This used to be an agent-mode job whose prompt told the model to run the two
# scripts through the terminal tool. That tool has a 180s timeout, each monitor
# waits up to 600s for the shared Chrome profile lock, so the monitors were
# killed mid-wait on every busy tick and the model reported whatever it
# guessed. As a cron `script`, the runner executes this wrapper itself with a
# one-hour budget and the agent only sees the finished output.
#
# Each monitor takes the Chrome profile lock on its own, so they run
# sequentially here and never fight each other. A monitor that cannot get the
# lock exits with "Skipping run" and an empty list; that is a normal outcome.
set -uo pipefail
PIPE_NAME=jobs_social_feed_scan
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"

PY="$HOME/Development/Aarambh/hermes-agent/.venv/bin/python"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== X_FEED_MONITOR ==="
"$PY" "$DIR/x_feed_monitor.py" 2>/dev/null
echo
echo "=== LINKEDIN_FEED_MONITOR ==="
"$PY" "$DIR/linkedin_feed_monitor.py" 2>/dev/null
echo
echo "=== END ==="
exit 0
