#!/bin/bash
# Launches Google Chrome with remote debugging so Playwright can attach to your
# logged-in session (e.g. claude.ai). Quit Chrome first if it is already running.

set -euo pipefail

pkill -a -i "Google Chrome" 2>/dev/null || true
sleep 2

/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --no-first-run \
  --no-default-browser-check \
  &

echo "✅ Chrome launched with remote debugging on port 9222"
echo "Log in to Claude.ai, then start your voice control server (python main.py)."
