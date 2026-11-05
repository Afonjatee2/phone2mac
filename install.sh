#!/bin/bash
# Installs Phone2Mac as a background service that starts at login and
# restarts itself automatically if it ever crashes.
set -euo pipefail

PORT="${PHONE2MAC_PORT:-8765}"
LABEL="com.phone2mac.server"
SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="$HOME/.phone2mac"
APP="$DATA/app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is for macOS. On other systems run: python3 phone2mac.py" >&2
  exit 1
fi

# --- find a working python3 (Apple's ships with the Command Line Tools) ---
PY=""
if xcode-select -p >/dev/null 2>&1 && /usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 8)' >/dev/null 2>&1; then
  PY=/usr/bin/python3
else
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
    if [[ -n "$c" && -x "$c" ]] && "$c" -c 'import sys; assert sys.version_info >= (3, 8)' >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "Python 3 is needed. Install Apple's Command Line Tools with:"
  echo "    xcode-select --install"
  echo "then run ./install.sh again."
  exit 1
fi
echo "Using Python: $PY ($("$PY" --version 2>&1))"

# --- copy the app somewhere launchd can always read ---------------------
# (launchd agents can't read ~/Desktop, ~/Documents or ~/Downloads without
#  extra permissions, so we never run it from wherever you downloaded it.)
mkdir -p "$APP/web" "$DATA/logs" "$HOME/Library/LaunchAgents" "$HOME/Phone2Mac"
cp "$SRC/phone2mac.py" "$APP/phone2mac.py"
cp "$SRC/web/index.html" "$APP/web/index.html"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$APP/phone2mac.py</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LANG</key><string>en_US.UTF-8</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>$DATA/logs/phone2mac.log</string>
  <key>StandardErrorPath</key><string>$DATA/logs/phone2mac.log</string>
</dict>
</plist>
EOF

# --- (re)start the service ------------------------------------------------
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
sleep 0.5
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

echo -n "Starting Phone2Mac"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; then echo " ✓"; break; fi
  echo -n "."; sleep 0.5
done
if ! curl -fsS "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; then
  echo
  echo "Phone2Mac didn't start. Last log lines:"
  tail -n 20 "$DATA/logs/phone2mac.log" 2>/dev/null || true
  exit 1
fi

# --- firewall hint ----------------------------------------------------------
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
if [[ -x "$FW" ]] && "$FW" --getglobalstate 2>/dev/null | grep -qi "enabled"; then
  if "$FW" --getblockall 2>/dev/null | grep -qi "enabled"; then
    echo
    echo "⚠️  Your firewall is set to 'Block all incoming connections', so your phone can't connect."
    echo "   Turn that off in System Settings → Network → Firewall → Options."
  else
    echo
    echo "ℹ️  The macOS firewall is on. If macOS asks whether Python may accept incoming"
    echo "   network connections, click Allow (otherwise the phone can't connect)."
  fi
fi

echo
echo "Phone2Mac is running and will start automatically when you log in."
echo "Opening the pairing page. Scan the QR code with your phone."
echo "  Files from your phone are saved in: ~/Phone2Mac"
echo "  Logs: $DATA/logs/phone2mac.log"
open "http://localhost:$PORT/pair"
