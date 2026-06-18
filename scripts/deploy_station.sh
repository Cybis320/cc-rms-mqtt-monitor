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
REPO_URL="${CC_REPO_URL:-https://github.com/Cybis320/cc-rms-mqtt-monitor.git}"
# Deploys into the familiar CC_Utils/MQTT_monitor folder (repo name independent).
DEST="${CC_DEST:-$HOME/source/CC_Utils/MQTT_monitor}"
VENV="${CC_VENV:-$HOME/vRMS}"
SERVICE_NAME="cc-rms-monitor"
RUN_USER="$(id -un)"

info() { printf '\033[32m[deploy]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$1"; }

# --- 0. Validate sudo up front (the systemd step below needs it) ------------
# Otherwise a sudo failure under `set -e` silently aborts AFTER the package is
# installed -- leaving no service and no error (the classic `curl | bash` trap).
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ]; then
    if ! sudo -v; then
        warn "This installer needs sudo to install the systemd service."
        warn "Run it from an interactive shell (not 'curl | bash') or as root, then retry."
        exit 1
    fi
fi

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
    info "Cloning $REPO_URL -> $DEST"
    mkdir -p "$(dirname "$DEST")"
    # Standalone repo: clone straight into DEST (which keeps the familiar
    # CC_Utils/MQTT_monitor folder layout even though the repo is its own).
    git clone --depth 1 "$REPO_URL" "$DEST"
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

# --- 3. Config + subscription group -----------------------------------------
# Keep the label human-readable (allow spaces); the slug for topics strips them.
sanitize() { printf '%s' "$1" | tr -cd 'A-Za-z0-9 _.-' | sed -E 's/^ +//; s/ +$//'; }
slugify()  { printf '%s' "$1" | sed -E 's/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//'; }

if [ ! -f "$DEST/config.yaml" ]; then
    cp "$DEST/config.example.yaml" "$DEST/config.yaml"

    STATIONS_DIR="${CC_STATIONS_DIR:-$HOME/source/Stations}"
    RMS_DIR="${CC_RMS_DIR:-$HOME/source/RMS}"
    DETECTED="$(grep -hE '^[[:space:]]*camera_group_name:' \
                    "$STATIONS_DIR"/*/.config "$RMS_DIR/.config" 2>/dev/null \
                | sed -E 's/^[[:space:]]*camera_group_name:[[:space:]]*//; s/[[:space:]]*$//' \
                | grep -viE '^(none)?$' | sort -u | head -n1 || true)"
    HOST="$(hostname)"

    # Decide the subscription group. Blank GROUP -> leave config.group null so
    # the monitor uses each station's live RMS camera_group_name.
    GROUP=""
    if [ -n "${CC_GROUP:-}" ]; then
        GROUP="${CC_GROUP}"                                  # non-interactive override
    elif [ -r /dev/tty ]; then
        echo
        echo "Subscription group for alerts (ntfy/Telegram):"
        if [ -n "$DETECTED" ]; then
            echo "  1) RMS camera_group_name:  ${DETECTED}   [default]"
        else
            echo "  1) (no camera_group_name set in RMS config)"
        fi
        echo "  2) hostname:               ${HOST}"
        echo "  3) something else (custom)"
        read -rp "Selection [1]: " sel < /dev/tty || true
        case "$sel" in
            2) GROUP="$HOST" ;;
            3) read -rp "Custom group name: " GROUP < /dev/tty || true ;;
            *) [ -z "$DETECTED" ] && GROUP="$HOST" ;;         # default; hostname if no RMS group
        esac
    else
        [ -z "$DETECTED" ] && GROUP="$HOST"                  # non-interactive, no RMS group
    fi

    GROUP="$(sanitize "$GROUP")"
    if [ -n "$GROUP" ]; then
        sed -i "s|^group:.*|group: \"$GROUP\"|" "$DEST/config.yaml"
        info "Subscription group: $GROUP   (topic slug: $(slugify "$GROUP"))"
    else
        info "Subscription group: per-station RMS camera_group_name (e.g. ${DETECTED:-none})"
    fi
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

