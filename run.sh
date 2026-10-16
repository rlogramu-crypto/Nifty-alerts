#!/usr/bin/env bash
# Starts the scanner (or the daily CSV job). Cron calls this; it is safe to call many times.
#   run.sh scanner   -> start the scanner if it is not already running (09:00-15:29 IST, Mon-Fri)
#   run.sh dayfile   -> send today's per-minute CSV to Telegram (about 15:40)
# Set FORCE=1 to ignore the weekday / time guard (for testing).
DIR="$(cd "$(dirname "$0")" && pwd)"
ENVFILE="${NIFTY_ENV:-$HOME/.nifty_env}"
cd "$DIR" || exit 1
mkdir -p logs
PY="$DIR/.venv/bin/python"
mode="${1:-scanner}"
[ -f "$ENVFILE" ] || { echo "Missing $ENVFILE"; exit 1; }
set -a; . "$ENVFILE"; set +a
export PYTHONUNBUFFERED=1
now=$((10#$(TZ=Asia/Kolkata date +%H%M)))
dow=$(TZ=Asia/Kolkata date +%u)
today=$(TZ=Asia/Kolkata date +%F)

if [ -z "$FORCE" ] && [ "$dow" -ge 6 ]; then exit 0; fi

if [ "$mode" = "scanner" ]; then
  if [ -z "$FORCE" ] && { [ "$now" -lt 855 ] || [ "$now" -gt 1529 ]; }; then exit 0; fi
  # already running? (the wrapper writes its own PID to .scanner.pid)
  if [ -f .scanner.pid ] && kill -0 "$(cat .scanner.pid)" 2>/dev/null; then exit 0; fi
  # a token failure stops restarts until the env file is updated with a new token
  if [ -f .token_failed ] && [ .token_failed -nt "$ENVFILE" ]; then exit 0; fi
  rm -f .token_failed
  export PY today
  nohup setsid bash -c 'echo $$ > .scanner.pid; "$PY" nifty_scanner_upstox.py >> "logs/scanner-$today.log" 2>&1; rc=$?; rm -f .scanner.pid; [ $rc -eq 3 ] && touch .token_failed' >/dev/null 2>&1 &
elif [ "$mode" = "dayfile" ]; then
  "$PY" day_file.py >> "logs/dayfile-$today.log" 2>&1
fi
