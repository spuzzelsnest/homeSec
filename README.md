# homeSec

homeSec is a small Raspberry Pi service: a camera watches a room, and when something moves homeSec mails you a picture.

It is one Python file (`homesec.py`) with no extra software: the camera is read with [picamera2](https://github.com/raspberrypi/picamera2), movement is detected with numpy, and mail is sent with Python's `smtplib`. Earlier versions needed `motion`, `fswebcam`, `mutt`, `msmtp` and `inotify-tools`; those are gone.

Target: Raspberry Pi 4B with Raspberry Pi OS (Debian based, 64-bit recommended) and a Raspberry Pi camera on the ribbon cable (for example the HQ camera). USB webcams work too (`--usb`, see below).

```
camera --> homesec.service --> detects movement --> saves jpg --> e-mail (SMTP) --> inbox
```

How detection works: the camera delivers a small grey picture (320x240, 5 per second). When the picture changes a lot between two frames, several frames in a row, that counts as movement. Slow changes such as daylight are ignored. After a mail, nothing is sent for `COOLDOWN` seconds (5 minutes by default).

## Install

```
git clone https://github.com/spuzzelsnest/homeSec.git
cd homeSec
./preps.sh            # add --usb for a USB webcam
```

`preps.sh` installs `python3-picamera2` and `python3-numpy` with apt, creates a `homesec` service user (member of `video`), installs the program as `/usr/local/bin/homesec`, creates `/etc/homesec/homesec.env` and installs and enables `homesec.service`. Run it again after updating the repo; your settings are left alone. If `motion` is installed it is disabled, because it would keep the camera busy.

## Settings

```
sudoedit /etc/homesec/homesec.env
```

At least `MAIL_TO`, `SMTP_HOST`, `SMTP_USER` and `SMTP_PASSWORD`. All settings are explained in [`homesec.env.example`](homesec.env.example). After editing: `sudo systemctl restart homesec`.

**Gmail:** the old "Less secure apps" option no longer exists. Turn on 2-step verification and create an [app password](https://myaccount.google.com/apppasswords), then use `smtp.gmail.com`, your address as `SMTP_USER` and the app password as `SMTP_PASSWORD`. Any other SMTP provider works the same way. Google changes these rules from time to time, so check their current documentation.

Connections are encrypted (`starttls` on port 587, or `ssl` on 465) and the server certificate is checked.

## Test, step by step

1. Camera seen? `rpicam-hello --list-cameras` must list it. If it says "No cameras available", power off, reseat the ribbon cable (contacts towards the HDMI ports on a Pi 4B) and check `camera_auto_detect=1` in `/boot/firmware/config.txt`.
2. Mail works? `sudo -u homesec homesec mailtest` sends a test mail, no camera needed.
3. Camera + mail work? `sudo -u homesec homesec snapshot` takes a picture and mails it. Stop the service first (`sudo systemctl stop homesec`); only one program can use the camera.
4. Start it: `sudo systemctl start homesec`, watch the log with `journalctl -u homesec -f` and walk in front of the camera.

## Commands

| Command | What it does |
|---|---|
| `homesec run` | Watch and mail (this is what the service runs) |
| `homesec snapshot` | Take one picture now and mail it |
| `homesec mailtest` | Send a test mail |
| `homesec slacktest` | Post a test message to Slack |

Options: `--env FILE` uses another settings file, `-v` shows debug messages (including ignored movement, useful for tuning `THRESHOLD`). Exit code 78 means a setting is wrong; the service then stops instead of retrying.

## Slack

Set `SLACK_WEBHOOK_URL` (an [incoming webhook](https://api.slack.com/messaging/webhooks) of your Slack app) and optionally `SLACK_CHANNEL="#sec-cams"` in the settings file, then run `sudo -u homesec homesec slacktest`. Every motion alert then also posts a message with links to the picture and the console. Webhooks cannot upload files, so the picture itself is a link (it opens on your home network only). Webhooks of Slack apps post to the channel chosen when the webhook was created and may ignore `SLACK_CHANNEL`: create the webhook for `#sec-cams`. A Slack failure never stops the mail.

## Web console

While the service runs, open `http://<name-or-ip-of-the-Pi>:8080/` (for example `http://pycam.local:8080/`). It has the live camera view (MJPEG, up to 4 viewers), a motion meter with graph, today's activity, the event log with pictures, and one button that pauses or resumes detection. While paused the camera is not watched and no alerts are sent. The choice survives a restart. The files are in `static/` (`index.html`, `style.css`, `app.js`); `preps.sh` copies them to `/usr/local/share/homesec/static`.

Watching the live view makes detection slower while a page is open, more so at a high `RESOLUTION`; `1280x720` and `FPS=10` is a good choice.

There is **no login**: anyone on your network who can reach the port sees the live camera and can pause the alarm. Use it on a trusted home network only, never forward the port to the internet. `WEB_HOST="127.0.0.1"` limits it to the Pi itself (for example through an SSH tunnel), `WEB_PORT=0` switches it off. Requests from other websites and unknown host names are refused (`WEB_ALLOWED_HOSTS` adds names).

## Tuning

- **False alarms** (light changes, curtains, insects at night): raise `THRESHOLD` (default 7) or `FRAMES`.
- **Movement is missed**: lower `THRESHOLD` (try 4), or raise `FPS`.
- **Too many mails**: raise `COOLDOWN`.
- Run `sudo -u homesec homesec run -v` in a terminal to see the score of every ignored movement.

## USB webcam

Run `./preps.sh --usb` (installs `python3-opencv`) and set `CAMERA="opencv"`, optionally `VIDEO_DEVICE="/dev/video0"`, in the settings file. With `CAMERA="auto"` a USB webcam is used when no Pi camera is found. The Pi camera path is the one developed for this project; the USB path is basic.

## Updating from the older version (motion + fswebcam + msmtp)

Run `./preps.sh`. It stops the old service, disables `motion`, gives the service user ownership of `/var/lib/homesec` and keeps your `/etc/homesec/homesec.env`. That file needs the new mail settings (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`; `EMAIL` still works as `MAIL_TO`). Afterwards you can remove the old software, the installer prints the commands.

## Troubleshooting

- **`settings: ...` and the service does not start**: fix `/etc/homesec/homesec.env`, then `sudo systemctl restart homesec`.
- **"not enough contiguous memory (CMA)" / `Cannot allocate memory`**: see "Out of camera memory" below.
- **"cannot open the camera ... busy"**: another program uses it. `sudo systemctl stop homesec` before `snapshot`, and make sure `motion` is disabled (`systemctl is-enabled motion`).
- **No mail**: `journalctl -u homesec -e`. Failed mails are retried twice; the picture stays in `/var/lib/homesec`.
- **Permission denied reading settings**: run commands as the service user: `sudo -u homesec homesec ...`.
- **A different `IMAGE_DIR`**: also add it to `ReadWritePaths=` in the service file (`sudo systemctl edit homesec`), then `sudo systemctl daemon-reload && sudo systemctl restart homesec`.

## Out of camera memory

The camera needs its picture buffers in one contiguous block of memory, the CMA pool, which is separate from normal RAM and has a fixed size. Each buffer holds a full picture, so large pictures and many buffers can exhaust it. The error looks like `OSError: [Errno 12] Cannot allocate memory` or "not enough contiguous memory (CMA)".

1. See how much CMA you have: `grep -i cma /proc/meminfo` (`CmaTotal` is the pool, `CmaFree` what is left while nothing uses the camera). Also check that no other camera program runs (`ps aux | grep -i -E 'cam|python'`), for example an older camera service.
2. Use smaller pictures: set `RESOLUTION="1280x720"` in `/etc/homesec/homesec.env`. The memory use is roughly width x height x 3 bytes x `BUFFER_COUNT`, so 1920x1080 with 3 buffers needs about 19 MB and 1280x720 about 8 MB, plus what the camera system keeps for itself.
3. Use fewer buffers: `BUFFER_COUNT=2` (default 3, picamera2 itself defaults to 6).
4. Give the camera more CMA: it is set with a parameter of the display overlay in `/boot/firmware/config.txt`, for example `dtoverlay=vc4-kms-v3d,cma-256`. The values your system supports are listed by `dtoverlay -h vc4-kms-v3d` (look for `cma-...`). Reboot afterwards. If your `config.txt` has no `vc4-kms-v3d` line, check the Raspberry Pi documentation for your OS version before changing it.

## Development

```
python3 -m unittest discover -s tests -v
```

The tests use a fake camera, a fake `picamera2` module and a mocked mail server, so they run on any computer with Python and numpy. They do not replace a test with the real camera.
