#!/usr/bin/env bash
set -euo pipefail

URL="${1:-http://127.0.0.1:8080}"

command -v xset >/dev/null 2>&1 && xset s off || true
command -v xset >/dev/null 2>&1 && xset -dpms || true
command -v xset >/dev/null 2>&1 && xset s noblank || true

if command -v chromium >/dev/null 2>&1; then
  CHROME_BIN="chromium"
elif command -v chromium-browser >/dev/null 2>&1; then
  CHROME_BIN="chromium-browser"
else
  echo "Chromium not found. Install: sudo apt-get install -y chromium-browser"
  exit 1
fi

EXTRA=""
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
  EXTRA="--ozone-platform=wayland --enable-features=UseOzonePlatform"
fi

exec $CHROME_BIN       --app="$URL"       --kiosk       --incognito       --noerrdialogs       --disable-infobars       --disable-session-crashed-bubble       --disable-features=TranslateUI       --check-for-update-interval=31536000       --autoplay-policy=no-user-gesture-required       --class=HoverscanKiosk       $EXTRA
