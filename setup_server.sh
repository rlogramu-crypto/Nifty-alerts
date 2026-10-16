#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu / Debian server. Run from inside the cloned repo:  bash setup_server.sh
set -e
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
DIR="$(cd "$(dirname "$0")" && pwd)"
ENVFILE="$HOME/.nifty_env"

echo "== Installing packages"
$SUDO apt-get update -y
$SUDO apt-get install -y python3 python3-venv python3-pip git cron
$SUDO timedatectl set-timezone Asia/Kolkata || true

echo "== Python environment"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

if [ ! -f "$ENVFILE" ]; then
  echo "== Secrets (stored only in $ENVFILE, readable only by you)"
  read -r -p "Upstox token (Analytics token): " UT
  read -r -p "Telegram bot token: " TB
  read -r -p "Telegram chat id: " TC
  ( umask 077; printf 'UPSTOX_ACCESS_TOKEN=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$UT" "$TB" "$TC" > "$ENVFILE" )
fi
chmod 600 "$ENVFILE"
chmod +x "$DIR/run.sh"

echo "== Schedule (India time, Mon-Fri)"
( crontab -l 2>/dev/null | grep -v "$DIR/run.sh" || true
  echo "55 8 * * 1-5 $DIR/run.sh scanner"
  echo "*/5 9-15 * * 1-5 $DIR/run.sh scanner"
  echo "40 15 * * 1-5 $DIR/run.sh dayfile" ) | crontab -
$SUDO systemctl enable --now cron >/dev/null 2>&1 || true

echo "== Telegram test"
cd "$DIR"
set -a; . "$ENVFILE"; set +a
"$DIR/.venv/bin/python" -c "from nifty_scanner_upstox import send_telegram; send_telegram('Server setup done. The scanner starts by itself at 08:55 IST on weekdays.')"
echo "Done. Check Telegram for the test message."
