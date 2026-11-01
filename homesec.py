#!/usr/bin/python3
"""homeSec - mail a picture when the camera sees movement.

Commands:
  homesec run        watch the camera and mail a picture on movement (the service)
  homesec snapshot   take one picture now and mail it (stop the service first)
  homesec mailtest   send a test mail without using the camera
  homesec slacktest  post a test message to Slack

Settings are read from the first of these files that exists:
  --env FILE, $HOMESEC_ENV, ./.env, /etc/homesec/homesec.env
Environment variables override the file. See homesec.env.example.

Needs only the Raspberry Pi OS packages python3-picamera2 and python3-numpy
(python3-opencv as well for a USB webcam). No pip, no other software.
"""
from __future__ import annotations

import argparse
import errno
import io
import ipaddress
import json
import logging
import mimetypes
import os
import queue
import re
import select
import signal
import smtplib
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

log = logging.getLogger("homesec")

EX_CONFIG = 78  # the systemd unit does not restart the service on this exit code
DEFAULT_ENV_FILE = "/etc/homesec/homesec.env"
STATIC_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")  # one file name, no folders
MAX_VIEWERS = 4  # live views at the same time; every one costs the Pi a JPEG encode per frame


def default_static_dir() -> Path:
    """The web console files: the "static" folder next to this file, or where preps.sh installs them."""
    here = Path(__file__).resolve().parent / "static"
    return here if here.is_dir() else Path("/usr/local/share/homesec/static")


class ConfigError(Exception):
    """A setting is missing or invalid."""


class CameraError(Exception):
    """The camera cannot be opened or gives no pictures."""


# --------------------------------------------------------------------------- settings


