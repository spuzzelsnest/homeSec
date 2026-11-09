# homeSec

homeSec is a small Raspberry Pi service: a camera watches a room, and when something moves homeSec mails you a picture.

It is one Python file (`homesec.py`) with no extra software: the camera is read with [picamera2](https://github.com/raspberrypi/picamera2), movement is detected with numpy, a small AI model checks with OpenCV that it is a person, and mail is sent with Python's `smtplib`. Earlier versions needed `motion`, `fswebcam`, `mutt`, `msmtp` and `inotify-tools`; those are gone.

Target: Raspberry Pi 4B with Raspberry Pi OS (Debian based, 64-bit recommended) and a Raspberry Pi camera on the ribbon cable (for example the HQ camera). USB webcams work too (`--usb`, see below).

```
camera --> homesec.service --> movement? --> person in the picture? --> saves jpg --> e-mail / Slack
```

How detection works, in two steps:

1. **Movement.** The camera delivers a small grey picture (320x240, 5 per second), cut into small blocks. Each block is compared with a background picture that slowly follows the scene, but only its *pattern* is compared, not its brightness. A police car's blue light on the wall, a lamp going on, clouds or the camera adjusting its exposure make the wall brighter or darker without changing its pattern, so they are ignored. A person in front of the wall changes the pattern. Touching changed blocks form a region; when the largest region covers at least `MIN_AREA` % of the picture for `FRAMES` frames in a row, that is movement. Camera noise at night is measured and ignored, and parts of the picture can be masked (`MASK`).
2. **Person check.** Then a small AI model ([YOLOX-tiny](https://github.com/Megvii-BaseDetection/YOLOX), Apache-2.0, runs on the Pi itself) looks at the full picture, and at a close-up of the movement when it is small. Only if it sees a person (or what you set in `OBJECTS`) is an alert sent. On a Pi 4 a check takes roughly half a second, and it only runs when something moves.

Each alert mail has the full-size picture and a short animated GIF: about 3 seconds before to 2 seconds after the alert, with the time on every frame and a red border from the moment of the alert (`CLIP_*` settings). The web console shows the clip when you open an event. After an alert, nothing is sent for `COOLDOWN` seconds (5 minutes by default).

## Install

```
git clone https://github.com/spuzzelsnest/homeSec.git
cd homeSec
./preps.sh            # --no-check: without the person check
```

`preps.sh` installs `python3-picamera2`, `python3-numpy`, `python3-pil` and `python3-opencv` with apt, downloads the person-check model (about 20 MB, checksum verified) to `/usr/local/share/homesec/models`, creates a `homesec` service user (member of `video`), installs the program as `/usr/local/bin/homesec`, creates `/etc/homesec/homesec.env` and installs and enables `homesec.service`. Run it again after updating the repo; your settings are left alone. If `motion` is installed it is disabled, because it would keep the camera busy.

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
4. Person check works? `sudo -u homesec homesec check /var/lib/homesec/<picture>.jpg` on the snapshot from step 3 (with you in it) must say `person, ..% sure`.
5. Start it: `sudo systemctl start homesec`, watch the log with `journalctl -u homesec -f` and walk in front of the camera.

## Commands

| Command | What it does |
|---|---|
| `homesec run` | Watch and mail (this is what the service runs) |
| `homesec snapshot` | Take one picture now and mail it |
| `homesec mailtest` | Send a test mail |
| `homesec slacktest` | Post a test message to Slack |
| `homesec check IMAGE` | Run the person check on a picture and show what it finds |

Options: `--env FILE` uses another settings file, `-v` shows debug messages (including ignored movement and light changes, useful for tuning). Exit code 78 means a setting is wrong; the service then stops instead of retrying.

## Slack

Set `SLACK_WEBHOOK_URL` (an [incoming webhook](https://api.slack.com/messaging/webhooks) of your Slack app) and optionally `SLACK_CHANNEL="#sec-cams"` in the settings file, then run `sudo -u homesec homesec slacktest`. Every motion alert then also posts a message with links to the picture and the console. Webhooks cannot upload files, so the picture itself is a link (it opens on your home network only). Webhooks of Slack apps post to the channel chosen when the webhook was created and may ignore `SLACK_CHANNEL`: create the webhook for `#sec-cams`. A Slack failure never stops the mail.

## Web console

While the service runs, open `http://<name-or-ip-of-the-Pi>:8080/` (for example `http://pycam.local:8080/`). It has the live camera view (MJPEG, up to 4 viewers), a motion meter with graph, today's activity, the event log with pictures, and one button that pauses or resumes detection. While paused the camera is not watched and no alerts are sent. The choice survives a restart. The files are in `static/` (`index.html`, `style.css`, `app.js`); `preps.sh` copies them to `/usr/local/share/homesec/static`.

Watching the live view makes detection slower while a page is open, more so at a high `RESOLUTION`; `1280x720` and `FPS=10` is a good choice.

There is **no login**: anyone on your network who can reach the port sees the live camera and can pause the alarm. Use it on a trusted home network only, never forward the port to the internet. `WEB_HOST="127.0.0.1"` limits it to the Pi itself (for example through an SSH tunnel), `WEB_PORT=0` switches it off. Requests from other websites and unknown host names are refused (`WEB_ALLOWED_HOSTS` adds names).

## Tuning

Every alert says what triggered it, for example `Person detected (person 87% sure, movement over 4.2% of the picture)`. The log (`journalctl -u homesec -f`) also lists movements that were *not* alerted because no person was seen. To look at those pictures, set `KEEP_REJECTED=yes`; they are kept in `/var/lib/homesec/rejected` (and deleted after `KEEP_DAYS`).

- **Still false alarms with a person in them that is not there**: raise `OBJECT_CONFIDENCE` (try 0.6–0.7).
- **People are missed**: check one of their pictures with `homesec check`. Lower `OBJECT_CONFIDENCE` (try 0.35), or `MIN_AREA` (try 0.5) when they are far away. In the dark the model sees much less: a lamp or an IR light helps.
- **Something always moves** (a tree, a TV, a window to the street): mask it with `MASK`.
- **Pets**: they are not people, so they are not alerted. Want them? `OBJECTS="person,cat,dog"`.
- **Too many mails**: raise `COOLDOWN`.
- `THRESHOLD` from older versions is no longer used; the log warns when it is still set.

## USB webcam

Run `./preps.sh` (it installs `python3-opencv`) and set `CAMERA="opencv"`, optionally `VIDEO_DEVICE="/dev/video0"`, in the settings file. With `CAMERA="auto"` a USB webcam is used when no Pi camera is found. The Pi camera path is the one developed for this project; the USB path is basic.

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
