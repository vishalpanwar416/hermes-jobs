# Sourced by every run_*.sh wrapper. Tees the whole run to
# ~/.hermes/logs/pipelines/<name>/ while still writing to stdout, so the cron
# agent sees output exactly as before and the detail survives on disk.
PIPE_NAME="${PIPE_NAME:-$(basename "$0" .sh)}"
PIPE_LOG_DIR="$HOME/.hermes/logs/pipelines/$PIPE_NAME"
mkdir -p "$PIPE_LOG_DIR"
PIPE_LOG="$PIPE_LOG_DIR/$(date +%Y-%m-%d_%H-%M-%S).log"
export PIPE_LOG
exec > >(tee -a "$PIPE_LOG") 2>&1
ln -sfn "$PIPE_LOG" "$PIPE_LOG_DIR/latest.log"
echo "=== $PIPE_NAME :: $(date -Is) ==="
# Keep the newest 200 runs; these are small text files but jobs run hourly.
ls -1t "$PIPE_LOG_DIR"/*.log 2>/dev/null | tail -n +201 | xargs -r rm -f
