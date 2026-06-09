#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/bikelogger
DATA_DIR=/var/lib/bikelogger
SERVICE_NAME=bikelogger.service
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh"
  exit 1
fi

echo "=== BikeLogger install from repo ==="
echo "Repo: $REPO_DIR"

export DEBIAN_FRONTEND=noninteractive
apt-get update || true
apt-get install -y \
  python3 python3-flask python3-serial python3-gpiozero python3-smbus \
  i2c-tools wireless-tools iw bluez rfkill libcamera-apps python3-picamera2 \
  sqlite3 rsync curl jq git || true

if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
  raspi-config nonint do_serial_cons 1 || true
  raspi-config nonint do_serial_hw 0 || true
fi
CONFIG_FILE=/boot/firmware/config.txt
[[ -f /boot/config.txt && ! -f "$CONFIG_FILE" ]] && CONFIG_FILE=/boot/config.txt
if [[ -f "$CONFIG_FILE" ]]; then
  grep -q '^dtparam=i2c_arm=on' "$CONFIG_FILE" || echo 'dtparam=i2c_arm=on' >> "$CONFIG_FILE"
  grep -q '^enable_uart=1' "$CONFIG_FILE" || echo 'enable_uart=1' >> "$CONFIG_FILE"
fi
CMDLINE_FILE=/boot/firmware/cmdline.txt
[[ -f /boot/cmdline.txt && ! -f "$CMDLINE_FILE" ]] && CMDLINE_FILE=/boot/cmdline.txt
if [[ -f "$CMDLINE_FILE" ]]; then
  cp "$CMDLINE_FILE" "$CMDLINE_FILE.bikelogger.bak.$(date +%Y%m%d_%H%M%S)"
  sed -i -E 's/(^| )console=(serial0|ttyAMA0|ttyS0),[0-9]+//g; s/  +/ /g' "$CMDLINE_FILE"
fi

mkdir -p "$APP_DIR" "$DATA_DIR/rides" /var/log/bikelogger
install -m 0755 "$REPO_DIR/bikelogger/bikelogger.py" "$APP_DIR/bikelogger.py"
if [[ ! -f "$APP_DIR/config.json" ]]; then
  install -m 0644 "$REPO_DIR/config/config.pi4.json" "$APP_DIR/config.json"
else
  echo "Keeping existing $APP_DIR/config.json. Compare with config/config.pi4.json for new options."
fi
install -m 0644 "$REPO_DIR/systemd/bikelogger.service" "/etc/systemd/system/$SERVICE_NAME"
install -m 0755 "$REPO_DIR/scripts/bikelogger-test" /usr/local/bin/bikelogger-test
install -m 0755 "$REPO_DIR/scripts/bikelogger-report" /usr/local/bin/bikelogger-report
install -m 0755 "$REPO_DIR/scripts/bikelogger-update" /usr/local/bin/bikelogger-update
install -m 0755 "$REPO_DIR/scripts/bikelogger-export-latest" /usr/local/bin/bikelogger-export-latest

# Save repo location for one-command updater.
echo "$REPO_DIR" > /opt/bikelogger/.repo_path

systemctl daemon-reload
systemctl enable bikelogger.service
systemctl restart bikelogger.service

echo "=== Installed ==="
systemctl status bikelogger.service --no-pager -l || true
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8080/"
echo "Test: sudo bikelogger-test"
echo "Report: sudo bikelogger-report"
