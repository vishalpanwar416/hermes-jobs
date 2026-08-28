#!/usr/bin/env bash
# generate -> stamp the real logo -> publish. Branding must happen between the
# two Node steps, which is why this is a wrapper and not 'node src/index.js run'.
set -euo pipefail
PIPE_NAME=tabletap_instagram_post
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"
cd "/home/vishalpanwar/Development/Aarambh/Table-Tap media"
node src/index.js generate
"/home/vishalpanwar/Development/Aarambh/hermes-agent/.venv/bin/python" "/home/vishalpanwar/.hermes/scripts/brand_poster.py"
cd "/home/vishalpanwar/Development/Aarambh/Table-Tap media"
exec node src/index.js publish
