#!/usr/bin/env bash
# Sanero Media: refresh Instagram engagement for recent posts and mirror the
# numbers into Firestore for the admin panel.
set -euo pipefail
PIPE_NAME=sanero_engagement
export PIPE_NAME
source "$(dirname "$0")/_log_preamble.sh"
export BRAND_DIR="/home/vishalpanwar/Development/Aarambh/Table-Tap media/brands/sanero"
cd "/home/vishalpanwar/Development/Aarambh/Table-Tap media"
exec node src/index.js engagement
