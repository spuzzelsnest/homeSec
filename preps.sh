#!/usr/bin/env bash
#
# Install homeSec on Raspberry Pi OS (Debian). Run as your normal user; sudo is asked for.
# Safe to run again: it installs the latest program and service file, and keeps your settings.
#
# Usage: ./preps.sh [--usb]
#   --usb   also install python3-opencv (only needed for a USB webcam)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
service_user=homesec
image_dir=/var/lib/homesec
env_file=/etc/homesec/homesec.env

use_usb=0
case "${1:-}" in
    "")      ;;
    --usb)   use_usb=1 ;;
    *)       echo "Usage: $0 [--usb]" >&2; exit 2 ;;
esac

if [[ $EUID -eq 0 ]]; then
    echo "Please run this as your normal user, not as root." >&2
    exit 1
fi

echo "== Installing packages =="
packages=(python3-picamera2 python3-numpy)
(( use_usb )) && packages+=(python3-opencv)
missing=()
for pkg in "${packages[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if (( ${#missing[@]} )); then
    sudo apt-get update
    # No recommends: keeps the install small and free of desktop/Qt packages on a headless Pi.
    sudo apt-get install -y --no-install-recommends "${missing[@]}"
else
    echo "All packages already installed."
fi

echo "== Stopping older versions =="
sudo systemctl stop homesec.service 2>/dev/null || true
# The old version used motion, which keeps the camera open and would block homeSec.
if systemctl list-unit-files motion.service 2>/dev/null | grep -q '^motion.service'; then
    echo "Disabling motion (homeSec detects movement itself now)"
    sudo systemctl disable --now motion.service
fi

echo "== Service user =="
if getent passwd "$service_user" >/dev/null; then
    sudo usermod -aG video "$service_user"
else
    sudo useradd --system --user-group --groups video --home-dir "$image_dir" \
        --no-create-home --shell /usr/sbin/nologin "$service_user"
    echo "Created user $service_user"
fi

echo "== Installing homeSec =="
sudo install -m 0755 "$SCRIPT_DIR/homesec.py" /usr/local/bin/homesec
sudo install -d -m 0750 -o "$service_user" -g "$service_user" "$image_dir"
sudo chown -R "$service_user:$service_user" "$image_dir"   # older versions used the motion user

sudo install -d -m 0750 -o root -g "$service_user" /etc/homesec
if [[ -e $env_file ]]; then
    echo "$env_file already exists, leaving the settings alone."
    sudo chgrp "$service_user" "$env_file"
    sudo chmod 0640 "$env_file"
    if ! sudo grep -q '^SMTP_HOST=' "$env_file"; then
        echo "NOTE: $env_file has no SMTP_HOST. It is from the older version, which used msmtp."
        echo "      Add the mail settings from $SCRIPT_DIR/homesec.env.example (SMTP_HOST, SMTP_USER, SMTP_PASSWORD)."
    fi
else
    sudo install -m 0640 -o root -g "$service_user" "$SCRIPT_DIR/homesec.env.example" "$env_file"
    echo "Created $env_file"
fi

echo "== Installing service =="
sudo install -m 0644 "$SCRIPT_DIR/homesec.service" /etc/systemd/system/homesec.service
sudo systemctl daemon-reload
sudo systemctl enable homesec.service

cat <<EOF

Done. The service is enabled but NOT started yet. Next steps (see README.md):
  1. sudoedit $env_file            # MAIL_TO and the SMTP_* mail settings
  2. sudo -u $service_user homesec mailtest     # does a test mail arrive?
  3. sudo -u $service_user homesec snapshot     # does a picture from the camera arrive?
  4. sudo systemctl start homesec               # and: journalctl -u homesec -f

Software from older versions is no longer needed. If nothing else on this Pi uses it, remove it:
  sudo apt purge motion fswebcam mutt msmtp-mta inotify-tools && sudo apt autoremove
  sudo rm -f /etc/msmtprc /etc/msmtp-password    # old mail settings, may contain your password
EOF