def parse_env_file(text: str) -> dict:
    """Parse KEY=value lines (# comments, optional quotes, optional "export ")."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def read_settings(env_file: Optional[str], environ: Mapping[str, str]) -> dict:
    """Settings from the env file (if any), overridden by real environment variables."""
    if env_file:
        candidates = [env_file]
    else:
        candidates = [environ.get("HOMESEC_ENV"), ".env", DEFAULT_ENV_FILE]
    settings = {}
    for candidate in candidates:
        if not candidate:
            continue
        try:
            settings = parse_env_file(Path(candidate).read_text(encoding="utf-8"))
            log.debug("settings read from %s", candidate)
            break
        except FileNotFoundError:
            if env_file:
                raise ConfigError(f"settings file {candidate} not found") from None
        except PermissionError:
            raise ConfigError(
                f"no permission to read {candidate} (run as the service user: sudo -u homesec homesec ...)"
            ) from None
    settings.update(environ)
    return settings


class _Reader:
    """Typed access to the settings, with clear error messages."""

    def __init__(self, settings: Mapping[str, str]):
        self._s = settings

    def text(self, key: str, default: str = "", legacy: tuple = (), strip: bool = True) -> str:
        for name in (key, *legacy):
            value = self._s.get(name)
            if value is not None and value.strip() != "":
                return value.strip() if strip else value
        return default

    def number(self, key: str, default, kind, minimum=None, maximum=None):
        raw = self.text(key)
        if raw == "":
            return default
        try:
            value = kind(raw)
        except ValueError:
            raise ConfigError(f"{key} must be a number, got {raw!r}") from None
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            raise ConfigError(f"{key} must be between {minimum} and {maximum}, got {raw}")
        return value

    def choice(self, key: str, default: str, options: tuple) -> str:
        value = self.text(key, default).lower()
        if value not in options:
            raise ConfigError(f"{key} must be one of {', '.join(options)}, got {value!r}")
        return value

    def size(self, key: str, default: str) -> tuple:
        raw = self.text(key, default).lower()
        try:
            width, height = (int(part) for part in raw.split("x"))
        except ValueError:
            raise ConfigError(f"{key} must look like 1920x1080, got {raw!r}") from None
        if width < 64 or height < 64 or width % 2 or height % 2:
            raise ConfigError(f"{key} must be at least 64x64 with even numbers, got {raw}")
        return width, height


@dataclass(frozen=True)
class Config:
    mail_to: tuple
    mail_from: str
    mail_subject: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str = field(repr=False)  # secrets stay out of repr(), so they cannot end up in a log
    smtp_security: str
    smtp_timeout: int
    image_dir: Path
    camera: str
    video_device: str
    resolution: tuple
    detect_size: tuple
    fps: float
    buffer_count: int
    threshold: float
    frames: int
    warmup: float
    cooldown: float
    keep_days: int
    jpeg_quality: int
    web_host: str
    web_port: int
    web_url: str
    web_allowed_hosts: tuple
    static_dir: Path
    slack_webhook: str = field(repr=False)
    slack_channel: str
    log_level: str

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> "Config":
        r = _Reader(settings)

        recipients = tuple(a.strip() for a in r.text("MAIL_TO", legacy=("EMAIL",)).split(",") if a.strip())
        if not recipients:
            raise ConfigError("MAIL_TO is not set (the address the pictures are mailed to)")
        if any("@" not in a for a in recipients):
            raise ConfigError(f"MAIL_TO has an invalid address: {', '.join(recipients)}")

        smtp_host = r.text("SMTP_HOST")
        if not smtp_host:
            raise ConfigError("SMTP_HOST is not set (for example smtp.gmail.com)")
        smtp_user = r.text("SMTP_USER")
        smtp_password = r.text("SMTP_PASSWORD", strip=False)
        if smtp_user and not smtp_password:
            raise ConfigError("SMTP_USER is set but SMTP_PASSWORD is not")
        security = r.choice("SMTP_SECURITY", "starttls", ("starttls", "ssl", "none"))
        default_port = 465 if security == "ssl" else 587
        mail_from = r.text("MAIL_FROM") or (smtp_user if "@" in smtp_user else f"homesec@{socket.gethostname()}")

        resolution = r.size("RESOLUTION", "1920x1080")
        detect_size = r.size("DETECT_SIZE", "320x240")
        if detect_size[0] > resolution[0] or detect_size[1] > resolution[1]:
            raise ConfigError("DETECT_SIZE must not be larger than RESOLUTION")

        slack_webhook = r.text("SLACK_WEBHOOK_URL")
        slack_channel = r.text("SLACK_CHANNEL")
        if slack_webhook and not slack_webhook.startswith("https://"):
            raise ConfigError("SLACK_WEBHOOK_URL must start with https://")
        if slack_channel and not slack_webhook:
            raise ConfigError("SLACK_CHANNEL is set but SLACK_WEBHOOK_URL is not")
        web_url = r.text("WEB_URL").rstrip("/")
        if web_url and not web_url.startswith(("http://", "https://")):
            raise ConfigError("WEB_URL must start with http:// or https://")
        allowed_hosts = tuple(h.strip().lower() for h in r.text("WEB_ALLOWED_HOSTS").split(",") if h.strip())
        static_dir = Path(r.text("STATIC_DIR")) if r.text("STATIC_DIR") else default_static_dir()

        return cls(
            mail_to=recipients,
            mail_from=mail_from,
            mail_subject=r.text("MAIL_SUBJECT", "Motion detected", legacy=("SUBJECT",)),
            smtp_host=smtp_host,
            smtp_port=r.number("SMTP_PORT", default_port, int, 1, 65535),
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            smtp_security=security,
            smtp_timeout=r.number("SMTP_TIMEOUT", 30, int, 1, 600),
            image_dir=Path(r.text("IMAGE_DIR", "/var/lib/homesec")),
            camera=r.choice("CAMERA", "auto", ("auto", "picamera2", "opencv")),
            video_device=r.text("VIDEO_DEVICE", "0"),
            resolution=resolution,
            detect_size=detect_size,
            fps=r.number("FPS", 5.0, float, 0.5, 30),
            buffer_count=r.number("BUFFER_COUNT", 3, int, 2, 12),
            threshold=r.number("THRESHOLD", 7.0, float, 0, 65025),
            frames=r.number("FRAMES", 2, int, 1, 100),
            warmup=r.number("WARMUP", 5.0, float, 0, 600),
            cooldown=r.number("COOLDOWN", 300.0, float, 1, 86400),
            keep_days=r.number("KEEP_DAYS", 7, int, 1, 3650),
            jpeg_quality=r.number("JPEG_QUALITY", 90, int, 10, 100),
            web_host=r.text("WEB_HOST", "0.0.0.0"),
            web_port=r.number("WEB_PORT", 8080, int, 0, 65535),  # 0 = no web page
            web_url=web_url,
            web_allowed_hosts=allowed_hosts,
            static_dir=static_dir,
            slack_webhook=slack_webhook,
            slack_channel=slack_channel,
            log_level=r.choice("LOG_LEVEL", "info", ("debug", "info", "warning", "error")),
        )

    @classmethod
    def load(cls, env_file: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> "Config":
        return cls.from_settings(read_settings(env_file, os.environ if environ is None else environ))


# ----------------------------------------------------------------------------- mail


def build_message(cfg: Config, image: Optional[Path] = None, subject: Optional[str] = None,
                  body: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject or cfg.mail_subject
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=socket.gethostname())
    msg.set_content(body or f"Motion detected on {socket.gethostname()} at {datetime.now():%Y-%m-%d %H:%M:%S}.")
    if image is not None:
        msg.add_attachment(image.read_bytes(), maintype="image", subtype="jpeg", filename=image.name)
    return msg


def send_message(cfg: Config, msg: EmailMessage) -> None:
    """Send over SMTP with certificate checking. Raises OSError (incl. smtplib errors) on failure."""
    context = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout, context=context)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout)
    with server:
        if cfg.smtp_security == "starttls":
            server.starttls(context=context)
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


class _RetryWorker:
    """Delivers items from a background thread, with retries, so a slow server never blocks detection."""

    NAME = "worker"
    ATTEMPTS = 3
    RETRY_DELAYS = (10, 60)  # seconds before attempt 2 and 3

    def __init__(self, cfg: Config, sender: Callable):
        self._cfg = cfg
        self._send = sender
        self._queue: queue.Queue = queue.Queue()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._work, name=self.NAME, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, item) -> None:
        self._queue.put(item)

    def close(self, timeout: float = 30) -> None:
        """Finish the delivery in progress (without waiting for retries) and stop."""
        self._stopping.set()
        self._queue.put(None)
        self._thread.join(timeout)

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self.deliver(item)

    def deliver(self, item) -> bool:
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                self._deliver_once(item)
                return True
            except OSError as exc:
                log.error("%s attempt %d/%d failed: %s", self.NAME, attempt, self.ATTEMPTS, exc)
            if attempt < self.ATTEMPTS and self._stopping.wait(self.RETRY_DELAYS[attempt - 1]):
                break
        log.error("giving up on %s", self._describe(item))
        return False

    def _deliver_once(self, item) -> None:
        raise NotImplementedError

    def _describe(self, item) -> str:
        return str(item)


class Mailer(_RetryWorker):
    """Mails pictures."""

    NAME = "mail"

    def __init__(self, cfg: Config, sender: Callable = send_message):
        super().__init__(cfg, sender)

    def _deliver_once(self, image: Path) -> None:
        self._send(self._cfg, build_message(self._cfg, image))
        log.info("mailed %s to %s", image.name, ", ".join(self._cfg.mail_to))

    def _describe(self, image: Path) -> str:
        return f"{image.name} (the picture stays in {self._cfg.image_dir})"


# ---------------------------------------------------------------------------- slack


class SlackError(OSError):
    """Slack refused the message."""


def _slack_post(url: str, payload: dict, timeout: float = 10) -> None:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, response.read(200).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:  # Slack explains itself in the body, e.g. "channel_not_found"
        raise SlackError(f"Slack answered {exc.code}: {exc.read(200).decode('utf-8', 'replace').strip()}") from None
    if status != 200 or body != "ok":
        raise SlackError(f"Slack answered {status}: {body}")


def post_slack(cfg: Config, text: str) -> None:
    """Post to the incoming webhook. The webhook URL is a secret and is never logged.

    Slack-app webhooks always post to the channel chosen when the webhook was created and may refuse
    a different one; legacy webhooks honour SLACK_CHANNEL. So if Slack complains about the channel,
    post again without it, and say so.
    """
    payload = {"text": text}
    if cfg.slack_channel:
        payload["channel"] = cfg.slack_channel
    try:
        _slack_post(cfg.slack_webhook, payload)
    except SlackError as exc:
        if "channel" not in payload or "channel" not in str(exc).lower():
            raise
        log.warning("Slack did not accept the channel %s (%s); posting to the channel the webhook was "
                    "created for instead", cfg.slack_channel, exc)
        _slack_post(cfg.slack_webhook, {"text": text})


def slack_text(cfg: Config, image: Optional[Path], score: float) -> str:
    host = socket.gethostname()
    text = f":rotating_light: *Motion detected* on `{host}` at {datetime.now():%Y-%m-%d %H:%M:%S} (score {score:.1f})"
    if image is not None and cfg.web_port:
        base = cfg.web_url or f"http://{host}.local:{cfg.web_port}"
        text += f"\n<{base}/pictures/{image.name}|View picture>  ·  <{base}/|Open console>"
    return text


class SlackNotifier(_RetryWorker):
    """Posts motion alerts to Slack."""

    NAME = "slack"

    def __init__(self, cfg: Config, sender: Callable = post_slack):
        super().__init__(cfg, sender)

    def _deliver_once(self, text: str) -> None:
        self._send(self._cfg, text)
        log.info("posted to Slack%s", f" ({self._cfg.slack_channel})" if self._cfg.slack_channel else "")

    def _describe(self, text: str) -> str:
        return "the Slack message"


# --------------------------------------------------------------------------- cameras


def describe_camera_error(exc: Exception, cfg: Config) -> str:
    """Turn a low-level camera exception into advice the user can act on."""
    if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
        return (
            f"the camera could not get enough contiguous memory (CMA) for {cfg.resolution[0]}x{cfg.resolution[1]} "
            f"pictures with {cfg.buffer_count} buffers. Lower RESOLUTION (try 1280x720) or BUFFER_COUNT in "
            "the settings file, or give the camera more CMA memory; see 'Out of camera memory' in the README."
        )
    return f"camera setup failed: {exc}"


class PiCamera:
    """Raspberry Pi camera (ribbon cable, and USB cameras libcamera supports) via picamera2.

    Two streams: "main" (full size, saved as JPEG) and "lores" (small YUV420, used for detection;
    the first plane of YUV420 is the grey image, so no conversion is needed).
    """

    NAME = "picamera2"

    def __init__(self, cfg: Config):
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraError(f"picamera2 is not installed ({exc}); run: sudo apt install python3-picamera2") from None
        try:
            self._cam = Picamera2()
        except Exception as exc:  # picamera2 raises RuntimeError and others
            raise CameraError(
                f"cannot open the camera: {exc}. Is it connected ('rpicam-hello --list-cameras'), "
                "or already used by another program (sudo systemctl stop homesec)?"
            ) from None
        self._cfg = cfg
        self._width, self._height = cfg.detect_size
        try:
            model = str(self._cam.camera_properties.get("Model", "")).upper()  # e.g. IMX477
        except Exception:  # not every camera reports a model
            model = ""
        self.label = f"{model} · picamera2" if model else self.NAME
        try:
            config = self._cam.create_video_configuration(
                main={"size": cfg.resolution, "format": "RGB888"},
                lores={"size": cfg.detect_size, "format": "YUV420"},
                controls={"FrameRate": cfg.fps},
                buffer_count=cfg.buffer_count,  # picamera2 default is 6; every buffer is a full picture in CMA memory
            )
            self._cam.configure(config)
            self._cam.options["quality"] = cfg.jpeg_quality
            self._stride = self._cam.stream_configuration("lores")["stride"]
        except Exception as exc:
            self._release()
            raise CameraError(describe_camera_error(exc, cfg)) from None

    def _release(self) -> None:
        try:
            self._cam.close()
        except Exception as exc:  # already failing; do not hide the original error
            log.debug("closing the camera failed: %s", exc)

    def start(self) -> None:
        try:
            self._cam.start()
        except Exception as exc:
            self._release()
            raise CameraError(describe_camera_error(exc, self._cfg)) from None

    def read_gray(self) -> np.ndarray:
        buf = self._cam.capture_buffer("lores")
        plane = buf[: self._stride * self._height].reshape(self._height, self._stride)
        return np.ascontiguousarray(plane[:, : self._width])

    def capture(self, path: Path) -> None:
        self._cam.capture_file(str(path))

    def jpeg(self) -> bytes:
        """The current picture as JPEG bytes, for the live view."""
        buffer = io.BytesIO()
        self._cam.capture_file(buffer, format="jpeg")
        return buffer.getvalue()

    def close(self) -> None:
        try:
            self._cam.stop()
        finally:
            self._cam.close()


class UsbCamera:
    """USB webcam via OpenCV (python3-opencv)."""

    NAME = "opencv"
    label = "USB camera · opencv"

    def __init__(self, cfg: Config):
        try:
            import cv2
        except ImportError as exc:
            raise CameraError(f"OpenCV is not installed ({exc}); run: sudo apt install python3-opencv") from None
        self._cv2 = cv2
        self._detect_size = cfg.detect_size
        self._quality = cfg.jpeg_quality
        self._last = None
        device = int(cfg.video_device) if cfg.video_device.isdigit() else cfg.video_device
        self._cap = cv2.VideoCapture(device)
        if not self._cap.isOpened():
            raise CameraError(f"cannot open USB camera {cfg.video_device} (check 'lsusb' and 'ls /dev/video*')")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.resolution[0])
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.resolution[1])
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def start(self) -> None:
        pass

    def read_gray(self) -> np.ndarray:
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise CameraError("the USB camera returned no picture")
        self._last = frame
        gray = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2GRAY)
        return self._cv2.resize(gray, self._detect_size, interpolation=self._cv2.INTER_AREA)

    def capture(self, path: Path) -> None:
        if self._last is None:
            self.read_gray()
        if not self._cv2.imwrite(str(path), self._last, [self._cv2.IMWRITE_JPEG_QUALITY, self._quality]):
            raise CameraError(f"could not write {path}")

    def jpeg(self) -> bytes:
        """A fresh picture as JPEG bytes, for the live view."""
        self.read_gray()
        ok, data = self._cv2.imencode(".jpg", self._last, [self._cv2.IMWRITE_JPEG_QUALITY, self._quality])
        if not ok:
            raise CameraError("could not encode the picture")
        return data.tobytes()

    def close(self) -> None:
        self._cap.release()


def open_camera(cfg: Config):
    choice = cfg.camera
    if choice == "auto":
        choice = "opencv"
        try:
            from picamera2 import Picamera2

            if Picamera2.global_camera_info():
                choice = "picamera2"
        except Exception as exc:  # no picamera2 or no libcamera: fall back to a USB webcam
            log.debug("picamera2 not usable (%s), trying a USB webcam", exc)
    log.info("using camera backend %s", choice)
    return PiCamera(cfg) if choice == "picamera2" else UsbCamera(cfg)


# ------------------------------------------------------------------ motion detection


class MotionDetector:
    """Frame differencing: mean squared grey-level change between two successive small frames.

    Slow changes (daylight, auto exposure) change little between frames and are ignored.
    Movement must be seen in `frames_needed` successive frames, which filters out flicker.
    """

    def __init__(self, threshold: float, frames_needed: int):
        self.threshold = threshold
        self.frames_needed = frames_needed
        self._previous: Optional[np.ndarray] = None
        self._hits = 0

    def update(self, gray: np.ndarray) -> tuple:
        """Returns (motion, score). int32 so the squared difference cannot overflow."""
        current = gray.astype(np.int32)
        previous, self._previous = self._previous, current
        if previous is None:
            return False, 0.0
        diff = current - previous
        score = float(np.mean(diff * diff))
        self._hits = self._hits + 1 if score > self.threshold else 0
        if self._hits >= self.frames_needed:
            self._hits = 0
            return True, score
        return False, score


def prune(image_dir: Path, keep_days: int) -> int:
    """Delete pictures older than keep_days so the SD card does not fill up."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in image_dir.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)
    return removed


