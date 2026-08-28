#!/usr/bin/env bash
set -euo pipefail
PIPE_NAME=social_bait_muter
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"
cd "/home/vishalpanwar/.hermes/scripts"
exec "/home/vishalpanwar/Development/Aarambh/hermes-agent/.venv/bin/python" mute_bait_accounts.py --threshold 3 --limit 5
