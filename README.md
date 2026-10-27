# homeSec

homeSec is a small Raspberry Pi app: a USB webcam watched by [motion](https://motion-project.github.io/) sends you a picture by e-mail when it detects movement.

Tested design target: Raspberry Pi 4B, Raspberry Pi OS (Debian based, 64-bit recommended), USB webcam.
Pi Camera Modules (CSI) are not covered here; they need `rpicam`/libcamera instead of `fswebcam`.

## How it works

```
webcam --> motion (detects movement, saves a jpg)
                 |  on_picture_save
                 v
           homesec (cooldown, mail, cleanup)
                 |
                 v
        mutt --> msmtp --> your SMTP server --> inbox
```

motion keeps the webcam open, so homesec uses the picture motion already saved instead of opening the camera a second time.

## Install

```
git clone https://github.com/spuzzelsnest/homeSec.git
cd homeSec
./preps.sh
```

`preps.sh` installs `msmtp-mta mutt fswebcam motion`, adds your user to the `video` and `motion` groups, installs the script as `/usr/local/bin/homesec`, creates `/var/lib/homesec` for pictures and creates `/etc/homesec.conf`. Run it again after updating the repo to install the new script version.

Then set your address:

```
sudoedit /etc/homesec.conf      # EMAIL="you@example.com"
```

## E-mail (msmtp)

motion runs as the system user `motion`, so it cannot read a `~/.msmtprc` in your home directory. Use the system-wide config `/etc/msmtprc` instead:

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

Send a test mail through the same path motion will use:

```
sudo -u motion homesec --test
```

It takes a picture with the webcam and mails it. If it fails, read `journalctl -t msmtp` and the messages homesec prints.

## motion

Edit `/etc/motion/motion.conf` and set (or add):

```
video_device /dev/video0
width 1280
height 720
framerate 5
movie_output off
picture_output first
target_dir /var/lib/homesec
picture_filename %Y-%m-%d_%H%M%S-%v
on_picture_save /usr/local/bin/homesec %f
```

- `picture_output first` saves one picture per motion event, `%f` hands that file to homesec.
- A low `framerate` keeps CPU load down on the Pi.
- Option names are for motion 4.x; if your version complains about one, check `man motion`.

Then start it:

```
sudo systemctl enable --now motion
sudo systemctl status motion
```

## Settings (`/etc/homesec.conf`)

| Setting | Default | Meaning |
|---|---|---|
| `EMAIL` | none | Recipient (required) |
| `MAIL_FROM` | empty | Optional From address |
| `SUBJECT` | `Motion detected` | Mail subject |
| `IMAGE_DIR` | `/var/lib/homesec` | Pictures; same folder as motion's `target_dir` |
| `COOLDOWN` | `300` | Minimum seconds between two mails |
| `KEEP_DAYS` | `7` | Pictures older than this are deleted |
| `VIDEO_DEVICE`, `RESOLUTION`, `SKIP_FRAMES` | `/dev/video0`, `1280x720`, `10` | Only used when homesec takes the picture itself |

## Troubleshooting

- **"Device or resource busy"**: something else (usually motion) has the webcam open. Let motion take the picture; don't run `fswebcam` while motion runs.
- **No mail, no error**: run `sudo -u motion homesec --test` and `journalctl -u motion -e`.
- **Permission denied on the webcam**: `video` group membership needs a re-login (or reboot).
- **Too many mails**: raise `COOLDOWN`.
- **No mail after a reboot**: `systemctl is-enabled motion`.