class Watcher:
    """One detection step per camera frame; mails a picture on movement."""

    def __init__(self, cfg: Config, camera, mailer: Mailer,
                 clock: Callable = time.monotonic, now: Callable = datetime.now,
                 control: Optional["Control"] = None, slack: Optional["SlackNotifier"] = None):
        self.cfg = cfg
        self.camera = camera
        self.mailer = mailer
        self.control = control
        self.slack = slack
        self.last_score = 0.0  # for the motion meter on the web page
        self.detector = MotionDetector(cfg.threshold, cfg.frames)
        self._clock = clock
        self._now = now
        self._started = clock()
        self._last_trigger: Optional[float] = None
        self._was_paused = False

    def step(self) -> Optional[Path]:
        if self.control is not None and self.control.paused:
            if not self._was_paused:
                log.info("detection paused")
                self._was_paused = True
            self.last_score = 0.0
            return None  # frames are not even read
        if self._was_paused:
            log.info("detection resumed")
            self._was_paused = False
            self.detector = MotionDetector(self.cfg.threshold, self.cfg.frames)  # old frame is stale
        t = self._clock()
        moved, score = self.detector.update(self.camera.read_gray())
        self.last_score = score
        if not moved:
            return None
        if t - self._started < self.cfg.warmup:
            log.debug("motion (score %.1f) ignored during warm-up", score)
            return None
        if self._last_trigger is not None and t - self._last_trigger < self.cfg.cooldown:
            log.debug("motion (score %.1f) ignored, cooldown active", score)
            return None
        path = self.cfg.image_dir / f"{self._now():%Y-%m-%d_%H%M%S}.jpg"
        self.camera.capture(path)
        self._last_trigger = t
        log.info("motion (score %.1f), saved %s", score, path.name)
        self.mailer.submit(path)
        if self.slack is not None:
            self.slack.submit(slack_text(self.cfg, path, score))
        prune(self.cfg.image_dir, self.cfg.keep_days)
        return path


