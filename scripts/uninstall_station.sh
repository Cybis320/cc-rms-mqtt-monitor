#!/usr/bin/env bash
#
# Uninstaller for the CC RMS MQTT monitor -- the inverse of deploy_station.sh.
#
#   ./scripts/uninstall_station.sh            # remove service + timer, clear broker
#   CC_PURGE=1 ./scripts/uninstall_station.sh # also delete the checkout + config
#
# It is safe to re-run. By default it leaves the code/config in place (so a
# re-install keeps your settings); set CC_PURGE=1 to remove those too.
#
set -euo pipefail

# --- Settings (match deploy_station.sh; override via environment) -----------
DEST="${CC_DEST:-$HOME/source/CC_Utils/MQTT_monitor}"
VENV="${CC_VENV:-$HOME/vRMS}"
SERVICE_NAME="cc-rms-monitor"

info() { printf '\033[32m[uninstall]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[uninstall]\033[0m %s\n' "$1"; }

# Prefer running from inside a checkout if we're in one.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
    DEST="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

if [ -x "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"
elif [ -x "$DEST/.venv/bin/python" ]; then
    PY="$DEST/.venv/bin/python"
else
    PY="$(command -v python3 || true)"
fi

# --- 1. Stop + disable + remove the systemd units ---------------------------
# Stop the agent FIRST so it can't re-publish the records we're about to clear.
if command -v systemctl >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
    for unit in "$SERVICE_NAME.service" "$SERVICE_NAME-update.timer" "$SERVICE_NAME-update.service"; do
        if $SUDO systemctl list-unit-files "$unit" >/dev/null 2>&1 \
           && $SUDO systemctl cat "$unit" >/dev/null 2>&1; then
            info "Stopping and disabling $unit"
            $SUDO systemctl disable --now "$unit" >/dev/null 2>&1 || true
            $SUDO rm -f "/etc/systemd/system/$unit"
        fi
    done
    $SUDO systemctl daemon-reload
    $SUDO systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
    info "systemd units removed"
else
    warn "systemd not found; if you started it manually, stop that process now,"
    warn "  otherwise it will re-publish the records cleared in the next step"
fi

# --- 2. Clear retained broker data (best-effort, AFTER the agent is stopped) -
# Otherwise the dashboard keeps showing this host's stale records forever. This
# also clears the "offline" Last-Will the stop above leaves on the status topic.
if [ -n "$PY" ] && [ -f "$DEST/config.yaml" ]; then
    info "Clearing retained broker records (status + host + stations)"
    "$PY" -m cc_mqtt_monitor --config "$DEST/config.yaml" --unpublish \
        || warn "Could not reach broker to clear records (continuing anyway)"
else
    warn "Skipping broker clear (no python/config found); records may linger"
fi

# --- 3. Optionally remove the checkout + config -----------------------------
if [ "${CC_PURGE:-0}" = "1" ]; then
    if [ -n "$VENV" ] && [ -x "$VENV/bin/pip" ]; then
        info "Uninstalling the package from $VENV"
        "$VENV/bin/pip" uninstall -y cc-rms-mqtt-monitor >/dev/null 2>&1 || true
    fi
    if [ -d "$DEST" ]; then
        info "Removing checkout $DEST"
        rm -rf "$DEST"
    fi
    info "Purge complete."
else
    info "Left checkout + config in place at $DEST"
    info "  (re-install keeps your settings; set CC_PURGE=1 to remove them too)"
fi

info "Done. The monitor no longer runs and its broker records are cleared."
