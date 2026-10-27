# homeSec

homeSec is a small Raspberry Pi service: a USB webcam watched by [motion](https://motion-project.github.io/) saves a picture when it sees movement, and homeSec mails it to you.

Design target: Raspberry Pi 4B, Raspberry Pi OS (Debian based, 64-bit recommended), USB webcam.
Pi Camera Modules (CSI) are not covered here; they need `rpicam`/libcamera instead of `fswebcam`.

## How it works

```
webcam --> motion.service            (detects movement, saves jpg to /var/lib/homesec)
                 |
                 v
           homesec.service           (watches the folder: cooldown, mail, cleanup)
                 |
                 v
        mutt --> msmtp --> your SMTP server --> inbox
```

The two services are independent: motion only saves pictures, homesec only mails them. Settings live in one env file, `/etc/homesec/homesec.env`.

## Install

```
git clone https://github.com/spuzzelsnest/homeSec.git
cd homeSec
./preps.sh
```

`preps.sh` installs `msmtp-mta mutt fswebcam motion inotify-tools`, adds your user to the `video` and `motion` groups, installs the script as `/usr/local/bin/homesec`, creates `/var/lib/homesec` for pictures, `/etc/homesec/homesec.env` for settings and the `homesec.service` unit (enabled, not started). Run it again after updating the repo to install the new versions; your env file is left alone.

## Settings (`/etc/homesec/homesec.env`)

```
sudoedit /etc/homesec/homesec.env      # EMAIL="you@example.com"
```

| Setting | Default | Meaning |
|---|---|---|
| `EMAIL` | none | Recipient (required) |
| `MAIL_FROM` | empty | Optional From address |
| `SUBJECT` | `Motion detected` | Mail subject |
| `IMAGE_DIR` | `/var/lib/homesec` | Pictures; same folder as motion's `target_dir` |
| `COOLDOWN` | `300` | Minimum seconds between two mails |
| `KEEP_DAYS` | `7` | Pictures older than this are deleted |
| `VIDEO_DEVICE`, `RESOLUTION`, `SKIP_FRAMES` | `/dev/video0`, `1280x720`, `10` | Only used by `homesec --test` |

The file is plain `KEY=value` lines, read by systemd and by the script. For development you can keep a `.env` next to `homeSec.sh` (it is git-ignored) or point `HOMESEC_ENV` at any file. After editing the env file: `sudo systemctl restart homesec`.

## E-mail (msmtp)

The services run as the system user `motion`, so a `~/.msmtprc` in your home directory is not used. Use the system-wide config `/etc/msmtprc`:

```
# /etc/msmtprc
defaults
auth            on
tls             on
tls_starttls    on
tls_trust_file  /etc/ssl/certs/ca-certificates.crt
syslog          on

account         gmail
host            smtp.gmail.com
port            587
from            you@gmail.com
user            you@gmail.com
passwordeval    cat /etc/msmtp-password

account default : gmail
```

Store the SMTP password in a file only root and the motion group can read:

```
sudo install -m 0640 -o root -g motion /dev/null /etc/msmtp-password
sudoedit /etc/msmtp-password     # put the password on the only line
```

**Gmail:** the old "Less secure apps" option no longer exists. Turn on 2-step verification and create an
[app password](https://myaccount.google.com/apppasswords), or use another SMTP provider. Google changes these rules from time to time, so check their current documentation.

Send a test mail through the same path the service uses:

```
sudo -u motion homesec --test
```

It takes a picture with the webcam and mails it (stop motion first: `sudo systemctl stop motion`, because motion keeps the webcam open).

## motion

Edit `/etc/motion/motion.conf` and set (or add). There is no hook script any more; motion only saves pictures:

```
video_device /dev/video0
width 1280
height 720
framerate 5
movie_output off
picture_output first
target_dir /var/lib/homesec
picture_filename %Y-%m-%d_%H%M%S-%v
```

- `picture_output first` saves one picture per motion event.
- A low `framerate` keeps CPU load down on the Pi.
- Option names are for motion 4.x; if your version complains about one, check `man motion`.

## Run it as a service

```
sudo systemctl enable --now motion homesec
systemctl status homesec
journalctl -u homesec -f          # live log
```

Both start at boot. Pictures that were saved while homesec was not running are not mailed afterwards.

## Troubleshooting

- **homesec won't start, "EMAIL is not set"**: edit `/etc/homesec/homesec.env`, then `sudo systemctl restart homesec` (the unit deliberately does not retry on a settings error).
- **"Device or resource busy"**: something else (usually motion) has the webcam open. Only `homesec --test` uses the camera itself, so stop motion first.
- **No mail, no error**: `journalctl -u homesec -e` and `journalctl -t msmtp -e`; try `sudo -u motion homesec --test`.
- **Permission denied on the webcam**: `video` group membership needs a re-login (or reboot).
- **Too many mails**: raise `COOLDOWN`.
- **Different `IMAGE_DIR`**: also add it to `ReadWritePaths=` in `/etc/systemd/system/homesec.service` (`sudo systemctl edit homesec` works too), then `sudo systemctl daemon-reload && sudo systemctl restart homesec`.
