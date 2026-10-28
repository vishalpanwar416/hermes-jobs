#!/usr/bin/env bash
# inbox_reply_scanner: scan the Gmail inbox for replies to sent applications,
# then send the due follow-ups, and hand both JSON results to the cron agent.
#
# Was an agent-mode job; on 2026-09-11 the model wandered off into a search
# tool that timed out and asked the user to retry. As a cron `script` the
# runner executes this wrapper and the agent only formats the output.
set -uo pipefail
PIPE_NAME=inbox_reply_scanner
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"

PY="$HOME/Development/Aarambh/hermes-agent/.venv/bin/python"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== SCAN ==="
"$PY" "$DIR/application_tracker.py" scan
echo
echo "=== FOLLOWUP ==="
"$PY" "$DIR/application_tracker.py" followup
echo
echo "=== STATS ==="
"$PY" "$DIR/application_tracker.py" stats
echo "=== END ==="
exit 0
