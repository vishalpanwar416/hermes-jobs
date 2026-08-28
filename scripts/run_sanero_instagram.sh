#!/usr/bin/env bash
# Sanero Media daily Instagram post: generate -> stamp logo -> publish.
# brand_poster.py honors BRAND_DIR (logo + queue resolve under the brand dir).
set -euo pipefail
PIPE_NAME=sanero_instagram_post
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"
export BRAND_DIR="/home/vishalpanwar/Development/Aarambh/Table-Tap media/brands/sanero"
export BRAND_LOGO_STYLE="light-mark"
cd "/home/vishalpanwar/Development/Aarambh/Table-Tap media"
node src/index.js generate
"/home/vishalpanwar/Development/Aarambh/hermes-agent/.venv/bin/python" "/home/vishalpanwar/.hermes/scripts/brand_poster.py"
cd "/home/vishalpanwar/Development/Aarambh/Table-Tap media"
exec node src/index.js publish
