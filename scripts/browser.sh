#!/usr/bin/env bash
# Launch a browser with remote debugging enabled for the collection steps that
# need a logged-in session (FR-201).
#
# This uses a DEDICATED profile directory, so your normal browser profile is
# untouched. You log in yourself; Dream Job never sees your credentials
# (FR-202, NFR-203).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${DREAMJOB_BROWSER_PROFILE_DIR:-$ROOT/.chrome-profile}"
PORT="${DREAMJOB_CDP_PORT:-9222}"
mkdir -p "$PROFILE"

find_browser() {
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
    "$(command -v google-chrome || true)" \
    "$(command -v chromium || true)"; do
    [ -x "$candidate" ] && echo "$candidate" && return 0
  done
  return 1
}

BROWSER="$(find_browser)" || {
  echo "No Chromium-based browser found. Install Chrome, Chromium, Edge or Brave."
  exit 1
}

echo "Launching: $BROWSER"
echo "  debugging port : $PORT"
echo "  profile        : $PROFILE"
echo
echo "Log in to LinkedIn (or Glassdoor) in the window that opens, then return"
echo "to Dream Job and start the browser-automation step. Automation is paced"
echo "deliberately and stops on any challenge page."

exec "$BROWSER" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check