# ------------------------------------------------------------------------ web console


class Control:
    """Pause switch shared by the web page and the detection loop. Kept in a file, so it survives restarts."""

    def __init__(self, state_file: Path):
        self._file = state_file
        self.paused = state_file.exists()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        try:
            if paused:
                self._file.touch()
            else:
                self._file.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not save the pause state: %s", exc)


EVENT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}\.jpg$")
EVENT_FORMAT = "%Y-%m-%d_%H%M%S"


def _event_time(name: str) -> Optional[datetime]:
    try:
        return datetime.strptime(name[:-4], EVENT_FORMAT)
    except ValueError:
        return None


def event_names(image_dir: Path) -> list:
    """Names of the saved motion pictures (2026-11-01_044012.jpg), newest first."""
    try:
        names = [p.name for p in image_dir.iterdir() if EVENT_NAME.match(p.name)]
    except OSError:
        return []
    return sorted((n for n in names if _event_time(n)), reverse=True)


def describe_event(name: str) -> dict:
    stamp = _event_time(name)
    return {"name": name, "url": f"/pictures/{name}", "date": f"{stamp:%Y-%m-%d}",
            "time": f"{stamp:%H:%M:%S}", "ts": int(stamp.timestamp())}


class FrameHub:
    """Latest camera picture for the live view: the detection loop publishes, stream handlers wait."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame = b""
        self._seq = 0
        self._viewers = 0
        self.closed = False

    @property
    def viewers(self) -> int:
        return self._viewers

    def add_viewer(self) -> int:
        """Returns the current frame number, so a new viewer starts with a fresh picture."""
        with self._cond:
            self._viewers += 1
            return self._seq

    def remove_viewer(self) -> None:
        with self._cond:
            self._viewers = max(0, self._viewers - 1)

    def publish(self, jpeg: bytes) -> None:
        with self._cond:
            self._frame = jpeg
            self._seq += 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def wait(self, last_seq: int, timeout: float) -> Optional[tuple]:
        """Block until there is a frame newer than last_seq. Returns (seq, jpeg) or None on timeout/close."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq or self.closed, timeout)
            if self.closed or self._seq == last_seq:
                return None
            return self._seq, self._frame


