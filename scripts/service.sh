#!/usr/bin/env bash
# Run the backend as a macOS login service (launchd user agent) so it is up
# whenever you are logged in, instead of starting scripts/run.sh by hand.
#
#   scripts/service.sh install     write the plist and start it now
#   scripts/service.sh uninstall   stop it and remove the plist
#   scripts/service.sh restart     e.g. after pulling code changes
#   scripts/service.sh status
#   scripts/service.sh logs        tail the backend log
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.outlook2obsidian2do.backend"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/outlook2obsidian2do"
LOG="$LOG_DIR/backend.log"
DOMAIN="gui/$(id -u)"

write_plist() {
  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <!-- run.sh sources .env, checks the TLS cert and execs uvicorn from .venv. -->
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/scripts/run.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO</string>

  <!-- launchd gives agents a minimal PATH; add Homebrew so ollama/security
       and anything run.sh shells out to are found. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>
  <!-- Restart if it exits for any reason (crash, kill, cert missing after
       an upgrade). ThrottleInterval keeps a persistent failure from spinning. -->
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
EOF
}

is_loaded() {
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

start() {
  # bootstrap fails if already loaded, so unload first for a clean restart.
  # RunAtLoad starts the process as part of bootstrap.
  if is_loaded; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    sleep 1
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST"
}

stop() {
  if is_loaded; then
    launchctl bootout "$DOMAIN/$LABEL"
  fi
}

status() {
  if is_loaded; then
    launchctl print "$DOMAIN/$LABEL" | grep -E "state|pid|last exit" || true
  else
    echo "$LABEL: not loaded"
  fi
  local port
  port="$(grep -E '^PORT=' "$REPO/.env" 2>/dev/null | cut -d= -f2 || true)"
  port="${port:-8000}"
  if curl -sk --max-time 3 "https://localhost:${port}/api/health" >/dev/null 2>&1; then
    echo "backend: responding on https://localhost:${port}"
  else
    echo "backend: NOT responding on https://localhost:${port} (see: $0 logs)"
  fi
  if command -v ollama >/dev/null 2>&1; then
    if curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
      echo "ollama: running"
    else
      echo "ollama: not running. For a login service: brew services start ollama"
    fi
  fi
}

case "${1:-}" in
  install)
    if [[ ! -x "$REPO/.venv/bin/python" ]]; then
      echo "No .venv - run the Setup steps in README.md first." >&2
      exit 1
    fi
    write_plist
    start
    echo "Installed $PLIST"
    echo "Log: $LOG"
    sleep 2
    status
    ;;
  uninstall)
    stop
    rm -f "$PLIST"
    echo "Removed $LABEL"
    ;;
  restart)
    [[ -f "$PLIST" ]] || { echo "Not installed. Run: $0 install" >&2; exit 1; }
    start
    sleep 2
    status
    ;;
  status)
    status
    ;;
  logs)
    touch "$LOG"
    tail -n 50 -f "$LOG"
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