# Auto-update timer: periodically git-pulls and restarts on change. Runs as
# root (so it can restart the service); git/pip run as the repo owner.
# Skip by setting CC_NO_AUTOUPDATE=1.
render_update_service() {
    cat <<EOF
[Unit]
Description=Auto-update CC RMS MQTT monitor from git
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=CC_DIR=${DEST}
Environment=CC_USER=${RUN_USER}
Environment=CC_VENV=${VENV}
Environment=CC_SERVICE=${SERVICE_NAME}
Environment=CC_BRANCH=${CC_BRANCH:-master}
ExecStart=${DEST}/scripts/autoupdate.sh
EOF
}
render_update_timer() {
    cat <<EOF
[Unit]
Description=Periodically auto-update CC RMS MQTT monitor from git

[Timer]
OnBootSec=2min
OnUnitActiveSec=${CC_UPDATE_INTERVAL:-15min}
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

if command -v systemctl >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
    manual_hint() {
        warn "systemd step FAILED -- package is installed but NOT running as a service."
        warn "Finish manually as root:"
        warn "  sudo cp systemd/${SERVICE_NAME}.service /etc/systemd/system/   # then fix User=/ExecStart paths"
        warn "  sudo systemctl daemon-reload && sudo systemctl enable --now ${SERVICE_NAME}"
    }
    info "Installing systemd service ($UNIT_PATH)"
    if ! render_unit | $SUDO tee "$UNIT_PATH" >/dev/null; then manual_hint; exit 1; fi
    chmod +x "$DEST/scripts/autoupdate.sh" 2>/dev/null || true

    if [ "${CC_NO_AUTOUPDATE:-0}" != "1" ]; then
        info "Installing auto-update timer (${CC_UPDATE_INTERVAL:-15min})"
        render_update_service | $SUDO tee "/etc/systemd/system/${SERVICE_NAME}-update.service" >/dev/null || true
        render_update_timer   | $SUDO tee "/etc/systemd/system/${SERVICE_NAME}-update.timer"   >/dev/null || true
    fi

    $SUDO systemctl daemon-reload || { manual_hint; exit 1; }
    if ! $SUDO systemctl enable --now "$SERVICE_NAME"; then manual_hint; exit 1; fi
    [ "${CC_NO_AUTOUPDATE:-0}" != "1" ] && $SUDO systemctl enable --now "${SERVICE_NAME}-update.timer" || true

    # Verify it actually came up -- don't claim success over a dead service.
    if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
        info "Service active: $SERVICE_NAME"
    else
        warn "Service installed but NOT active -- check: journalctl -u $SERVICE_NAME -n 50"
    fi
    echo
    info "Follow logs with:  journalctl -u $SERVICE_NAME -f"
    [ "${CC_NO_AUTOUPDATE:-0}" != "1" ] && info "Auto-update runs every ${CC_UPDATE_INTERVAL:-15min}; check: systemctl list-timers ${SERVICE_NAME}-update.timer"
else
    warn "systemd not found; run manually:  $PY -m cc_mqtt_monitor --config $DEST/config.yaml"
fi

# --- 5. ntfy subscription help ---------------------------------------------
# Print the exact topics to subscribe to, derived from this host's own data.
# (Topics use the "cc-" prefix per the contrailcast ntfy/Telegram bridge.)
echo
info "Get alerts via Telegram (all platforms, best on iOS) or ntfy (Android/desktop/web):"
info "  Telegram: message @contrailcast_rms_bot  ->  /subscribe <token>   (token = a cc- name below, without 'cc-')"
info "  ntfy:     app server https://ntfy.contrailcast.com  ->  subscribe to the cc- topics below"
info "            (ntfy iOS push is limited; on iPhone/iPad use Telegram)"
"$PY" -m cc_mqtt_monitor --config "$DEST/config.yaml" --status 2>/dev/null | "$PY" -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
stations = d.get("stations", [])
slugs = sorted({s.get("group_slug") for s in stations if s.get("group_slug")})
sids = [s["station_id"] for s in stations]
for sl in slugs:
    print("    your group         ->  cc-%s" % sl)
for sid in sids:
    print("    one camera         ->  cc-%s" % sid)
print("    a whole network    ->  cc-USC, cc-CAC, cc-USL, cc-USV, ...")
print("                           any leading prefix of a station ID, 3+ chars.")
print("                           Subscribe to a network prefix ONCE and every current")
print("                           AND future station with that prefix is covered")
print("                           automatically -- no app change when new stations deploy.")
' || true

# --- 6. publish-consent note -----------------------------------------------
echo
info "Publish consent: this monitor honors RMS 'weblog_enable'. A camera with"
info "  weblog_enable: false is NOT transmitted to MQTT (and any prior data is"
info "  cleared); a host with no opted-in cameras transmits nothing at all."
OPTED_OUT="$(grep -lEi '^[[:space:]]*weblog_enable:[[:space:]]*(false|0|no)' \
                 "${CC_STATIONS_DIR:-$HOME/source/Stations}"/*/.config \
                 "${CC_RMS_DIR:-$HOME/source/RMS}/.config" 2>/dev/null | wc -l || true)"
if [ "${OPTED_OUT:-0}" -gt 0 ]; then
    warn "  ${OPTED_OUT} station config(s) here have weblog_enable=false -> not published."
fi

echo
info "To remove later: ./scripts/uninstall_station.sh  (add CC_PURGE=1 to delete the checkout too)"

info "Done."
