#!/bin/bash
#
# Install the CC RMS MQTT monitor into the RMS virtualenv and (optionally) set
# up the systemd service.
#
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${VENV_PYTHON:-$HOME/vRMS/bin/python}"

echo "[INFO] Installing into $VENV_PYTHON"
"$VENV_PYTHON" -m pip install -e "$PROJECT_DIR"

if [ ! -f "$PROJECT_DIR/config.yaml" ]; then
    cp "$PROJECT_DIR/config.example.yaml" "$PROJECT_DIR/config.yaml"
    echo "[INFO] Created config.yaml from the example -- edit the broker host."
fi

echo
echo "Quick check (no broker needed):"
echo "    $VENV_PYTHON -m cc_mqtt_monitor --status"
echo
echo "Run the publisher:"
echo "    $VENV_PYTHON -m cc_mqtt_monitor --config $PROJECT_DIR/config.yaml"
echo
echo "To install the systemd service (as root):"
echo "    sudo cp $PROJECT_DIR/systemd/cc-rms-monitor.service /etc/systemd/system/"
echo "    sudo systemctl daemon-reload"
echo "    sudo systemctl enable --now cc-rms-monitor"