class App:
    """What the web console can see and change."""

    def __init__(self, cfg: Config, control: Control, backend: str = "", names_ttl: float = 2.0):
        self.cfg = cfg
        self.control = control
        self.backend = backend
        self.hub = FrameHub()
        self.started = time.time()
        self.score = 0.0  # latest motion score, written by the detection loop
        self._names: list = []
        self._names_at = float("-inf")
        self._names_ttl = names_ttl

    def names(self) -> list:
        """Picture names, cached briefly so several open pages do not rescan the folder all the time."""
        now = time.monotonic()
        if now - self._names_at >= self._names_ttl:
            self._names, self._names_at = event_names(self.cfg.image_dir), now
        return self._names

    def status(self) -> dict:
        cfg = self.cfg
        names = self.names()
        today = f"{datetime.now():%Y-%m-%d}"
        return {
            "detection": "paused" if self.control.paused else "on",
            "score": round(self.score, 2),
            "threshold": cfg.threshold,
            "events_today": sum(n.startswith(today) for n in names),
            "last_motion": describe_event(names[0]) if names else None,
            "camera": self.backend,
            "resolution": f"{cfg.resolution[0]}x{cfg.resolution[1]}",
            "fps": cfg.fps,
            "viewers": self.hub.viewers,
            "uptime": int(time.time() - self.started),
            "now": int(time.time()),
            "host": socket.gethostname(),
            "slack": {"enabled": bool(cfg.slack_webhook), "channel": cfg.slack_channel},
        }

    def events(self, limit: int = 30) -> list:
        return [describe_event(n) for n in self.names()[:limit]]


