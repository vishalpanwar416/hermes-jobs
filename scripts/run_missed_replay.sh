#!/usr/bin/env bash
# Hermes runs the 'script' field as a FILE inside ~/.hermes/scripts, not as a
# shell command, so multi-step commands need a wrapper like this one.
set -euo pipefail
PIPE_NAME=cron_missed_replay
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"
cd "/home/vishalpanwar/Development/Aarambh/hermes-agent"
exec "/home/vishalpanwar/Development/Aarambh/hermes-agent/.venv/bin/python" -m cron.missed_queue replay-pending --limit 1 --max-age-hours 24
