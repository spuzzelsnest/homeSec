#!/usr/bin/env bash
#
# Prepare a Raspberry Pi (Raspberry Pi OS / Debian) for homeSec.
# Run as your normal user (not with sudo); sudo is asked for where needed.
# Safe to run again: it also installs the latest homeSec.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -eq 0 ]]; then
    echo "Please run this as your normal user, not as root." >&2
    exit 1
fi
user=$(id -un)

packages=(msmtp-mta mutt fswebcam motion)
groups=(video motion)
image_dir=/var/lib/homesec

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
sudo install -d -m 2775 -o motion -g motion "$image_dir"
sudo install -d -m 0755 -o motion -g motion /var/log/motion

if [[ -e /etc/homesec.conf ]]; then
    echo "/etc/homesec.conf already exists, leaving it alone."
else
    sudo install -m 0640 -o root -g motion "$SCRIPT_DIR/homesec.conf.example" /etc/homesec.conf
    echo "Created /etc/homesec.conf"
fi

cat <<EOF

Done. Next steps (see README.md):
  1. sudoedit /etc/homesec.conf          # set EMAIL
  2. configure /etc/msmtprc              # SMTP account
  3. configure /etc/motion/motion.conf   # on_picture_save hook
  4. sudo -u motion homesec --test       # check that a mail arrives
EOF
