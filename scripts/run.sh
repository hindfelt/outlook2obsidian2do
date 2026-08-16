#!/usr/bin/env bash
# Start the backend over HTTPS (Office add-ins refuse plain HTTP) and serve the
# add-in's static files from the same origin.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

CERT="${SSL_CERTFILE:-$HOME/.office-addin-dev-certs/localhost.crt}"
KEY="${SSL_KEYFILE:-$HOME/.office-addin-dev-certs/localhost.key}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  echo "Trusted localhost certificate not found at:"
  echo "  $CERT"
  echo "  $KEY"
  echo
  echo "Generate one (installs into the macOS keychain, asks for your password):"
  echo "  npx --yes office-addin-dev-certs install"
  exit 1
fi

exec .venv/bin/python -m uvicorn backend.app.main:app \
  --host "$HOST" --port "$PORT" \
  --ssl-certfile "$CERT" --ssl-keyfile "$KEY"
