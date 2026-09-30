#!/usr/bin/env bash
# Copy the app from your Mac to the server over SSH. Run from the project folder on your Mac:
#     deploy/push.sh ubuntu@<server-ip>              code only: use this for updates (then restart the service)
#     deploy/push.sh ubuntu@<server-ip> --with-data  the first time: also your accounts, bot and paper-test
#                                                    history, keys, price and market history. It refuses if the app is already running there.
# Your Mac's .env is never copied (the server has its own). After moving the data, stop the app on your Mac so
# only one copy runs; otherwise both trade their own demo accounts and both send you alerts.
set -euo pipefail
TARGET="${1:?usage: deploy/push.sh user@server [--with-data]}"
cd "$(dirname "$0")/.."
DEST="copy-signals"

echo "== Copying the code to $TARGET:~/$DEST"
rsync -az --delete \
  --exclude /.venv --exclude /data --exclude /.env --exclude /.claude \
  --exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store \
  ./ "$TARGET:$DEST/"

if [ "${2:-}" = "--with-data" ]; then
  if ssh "$TARGET" "systemctl is-active --quiet copy-signals"; then
    echo "The app is running on the server; copying the data now would overwrite live data. Stop it first:" >&2
    echo "    ssh $TARGET sudo systemctl stop copy-signals" >&2
    exit 1
  fi
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "== Making consistent copies of the databases (safe while the app runs on your Mac)"
  sqlite3 data/trading.db ".backup '$TMP/trading.db'"
  [ -f data/history.db ] && sqlite3 data/history.db ".backup '$TMP/history.db'"
  cp data/secret.key "$TMP/secret.key"
  for f in ls_model.pkl ls_backtest.json; do  # the long/short model and its backtest (else they're rebuilt there)
    [ -f "data/$f" ] && cp "data/$f" "$TMP/$f"
  done
  echo "== Copying data (the key that decrypts your stored OKX key goes over the encrypted SSH connection)"
  ssh "$TARGET" "mkdir -p $DEST/data && chmod 700 $DEST/data"
  rsync -az "$TMP/" "$TARGET:$DEST/data/"
  ssh "$TARGET" "chmod 600 $DEST/data/secret.key"
fi
echo "== Done. On the server: bash $DEST/deploy/setup.sh (first time) or sudo systemctl restart copy-signals (updates)"
