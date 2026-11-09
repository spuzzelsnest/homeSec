#!/usr/bin/env bash
#
# Install homeSec on Raspberry Pi OS (Debian). Run as your normal user; sudo is asked for.
# Safe to run again: it installs the latest program and service file, and keeps your settings.
#
# Usage: ./deploy.sh [--no-check] [--no-test]
#   --no-check   skip the person check: no OpenCV, no model download (every movement is alerted)
#   --no-test    do not test and start the service at the end
#   --usb        accepted for older instructions; OpenCV (needed for a USB webcam) is installed anyway
#
# At the end it tests everything itself (camera + mail, person check, Slack if set up) and
# starts the service. That sends one test mail with a camera picture per run.

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
use_test=1
for arg in "$@"; do
    case "$arg" in
        --usb)      ;;
        --no-check) use_check=0 ;;
        --no-test)  use_test=0 ;;
        *)          echo "Usage: $0 [--no-check] [--no-test]" >&2; exit 2 ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    echo "Please run this as your normal user, not as root." >&2
    exit 1
fi

echo "== Installing packages =="
packages=(python3-picamera2 python3-numpy python3-pil python3-av curl)
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

old_software_note() {
    # Only when something from the old version (motion + fswebcam + mutt/msmtp) is still on this Pi.
    local old=() files=() pkg f
    for pkg in motion fswebcam mutt msmtp msmtp-mta inotify-tools; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed' && old+=("$pkg")
    done
    for f in /etc/msmtprc /etc/msmtp-password; do
        sudo test -e "$f" && files+=("$f")
    done
    (( ${#old[@]} + ${#files[@]} )) || return 0
    echo
    echo "Left over from the old version, no longer needed. If nothing else on this Pi uses it, remove it:"
    (( ${#old[@]} )) && echo "  sudo apt purge ${old[*]} && sudo apt autoremove"
    (( ${#files[@]} )) && echo "  sudo rm -f ${files[*]}    # old mail settings, may contain your password"
    return 0
}

if (( ! use_test )); then
    echo
    echo "Done. Not tested (--no-test); start with: sudo systemctl start homesec"
    old_software_note
    exit 0
fi

# ---- settings filled in? ---------------------------------------------------------------
# the example's placeholders: you@example.com, you@gmail.com, 'your app password'
placeholders() { sudo grep -qE 'you@example\.com|you@gmail\.com|your app password' "$env_file"; }

if placeholders || ! sudo grep -qE '^SMTP_HOST=.+' "$env_file"; then
    echo
    echo "The mail settings in $env_file are not filled in yet (only needed once; later runs keep them)."
    if [[ -t 0 ]]; then
        read -r -p "Open the settings file now? [Y/n] " answer
        if [[ ${answer:-y} =~ ^[Yy] ]]; then
            sudoedit "$env_file"
        fi
    fi
    if placeholders || ! sudo grep -qE '^SMTP_HOST=.+' "$env_file"; then
        echo "Still not filled in. Edit it (sudoedit $env_file) and run ./deploy.sh again."
        old_software_note
        exit 1
    fi
fi

# ---- tests -------------------------------------------------------------------------------
echo
echo "== Testing =="
as_homesec() { sudo -u "$service_user" -- "$@"; }
results=()
failed=0
report() {  # report <test> <ok|FAIL|skipped> [detail]
    results+=("$(printf '  %-14s %-8s %s' "$1" "$2" "${3:-}")")
    [[ $2 == FAIL ]] && failed=1
    return 0
}

echo "-- camera + mail (takes a picture and mails it)"
if out=$(as_homesec homesec snapshot 2>&1); then
    picture=$(grep -oE '/var/lib/homesec/[0-9_-]+\.jpg' <<<"$out" | tail -1 || true)
    report "camera + mail" ok "picture mailed to $(sudo grep -E '^MAIL_TO=' "$env_file" | cut -d= -f2- | tr -d \"\')"
else
    picture=$(grep -oE '/var/lib/homesec/[0-9_-]+\.jpg' <<<"$out" | tail -1 || true)
    if [[ -n $picture ]]; then
        report "camera" ok "picture taken"
        report "mail" FAIL "$(grep -E 'ERROR' <<<"$out" | tail -1 | sed 's/.*ERROR //')"
    else
        report "camera" FAIL "$(grep -E 'ERROR' <<<"$out" | tail -1 | sed 's/.*ERROR //')"
    fi
fi

if (( use_check )); then
    echo "-- person check"
    if [[ -z ${picture:-} ]]; then
        report "person check" skipped "no camera picture"
    elif out=$(as_homesec homesec check "$picture" 2>&1); then
        report "person check" ok "$out"
    elif grep -q '^no ' <<<"$out"; then
        report "person check" ok "works; $out"   # nobody in view is fine
    else
        report "person check" FAIL "$(tail -1 <<<"$out" | sed 's/.*ERROR //')"
    fi
fi

if sudo grep -qE '^SLACK_WEBHOOK_URL=.+' "$env_file"; then
    echo "-- Slack"
    if out=$(as_homesec homesec slacktest 2>&1); then
        report "Slack" ok "test message posted"
    else
        report "Slack" FAIL "$(grep -E 'ERROR' <<<"$out" | tail -1 | sed 's/.*ERROR //')"
    fi
else
    report "Slack" skipped "SLACK_WEBHOOK_URL not set"
fi

# ---- start ---------------------------------------------------------------------------------
echo "-- starting the service"
sudo systemctl restart homesec.service
sleep 8
if systemctl is-active --quiet homesec.service; then
    report "service" ok "running"
else
    report "service" FAIL "not running, see: journalctl -u homesec -e"
fi

echo
echo "== Result =="
printf '%s\n' "${results[@]}"
echo
echo "Last log lines:"
sudo journalctl -u homesec.service -n 6 --no-pager -o cat | sed 's/^/  /'
echo
if (( failed )); then
    echo "Something failed (see above). Fix it and run ./deploy.sh again."
else
    echo "All good. Watch it live with: journalctl -u homesec -f   (web console: http://$(hostname).local:8080/)"
fi
old_software_note
exit "$failed"
