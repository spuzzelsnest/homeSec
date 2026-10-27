#!/usr/bin/env bash
#
# Prepare a Raspberry Pi (Raspberry Pi OS / Debian) for homeSec.
# Run as your normal user (not with sudo); sudo is asked for where needed.
# Safe to run again: it also installs the latest script and service file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -eq 0 ]]; then
    echo "Please run this as your normal user, not as root." >&2
    exit 1
fi
user=$(id -un)

packages=(msmtp-mta mutt fswebcam motion inotify-tools)
groups=(video motion)
image_dir=/var/lib/homesec
env_file=/etc/homesec/homesec.env

echo "== Installing packages =="
missing=()
for pkg in "${packages[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if (( ${#missing[@]} )); then
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
else
    echo "All packages already installed."
fi

echo "== Checking groups for $user =="
for grp in "${groups[@]}"; do
    if id -nG "$user" | tr ' ' '\n' | grep -qx "$grp"; then
        echo "Member of $grp"
    else
        echo "Adding $user to $grp (log out and in again to activate)"
        sudo usermod -aG "$grp" "$user"
    fi
done

echo "== Installing homeSec =="
sudo install -m 0755 "$SCRIPT_DIR/homeSec.sh" /usr/local/bin/homesec

# Pictures live outside /home so the motion service can always reach them.
sudo install -d -m 0775 -o motion -g motion "$image_dir"
sudo install -d -m 0755 -o motion -g motion /var/log/motion

sudo install -d -m 0750 -o root -g motion /etc/homesec
if [[ -e $env_file ]]; then
    echo "$env_file already exists, leaving it alone."
else
    sudo install -m 0640 -o root -g motion "$SCRIPT_DIR/homesec.env.example" "$env_file"
    echo "Created $env_file"
fi

echo "== Installing service =="
sudo install -m 0644 "$SCRIPT_DIR/homesec.service" /etc/systemd/system/homesec.service
sudo systemctl daemon-reload
sudo systemctl enable homesec.service

cat <<EOF

Done. The service is enabled but NOT started yet. Next steps (see README.md):
  1. sudoedit $env_file                # set EMAIL
  2. configure /etc/msmtprc            # SMTP account
  3. configure /etc/motion/motion.conf # save pictures to $image_dir
  4. sudo -u motion homesec --test     # check that a mail arrives
  5. sudo systemctl enable --now motion homesec
EOF
