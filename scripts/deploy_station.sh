#!/usr/bin/env bash
#
# One-command installer for the CC RMS MQTT monitor on an RMS station.
#
#   curl -fsSL <raw-url>/scripts/deploy_station.sh | bash
#       -- or, from a clone --
#   ./scripts/deploy_station.sh
#
# It is idempotent: clones or updates the repo, installs into the RMS
# virtualenv, seeds config.yaml, and installs + starts a hardened systemd
# service (needs sudo for the service step only).
#
set -euo pipefail

# --- Settings (override via environment) ------------------------------------
REPO_URL="${CC_REPO_URL:-https://github.com/CroatianMeteorNetwork/CC_Utils.git}"
DEST="${CC_DEST:-$HOME/source/CC_Utils/MQTT_monitor}"
VENV="${CC_VENV:-$HOME/vRMS}"
SERVICE_NAME="cc-rms-monitor"
RUN_USER="$(id -un)"

info() { printf '\033[32m[deploy]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$1"; }

# --- 1. Get the code --------------------------------------------------------
# If we're already running from inside a clone, use it; otherwise clone/update.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
    DEST="$(cd "$SCRIPT_DIR/.." && pwd)"
    info "Using existing checkout at $DEST"
elif [ -d "$DEST/.git" ]; then
    info "Updating existing checkout at $DEST"
    git -C "$DEST" pull --ff-only
else
    info "Cloning $REPO_URL"
    mkdir -p "$(dirname "$DEST")"
    # The project lives in the MQTT_monitor subdir of the CC_Utils repo.
    tmp="$(mktemp -d)"
    git clone --depth 1 "$REPO_URL" "$tmp"
    if [ -d "$tmp/MQTT_monitor" ]; then
        mkdir -p "$(dirname "$DEST")"
        cp -r "$tmp/MQTT_monitor" "$DEST"
    else
        cp -r "$tmp" "$DEST"
    fi
    rm -rf "$tmp"
fi

# --- 2. Python environment --------------------------------------------------
if [ -x "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"
    info "Using virtualenv $VENV"
else
    warn "No virtualenv at $VENV; creating one at $DEST/.venv"
    python3 -m venv "$DEST/.venv"
    PY="$DEST/.venv/bin/python"
fi

info "Installing package + dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e "$DEST"

# --- 3. Config --------------------------------------------------------------
if [ ! -f "$DEST/config.yaml" ]; then
    cp "$DEST/config.example.yaml" "$DEST/config.yaml"
    info "Created config.yaml (defaults: mqtt.contrailcast.com:8883 TLS)"
else
    info "Keeping existing config.yaml"
fi

# --- 4. systemd service (hardened) ------------------------------------------
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
render_unit() {
    cat <<EOF
[Unit]
Description=CC RMS MQTT health monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
ExecStart=${PY} -m cc_mqtt_monitor --config ${DEST}/config.yaml
Restart=always
RestartSec=10

# Survive the OOM-killer so the monitor outlives the RMS process it reports on.
OOMScoreAdjust=-900
# Self-guard: cap the monitor itself so it can never add to memory pressure.
MemoryMax=128M
Nice=5

[Install]
WantedBy=multi-user.target
EOF
}

if command -v systemctl >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
    info "Installing systemd service ($UNIT_PATH)"
    render_unit | $SUDO tee "$UNIT_PATH" >/dev/null
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now "$SERVICE_NAME"
    info "Service started. Status:"
    $SUDO systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
    echo
    info "Follow logs with:  journalctl -u $SERVICE_NAME -f"
else
    warn "systemd not found; run manually:  $PY -m cc_mqtt_monitor --config $DEST/config.yaml"
fi

info "Done."
