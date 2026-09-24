#!/usr/bin/env bash
#
# One-line install / update of the CC RMS MQTT monitor into ~/source/CC_Utils/MQTT_monitor:
#
#   curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-mqtt-monitor/master/install.sh | bash
#
# Same command on every CC RMS utility. Idempotent: re-run it any time.
# Overrides: CC_DEST (checkout), CC_BRANCH, CC_REPO_URL,
# CC_GROUP, CC_NO_AUTOUPDATE=1 (see scripts/deploy_station.sh).
#
set -euo pipefail

REPO_URL="${CC_REPO_URL:-https://github.com/Cybis320/cc-rms-mqtt-monitor.git}"
BRANCH="${CC_BRANCH:-master}"
DEST="${CC_DEST:-$HOME/source/CC_Utils/MQTT_monitor}"
SETUP="scripts/deploy_station.sh"

# Run from a checkout (./install.sh): set up that checkout as is.
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
    HERE="$(cd "$(dirname "$SELF")" && pwd)"
    if [ -f "$HERE/$SETUP" ]; then
        exec bash "$HERE/$SETUP" "$@"
    fi
fi

# Piped from curl: clone or update the checkout, then run its setup.
command -v git >/dev/null 2>&1 || { echo "git is required: sudo apt-get install -y git" >&2; exit 1; }
if [ -d "$DEST/.git" ]; then
    echo "Updating $DEST"
    if ! { git -C "$DEST" fetch --quiet origin "$BRANCH" \
            && git -C "$DEST" merge --ff-only --quiet "origin/$BRANCH"; }; then
        echo "WARNING: could not fast-forward $DEST (offline, or local changes); using it as is." >&2
    fi
else
    echo "Cloning $REPO_URL -> $DEST"
    mkdir -p "$(dirname "$DEST")"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DEST"
fi
if [ ! -f "$DEST/$SETUP" ]; then
    echo "ERROR: $DEST predates this installer and could not be updated." >&2
    echo "       See: git -C $DEST status   (then re-run this command)" >&2
    exit 1
fi
exec bash "$DEST/$SETUP" "$@"
