#!/usr/bin/env bash
#
# Auto-update the CC RMS MQTT monitor from git, restart only if something
# changed. Intended to run as root from the systemd timer (so it can restart
# the service); git and pip run as the repo owner.
#
# Configurable via environment (the systemd unit sets these):
#   CC_DIR      checkout to update     (default /home/ops/source/CC_Utils/MQTT_monitor)
#   CC_USER     repo/venv owner        (default ops)
#   CC_VENV     virtualenv             (default /home/<CC_USER>/vRMS)
#   CC_SERVICE  systemd service        (default cc-rms-monitor)
#   CC_BRANCH   branch to track        (default master)
#
set -euo pipefail

DIR="${CC_DIR:-/home/ops/source/CC_Utils/MQTT_monitor}"
RUN_USER="${CC_USER:-ops}"
VENV="${CC_VENV:-/home/${RUN_USER}/vRMS}"
SERVICE="${CC_SERVICE:-cc-rms-monitor}"
BRANCH="${CC_BRANCH:-master}"
# A blocked update used to exit 0 and print to a journal nobody reads, so a station could
# sit on old code indefinitely with nothing to show for it. Record the reason where the
# monitor can publish it, and exit non-zero so systemd marks the unit failed.
MARKER="${CC_UPDATE_MARKER:-/var/lib/cc-rms-monitor/update_blocked}"
mkdir -p "$(dirname "$MARKER")" 2>/dev/null || true
blocked() {
    printf '%s\n' "$1" > "$MARKER" 2>/dev/null || true
    echo "$1" >&2
    exit 1
}

# Run a command as the repo owner when we're root; otherwise run it directly.
run_as() {
    if [ "$(id -u)" -eq 0 ]; then
        sudo -u "$RUN_USER" -H "$@"
    else
        "$@"
    fi
}

before="$(run_as git -C "$DIR" rev-parse HEAD)"
run_as git -C "$DIR" fetch --quiet origin "$BRANCH"

# Fast-forward only: never clobber local commits / diverged history.
if ! run_as git -C "$DIR" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
    blocked "diverged from origin/$BRANCH (local commits or dirty tree); auto-update skipped"
fi

after="$(run_as git -C "$DIR" rev-parse HEAD)"

rm -f "$MARKER" 2>/dev/null || true          # we fast-forwarded fine: not blocked

if [ "$before" = "$after" ]; then
    echo "Already up to date ($after)."
    exit 0
fi

echo "Updated $before -> $after; reinstalling and restarting $SERVICE."
# Reinstall (cheap; picks up any dependency/entry-point changes).
run_as "$VENV/bin/pip" install --quiet -e "$DIR"
systemctl restart "$SERVICE"
echo "Restarted $SERVICE."
