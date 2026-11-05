#!/bin/bash
# Stops Phone2Mac and removes it from login items.
# Your received files in ~/Phone2Mac are kept.
set -uo pipefail
LABEL="com.phone2mac.server"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/.phone2mac/app"
echo "Phone2Mac stopped and removed from login."
echo "Kept: ~/Phone2Mac (your received files) and ~/.phone2mac (history + pairing key)."
echo "To erase those too:  rm -rf ~/.phone2mac ~/Phone2Mac"
