#!/usr/bin/env bash
# Push this repo's scripts into the live Hermes scripts directory.
#
# Hermes resolves a job's `script` field as a filename inside ~/.hermes/scripts,
# so that directory has to hold the real files — it cannot point at a checkout.
# This repo is the source of truth and this script copies it into place.
#
#   ./sync.sh            # show what would change, then copy
#   ./sync.sh --dry-run  # show what would change, copy nothing
#   ./sync.sh --pull     # copy the other way: live -> repo (to capture a hotfix)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LIVE="${HERMES_SCRIPTS:-$HOME/.hermes/scripts}"
MODE=push
DRY=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY="--dry-run" ;;
    --pull) MODE=pull ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -d "$LIVE" ]]; then
  echo "no live scripts directory at $LIVE" >&2
  exit 1
fi

# Only the files this repo tracks move. The live directory also holds ad-hoc
# debugging scripts and .env files that deliberately stay out of git, so a
# mirroring --delete would destroy them.
mapfile -t FILES < <(cd "$REPO/scripts" && ls -1)

if [[ "$MODE" == push ]]; then
  echo "repo -> $LIVE"
  for f in "${FILES[@]}"; do
    if ! cmp -s "$REPO/scripts/$f" "$LIVE/$f" 2>/dev/null; then
      echo "  update $f"
      [[ -n "$DRY" ]] || cp -p "$REPO/scripts/$f" "$LIVE/$f"
    fi
  done
  [[ -n "$DRY" ]] || chmod +x "$LIVE"/run_*.sh
else
  echo "$LIVE -> repo"
  for f in "${FILES[@]}"; do
    if [[ -f "$LIVE/$f" ]] && ! cmp -s "$LIVE/$f" "$REPO/scripts/$f"; then
      echo "  update $f"
      [[ -n "$DRY" ]] || cp -p "$LIVE/$f" "$REPO/scripts/$f"
    fi
  done
fi

echo "done${DRY:+ (dry run, nothing written)}"
