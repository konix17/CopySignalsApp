#!/usr/bin/env bash
# One-time setup on the server (Ubuntu 24.04 or newer, ARM or x86). Run from the app folder on the server:
#     bash deploy/setup.sh
# Installs the app's pinned dependencies (checksums verified) and a systemd service that runs the app 24/7.
# Safe to run again, e.g. after an update that changes requirements.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
APP_USER="$(id -un)"

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
  echo "Python 3.12 or newer is needed (Ubuntu 24.04 has it). Found: $(python3 --version)" >&2
  exit 1
fi

echo "== Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv sqlite3 >/dev/null

echo "== Installing the app's dependencies"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q --require-hashes -r requirements.txt

mkdir -p data
chmod 700 data
[ -f data/secret.key ] && chmod 600 data/secret.key
if [ ! -f .env ]; then
  cp deploy/env.server.example .env
  chmod 600 .env
  echo "== Created .env: put your Tailscale address in ALLOWED_HOSTS (see README → Hosting)"
fi

echo "== Installing the service"
sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@APP_USER@|$APP_USER|g" deploy/copy-signals.service \
  | sudo tee /etc/systemd/system/copy-signals.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable copy-signals >/dev/null
sudo systemctl restart copy-signals

sleep 4
if curl -fsS -o /dev/null http://127.0.0.1:8000/login.html; then
  echo "== Copy Signals is running (it restarts by itself after crashes and reboots)."
  echo "   Logs: journalctl -u copy-signals -f   and   $APP_DIR/data/logs/app.log"
else
  echo "== The app didn't answer. Check: journalctl -u copy-signals -n 50" >&2
  exit 1
fi
