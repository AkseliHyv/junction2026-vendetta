#!/usr/bin/env bash
# Copy this App Lab app to an Arduino UNO Q over SSH.
#   ./deploy.sh <board-ip-or-hostname> [app-folder-name]
# Then open it in Arduino App Lab and press Run (that flashes sketch/ to the
# STM32 and starts python/main.py on the Linux side).
set -euo pipefail
BOARD="${1:?usage: ./deploy.sh <board-ip-or-hostname> [app-folder-name]}"
NAME="${2:-night-noise-box}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="/home/arduino/ArduinoApps/$NAME"

echo "-> copying $HERE to arduino@$BOARD:$DEST"
# tar keeps it dependency-free (no rsync needed) and skips local junk.
tar -C "$HERE" -cz \
    --exclude='__pycache__' --exclude='*.sqlite3' --exclude='*.calibration.json' \
    --exclude='deploy.sh' --exclude='.gitignore' . \
  | ssh "arduino@$BOARD" "mkdir -p '$DEST' && tar -C '$DEST' -xz"
echo "-> done. Open '$NAME' in Arduino App Lab and press Run."
