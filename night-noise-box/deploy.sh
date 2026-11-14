#!/usr/bin/env bash
# Copies this app to the board over SSH. Then open it in App Lab and press Run.
#   ./deploy.sh <board-ip-or-hostname> [app-folder-name]
set -euo pipefail
BOARD="${1:?usage: ./deploy.sh <board-ip-or-hostname> [app-folder-name]}"
NAME="${2:-night-noise-box}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="/home/arduino/ArduinoApps/$NAME"

echo "-> copying $HERE to arduino@$BOARD:$DEST"
COPYFILE_DISABLE=1 tar -C "$HERE" -cz --exclude='._*' --exclude='.DS_Store' --exclude='.cache' \
    --exclude='__pycache__' --exclude='*.sqlite3' --exclude='*.calibration.json' \
    --exclude='deploy.sh' --exclude='.gitignore' . \
  | ssh "arduino@$BOARD" "mkdir -p '$DEST' && tar -C '$DEST' -xz"
echo "-> done. Open '$NAME' in Arduino App Lab and press Run."
