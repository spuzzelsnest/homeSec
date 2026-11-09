#!/usr/bin/env bash
#
# Install homeSec on Raspberry Pi OS (Debian). Run as your normal user; sudo is asked for.
# Safe to run again: it installs the latest program and service file, and keeps your settings.
#
# Usage: ./preps.sh [--no-check]
#   --no-check   skip the person check: no OpenCV, no model download (every movement is alerted)
#   --usb        accepted for older instructions; OpenCV (needed for a USB webcam) is installed anyway

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
service_user=homesec
image_dir=/var/lib/homesec
env_file=/etc/homesec/homesec.env
model_dir=/usr/local/share/homesec/models
# Person check model: YOLOX-tiny (Megvii, Apache-2.0), ~20 MB. The checksum guards the download.
model_url=https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx
model_sha256=427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7
model_file=$model_dir/yolox_tiny.onnx

use_check=1
case "${1:-}" in
    ""|--usb)   ;;
    --no-check) use_check=0 ;;
    *)          echo "Usage: $0 [--no-check]" >&2; exit 2 ;;
esac

if [[ $EUID -eq 0 ]]; then
    echo "Please run this as your normal user, not as root." >&2
    exit 1
fi

echo "== Installing packages =="
packages=(python3-picamera2 python3-numpy python3-pil curl)
(( use_check )) && packages+=(python3-opencv)
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
sudo rm -rf /usr/local/share/homesec/static
sudo install -d -m 0755 /usr/local/share/homesec
sudo cp -r "$SCRIPT_DIR/static" /usr/local/share/homesec/static
sudo chmod -R u=rwX,go=rX /usr/local/share/homesec

if (( use_check )); then
    echo "== Person check model =="
    if [[ -f $model_file ]] && echo "$model_sha256  $model_file" | sha256sum --check --status; then
        echo "Model already installed."
    else
        tmp=$(mktemp)
        trap 'rm -f "$tmp"' EXIT
        echo "Downloading $model_url"
        curl -fL --retry 3 -o "$tmp" "$model_url"
        if ! echo "$model_sha256  $tmp" | sha256sum --check --status; then
            echo "The downloaded model has the wrong checksum; not installing it." >&2
            exit 1
        fi
        sudo install -d -m 0755 "$model_dir"
        sudo install -m 0644 "$tmp" "$model_file"
        echo "Installed $model_file"
    fi
fi
sudo install -d -m 0750 -o "$service_user" -g "$service_user" "$image_dir"
sudo chown -R "$service_user:$service_user" "$image_dir"   # older versions used the motion user

sudo install -d -m 0750 -o root -g "$service_user" /etc/homesec
# Checked with sudo: /etc/homesec is not readable for your normal user (it holds the mail password),
# so a plain [[ -e ]] would always say "missing" and the example would overwrite your settings.
if sudo test -e "$env_file"; then
    echo "$env_file already exists, leaving the settings alone."
    sudo chgrp "$service_user" "$env_file"
    sudo chmod 0640 "$env_file"
    if ! sudo grep -q '^SMTP_HOST=' "$env_file"; then
        echo "NOTE: $env_file has no SMTP_HOST. It is from the older version, which used msmtp."
        echo "      Add the mail settings from $SCRIPT_DIR/homesec.env.example (SMTP_HOST, SMTP_USER, SMTP_PASSWORD)."
    fi
else
    sudo install -m 0640 -o root -g "$service_user" "$SCRIPT_DIR/homesec.env.example" "$env_file"
    echo "Created $env_file: fill in your mail settings once (sudoedit $env_file); later runs keep them."
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
  4. sudo -u $service_user homesec slacktest    # optional: Slack set up?
  5. sudo -u $service_user homesec check /var/lib/homesec/<a picture>.jpg   # person check works?
  6. sudo systemctl start homesec               # and: journalctl -u homesec -f

Software from older versions is no longer needed. If nothing else on this Pi uses it, remove it:
  sudo apt purge motion fswebcam mutt msmtp-mta inotify-tools && sudo apt autoremove
  sudo rm -f /etc/msmtprc /etc/msmtp-password    # old mail settings, may contain your password
EOF
