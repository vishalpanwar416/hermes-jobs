#!/usr/bin/env bash
set -euo pipefail
PIPE_NAME=tabletap_linkedin_post
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"
cd "/home/vishalpanwar/.hermes/scripts"
exec "/home/vishalpanwar/Development/Aarambh/hermes-agent/.venv/bin/python" tabletap_linkedin_post.py