def host_allowed(host_header: str, extra: tuple = ()) -> bool:
    """Accept IP addresses and local names only. A web page on another site that tricks the browser into
    reaching this Pi under its own name (DNS rebinding) is refused, so it cannot read the camera."""
    name = host_header.strip().lower()
    if name.startswith("["):  # [::1]:8080
        name = name[1:].split("]")[0]
    elif name.count(":") == 1:  # name:8080
        name = name.split(":")[0]
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in ("localhost", socket.gethostname().lower()) or name.endswith(".local") \
        or "." not in name or name in extra


def make_handler(app: App):
    cfg = app.cfg
    security = {
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                   "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }

    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # seconds: a stalled or half-open connection must not hold a thread for ever

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
                  cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for key, value in security.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data) -> None:
            self._send(200, json.dumps(data).encode(), "application/json")

        def _host_ok(self) -> bool:
            if host_allowed(self.headers.get("Host", ""), cfg.web_allowed_hosts):
                return True
            self._send(403, b"host name not allowed; add it to WEB_ALLOWED_HOSTS in the settings file")
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            path = urlparse(self.path).path
            if path == "/api/status":
                return self._json(app.status())
            if path == "/api/events":
                return self._json({"events": app.events()})
            if path == "/stream.mjpg":
                return self._stream()
            if path.startswith("/pictures/"):
                return self._picture(path[len("/pictures/"):])
            self._static("index.html" if path in ("/", "/index.html") else path.lstrip("/"))

        def _static(self, name: str) -> None:
            if not STATIC_NAME.match(name):
                return self._send(404, b"not found")
            try:
                data = (cfg.static_dir / name).read_bytes()
            except OSError:
                if name == "index.html":
                    return self._send(500, f"web console files not found in {cfg.static_dir}".encode())
                return self._send(404, b"not found")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, data, ctype, "no-cache")

        def _picture(self, name: str) -> None:
            if not EVENT_NAME.match(name):  # no slashes or dots: nothing outside the picture folder
                return self._send(404, b"not found")
            try:
                data = (cfg.image_dir / name).read_bytes()
            except OSError:
                return self._send(404, b"not found")
            self._send(200, data, "image/jpeg", "private, max-age=3600")

        def _client_gone(self) -> bool:
            """True when the viewer closed the connection. Needed because with no new pictures there is
            nothing to write, and only a failing write would show it."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
            except OSError:
                return True

        def _stream(self) -> None:
            hub = app.hub
            if hub.viewers >= MAX_VIEWERS:
                return self._send(503, b"too many viewers")
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            for key, value in security.items():
                self.send_header(key, value)
            self.end_headers()
            seq = hub.add_viewer()
            try:
                while not hub.closed:
                    item = hub.wait(seq, timeout=5)
                    if item is None:
                        if self._client_gone():
                            break
                        continue
                    seq, frame = item
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except OSError:  # the viewer closed the page
                pass
            finally:
                hub.remove_viewer()

        def do_POST(self):
            if not self._host_ok():
                return
            origin = self.headers.get("Origin")  # a page on another site must not flip the switch
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._send(403, b"forbidden")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if urlparse(self.path).path != "/api/detection":
                return self._send(404, b"not found")
            if not 0 <= length <= 100:
                return self._send(400, b"bad request")
            state = parse_qs(self.rfile.read(length).decode("ascii", "replace")).get("state", [""])[0]
            if state not in ("on", "off"):
                return self._send(400, b"bad request")
            app.control.set_paused(state == "off")
            log.info("detection switched %s from the web page (%s)", state, self.client_address[0])
            self._json(app.status())

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

    return Handler


def start_web(app: App):
    cfg = app.cfg
    if cfg.web_port == 0:
        return None
    try:
        server = ThreadingHTTPServer((cfg.web_host, cfg.web_port), make_handler(app))
    except OSError as exc:  # detection must keep working without the page
        log.error("web page not started on %s:%d: %s", cfg.web_host, cfg.web_port, exc)
        return None
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    shown = f"{socket.gethostname()}.local" if cfg.web_host in ("", "0.0.0.0") else cfg.web_host
    log.info("web console on http://%s:%d/ (no login: keep it on a trusted network)", shown, cfg.web_port)
    return server


# --------------------------------------------------------------------------- commands


def cmd_run(cfg: Config) -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    cfg.image_dir.mkdir(parents=True, exist_ok=True)
    prune(cfg.image_dir, cfg.keep_days)
    mailer = Mailer(cfg)
    mailer.start()
    slack = SlackNotifier(cfg) if cfg.slack_webhook else None
    if slack:
        slack.start()
    control = Control(cfg.image_dir / ".paused")
    if control.paused:
        log.info("starting with detection paused (use the web page to resume)")
    camera = open_camera(cfg)
    app = App(cfg, control, getattr(camera, "label", getattr(camera, "NAME", "")))
    server = None
    try:
        camera.start()
        server = start_web(app)
        watcher = Watcher(cfg, camera, mailer, control=control, slack=slack)
        log.info("watching for movement (%.1f fps, threshold %.1f, cooldown %ds), mailing to %s%s",
                 cfg.fps, cfg.threshold, cfg.cooldown, ", ".join(cfg.mail_to), " and posting to Slack" if slack else "")
        interval = 1.0 / cfg.fps
        view_failed = False
        while not stop.is_set():
            began = time.monotonic()
            watcher.step()
            app.score = watcher.last_score
            if app.hub.viewers:  # pictures for the live view are only made while someone is watching
                try:
                    app.hub.publish(camera.jpeg())
                    view_failed = False
                except Exception as exc:  # the live view must never stop the alarm
                    if not view_failed:
                        log.warning("live view picture failed: %s", exc)
                        view_failed = True
            stop.wait(max(0.0, interval - (time.monotonic() - began)))
        log.info("stopping")
    finally:
        app.hub.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        camera.close()
        mailer.close()
        if slack:
            slack.close()
    return 0


def cmd_slacktest(cfg: Config) -> int:
    if not cfg.slack_webhook:
        raise ConfigError("SLACK_WEBHOOK_URL is not set")
    try:
        post_slack(cfg, f":white_check_mark: homeSec test message from `{socket.gethostname()}`")
    except OSError as exc:
        log.error("posting to Slack failed: %s", exc)
        return 1
    log.info("test message posted to Slack%s", f" ({cfg.slack_channel})" if cfg.slack_channel else "")
    return 0


def cmd_snapshot(cfg: Config) -> int:
    cfg.image_dir.mkdir(parents=True, exist_ok=True)
    camera = open_camera(cfg)
    try:
        camera.start()
        settle_until = time.monotonic() + 2  # let auto exposure and white balance settle
        while time.monotonic() < settle_until:
            camera.read_gray()
        path = cfg.image_dir / f"{datetime.now():%Y-%m-%d_%H%M%S}.jpg"
        camera.capture(path)
    finally:
        camera.close()
    log.info("saved %s, sending it to %s", path, ", ".join(cfg.mail_to))
    try:
        send_message(cfg, build_message(cfg, path))
    except OSError as exc:
        log.error("sending the mail failed: %s", exc)
        return 1
    log.info("mail sent")
    return 0


def cmd_mailtest(cfg: Config) -> int:
    msg = build_message(cfg, subject="homeSec test mail",
                        body=f"If you read this, homeSec on {socket.gethostname()} can send mail.")
    try:
        send_message(cfg, msg)
    except OSError as exc:
        log.error("sending the mail failed: %s", exc)
        return 1
    log.info("test mail sent to %s", ", ".join(cfg.mail_to))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="homesec", description="Mail a picture when the camera sees movement.")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "snapshot", "mailtest", "slacktest"),
                        help="run (default): watch, mail and post to Slack; snapshot: mail one picture now; "
                             "mailtest / slacktest: send a test mail / Slack message")
    parser.add_argument("--env", metavar="FILE", help=f"settings file (default: first of $HOMESEC_ENV, ./.env, {DEFAULT_ENV_FILE})")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug messages")
    args = parser.parse_args(argv)

    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt, stream=sys.stderr)

    try:
        cfg = Config.load(args.env)
        if not args.verbose:
            logging.getLogger().setLevel(cfg.log_level.upper())
        return {"run": cmd_run, "snapshot": cmd_snapshot, "mailtest": cmd_mailtest,
                "slacktest": cmd_slacktest}[args.command](cfg)
    except ConfigError as exc:
        log.error("settings: %s", exc)
        return EX_CONFIG
    except CameraError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
