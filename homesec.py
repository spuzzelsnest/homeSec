#!/usr/bin/python3
"""homeSec - mail a picture when the camera sees movement.

Commands:
  homesec run          watch the camera and mail a picture on movement (the service)
  homesec adduser NAME add a login for the web console (asks for the password; --role viewer: watch only)
  homesec passwd NAME  change a password      homesec deluser NAME  remove a login
  homesec users        list the logins
  homesec snapshot     take one picture now and mail it (stop the service first)
  homesec mailtest     send a test mail without using the camera
  homesec slacktest    post a test message to Slack
  homesec check IMAGE  run the person check on a picture and show what it finds

Settings are read from the first of these files that exists:
  --env FILE, $HOMESEC_ENV, ./.env, /etc/homesec/homesec.env
Environment variables override the file. See homesec.env.example.

Needs only Raspberry Pi OS packages: python3-picamera2, python3-numpy and python3-pil (the alert
clip), plus python3-opencv for the person check (and for a USB webcam), python3-av for the visit videos. No pip. The person check also needs a model file, which
deploy.sh downloads (YOLOX-tiny, Apache-2.0 licence).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import secrets
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
import shutil
import signal
import smtplib
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
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
MODEL_NAME = "yolox_tiny.onnx"

# The 80 object classes the YOLOX model knows (COCO), in model order.
COCO_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
)


def default_static_dir() -> Path:
    """The web console files: the "static" folder next to this file, or where deploy.sh installs them."""
    here = Path(__file__).resolve().parent / "static"
    return here if here.is_dir() else Path("/usr/local/share/homesec/static")


def default_model_file() -> Path:
    """The person-check model: in "models" next to this file, or where deploy.sh installs it."""
    here = Path(__file__).resolve().parent / "models" / MODEL_NAME
    return here if here.is_file() else Path("/usr/local/share/homesec/models") / MODEL_NAME


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
            if candidate == ".env" and not env_file:
                # The optional ./.env in a folder the service user may not enter (your home folder):
                # skip it and use the real settings file.
                log.debug("cannot look for ./.env here (no permission), skipping it")
                continue
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

    def flag(self, key: str, default: bool) -> bool:
        value = self.text(key, "yes" if default else "no").lower()
        if value in ("yes", "true", "on", "1"):
            return True
        if value in ("no", "false", "off", "0"):
            return False
        raise ConfigError(f"{key} must be yes or no, got {value!r}")

    def areas(self, key: str) -> tuple:
        """Rectangles in percent of the picture: "x0,y0,x1,y1; x0,y0,x1,y1". Returned as fractions."""
        rects = []
        for part in self.text(key).replace("|", ";").split(";"):
            if not part.strip():
                continue
            try:
                x0, y0, x1, y1 = (float(v) for v in part.split(","))
            except ValueError:
                raise ConfigError(f"{key} must look like 0,0,30,50; 70,0,100,20 (left,top,right,bottom in %), "
                                  f"got {part.strip()!r}") from None
            if not (0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100):
                raise ConfigError(f"{key}: {part.strip()!r} needs 0 <= left < right <= 100 and 0 <= top < bottom <= 100")
            rects.append((x0 / 100, y0 / 100, x1 / 100, y1 / 100))
        return tuple(rects)

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
    min_area: float
    max_area: float
    frames: int
    mask: tuple
    object_check: str
    objects: tuple
    object_confidence: float
    object_model: Path
    recheck: float
    keep_rejected: bool
    clip_before: float
    clip_after: float
    clip_fps: float
    clip_width: int
    record_objects: tuple
    visit_confidence: float
    record_min_area: float
    video_before: float
    video_after: float
    video_max: float
    video_fps: float
    video_kbps: int
    visit_keep_days: int
    alert_video: bool
    db_file: Path
    login: bool
    session_hours: int
    tls: str
    tls_cert: Path
    tls_key: Path
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

        min_area = r.number("MIN_AREA", 1.0, float, 0.1, 100)
        max_area = r.number("MAX_AREA", 60.0, float, 1, 100)
        if max_area <= min_area:
            raise ConfigError("MAX_AREA must be larger than MIN_AREA")
        if r.text("THRESHOLD"):
            log.warning("THRESHOLD is no longer used: movement is now measured as the part of the picture that "
                        "changed (MIN_AREA, in %). Remove THRESHOLD from the settings file.")
        objects = tuple(o.strip().lower() for o in r.text("OBJECTS", "person").split(",") if o.strip())
        unknown = [o for o in objects if o not in COCO_NAMES]
        if unknown or not objects:
            raise ConfigError(f"OBJECTS: unknown name(s) {', '.join(unknown) or '(empty)'}; use names like "
                              "person, cat, dog, car (the COCO object names)")
        object_model = Path(r.text("OBJECT_MODEL")) if r.text("OBJECT_MODEL") else default_model_file()
        record_objects = tuple(o.strip().lower() for o in r.text("RECORD_OBJECTS", "bird").split(",")
                               if o.strip() and o.strip().lower() != "none")
        unknown = [o for o in record_objects if o not in COCO_NAMES]
        if unknown:
            raise ConfigError(f"RECORD_OBJECTS: unknown name(s) {', '.join(unknown)}; use names like bird, cat, dog")
        both = [o for o in record_objects if o in objects]
        if both:
            raise ConfigError(f"{', '.join(both)} is in both OBJECTS (alert) and RECORD_OBJECTS (record); choose one")

        return cls(
            mail_to=recipients,
            mail_from=mail_from,
            mail_subject=r.text("MAIL_SUBJECT", legacy=("SUBJECT",)),  # empty: "Person detected" etc.
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
            min_area=min_area,
            max_area=max_area,
            frames=r.number("FRAMES", 3, int, 1, 100),
            mask=r.areas("MASK"),
            object_check=r.choice("OBJECT_CHECK", "auto", ("auto", "on", "off")),
            objects=objects,
            object_confidence=r.number("OBJECT_CONFIDENCE", 0.5, float, 0.05, 0.99),
            object_model=object_model,
            recheck=r.number("RECHECK", 2.0, float, 0, 600),
            keep_rejected=r.flag("KEEP_REJECTED", False),
            clip_before=r.number("CLIP_BEFORE", 3.0, float, 0, 10),
            clip_after=r.number("CLIP_AFTER", 2.0, float, 0, 10),
            clip_fps=r.number("CLIP_FPS", 2.0, float, 0.5, 10),
            clip_width=r.number("CLIP_WIDTH", 640, int, 0, 1920),  # 0 = no clip
            record_objects=record_objects,
            visit_confidence=r.number("VISIT_CONFIDENCE", 0.3, float, 0.05, 0.99),
            record_min_area=r.number("RECORD_MIN_AREA", 0.3, float, 0.1, 100),
            video_before=r.number("VIDEO_BEFORE", 3.0, float, 0, 10),
            video_after=r.number("VIDEO_AFTER", 7.0, float, 1, 120),
            video_max=r.number("VIDEO_MAX", 60.0, float, 5, 600),
            video_fps=r.number("VIDEO_FPS", 15.0, float, 1, 30),
            video_kbps=r.number("VIDEO_KBPS", 8000, int, 500, 25000),
            visit_keep_days=r.number("VISIT_KEEP_DAYS", 30, int, 1, 3650),
            alert_video=r.flag("ALERT_VIDEO", True),
            db_file=Path(r.text("DB_FILE")) if r.text("DB_FILE") else Path(r.text("IMAGE_DIR", "/var/lib/homesec")) / "homesec.db",
            login=r.flag("LOGIN", True),
            session_hours=r.number("SESSION_HOURS", 720, int, 1, 8760),  # 30 days with "keep me signed in"
            tls=r.choice("TLS", "auto", ("auto", "on", "off")),
            tls_cert=Path(r.text("TLS_CERT", "/etc/homesec/tls/cert.pem")),
            tls_key=Path(r.text("TLS_KEY", "/etc/homesec/tls/key.pem")),
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
                  body: Optional[str] = None, extra: tuple = ()) -> EmailMessage:
    kinds = {".gif": ("image", "gif"), ".mp4": ("video", "mp4")}
    msg = EmailMessage()
    msg["Subject"] = subject or cfg.mail_subject or "Motion detected"
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=socket.gethostname())
    msg.set_content(body or f"Motion detected on {socket.gethostname()} at {datetime.now():%Y-%m-%d %H:%M:%S}.")
    for attachment in ((image,) if image is not None else ()) + tuple(extra):
        maintype, subtype = kinds.get(attachment.suffix.lower(), ("image", "jpeg"))
        msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name)
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


@dataclass(frozen=True)
class Event:
    """One alert: the saved picture and why it was sent."""

    path: Path
    area: float = 0.0          # moving part of the picture, percent
    label: str = ""            # what the object check saw ("person"); empty without object check
    confidence: float = 0.0    # how sure the object check is, 0..1
    when: datetime = field(default_factory=datetime.now)
    clip: tuple = field(default=(), repr=False, compare=False)  # ((clock time, small RGB frame), ...)
    clip_t: float = 0.0        # clock time of the alert, to mark it in the clip
    clip_fps: float = 2.0
    video: Optional[Path] = None   # the alert video (MP4) being recorded, linked from the mail

    def headline(self) -> str:
        return f"{self.label.capitalize()} detected" if self.label else "Motion detected"

    def details(self) -> str:
        parts = [f"movement over {self.area:.1f}% of the picture"]
        if self.label:
            parts.insert(0, f"{self.label} {self.confidence:.0%} sure")
        return ", ".join(parts)


def web_scheme(cfg: Config) -> str:
    """https when the certificate is there (TLS=auto) or TLS=on."""
    if cfg.tls == "on" or (cfg.tls == "auto" and cfg.tls_cert.is_file()):
        return "https"
    return "http"


def console_link(cfg: Config, path: str = "/") -> str:
    host = socket.gethostname()
    return (cfg.web_url or f"{web_scheme(cfg)}://{host}.local:{cfg.web_port}") + path


def event_body(event: Event, clip: Optional[Path] = None, cfg: Optional[Config] = None) -> str:
    text = (f"{event.headline()} on {socket.gethostname()} at {event.when:%Y-%m-%d %H:%M:%S}\n"
            f"({event.details()}).")
    if clip is not None and event.clip:
        first, last = event.clip[0][0] - event.clip_t, event.clip[-1][0] - event.clip_t
        text += (f"\n\nThe animation ({clip.name}) runs from {-first:.0f} s before to {last:.0f} s after the alert; "
                 "frames from the alert on have a red border.")
    if event.video is not None:
        where = (console_link(cfg, f"/pictures/{event.video.name}") if cfg is not None and cfg.web_port
                 else str(event.video))
        text += ("\n\nThe full video, from a few seconds before the alert until the movement stops, is being "
                 f"recorded: {where} (ready when the movement has stopped).")
    return text


def write_clip(path: Path, event: Event) -> Path:
    """The frames around an alert as an animated GIF, with the time on each frame and a red border from
    the moment of the alert. Written under a temporary name first, so the web page never shows half."""
    from PIL import Image, ImageDraw, ImageFont

    height, width = event.clip[0][1].shape[:2]
    try:
        font = ImageFont.load_default(size=max(12, width // 36))
    except TypeError:  # Pillow before 10.1 has one small font only
        font = ImageFont.load_default()
    adaptive = getattr(Image, "Palette", Image).ADAPTIVE
    images = []
    for t, rgb in event.clip:
        image = Image.fromarray(rgb)
        draw = ImageDraw.Draw(image)
        offset = t - event.clip_t
        label = f"{event.when + timedelta(seconds=offset):%H:%M:%S}  {offset:+.1f}s"
        x0, y0, x1, y1 = draw.textbbox((6, 4), label, font=font)
        draw.rectangle((0, 0, x1 + 6, y1 + 4), fill=(0, 0, 0))
        draw.text((6, 4), label, fill=(255, 255, 255), font=font)
        if offset >= -1e-6:
            draw.rectangle((0, 0, width - 1, height - 1), outline=(230, 40, 40), width=max(3, width // 160))
        images.append(image.convert("P", palette=adaptive, colors=192))
    tmp = path.with_name(path.name + ".tmp")
    images[0].save(tmp, format="GIF", save_all=True, append_images=images[1:],
                   duration=int(1000 / event.clip_fps), loop=0)
    tmp.replace(path)
    return path


class Mailer(_RetryWorker):
    """Mails pictures."""

    NAME = "mail"

    def __init__(self, cfg: Config, sender: Callable = send_message):
        super().__init__(cfg, sender)

    def _deliver_once(self, event: Event) -> None:
        clip = None
        if event.clip:
            clip = event.path.with_suffix(".gif")
            if not clip.exists():  # made once, also when the mail is retried
                try:
                    write_clip(clip, event)
                except Exception as exc:  # no clip is better than no mail
                    log.warning("could not make the clip %s: %s", clip.name, exc)
                    clip = None
        msg = build_message(self._cfg, event.path, subject=self._cfg.mail_subject or event.headline(),
                            body=event_body(event, clip, self._cfg), extra=(clip,) if clip else ())
        self._send(self._cfg, msg)
        log.info("mailed %s to %s", event.path.name, ", ".join(self._cfg.mail_to))

    def _describe(self, event: Event) -> str:
        return f"{event.path.name} (the picture stays in {self._cfg.image_dir})"


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


def slack_text(cfg: Config, event: Event, video: bool = False) -> str:
    host = socket.gethostname()
    text = (f":rotating_light: *{event.headline()}* on `{host}` at {event.when:%Y-%m-%d %H:%M:%S} "
            f"({event.details()})")
    if cfg.web_port:
        base = cfg.web_url or f"{web_scheme(cfg)}://{host}.local:{cfg.web_port}"
        links = [f"<{base}/pictures/{event.path.name}|View picture>"]
        if video:  # ready when the movement has stopped
            links.append(f"<{base}/pictures/{event.path.stem}.mp4|View video>")
        elif cfg.clip_width:
            links.append(f"<{base}/pictures/{event.path.stem}.gif|View clip>")
        text += "\n" + "  ·  ".join(links + [f"<{base}/|Open console>"])
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


def shrink_rgb(bgr: np.ndarray, width: int) -> np.ndarray:
    """A small RGB copy of a BGR picture (every n-th pixel; plenty for an alert clip)."""
    step = max(1, round(bgr.shape[1] / width))
    return np.ascontiguousarray(bgr[::step, ::step, 2::-1])


class VideoBuffer:
    """The last few seconds of video, encoded in hardware (H.264) and kept in memory, so a recording
    can start before the moment something was seen. open() writes them, and what follows, to an MP4
    until close(). Needs picamera2's CircularOutput2 and PyavOutput (python3-av).

    The buffer delays the output by `before` seconds, so a recording closed at time T ends at
    T - before: callers close `before` seconds after the last moment they want in the video.
    """

    def __init__(self, cam, before: float, kbps: int, fps: float):
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import CircularOutput2, PyavOutput
        import av  # noqa: F401  (python3-av; PyavOutput imports it only when used)

        self._cam = cam
        self._pyav = PyavOutput
        self._circular = CircularOutput2(buffer_duration_ms=int(before * 1000))
        # a keyframe every second: a recording can only start at a keyframe
        self._encoder = H264Encoder(bitrate=kbps * 1000, iperiod=max(1, round(fps)), repeat=True)
        self._current: Optional[tuple] = None

    def start(self) -> None:
        self._cam.start_encoder(self._encoder, self._circular, name="main")

    @property
    def recording(self) -> bool:
        return self._current is not None

    def open(self, path: Path) -> None:
        part = path.with_name(path.name + ".part")  # renamed when complete, so the web page never shows half
        try:  # faststart: the index at the front, so a browser can play while it downloads
            output = self._pyav(str(part), format="mp4", options={"movflags": "+faststart"})
            self._circular.open_output(output)
        except Exception as exc:
            log.debug("mp4 with faststart failed (%s), trying without", exc)
            output = self._pyav(str(part), format="mp4")
            self._circular.open_output(output)
        output.error_callback = lambda exc: log.error("writing %s failed: %s", path.name, exc)
        self._current = (part, path)

    def close(self, target: Optional[Path] = None) -> Optional[Path]:
        """Finish the recording; it is saved as `target`, or the name given to open()."""
        if self._current is None:
            return None
        part, path = self._current
        self._current = None
        self._circular.close_output()
        if part.is_file() and part.stat().st_size > 0:
            path = target or path
            part.replace(path)
            return path
        part.unlink(missing_ok=True)
        return None

    def stop(self) -> None:
        self.close()
        self._cam.stop_encoder(self._encoder)


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
        self.color_width = 0  # when set, read_gray also keeps a small colour copy in .color
        self.color: Optional[np.ndarray] = None
        self.video: Optional[VideoBuffer] = None
        video = cfg.alert_video or (bool(cfg.record_objects) and cfg.object_check != "off")
        try:
            model = str(self._cam.camera_properties.get("Model", "")).upper()  # e.g. IMX477
        except Exception:  # not every camera reports a model
            model = ""
        self.label = f"{model} · picamera2" if model else self.NAME
        try:
            config = self._cam.create_video_configuration(
                main={"size": cfg.resolution, "format": "RGB888"},
                lores={"size": cfg.detect_size, "format": "YUV420"},
                # With visit videos the camera runs at up to VIDEO_FPS, but may slow down to FPS in the
                # dark (longer exposure); detection still looks at FPS frames per second.
                controls=({"FrameDurationLimits": (int(1e6 / max(cfg.video_fps, cfg.fps)), int(1e6 / cfg.fps))}
                          if video else {"FrameRate": cfg.fps}),
                # picamera2 default is 6; every buffer is a full picture in CMA memory. The video encoder
                # holds one more.
                buffer_count=cfg.buffer_count + (1 if video else 0),
            )
            self._cam.configure(config)
            self._cam.options["quality"] = cfg.jpeg_quality
            self._stride = self._cam.stream_configuration("lores")["stride"]
        except Exception as exc:
            self._release()
            raise CameraError(describe_camera_error(exc, cfg)) from None
        if video:
            try:
                self.video = VideoBuffer(self._cam, cfg.video_before, cfg.video_kbps, cfg.video_fps)
            except ImportError as exc:
                log.warning("no videos: %s (sudo apt install python3-av, or run ./deploy.sh)", exc)

    def _release(self) -> None:
        try:
            self._cam.close()
        except Exception as exc:  # already failing; do not hide the original error
            log.debug("closing the camera failed: %s", exc)

    def start(self) -> None:
        if self.video is not None:
            try:
                self.video.start()
            except Exception as exc:  # the alarm matters more than the videos
                log.warning("no videos: the video encoder did not start (%s)", exc)
                self.video = None
        try:
            self._cam.start()
        except Exception as exc:
            self._release()
            raise CameraError(describe_camera_error(exc, self._cfg)) from None

    def read_gray(self) -> np.ndarray:
        request = self._cam.capture_request()  # both streams of the same frame
        try:
            buf = request.make_buffer("lores")
            # "RGB888" arrives in B, G, R order
            self.color = shrink_rgb(request.make_array("main"), self.color_width) if self.color_width else None
        finally:
            request.release()
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
            if self.video is not None:
                try:
                    self.video.stop()
                except Exception as exc:
                    log.warning("stopping the video encoder failed: %s", exc)
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
        self.color_width = 0
        self.color: Optional[np.ndarray] = None
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
        self.color = shrink_rgb(frame, self.color_width) if self.color_width else None
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


@dataclass(frozen=True)
class MotionResult:
    moving: bool                 # movement seen in enough successive frames
    area: float                  # largest moving region, percent of the watched picture
    changed: float               # all changed blocks together, percent
    box: Optional[tuple] = None  # (left, top, right, bottom) of the largest region, fractions 0..1
    reason: str = ""             # why nothing counts: "learning", "light change"
    active: float = 0.0          # part of the picture that changed since the previous frame, percent:
                                 # something is moving right now (someone standing still is not)


class MotionDetector:
    """Finds things that move, not light that changes.

    The small grey picture is halved (less noise) and cut into blocks of 8x8. Every block is compared
    with a background picture that slowly follows the scene. Per block, the brightness and any smooth
    light gradient are removed first, so only the pattern is compared (normalised correlation): a
    blue light on the wall, a lamp, the camera adjusting its exposure make the pattern brighter or
    darker but do not change it. A person walking in front of the wall does.

    Changed blocks that touch form a region. Movement is reported when the largest region covers at
    least min_area percent of the picture in `frames_needed` successive frames. When more than
    max_area percent changes at once (lights on/off, camera bumped), the background starts over.
    The noise level is measured continuously, so grain at night does not count as a pattern.
    """

    BLOCK = 8              # block size in pixels, after halving the detection picture
    TEXTURE = 2.5          # grey levels: flatter than this (or than 2.5x the noise) has no pattern
    MATCH = 0.5            # pattern correlation below this means the block changed
    ADAPT = 0.08           # background follows quiet blocks quickly (about 2.5 s at 5 fps)
    ADAPT_CHANGED = 0.02   # ... and changed blocks slowly (about 10 s), so a parked object fades in

    def __init__(self, min_area: float = 1.0, max_area: float = 60.0, frames_needed: int = 3,
                 mask: tuple = ()):
        self.min_area = min_area
        self.max_area = max_area
        self.frames_needed = frames_needed
        self.mask = tuple(mask)
        self.noise: Optional[float] = None
        self._bg: Optional[np.ndarray] = None
        self._hits = 0
        self._grid = (0, 0)
        self._watched: Optional[np.ndarray] = None
        self._basis: tuple = ()
        self._prev: Optional[np.ndarray] = None  # pattern of the previous frame

    def reset(self) -> None:
        self._bg = None
        self._prev = None
        self._hits = 0

    def _prepare(self, gray: np.ndarray) -> np.ndarray:
        g = gray.astype(np.float32)
        h, w = g.shape
        g = g[: h - h % 2, : w - w % 2]
        g = (g[0::2, 0::2] + g[1::2, 0::2] + g[0::2, 1::2] + g[1::2, 1::2]) * 0.25
        b = self.BLOCK
        h, w = g.shape
        return g[: h - h % b, : w - w % b]

    def _blocks(self, img: np.ndarray) -> np.ndarray:
        b = self.BLOCK
        rows, cols = img.shape[0] // b, img.shape[1] // b
        return img.reshape(rows, b, cols, b).transpose(0, 2, 1, 3).reshape(rows, cols, b * b)

    def _setup(self, img: np.ndarray) -> None:
        b = self.BLOCK
        rows, cols = img.shape[0] // b, img.shape[1] // b
        self._grid = (rows, cols)
        c = np.arange(b, dtype=np.float32) - (b - 1) / 2
        x, y = np.tile(c, b), np.repeat(c, b)
        self._basis = (x / np.dot(x, x), x, y / np.dot(y, y), y)
        watched = np.ones((rows, cols), bool)
        for x0, y0, x1, y1 in self.mask:  # MASK areas are ignored
            watched[int(y0 * rows):int(np.ceil(y1 * rows)), int(x0 * cols):int(np.ceil(x1 * cols))] = False
        self._watched = watched

    def _pattern(self, blocks: np.ndarray) -> np.ndarray:
        """Each block minus its mean brightness and its smooth slope: what is left is its pattern."""
        xs, x, ys, y = self._basis
        p = blocks - blocks.mean(axis=2, keepdims=True)
        p = p - (p @ xs)[..., None] * x
        return p - (p @ ys)[..., None] * y

    def update(self, gray: np.ndarray) -> MotionResult:
        img = self._prepare(gray)
        if self._bg is None or self._bg.shape != img.shape:
            self._setup(img)
            self._bg = img.copy()
            self._hits = 0
            self.noise = noise_level(img)
            return MotionResult(False, 0.0, 0.0, reason="learning")
        self.noise = 0.8 * self.noise + 0.2 * noise_level(img)
        cur = self._pattern(self._blocks(img))
        ref = self._pattern(self._blocks(self._bg))
        n = cur.shape[2]
        var_c = np.einsum("ijk,ijk->ij", cur, cur) / n
        var_r = np.einsum("ijk,ijk->ij", ref, ref) / n
        cov = np.einsum("ijk,ijk->ij", cur, ref) / n
        floor = max(self.TEXTURE, 2.5 * self.noise) ** 2
        # Only a block that shows a pattern now can show something new. A pattern that sank into the
        # dark and the noise (lights out), or a flat wall that stayed flat, is not movement.
        visible = var_c >= floor
        match = cov / np.sqrt(var_c * np.maximum(var_r, floor) + 1e-6)
        changed = visible & (match < self.MATCH) & self._watched
        active = 0.0
        if self._prev is not None:  # the same comparison with the previous frame instead of the background
            var_p = np.einsum("ijk,ijk->ij", self._prev, self._prev) / n
            cov_p = np.einsum("ijk,ijk->ij", cur, self._prev) / n
            match_p = cov_p / np.sqrt(var_c * np.maximum(var_p, floor) + 1e-6)
            now = ((var_c >= floor) | (var_p >= floor)) & (match_p < self.MATCH) & self._watched
            active = 100.0 * int(now.sum()) / max(1, int(self._watched.sum()))
        self._prev = cur

        rate = np.where(changed, self.ADAPT_CHANGED, self.ADAPT).astype(np.float32)
        b = self.BLOCK
        self._bg += np.repeat(np.repeat(rate, b, axis=0), b, axis=1) * (img - self._bg)

        total = max(1, int(self._watched.sum()))
        changed_pct = 100.0 * int(changed.sum()) / total
        if changed_pct > self.max_area:
            self._bg = img.copy()
            self._hits = 0
            return MotionResult(False, 0.0, changed_pct, reason="light change", active=active)
        size, box = largest_region(changed)
        area = 100.0 * size / total
        self._hits = self._hits + 1 if area >= self.min_area else 0
        moving = self._hits >= self.frames_needed
        if moving:
            self._hits = 0
        rows, cols = self._grid
        frac = None if box is None else (box[1] / cols, box[0] / rows, (box[3] + 1) / cols, (box[2] + 1) / rows)
        return MotionResult(moving, area, changed_pct, frac, active=active)


def noise_level(img: np.ndarray) -> float:
    """Camera noise (standard deviation in grey levels) estimated from the picture itself: the median
    of a 3x3 second-derivative filter. Smooth surfaces and soft light give about 0, grain does not,
    and the median ignores the few sharp edges."""
    lap = (img[:-2, :-2] - 2 * img[:-2, 1:-1] + img[:-2, 2:]
           - 2 * img[1:-1, :-2] + 4 * img[1:-1, 1:-1] - 2 * img[1:-1, 2:]
           + img[2:, :-2] - 2 * img[2:, 1:-1] + img[2:, 2:])
    return float(np.median(np.abs(lap))) * 1.4826 / 6.0


def largest_region(cells: np.ndarray) -> tuple:
    """Size and (row0, col0, row1, col1) of the largest group of touching True cells (8 neighbours)."""
    rows, cols = cells.shape
    seen = np.zeros_like(cells)
    best, best_box = 0, None
    for r0, c0 in zip(*np.nonzero(cells)):
        if seen[r0, c0]:
            continue
        seen[r0, c0] = True
        stack = [(r0, c0)]
        size, top, left, bottom, right = 0, r0, c0, r0, c0
        while stack:
            r, c = stack.pop()
            size += 1
            top, bottom, left, right = min(top, r), max(bottom, r), min(left, c), max(right, c)
            for rr in (r - 1, r, r + 1):
                for cc in (c - 1, c, c + 1):
                    if 0 <= rr < rows and 0 <= cc < cols and cells[rr, cc] and not seen[rr, cc]:
                        seen[rr, cc] = True
                        stack.append((rr, cc))
        if size > best:
            best, best_box = size, (int(top), int(left), int(bottom), int(right))
    return best, best_box


# ---------------------------------------------------------------------- object check


@dataclass(frozen=True)
class Found:
    label: str
    confidence: float
    box: tuple  # (left, top, right, bottom), fractions of the picture


class ObjectCheck:
    """Looks for a person (or other OBJECTS) in a picture with the YOLOX-tiny model via OpenCV.

    Run only when the motion detector reports movement: on the full picture and, when the movement
    is small, once more on a crop around it (far-away people are small). Optionally it also looks for
    `extra` objects (RECORD_OBJECTS, e.g. birds) that are recorded instead of alerted. Those are small
    and fast, so they get a tight square zoom on the movement and their own, lower confidence.
    The alert objects always win: a person next to a bird is an alert.
    """

    SIZE = 416  # model input, pixels

    def __init__(self, model: Path, objects: tuple = ("person",), confidence: float = 0.5,
                 extra: tuple = (), extra_confidence: float = 0.25):
        try:
            import cv2
        except ImportError as exc:
            raise ConfigError(f"the object check needs OpenCV ({exc}); run: sudo apt install python3-opencv") from None
        if not model.is_file():
            raise ConfigError(f"model file {model} not found; run ./deploy.sh again, which downloads it")
        self._cv2 = cv2
        try:
            self._net = cv2.dnn.readNetFromONNX(str(model))
        except Exception as exc:  # cv2.error
            raise ConfigError(f"cannot load the model {model}: {exc}") from None
        self.objects = tuple(objects)
        self.confidence = confidence
        self.extra = tuple(extra)
        self.extra_confidence = extra_confidence
        grids, strides = [], []
        for stride in (8, 16, 32):
            n = self.SIZE // stride
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), stride))
        self._grid = np.concatenate(grids).astype(np.float32)
        self._stride = np.concatenate(strides).astype(np.float32)

    def _run(self, image: np.ndarray) -> tuple:
        """The model's raw answer for a BGR picture, plus what is needed to map boxes back."""
        cv2, size = self._cv2, self.SIZE
        h, w = image.shape[:2]
        scale = min(size / h, size / w)
        canvas = np.full((size, size, 3), 114, np.uint8)
        canvas[: int(h * scale), : int(w * scale)] = cv2.resize(image, (int(w * scale), int(h * scale)))
        self._net.setInput(cv2.dnn.blobFromImage(canvas))
        return self._net.forward()[0], scale, w, h  # rows: cx, cy, w, h, objectness, 80 class scores

    def _best(self, run: tuple, names: tuple, confidence: float) -> Optional[Found]:
        out, scale, w, h = run
        if not names:
            return None
        ids = np.array([COCO_NAMES.index(n) for n in names])
        scores = out[:, 4:5] * out[:, 5:][:, ids]
        row, col = np.unravel_index(int(np.argmax(scores)), scores.shape)
        best = float(scores[row, col])
        if best < confidence:
            return None
        cx, cy = (out[row, :2] + self._grid[row]) * self._stride[row]
        bw, bh = np.exp(out[row, 2:4]) * self._stride[row]
        box = ((cx - bw / 2) / scale / w, (cy - bh / 2) / scale / h, (cx + bw / 2) / scale / w, (cy + bh / 2) / scale / h)
        return Found(names[col], best, tuple(float(min(1.0, max(0.0, v))) for v in box))

    def _detect(self, image: np.ndarray) -> Optional[Found]:
        """Best alert object in a BGR picture, or None."""
        return self._best(self._run(image), self.objects, self.confidence)

    @staticmethod
    def _crop(image: np.ndarray, near: tuple, factor: float, minimum: float, square: bool) -> tuple:
        """A crop around `near` (fractions), `factor` times its size, at least `minimum` of the picture."""
        h, w = image.shape[:2]
        x0, y0, x1, y1 = near
        cx, cy = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h
        if square:  # in pixels: small things are compared at their real shape
            side = max(max((x1 - x0) * w, (y1 - y0) * h) * factor, minimum * min(w, h))
            half_w = half_h = side / 2
        else:
            side = max(max(x1 - x0, y1 - y0) * factor, minimum)
            half_w, half_h = side * w / 2, side * h / 2
        left, top = int(max(0, cx - half_w)), int(max(0, cy - half_h))
        right, bottom = int(min(w, cx + half_w)), int(min(h, cy + half_h))
        return image[top:bottom, left:right], (left, top, right, bottom)

    @staticmethod
    def _uncrop(found: Optional[Found], frame: tuple, w: int, h: int) -> Optional[Found]:
        if found is None:
            return None
        left, top, right, bottom = frame
        bx0, by0, bx1, by1 = found.box
        cw, ch = right - left, bottom - top
        box = ((left + bx0 * cw) / w, (top + by0 * ch) / h, (left + bx1 * cw) / w, (top + by1 * ch) / h)
        return Found(found.label, found.confidence, box)

    def find(self, image: np.ndarray, near: Optional[tuple] = None) -> Optional[Found]:
        h, w = image.shape[:2]
        full = self._run(image)
        found = self._best(full, self.objects, self.confidence)
        if found is not None:
            return found
        small = near is not None and not (near[2] - near[0] > 0.5 and near[3] - near[1] > 0.5)
        if small:  # some room around the movement, at least a third of the picture
            crop, frame = self._crop(image, near, 2.5, 1 / 3, square=False)
            found = self._uncrop(self._best(self._run(crop), self.objects, self.confidence), frame, w, h)
            if found is not None:
                return found
        if not self.extra:
            return None
        found = self._best(full, self.extra, self.extra_confidence)
        if found is None and small:  # tight zoom: a bird is a few dozen pixels in the full picture
            crop, frame = self._crop(image, near, 2.0, 0.12, square=True)
            found = self._uncrop(self._best(self._run(crop), self.extra, self.extra_confidence), frame, w, h)
        if found is not None and near is not None and not overlaps(found.box, near):
            return None  # the model is less sure of these: it must be where the movement is
        return found

    def find_in_file(self, path: Path, near: Optional[tuple] = None) -> Optional[Found]:
        image = self._cv2.imread(str(path))
        if image is None:
            raise OSError(f"cannot read the picture {path}")
        return self.find(image, near)


def overlaps(box: tuple, near: tuple, margin: float = 0.5) -> bool:
    """Whether box touches `near` grown by `margin` of its size on every side (all fractions)."""
    x0, y0, x1, y1 = near
    gx, gy = (x1 - x0) * margin + 0.01, (y1 - y0) * margin + 0.01
    return box[0] <= x1 + gx and box[2] >= x0 - gx and box[1] <= y1 + gy and box[3] >= y0 - gy


def open_object_check(cfg: Config) -> Optional[ObjectCheck]:
    """The object check, or None when it is off. With OBJECT_CHECK=auto a missing OpenCV or model
    only gives a warning: then every movement is alerted, as before."""
    if cfg.object_check == "off":
        return None
    try:
        check = ObjectCheck(cfg.object_model, cfg.objects, cfg.object_confidence,
                            cfg.record_objects, cfg.visit_confidence)
    except ConfigError as exc:
        if cfg.object_check == "on":
            raise
        log.warning("object check not available (%s); alerting on every movement", exc)
        return None
    log.info("object check on: alerting only for %s%s (model %s)", ", ".join(cfg.objects),
             f", recording visits of {', '.join(cfg.record_objects)}" if cfg.record_objects else "",
             cfg.object_model.name)
    return check


def prune_visits(visit_dir: Path, keep_days: int) -> int:
    """Delete visit videos (and their picture and notes) older than keep_days, and stale half files."""
    now = time.time()
    removed = 0
    for path in [*visit_dir.glob("*.mp4"), *visit_dir.glob("*.jpg"), *visit_dir.glob("*.json"),
                 *visit_dir.glob("*.part")]:
        try:
            age = now - path.stat().st_mtime
            if age > keep_days * 86400 or (path.suffix == ".part" and age > 3600):
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)
    return removed


def prune(image_dir: Path, keep_days: int) -> int:
    """Delete pictures older than keep_days so the SD card does not fill up."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in [*image_dir.glob("*.jpg"), *image_dir.glob("*.gif"), *image_dir.glob("*.mp4"), *image_dir.glob("*.json"),
                 *image_dir.glob("*.mp4.part"), *image_dir.glob("rejected/*.jpg")]:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)
    return removed


class ClipRecorder:
    """Keeps the last `before` seconds as small frames, and after an alert collects `after` seconds
    more; then hands the whole sequence over (to the mailer, which turns it into a GIF)."""

    def __init__(self, before: float, after: float, fps: float):
        self.before = before
        self.after = after
        self.interval = 1.0 / fps
        self._ring: deque = deque()
        self._next = float("-inf")
        self._open: list = []  # [until, frames, done]

    def due(self, t: float) -> bool:
        return t >= self._next

    def add(self, t: float, frame: np.ndarray) -> None:
        late = t - self._next
        self._next = self._next + self.interval if 0 <= late < self.interval else t + self.interval
        self._ring.append((t, frame))
        while self._ring and self._ring[0][0] < t - self.before - 1e-6:
            self._ring.popleft()
        for clip in self._open:
            clip[1].append((t, frame))
        self._finish([c for c in self._open if t >= c[0] - 1e-6])

    def start(self, t: float, done: Callable) -> None:
        clip = [t + self.after, list(self._ring), done]
        self._open.append(clip)
        if self.after <= 0:
            self._finish([clip])

    def flush(self) -> None:
        """Hand over the clips in progress as they are (when pausing or stopping)."""
        self._finish(list(self._open))

    def _finish(self, clips: list) -> None:
        for clip in clips:
            self._open.remove(clip)
            clip[2](tuple(clip[1]))


@dataclass
class Recording:
    """A video being recorded: an alert (until the movement stops) or a visit (while the visitor is seen)."""

    kind: str           # "alert" or "visit"
    base: Path          # where it ends up, without suffix: 2026-11-10_073012 (.mp4, .jpg, .json)
    label: str
    confidence: float
    start: float        # clock times
    until: float
    event: Optional[Event] = None


class Watcher:
    """One detection step per camera frame; alerts with a picture and a video when something moves."""

    def __init__(self, cfg: Config, camera, mailer: Mailer,
                 clock: Callable = time.monotonic, now: Callable = datetime.now,
                 control: Optional["Control"] = None, slack: Optional["SlackNotifier"] = None,
                 check: Optional[ObjectCheck] = None):
        self.cfg = cfg
        self.camera = camera
        self.mailer = mailer
        self.control = control
        self.slack = slack
        self.check = check
        self.last_score = 0.0  # moving part of the picture in percent, for the meter on the web page
        self.video = getattr(camera, "video", None)  # VideoBuffer, or None: no videos
        self.visits = self.video is not None and check is not None and bool(cfg.record_objects)
        self.alert_videos = self.video is not None and cfg.alert_video
        self.rec: Optional[Recording] = None
        # birds are small: with visits on, smaller movements are looked at too (the check decides)
        min_area = min(cfg.min_area, cfg.record_min_area) if self.visits else cfg.min_area
        self.detector = MotionDetector(min_area, cfg.max_area, cfg.frames, cfg.mask)
        # the animated GIF around the alert, for the mail (CLIP_*)
        self.recorder = ClipRecorder(cfg.clip_before, cfg.clip_after, cfg.clip_fps) if cfg.clip_width else None
        self._clock = clock
        self._now = now
        self._started = clock()
        self._last_trigger: Optional[float] = None
        self._last_check: Optional[float] = None
        self._was_paused = False

    @property
    def visit(self) -> Optional[Recording]:
        """The visit being recorded, if any (for the web page)."""
        return self.rec if self.rec is not None and self.rec.kind == "visit" else None

    def step(self) -> Optional[Path]:
        if self.control is not None and self.control.paused:
            if not self._was_paused:
                log.info("detection paused")
                self._was_paused = True
                self.flush()
            self.last_score = 0.0
            return None  # frames are not even read
        if self._was_paused:
            log.info("detection resumed")
            self._was_paused = False
            self.detector.reset()  # the background is stale
        t = self._clock()
        # the video lags VIDEO_BEFORE seconds behind (it starts that early), so it is closed that much later
        if self.rec is not None and t >= self.rec.until + self.cfg.video_before:
            self._finish(t)
        grab = self.recorder is not None and self.recorder.due(t)
        if grab:
            self.camera.color_width = self.cfg.clip_width
        try:
            gray = self.camera.read_gray()
        finally:
            if grab:
                self.camera.color_width = 0
        frame = getattr(self.camera, "color", None) if grab else None
        if frame is not None:
            self.recorder.add(t, frame)
        result = self.detector.update(gray)
        self.last_score = result.area
        if result.reason == "light change":
            log.debug("light change over %.0f%% of the picture ignored", result.changed)
        alert_running = self.rec is not None and self.rec.kind == "alert"
        if alert_running and result.active >= self.detector.min_area:
            self._extend(t)  # an alert video runs until the movement has stopped
        if not result.moving:
            return None
        if t - self._started < self.cfg.warmup:
            log.debug("movement (%.1f%%) ignored during warm-up", result.area)
            return None
        if alert_running:
            return None  # being filmed already; nothing more to decide
        cooldown = self._last_trigger is not None and t - self._last_trigger < self.cfg.cooldown
        if cooldown and not self.visits:
            log.debug("movement (%.1f%%) ignored, cooldown active", result.area)
            return None
        if self.check is not None and self._last_check is not None and t - self._last_check < self.cfg.recheck:
            return None  # checked a moment ago; do not keep the CPU busy during a long light show
        stamp = self._now()
        path = self.cfg.image_dir / f"{stamp:%Y-%m-%d_%H%M%S}.jpg"
        self.camera.capture(path)
        found = None
        if self.check is not None:
            self._last_check = t
            try:
                found = self.check.find_in_file(path, result.box)
            except Exception as exc:  # a failing check must not silence the alarm
                log.error("object check failed (%s); alerting anyway", exc)
            else:
                if found is not None and found.label in self.cfg.record_objects:
                    self._seen_visitor(t, path, found)
                    return None
                if found is None:
                    if result.area >= self.cfg.min_area:  # smaller ones are only looked at for visitors
                        log.info("movement over %.1f%% of the picture, but no %s seen: no alert",
                                 result.area, " or ".join(self.check.objects))
                    else:
                        log.debug("small movement (%.1f%%), nothing recognised", result.area)
                    self._reject(path)
                    return None
        if cooldown:  # a person again, within the cooldown: no new alert
            log.debug("%s seen again (%.1f%%), cooldown active", found.label if found else "movement", result.area)
            path.unlink(missing_ok=True)
            return None
        event = Event(path, result.area, found.label if found else "", found.confidence if found else 0.0, stamp)
        self._alert(t, event)
        return path

    # ---- alerts
    @staticmethod
    def _notes(base: Path, **values) -> None:
        """Small JSON notes next to an alert or visit (what was seen, how sure, video length), for the console."""
        path = base.with_suffix(".json")
        try:
            notes = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            notes = {}
        notes.update(values)
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(notes))
            tmp.replace(path)  # a rename changes the folder's mtime: the database notices
        except OSError as exc:
            log.warning("could not save the notes %s: %s", path.name, exc)

    def _alert(self, t: float, event: Event) -> None:
        cfg = self.cfg
        self._notes(event.path.with_suffix(""), label=event.label or "motion",
                    confidence=round(event.confidence, 2), area=round(event.area, 1))
        self._last_trigger = t
        log.info("%s (%s), saved %s", event.headline().lower(), event.details(), event.path.name)
        filming = self.alert_videos and self._film_alert(t, event)
        if filming:  # the mail links to it; the video itself is watched in the console
            event = replace(event, video=event.path.with_suffix(".mp4"))
        if self.recorder is not None:  # the mail goes out when the GIF is complete, CLIP_AFTER seconds on
            self.recorder.start(t, lambda frames, e=event: self.mailer.submit(
                replace(e, clip=frames, clip_t=t, clip_fps=cfg.clip_fps)))
        else:
            self.mailer.submit(event)
        if self.slack is not None:  # Slack straight away
            self.slack.submit(slack_text(cfg, event, video=filming))
        prune(cfg.image_dir, cfg.keep_days)

    def _film_alert(self, t: float, event: Event) -> bool:
        cfg = self.cfg
        base = event.path.with_suffix("")
        rec = self.rec
        if rec is not None:  # a visit was being filmed: it becomes the alert video
            rec.base.with_suffix(".jpg").unlink(missing_ok=True)
            rec.kind, rec.base, rec.event = "alert", base, event
            rec.label, rec.confidence = event.label, event.confidence
            self._extend(t)
            log.info("the %s video continues as the alert video %s.mp4", "visit", base.name)
            return True
        try:
            self.video.open(base.with_suffix(".mp4"))
        except Exception as exc:
            log.error("could not start the alert video (%s); mailing the picture now", exc)
            return False
        self.rec = Recording("alert", base, event.label, event.confidence, t, t + cfg.video_after, event)
        return True

    def _extend(self, t: float) -> None:
        rec = self.rec
        rec.until = min(rec.start + self.cfg.video_max, max(rec.until, t + self.cfg.video_after))

    # ---- visits (RECORD_OBJECTS): a video, no alert
    def _seen_visitor(self, t: float, path: Path, found: Found) -> None:
        cfg = self.cfg
        if self.rec is not None:  # still there: keep recording, up to VIDEO_MAX
            self._extend(t)
            self.rec.confidence = max(self.rec.confidence, found.confidence)
            path.unlink(missing_ok=True)
            return
        visit_dir = cfg.image_dir / "visits"
        base = visit_dir / path.stem
        try:
            visit_dir.mkdir(exist_ok=True)
            self.video.open(base.with_suffix(".mp4"))
            path.replace(base.with_suffix(".jpg"))  # the moment it was recognised, as a thumbnail
        except Exception as exc:
            log.error("could not start the visit video: %s", exc)
            path.unlink(missing_ok=True)
            return
        self.rec = Recording("visit", base, found.label, found.confidence, t, t + cfg.video_after)
        log.info("%s visiting (%.0f%% sure): recording %s.mp4", found.label, found.confidence * 100, base.name)
        prune_visits(visit_dir, cfg.visit_keep_days)

    def _finish(self, t: float) -> None:
        rec, self.rec = self.rec, None
        # the video covers start - VIDEO_BEFORE .. until (or less, when closed early)
        seconds = round(min(t - self.cfg.video_before, rec.until) - rec.start + self.cfg.video_before)
        try:
            video = self.video.close(rec.base.with_suffix(".mp4"))
        except Exception as exc:
            log.error("could not finish the video %s.mp4: %s", rec.base.name, exc)
            video = None
        if rec.kind == "alert":
            if video is None:
                log.warning("the alert video %s.mp4 came out empty", rec.base.name)
            else:
                log.info("alert video saved: %s (about %d s, %.1f MB)", video.name, seconds, video.stat().st_size / 1e6)
                self._notes(rec.base, seconds=seconds)
            return
        if video is None:
            log.warning("the visit video %s.mp4 came out empty", rec.base.name)
            rec.base.with_suffix(".jpg").unlink(missing_ok=True)
            return
        self._notes(rec.base, label=rec.label, confidence=round(rec.confidence, 2), seconds=seconds)
        log.info("%s visit saved: %s (about %d s, %.1f MB)", rec.label, video.name, seconds, video.stat().st_size / 1e6)

    def flush(self) -> None:
        """Send alerts whose GIF is still being made, and finish a video now (pause, shutdown)."""
        if self.recorder is not None:
            self.recorder.flush()
        if self.rec is not None:
            self._finish(self._clock())

    def _reject(self, path: Path) -> None:
        """Keep the picture of a rejected movement in rejected/ (KEEP_REJECTED=yes), else delete it."""
        try:
            if self.cfg.keep_rejected:
                target = path.parent / "rejected"
                target.mkdir(exist_ok=True)
                path.replace(target / path.name)
            else:
                path.unlink()
        except OSError as exc:
            log.warning("could not move or remove %s: %s", path.name, exc)


# --------------------------------------------------------------------------- database


USER_NAME = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
SCRYPT = (2 ** 14, 8, 1)  # n, r, p: about 16 MB and 0.1 s per check on a Pi 4


def hash_password(password: str) -> str:
    """scrypt with a random salt, stored as scrypt$n$r$p$salt$hash; never the password itself."""
    n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return "$".join(["scrypt", str(n), str(r), str(p), base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        check = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                               dklen=len(base64.b64decode(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(check, base64.b64decode(digest))


def token_hash(token: str) -> str:
    """Sessions are stored hashed: a stolen database does not give working cookies."""
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    """The SQLite database (one file, IMAGE_DIR/homesec.db): the index of all events and their files, plus
    users, sessions and an audit log. The pictures and videos stay files on disk; the database follows
    the disk (sync) and keeps the history of events whose files are long gone.

    One connection, shared by the web threads and the detection loop behind a lock; WAL mode, so reads
    do not wait for writes."""

    VERSION = 1
    SCHEMA = (
        """CREATE TABLE events (
             id INTEGER PRIMARY KEY,
             folder TEXT NOT NULL,              -- alerts, visits, rejected
             stem TEXT NOT NULL,                -- 2026-11-11_014202
             label TEXT NOT NULL DEFAULT '',    -- person, bird, motion, ...
             confidence REAL,
             area REAL,
             seconds INTEGER,                   -- video length
             started_at TEXT NOT NULL,          -- local time, 2026-11-11 01:42:02
             created_at TEXT NOT NULL,
             deleted_at TEXT,                   -- set when all its files are gone; the row stays as history
             UNIQUE (folder, stem))""",
        "CREATE INDEX events_started ON events (started_at)",
        """CREATE TABLE files (
             id INTEGER PRIMARY KEY,
             event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
             name TEXT NOT NULL,
             kind TEXT NOT NULL,                -- jpg, gif, mp4
             bytes INTEGER NOT NULL,
             created_at TEXT NOT NULL,
             deleted_at TEXT,
             UNIQUE (event_id, name))""",
        """CREATE TABLE users (
             id INTEGER PRIMARY KEY,
             name TEXT NOT NULL UNIQUE COLLATE NOCASE,
             password TEXT NOT NULL,            -- scrypt hash, never the password
             role TEXT NOT NULL DEFAULT 'admin',
             created_at TEXT NOT NULL,
             last_login TEXT)""",
        """CREATE TABLE sessions (
             token TEXT PRIMARY KEY,            -- sha256 of the cookie value
             user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
             created_at TEXT NOT NULL,
             expires_at TEXT NOT NULL,
             last_seen TEXT NOT NULL,
             ip TEXT,
             agent TEXT)""",
        """CREATE TABLE audit (
             id INTEGER PRIMARY KEY,
             at TEXT NOT NULL,
             user TEXT,
             ip TEXT,
             action TEXT NOT NULL,              -- login, login_failed, logout, delete, cleanup, detection, ...
             detail TEXT)""",
    )

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        try:
            os.chmod(path, 0o600)  # password hashes and sessions: for the service user only
        except OSError:
            pass

    @staticmethod
    def now() -> str:
        return f"{datetime.now():%Y-%m-%d %H:%M:%S}"

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > self.VERSION:
            raise ConfigError(f"{self.path} is from a newer homeSec (database version {version}); update homeSec")
        if version < 1:
            with self.tx() as db:
                for statement in self.SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={self.VERSION}")
            log.info("database created: %s", self.path)

    @contextmanager
    def tx(self):
        """A transaction: everything inside is saved together, or not at all."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def rows(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- events and files
    def sync(self, scanned: list, folders: tuple = ()) -> tuple:
        """Make the database match the disk. `scanned` is what scan_disk() found (in `folders`, or all).
        New events and files are added, changed ones updated, missing ones marked deleted.
        Returns (events added, events marked deleted)."""
        now = self.now()
        added = gone = 0
        with self.tx() as db:
            seen = set()
            for e in scanned:
                seen.add((e["folder"], e["stem"]))
                row = db.execute("SELECT id, deleted_at FROM events WHERE folder = ? AND stem = ?",
                                 (e["folder"], e["stem"])).fetchone()
                if row is None:
                    cur = db.execute(
                        "INSERT INTO events (folder, stem, label, confidence, area, seconds, started_at, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (e["folder"], e["stem"], e["label"], e["confidence"], e.get("area"), e["seconds"],
                         f"{e['date']} {e['time']}", now))
                    event_id = cur.lastrowid
                    added += 1
                else:
                    event_id = row["id"]
                    db.execute("UPDATE events SET label = ?, confidence = ?, area = COALESCE(?, area), seconds = ?, "
                               "deleted_at = NULL WHERE id = ?",
                               (e["label"], e["confidence"], e.get("area"), e["seconds"], event_id))
                names = []
                for f in e["files"]:
                    names.append(f["name"])
                    db.execute("INSERT INTO files (event_id, name, kind, bytes, created_at) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT (event_id, name) DO UPDATE SET bytes = excluded.bytes, deleted_at = NULL",
                               (event_id, f["name"], f["kind"], f["bytes"], now))
                marks = ",".join("?" * len(names))
                db.execute(f"UPDATE files SET deleted_at = ? WHERE event_id = ? AND deleted_at IS NULL "
                           f"AND name NOT IN ({marks})", (now, event_id, *names))
            where = f"AND folder IN ({','.join('?' * len(folders))})" if folders else ""
            for row in db.execute(f"SELECT id, folder, stem FROM events WHERE deleted_at IS NULL {where}",
                                  tuple(folders)).fetchall():
                if (row["folder"], row["stem"]) not in seen:
                    db.execute("UPDATE events SET deleted_at = ? WHERE id = ?", (now, row["id"]))
                    db.execute("UPDATE files SET deleted_at = ? WHERE event_id = ? AND deleted_at IS NULL",
                               (now, row["id"]))
                    gone += 1
        return added, gone

    def catalog(self) -> list:
        """The events whose files are still there, newest first, with their files."""
        events = self.rows("SELECT * FROM events WHERE deleted_at IS NULL ORDER BY started_at DESC, id DESC")
        files: dict = {}
        for f in self.rows("SELECT event_id, name, kind, bytes FROM files WHERE deleted_at IS NULL"):
            files.setdefault(f["event_id"], []).append(f)
        out = []
        order = {"mp4": 0, "jpg": 1, "gif": 2}
        for e in events:
            fs = sorted(files.get(e["id"], []), key=lambda f: order.get(f["kind"], 9))
            if not fs:
                continue
            out.append({**e, "files": fs})
        return out

    def history(self) -> dict:
        """All events ever seen, also those whose files were deleted: the database's memory."""
        rows = self.rows("SELECT folder, label, COUNT(*) AS n, MIN(started_at) AS first FROM events GROUP BY folder, label")
        first = min((r["first"] for r in rows), default=None)
        count = lambda pred: sum(r["n"] for r in rows if pred(r))  # noqa: E731
        return {"since": first[:10] if first else None,
                "people": count(lambda r: r["label"] == "person"),
                "visits": count(lambda r: r["folder"] == "visits"),
                "rejected": count(lambda r: r["folder"] == "rejected"),
                "events": count(lambda r: True)}

    GROUP = ("CASE WHEN e.folder = 'visits' THEN 'birds' WHEN e.folder = 'rejected' THEN 'rejected' "
             "WHEN e.label = 'person' THEN 'people' ELSE 'other' END")
    GROUPS = ("people", "birds", "other", "rejected")

    def event_stats(self, days: int = 30) -> dict:
        """Every event ever seen with the pictures, GIFs and videos it made, also the deleted ones."""
        groups = {g: {"key": g, "events": 0, "kept": 0, "seconds": 0, "videos": 0, "confidence": None,
                      "first": None, "last": None,
                      "files": {k: {"made": 0, "kept": 0, "bytes": 0, "kept_bytes": 0} for k in ("jpg", "gif", "mp4")}}
                  for g in self.GROUPS}
        for r in self.rows(f"SELECT {self.GROUP} AS g, COUNT(*) AS n, SUM(e.deleted_at IS NULL) AS kept, "
                           "SUM(COALESCE(e.seconds, 0)) AS secs, SUM(e.seconds IS NOT NULL) AS videos, "
                           "AVG(e.confidence) AS conf, MIN(e.started_at) AS first, MAX(e.started_at) AS last "
                           "FROM events e GROUP BY g"):
            groups[r["g"]].update(events=r["n"], kept=r["kept"], seconds=r["secs"], videos=r["videos"],
                                  confidence=round(r["conf"], 2) if r["conf"] is not None else None,
                                  first=r["first"], last=r["last"])
        for r in self.rows(f"SELECT {self.GROUP} AS g, f.kind, COUNT(*) AS made, SUM(f.deleted_at IS NULL) AS kept, "
                           "SUM(f.bytes) AS bytes, SUM(CASE WHEN f.deleted_at IS NULL THEN f.bytes ELSE 0 END) AS kept_bytes "
                           "FROM files f JOIN events e ON e.id = f.event_id GROUP BY g, f.kind"):
            if r["kind"] in groups[r["g"]]["files"]:
                groups[r["g"]]["files"][r["kind"]] = {k: r[k] for k in ("made", "kept", "bytes", "kept_bytes")}
        today = datetime.now().date()
        per_day = {(today - timedelta(days=i)).isoformat(): dict.fromkeys(self.GROUPS, 0) for i in range(days)}
        for r in self.rows(f"SELECT substr(e.started_at, 1, 10) AS d, {self.GROUP} AS g, COUNT(*) AS n FROM events e "
                           "WHERE e.started_at >= ? GROUP BY d, g", (min(per_day),)):
            if r["d"] in per_day:
                per_day[r["d"]][r["g"]] = r["n"]
        per_hour = [dict.fromkeys(self.GROUPS, 0) for _ in range(24)]
        for r in self.rows(f"SELECT CAST(substr(e.started_at, 12, 2) AS INTEGER) AS h, {self.GROUP} AS g, COUNT(*) AS n "
                           "FROM events e GROUP BY h, g"):
            if 0 <= r["h"] < 24:
                per_hour[r["h"]][r["g"]] = r["n"]
        return {"groups": [groups[g] for g in self.GROUPS],
                "days": [{"date": d, **per_day[d]} for d in sorted(per_day)],
                "hours": [{"hour": h, **c} for h, c in enumerate(per_hour)]}

    # ---- users and sessions
    def add_user(self, name: str, password: str, role: str = "admin") -> None:
        if not USER_NAME.match(name):
            raise ConfigError("a user name is 1-32 letters, digits, dots, dashes or underscores")
        if role not in ("admin", "viewer"):
            raise ConfigError("role must be admin or viewer")
        try:
            with self.tx() as db:
                db.execute("INSERT INTO users (name, password, role, created_at) VALUES (?, ?, ?, ?)",
                           (name, hash_password(password), role, self.now()))
        except sqlite3.IntegrityError:
            raise ConfigError(f"user {name} already exists (homesec passwd {name} changes the password)") from None

    def set_password(self, name: str, password: str) -> bool:
        with self.tx() as db:
            cur = db.execute("UPDATE users SET password = ? WHERE name = ?", (hash_password(password), name))
            if cur.rowcount:  # a new password logs out every session of that user
                db.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE name = ?)", (name,))
            return bool(cur.rowcount)

    def delete_user(self, name: str) -> bool:
        with self.tx() as db:
            return bool(db.execute("DELETE FROM users WHERE name = ?", (name,)).rowcount)

    def users(self) -> list:
        return self.rows("SELECT id, name, role, created_at, last_login FROM users ORDER BY name")

    def check_password(self, name: str, password: str) -> Optional[dict]:
        rows = self.rows("SELECT * FROM users WHERE name = ?", (name,))
        if not rows:
            hash_password(password)  # same work as for a real user: the answer time does not tell who exists
            return None
        return rows[0] if verify_password(password, rows[0]["password"]) else None

    def new_session(self, user_id: int, hours: int, ip: str = "", agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        with self.tx() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (f"{now:%Y-%m-%d %H:%M:%S}",))
            db.execute("INSERT INTO sessions (token, user_id, created_at, expires_at, last_seen, ip, agent) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (token_hash(token), user_id, f"{now:%Y-%m-%d %H:%M:%S}",
                        f"{now + timedelta(hours=hours):%Y-%m-%d %H:%M:%S}", f"{now:%Y-%m-%d %H:%M:%S}",
                        ip or None, (agent or "")[:200] or None))
            db.execute("UPDATE users SET last_login = ? WHERE id = ?", (f"{now:%Y-%m-%d %H:%M:%S}", user_id))
        return token

    def session_user(self, token: str) -> Optional[dict]:
        """The user of a session cookie, or None when it is unknown or expired."""
        if not token or len(token) > 100:
            return None
        rows = self.rows("SELECT u.id, u.name, u.role, s.expires_at, s.last_seen FROM sessions s "
                         "JOIN users u ON u.id = s.user_id WHERE s.token = ?", (token_hash(token),))
        if not rows or rows[0]["expires_at"] < self.now():
            return None
        if rows[0]["last_seen"][:16] != self.now()[:16]:  # at most one write a minute per session
            with self.tx() as db:
                db.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (self.now(), token_hash(token)))
        return rows[0]

    def end_session(self, token: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM sessions WHERE token = ?", (token_hash(token),))

    # ---- audit
    def audit(self, action: str, user: str = "", ip: str = "", detail: str = "") -> None:
        try:
            with self.tx() as db:
                db.execute("INSERT INTO audit (at, user, ip, action, detail) VALUES (?, ?, ?, ?, ?)",
                           (self.now(), user or None, ip or None, action, detail or None))
        except sqlite3.Error as exc:  # the audit log must never break what it records
            log.warning("could not write the audit log: %s", exc)


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
MEDIA_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}\.(jpg|gif|mp4)$")
VISIT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}\.mp4$")
VISIT_MEDIA = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}\.(mp4|jpg)$")
STEM = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
STEM_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})\.(jpg|gif|mp4|json)$")
FOLDERS = ("alerts", "visits", "rejected")  # alerts live in IMAGE_DIR itself
FOLDER_URL = {"alerts": "/pictures/", "visits": "/videos/", "rejected": "/rejected/"}
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

    def __init__(self, cfg: Config, control: Control, backend: str = "", names_ttl: float = 2.0,
                 objects: tuple = (), store: Optional[Store] = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_file)
        self._failures: dict = {}        # ip or user name -> times of failed logins (lockout)
        self._dir_marks: dict = {}       # folder -> mtime of the folder at the last sync
        self._sync_lock = threading.Lock()
        self._catalog: Optional[list] = None
        self.control = control
        self.backend = backend
        self.objects = objects  # what the object check looks for; empty when it is off
        self.record: tuple = ()  # what is recorded as a visit; empty when visits are off
        self.visiting = ""       # label of the visit being recorded right now
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
        today = f"{datetime.now():%Y-%m-%d}"
        alerts = [e for e in self.catalog() if e["folder"] == "alerts"]
        last = alerts[0] if alerts else None
        return {
            "detection": "paused" if self.control.paused else "on",
            "score": round(self.score, 2),
            "threshold": cfg.min_area,
            "check": {"enabled": bool(self.objects), "objects": list(self.objects)},
            "visits": {"enabled": bool(self.record), "objects": list(self.record), "recording": self.visiting,
                       "today": self._visits_today(), "last": self._last_visit()},
            "events_today": sum(e["date"] == today for e in alerts),
            "last_motion": ({"name": f"{last['stem']}.jpg", "url": f"/pictures/{last['stem']}.jpg", "date": last["date"],
                             "time": last["time"], "ts": last["ts"]} if last else None),
            "camera": self.backend,
            "resolution": f"{cfg.resolution[0]}x{cfg.resolution[1]}",
            "fps": cfg.fps,
            "viewers": self.hub.viewers,
            "uptime": int(time.time() - self.started),
            "disk": self.disk(),
            "now": int(time.time()),
            "host": socket.gethostname(),
            "slack": {"enabled": bool(cfg.slack_webhook), "channel": cfg.slack_channel},
        }

    DISK_TTL = 30.0  # seconds; counting the files every status poll would be wasteful

    def disk(self) -> dict:
        """Space on the disk with the pictures and videos, and how much of it is homeSec's own files."""
        now = time.monotonic()
        if now - getattr(self, "_disk_at", float("-inf")) < self.DISK_TTL:
            return self._disk
        try:
            usage = shutil.disk_usage(self.cfg.image_dir)
        except OSError:
            self._disk, self._disk_at = None, now
            return None
        ours = 0
        kinds = {kind: {"count": 0, "bytes": 0, "folders": {}} for kind in ("jpg", "gif", "mp4")}
        folders = [self.cfg.image_dir]
        while folders:
            folder = folders.pop()
            where = "alerts" if folder == self.cfg.image_dir else os.path.basename(folder)  # visits, rejected
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                folders.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                size = entry.stat(follow_symlinks=False).st_size
                                ours += size
                                kind = kinds.get(entry.name.rsplit(".", 1)[-1].lower())
                                if kind is not None:
                                    kind["count"] += 1
                                    kind["bytes"] += size
                                    kind["folders"][where] = kind["folders"].get(where, 0) + 1
                        except OSError:
                            pass
            except OSError:
                pass
        free_pct = 100.0 * usage.free / usage.total if usage.total else 0.0
        level = "low" if usage.free < 1e9 or free_pct < 5 else "warn" if usage.free < 3e9 or free_pct < 10 else "ok"
        if level != "ok" and getattr(self, "_disk_level", "ok") != level:
            log.warning("disk space %s: %.1f GB free of %.1f GB (homeSec files: %.1f GB); delete old pictures and "
                        "videos in the console, or lower KEEP_DAYS / VISIT_KEEP_DAYS", "very low" if level == "low"
                        else "getting low", usage.free / 1e9, usage.total / 1e9, ours / 1e9)
        self._disk_level = level
        self._disk = {"total": usage.total, "free": usage.free, "homesec": ours, "level": level, "files": kinds}
        self._disk_at = now
        return self._disk

    def forget_disk(self) -> None:
        """Count again on the next status (after a delete)."""
        self._disk_at = float("-inf")

    @property
    def visit_dir(self) -> Path:
        return self.cfg.image_dir / "visits"

    def visits(self, limit: int = 60) -> list:
        """The visit videos for the timeline, newest first."""
        visits = []
        for e in self.catalog():
            if e["folder"] != "visits":
                continue
            kinds = {f["kind"]: f for f in e["files"]}
            if "mp4" not in kinds:
                continue
            visit = {"name": kinds["mp4"]["name"], "url": kinds["mp4"]["url"], "date": e["date"], "time": e["time"],
                     "ts": e["ts"], "label": "" if e["label"] == "visit" else e["label"], "seconds": e["seconds"],
                     "confidence": e["confidence"], "mb": round(kinds["mp4"]["bytes"] / 1e6, 1)}
            if "jpg" in kinds:
                visit["thumb"] = kinds["jpg"]["url"]
            visits.append(visit)
            if len(visits) >= limit:
                break
        return visits

    def delete_visit(self, name: str) -> bool:
        if not VISIT_NAME.match(name):
            return False
        path = self.visit_dir / name
        if not path.is_file():
            return False
        path.unlink()
        for suffix in (".jpg", ".json"):
            path.with_suffix(suffix).unlink(missing_ok=True)
        self.forget_disk()
        self.ensure_synced(force=True)
        return True

    def delete_event(self, name: str) -> bool:
        """Delete a saved picture and its clip. False when there is no such picture."""
        if not EVENT_NAME.match(name):
            return False
        path = self.cfg.image_dir / name
        if not path.is_file():
            return False
        path.unlink()
        path.with_suffix(".gif").unlink(missing_ok=True)
        path.with_suffix(".mp4").unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
        self.forget_disk()
        self.ensure_synced(force=True)
        return True

    def _visits_today(self) -> int:
        today = f"{datetime.now():%Y-%m-%d}"
        return sum(e["folder"] == "visits" and e["date"] == today for e in self.catalog())

    def _last_visit(self) -> str:
        return next((f"{e['stem']}.mp4" for e in self.catalog() if e["folder"] == "visits"), "")

    # ---- the Files and Stats tabs
    def folder_path(self, folder: str) -> Path:
        return self.cfg.image_dir if folder == "alerts" else self.cfg.image_dir / folder

    def keep_days(self, folder: str) -> int:
        return self.cfg.visit_keep_days if folder == "visits" else self.cfg.keep_days

    # ---- login
    LOCK_AFTER = 5          # failed logins ...
    LOCK_WINDOW = 15 * 60   # ... within this many seconds lock the ip address and the user name this long

    def locked_for(self, *keys: str) -> int:
        """Seconds until a login from this ip / for this name may be tried again (0 = now)."""
        now = time.monotonic()
        wait = 0
        for key in keys:
            recent = [t for t in self._failures.get(key, []) if now - t < self.LOCK_WINDOW]
            self._failures[key] = recent
            if len(recent) >= self.LOCK_AFTER:
                wait = max(wait, int(self.LOCK_WINDOW - (now - recent[-self.LOCK_AFTER])) + 1)
        return wait

    def login_failed(self, *keys: str) -> None:
        now = time.monotonic()
        for key in keys:
            self._failures.setdefault(key, []).append(now)

    def login_ok(self, *keys: str) -> None:
        for key in keys:
            self._failures.pop(key, None)

    def ensure_synced(self, force: bool = False) -> None:
        """Bring the database up to date with the folders that changed since the last look. A folder's
        mtime changes whenever a file in it is created, renamed or deleted (homeSec writes its notes by
        renaming too), so this catches new alerts, finished videos, GIFs, prune and deletes."""
        with self._sync_lock:
            changed = []
            for folder in FOLDERS:
                try:
                    mark = self.folder_path(folder).stat().st_mtime_ns
                except OSError:
                    mark = None
                if force or self._dir_marks.get(folder, -1) != mark:
                    changed.append((folder, mark))
            if not changed:
                return
            folders = tuple(f for f, _ in changed)
            added, gone = self.store.sync(self.scan_disk(folders), folders)
            if added or gone:
                log.debug("database synced: %d new, %d gone (%s)", added, gone, ", ".join(folders))
            for folder, mark in changed:
                self._dir_marks[folder] = mark
            self._catalog = None

    def catalog(self) -> list:
        """Every saved event, with its files, from the database: what the timeline, Files and Stats show."""
        self.ensure_synced()
        if self._catalog is None:
            events = []
            for e in self.store.catalog():
                stamp = datetime.strptime(e["started_at"], "%Y-%m-%d %H:%M:%S")
                folder = e["folder"]
                events.append({
                    "id": f"{folder}/{e['stem']}", "folder": folder, "stem": e["stem"],
                    "date": f"{stamp:%Y-%m-%d}", "time": f"{stamp:%H:%M:%S}", "ts": int(stamp.timestamp()),
                    "label": e["label"], "confidence": e["confidence"], "seconds": e["seconds"],
                    "keep_until": f"{stamp + timedelta(days=self.keep_days(folder)):%Y-%m-%d}",
                    "bytes": sum(f["bytes"] for f in e["files"]),
                    "files": [{"name": f["name"], "kind": f["kind"], "bytes": f["bytes"],
                               "url": FOLDER_URL[folder] + f["name"]} for f in e["files"]],
                })
            self._catalog = events
        return self._catalog

    def scan_disk(self, folders: tuple = FOLDERS) -> list:
        """Every saved event in the folders on disk, with its files and notes, for the database.
        One event = the files sharing a name (2026-11-11_014202 .jpg .gif .mp4 .json)."""
        events = []
        for folder in folders:
            groups: dict = {}
            try:
                entries = list(os.scandir(self.folder_path(folder)))
            except OSError:
                continue
            for entry in entries:
                match = STEM_FILE.match(entry.name)
                if not match or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                groups.setdefault(match.group(1), []).append((entry.name, match.group(2), size))
            for stem, files in groups.items():
                stamp = _event_time(stem + ".jpg")
                if stamp is None:
                    continue
                notes = {}
                media = [f for f in files if f[1] != "json"]
                if not media:
                    continue
                if any(f[1] == "json" for f in files):
                    try:
                        notes = json.loads((self.folder_path(folder) / f"{stem}.json").read_text())
                    except (OSError, ValueError):
                        notes = {}
                order = {"mp4": 0, "jpg": 1, "gif": 2}
                media.sort(key=lambda f: order[f[1]])
                label = str(notes.get("label", "")) or {"visits": "visit", "rejected": "nothing"}.get(folder, "motion")
                events.append({
                    "id": f"{folder}/{stem}", "folder": folder, "stem": stem,
                    "date": f"{stamp:%Y-%m-%d}", "time": f"{stamp:%H:%M:%S}", "ts": int(stamp.timestamp()),
                    "label": label, "confidence": notes.get("confidence"), "seconds": notes.get("seconds"),
                    "area": notes.get("area"),
                    "bytes": sum(f[2] for f in files),
                    "files": [{"name": n, "kind": k, "bytes": b, "url": FOLDER_URL[folder] + n} for n, k, b in media],
                })
        events.sort(key=lambda e: e["ts"], reverse=True)
        return events

    def files(self) -> dict:
        events = self.catalog()
        folders = {f: {"events": 0, "bytes": 0} for f in FOLDERS}
        for e in events:
            folders[e["folder"]]["events"] += 1
            folders[e["folder"]]["bytes"] += e["bytes"]
        return {"folders": folders, "events": events, "path": str(self.cfg.image_dir)}

    def delete_files(self, folder: str, stem: str) -> int:
        """Delete every file of one event in a folder. Returns how many files went."""
        if folder not in FOLDERS or not STEM.match(stem):
            return 0
        removed = 0
        for suffix in ("jpg", "gif", "mp4", "json"):
            path = self.folder_path(folder) / f"{stem}.{suffix}"
            if path.is_file():
                path.unlink()
                removed += 1
        self.forget_disk()
        self.ensure_synced(force=True)
        return removed

    def stats(self, days: int = 14) -> dict:
        """Space per day and per kind, what the clean-up actions would remove, and the biggest files."""
        events = self.catalog()
        today = datetime.now().date()
        per_day = {(today - timedelta(days=i)).isoformat(): {"jpg": 0, "gif": 0, "mp4": 0} for i in range(days)}
        for e in events:
            if e["date"] in per_day:
                for f in e["files"]:
                    per_day[e["date"]][f["kind"]] += f["bytes"]
        cutoff = (today - timedelta(days=2)).isoformat()
        rejected = [e for e in events if e["folder"] == "rejected"]
        old_gifs = [f for e in events if e["folder"] == "alerts" and e["date"] < cutoff
                    for f in e["files"] if f["kind"] == "gif"]
        videos = sorted(((f, e) for e in events for f in e["files"] if f["kind"] == "mp4"),
                        key=lambda fe: fe[0]["bytes"], reverse=True)
        week = [per_day[d] for d in sorted(per_day)[-7:]]
        per_week_day = sum(sum(d.values()) for d in week) / 7
        disk = self.disk()
        return {
            "disk": disk,
            "days": [{"date": d, **per_day[d]} for d in sorted(per_day)],
            "days_left": round(disk["free"] / per_week_day, 1) if disk and per_week_day > 0 else None,
            "keep": {"alerts": self.cfg.keep_days, "visits": self.cfg.visit_keep_days, "rejected": self.cfg.keep_days},
            "cleanup": {
                "rejected": {"count": len(rejected), "bytes": sum(e["bytes"] for e in rejected)},
                "old_gifs": {"count": len(old_gifs), "bytes": sum(f["bytes"] for f in old_gifs), "before": cutoff},
            },
            "biggest": [{"id": e["id"], "name": f["name"], "bytes": f["bytes"], "date": e["date"], "time": e["time"],
                         "label": e["label"], "url": f["url"]} for f, e in videos[:8]],
            "totals": {"events": len(events), "people": sum(e["label"] == "person" for e in events),
                       "visits": sum(e["folder"] == "visits" for e in events)},
            "history": self.store.history(),
            "events": self.store.event_stats(),
        }

    def cleanup(self, action: str, before: str = "") -> dict:
        """The Stats tab's clean-up buttons. Returns how many files and bytes went."""
        events = self.catalog()
        gone = {"files": 0, "bytes": 0}

        def drop(folder: str, name: str, size: int) -> None:
            try:
                (self.folder_path(folder) / name).unlink()
                gone["files"] += 1
                gone["bytes"] += size
            except FileNotFoundError:
                pass

        if action == "rejected":
            for e in events:
                if e["folder"] == "rejected":
                    for f in e["files"]:
                        drop("rejected", f["name"], f["bytes"])
        elif action == "old_gifs":
            cutoff = (datetime.now().date() - timedelta(days=2)).isoformat()
            for e in events:
                if e["folder"] == "alerts" and e["date"] < cutoff:
                    for f in e["files"]:
                        if f["kind"] == "gif":
                            drop("alerts", f["name"], f["bytes"])
        elif action == "before":
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", before):
                raise ValueError("before must look like 2026-11-04")
            for e in events:
                if e["date"] < before:
                    gone["files"] += self.delete_files(e["folder"], e["stem"])
                    gone["bytes"] += e["bytes"]
        else:
            raise ValueError(f"unknown clean-up {action!r}")
        self.forget_disk()
        self.ensure_synced(force=True)
        return gone

    def events(self, limit: int = 60) -> list:
        """The alerts for the timeline, newest first."""
        events = []
        for e in self.catalog():
            if e["folder"] != "alerts":
                continue
            kinds = {f["kind"]: f for f in e["files"]}
            if "jpg" not in kinds:
                continue
            event = {"name": kinds["jpg"]["name"], "url": kinds["jpg"]["url"], "date": e["date"], "time": e["time"],
                     "ts": e["ts"], "label": "" if e["label"] == "motion" else e["label"],
                     "confidence": e["confidence"], "seconds": e["seconds"]}
            if "gif" in kinds:
                event["clip"] = kinds["gif"]["url"]
            if "mp4" in kinds:
                event["video"] = kinds["mp4"]["url"]
            events.append(event)
            if len(events) >= limit:
                break
        return events


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
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
                                   "script-src 'self'; "
                                   "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",  # no-referrer makes Chrome send "Origin: null" on form posts
    }

    login_page = cfg.static_dir / "login.html"
    public = {"/login", "/style.css", "/favicon.svg", "/login.css"}

    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # seconds: a stalled or half-open connection must not hold a thread for ever
        user: Optional[dict] = None

        @property
        def secure(self) -> bool:
            return isinstance(self.connection, ssl.SSLSocket)

        def _ip(self) -> str:
            return self.client_address[0]

        def _audit(self, action: str, detail: str = "", user: str = "") -> None:
            app.store.audit(action, user or (self.user or {}).get("name", ""), self._ip(), detail)

        def _redirect(self, where: str, cookie: str = "") -> None:
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            for key, value in security.items():
                self.send_header(key, value)
            self.end_headers()

        def _https_only(self) -> bool:
            """With https on, a plain http request gets a redirect (GET) or is refused."""
            if not getattr(self.server, "tls", None) or self.secure:
                return True
            if self.command == "GET":
                self.send_response(301)
                self.send_header("Location", f"https://{self.headers.get('Host', '')}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(403, b"use https")
            return False

        def _cookie(self, name: str) -> str:
            for part in self.headers.get("Cookie", "").split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    return value
            return ""

        def _session_cookie(self, token: str, max_age: Optional[int]) -> str:
            parts = [f"homesec={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
            if self.secure:
                parts.append("Secure")
            if max_age is not None:
                parts.append(f"Max-Age={max_age}")
            return "; ".join(parts)

        def _signed_in(self) -> bool:
            """Sets self.user. Without LOGIN everyone is an admin."""
            if not cfg.login:
                self.user = {"name": "", "role": "admin"}
                return True
            self.user = app.store.session_user(self._cookie("homesec"))
            return self.user is not None

        def _login_page(self, message: str = "") -> None:
            try:
                page = login_page.read_text(encoding="utf-8")
            except OSError:
                return self._send(500, b"login page not found")
            if not app.store.users():
                message = message or ("There are no logins yet. On the Pi, create one with: "
                                      "sudo -u homesec homesec adduser YOURNAME")
            note = f'<p class="login-msg" role="alert">{html_escape(message)}</p>' if message else ""
            nxt = parse_qs(urlparse(self.path).query).get("next", ["/"])[0]
            page = page.replace("<!--message-->", note).replace("__NEXT__", html_escape(safe_next(nxt)))
            self._send(200, page.encode(), "text/html; charset=utf-8")

        def _need_login(self, path: str) -> None:
            if path in ("/", "/index.html"):
                return self._redirect("/login")
            if path.startswith(("/pictures/", "/videos/", "/rejected/")):
                return self._redirect(f"/login?next={path}")
            self._send(401, b"please log in", extra={"WWW-Authenticate": 'FormBased realm="homeSec"'})

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
                  cache: str = "no-store", extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
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
            if not self._host_ok() or not self._https_only():
                return
            url = urlparse(self.path)
            path = url.path
            if path in public:
                if path == "/login":
                    if self._signed_in() and cfg.login:
                        return self._redirect("/")
                    return self._login_page({"wrong": "Wrong name or password.", "locked": "Too many tries. Wait a few minutes.",
                                             "out": "You are logged out."}.get(parse_qs(url.query).get("e", [""])[0], ""))
                return self._static(path.lstrip("/"))
            if not self._signed_in():
                return self._need_login(path)
            if path == "/api/status":
                status = app.status()
                status["user"] = {"name": self.user["name"], "role": self.user["role"], "login": cfg.login}
                return self._json(status)
            if path == "/api/events":
                return self._json({"events": app.events()})
            if path == "/stream.mjpg":
                return self._stream()
            if path == "/api/visits":
                return self._json({"visits": app.visits()})
            if path == "/api/files":
                return self._json(app.files())
            if path == "/api/stats":
                return self._json(app.stats())
            if path.startswith("/rejected/"):
                name = path[len("/rejected/"):]
                if not EVENT_NAME.match(name):
                    return self._send(404, b"not found")
                return self._file(cfg.image_dir / "rejected" / name, f"homesec_rejected_{name}",
                                  "download" in parse_qs(url.query))
            if path.startswith("/videos/"):
                return self._video(path[len("/videos/"):], "download" in parse_qs(url.query))
            if path.startswith("/pictures/"):
                return self._picture(path[len("/pictures/"):], "download" in parse_qs(url.query))
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

        def _picture(self, name: str, download: bool = False) -> None:
            if not MEDIA_NAME.match(name):  # no slashes, one dot: nothing outside the picture folder
                return self._send(404, b"not found")
            self._file(cfg.image_dir / name, f"homesec_{name}", download)

        def _video(self, name: str, download: bool) -> None:
            """Visit videos and their pictures."""
            if not VISIT_MEDIA.match(name):
                return self._send(404, b"not found")
            self._file(app.visit_dir / name, f"homesec_visit_{name}", download)

        def _file(self, path: Path, download_name: str, download: bool) -> None:
            """A picture or video. Supports byte ranges: browsers (Safari above all) need them to play
            and seek in a video."""
            name = path.name
            types = {".mp4": "video/mp4", ".gif": "image/gif"}
            try:
                size = path.stat().st_size
                handle = path.open("rb")
            except OSError:
                return self._send(404, b"not found")
            with handle:
                start, end = 0, size - 1
                ranged = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
                if ranged and (ranged.group(1) or ranged.group(2)):
                    if ranged.group(1):
                        start = int(ranged.group(1))
                        end = min(int(ranged.group(2)), size - 1) if ranged.group(2) else size - 1
                    else:  # the last N bytes
                        start = max(0, size - int(ranged.group(2)))
                    if start > end or start >= size:
                        return self._send(416, b"range not satisfiable", extra={"Content-Range": f"bytes */{size}"})
                self.send_response(206 if ranged and (ranged.group(1) or ranged.group(2)) else 200)
                self.send_header("Content-Type", types.get(path.suffix, "image/jpeg"))
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                if ranged and (ranged.group(1) or ranged.group(2)):
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if download:
                    self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
                self.send_header("Cache-Control", "private, max-age=3600")
                for key, value in security.items():
                    self.send_header(key, value)
                self.end_headers()
                handle.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = handle.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except OSError:  # the viewer stopped watching
                    pass

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

        def _same_site(self) -> bool:
            site = self.headers.get("Sec-Fetch-Site")
            if site:
                return site in ("same-origin", "none")
            origin = self.headers.get("Origin")
            return not origin or (origin != "null" and urlparse(origin).netloc == self.headers.get("Host"))

        def do_POST(self):
            if not self._host_ok() or not self._https_only():
                return
            if not self._same_site():  # a page on another site must not log in, delete or flip the switch
                return self._send(403, b"forbidden")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            path = urlparse(self.path).path
            if path == "/api/login":
                return self._login(length)
            if path not in ("/api/detection", "/api/events/delete", "/api/visits/delete", "/api/files/delete",
                            "/api/cleanup", "/api/logout"):
                return self._send(404, b"not found")
            if not 0 <= length <= 100:
                return self._send(400, b"bad request")
            form = parse_qs(self.rfile.read(length).decode("ascii", "replace"))
            if not self._signed_in():
                return self._send(401, b"please log in")
            if path == "/api/logout":
                token = self._cookie("homesec")
                if token:
                    app.store.end_session(token)
                self._audit("logout")
                return self._redirect("/login?e=out", self._session_cookie("", 0))
            if self.user["role"] != "admin":
                return self._send(403, b"this login can only watch")
            if path == "/api/files/delete":
                folder, stem = form.get("folder", [""])[0], form.get("stem", [""])[0]
                if folder not in FOLDERS or not STEM.match(stem):
                    return self._send(400, b"bad request")
                try:
                    removed = app.delete_files(folder, stem)
                except OSError as exc:
                    log.error("could not delete %s/%s: %s", folder, stem, exc)
                    return self._send(500, b"could not delete")
                if not removed:
                    return self._send(404, b"not found")
                log.info("%s/%s deleted from the web page (%d files, %s)", folder, stem, removed, self.client_address[0])
                self._audit("delete", f"{folder}/{stem}")
                return self._json({"deleted": f"{folder}/{stem}", "files": removed})
            if path == "/api/cleanup":
                action, before = form.get("action", [""])[0], form.get("before", [""])[0]
                try:
                    gone = app.cleanup(action, before)
                except ValueError:
                    return self._send(400, b"bad request")
                except OSError as exc:
                    log.error("clean-up %s failed: %s", action, exc)
                    return self._send(500, b"clean-up failed")
                log.info("clean-up %s%s from the web page: %d files, %.1f MB (%s)", action,
                         f" {before}" if before else "", gone["files"], gone["bytes"] / 1e6, self.client_address[0])
                self._audit("cleanup", f"{action} {before}".strip() + f": {gone['files']} files")
                return self._json(gone)
            if path == "/api/visits/delete":
                name = form.get("name", [""])[0]
                if not VISIT_NAME.match(name):
                    return self._send(400, b"bad request")
                try:
                    if not app.delete_visit(name):
                        return self._send(404, b"not found")
                except OSError as exc:
                    log.error("could not delete %s: %s", name, exc)
                    return self._send(500, b"could not delete the video")
                log.info("visit video %s deleted from the web page (%s)", name, self.client_address[0])
                self._audit("delete", f"visits/{name}")
                return self._json({"deleted": name, "visits": app.visits()})
            if path == "/api/events/delete":
                name = form.get("name", [""])[0]
                if not EVENT_NAME.match(name):
                    return self._send(400, b"bad request")
                try:
                    if not app.delete_event(name):
                        return self._send(404, b"not found")
                except OSError as exc:
                    log.error("could not delete %s: %s", name, exc)
                    return self._send(500, b"could not delete the picture")
                log.info("picture %s deleted from the web page (%s)", name, self.client_address[0])
                self._audit("delete", f"alerts/{name}")
                return self._json({"deleted": name, "events": app.events()})
            state = form.get("state", [""])[0]
            if state not in ("on", "off"):
                return self._send(400, b"bad request")
            app.control.set_paused(state == "off")
            log.info("detection switched %s from the web page (%s)", state, self.client_address[0])
            self._audit("detection", state)
            status = app.status()
            status["user"] = {"name": self.user["name"], "role": self.user["role"], "login": cfg.login}
            self._json(status)

        def _login(self, length: int) -> None:
            if not 0 <= length <= 600:
                return self._send(400, b"bad request")
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            name = form.get("name", [""])[0].strip()[:64]
            password = form.get("password", [""])[0][:200]
            nxt = safe_next(form.get("next", ["/"])[0])
            keys = (f"ip:{self._ip()}", f"user:{name.lower()}")
            if app.locked_for(*keys):
                self._audit("login_locked", user=name)
                return self._redirect("/login?e=locked")
            user = app.store.check_password(name, password) if name and password else None
            if user is None:
                app.login_failed(*keys)
                self._audit("login_failed", user=name)
                log.warning("failed login for %r from %s", name, self._ip())
                return self._redirect("/login?e=wrong")
            app.login_ok(*keys)
            remember = form.get("remember", [""])[0] == "on"
            hours = cfg.session_hours if remember else 12
            token = app.store.new_session(user["id"], hours, self._ip(), self.headers.get("User-Agent", ""))
            self.user = user
            self._audit("login")
            log.info("%s logged in from %s", user["name"], self._ip())
            self._redirect(nxt, self._session_cookie(token, hours * 3600 if remember else None))

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

    return Handler


def html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def safe_next(target: str) -> str:
    """Where to go after the login: only a path on this site, never another site."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


def tls_context(cfg: Config) -> Optional[ssl.SSLContext]:
    """The certificate for https, or None for plain http (TLS=off, or auto without a certificate)."""
    if cfg.tls == "off" or (cfg.tls == "auto" and not cfg.tls_cert.is_file()):
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(cfg.tls_cert), str(cfg.tls_key))
    except (OSError, ssl.SSLError) as exc:
        raise ConfigError(f"cannot load the https certificate {cfg.tls_cert} / {cfg.tls_key}: {exc}; "
                          "run ./deploy.sh, which makes one, or set TLS=off") from None
    return context


class WebServer(ThreadingHTTPServer):
    """https and plain http on the same port: a connection that starts with a TLS handshake is encrypted,
    any other gets a redirect to https. So old http:// links and bookmarks keep working."""

    daemon_threads = True

    def __init__(self, address, handler, tls: Optional[ssl.SSLContext] = None):
        super().__init__(address, handler)
        self.tls = tls

    def finish_request(self, request, client_address):
        if self.tls is None:
            return super().finish_request(request, client_address)
        request.settimeout(10)
        try:
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if first == b"\x16":  # a TLS handshake
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError) as exc:  # e.g. a browser that does not trust the certificate yet
                log.debug("https handshake with %s failed: %s", client_address[0], exc)
                return
            try:
                self.RequestHandlerClass(request, client_address, self)
            finally:
                try:
                    request.close()
                except OSError:
                    pass
            return
        self.RequestHandlerClass(request, client_address, self)


def start_web(app: App):
    cfg = app.cfg
    if cfg.web_port == 0:
        return None
    tls = tls_context(cfg)
    try:
        server = WebServer((cfg.web_host, cfg.web_port), make_handler(app), tls)
    except OSError as exc:  # detection must keep working without the page
        log.error("web page not started on %s:%d: %s", cfg.web_host, cfg.web_port, exc)
        return None
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    shown = f"{socket.gethostname()}.local" if cfg.web_host in ("", "0.0.0.0") else cfg.web_host
    log.info("web console on %s://%s:%d/ (%s)", "https" if tls else "http", shown, cfg.web_port,
             "login required" if cfg.login else "NO login: keep it on a trusted network")
    if cfg.login and not app.store.users():
        log.warning("there are no logins yet: create one with: sudo -u homesec homesec adduser YOURNAME")
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
    check = open_object_check(cfg)
    camera = open_camera(cfg)
    app = App(cfg, control, getattr(camera, "label", getattr(camera, "NAME", "")),
              objects=check.objects if check else ())
    server = None
    watcher = None
    try:
        camera.start()
        server = start_web(app)
        watcher = Watcher(cfg, camera, mailer, control=control, slack=slack, check=check)
        if watcher.alert_videos:
            log.info("alert videos on: from %.0f s before an alert until %.0f s after the movement stopped "
                     "(at most %.0f s)", cfg.video_before, cfg.video_after, cfg.video_max)
        if watcher.visits:
            app.record = cfg.record_objects
            log.info("recording visits of %s (%.0f s before to %.0f s after, %.0f fps)", ", ".join(cfg.record_objects),
                     cfg.video_before, cfg.video_after, cfg.video_fps)
        log.info("watching for movement (%.1f fps, min. area %.1f%%, cooldown %ds), mailing to %s%s",
                 cfg.fps, cfg.min_area, cfg.cooldown, ", ".join(cfg.mail_to), " and posting to Slack" if slack else "")
        interval = 1.0 / cfg.fps
        view_failed = False
        while not stop.is_set():
            began = time.monotonic()
            watcher.step()
            app.score = watcher.last_score
            app.visiting = watcher.visit.label if watcher.visit else ""
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
        if watcher is not None:
            watcher.flush()
        mailer.close()
        if slack:
            slack.close()
        app.store.close()
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


def cmd_check(cfg: Config, image: Optional[str]) -> int:
    if not image:
        raise ConfigError("give a picture: homesec check /var/lib/homesec/2026-11-08_031502.jpg")
    check = ObjectCheck(cfg.object_model, cfg.objects, cfg.object_confidence)
    began = time.monotonic()
    found = check.find_in_file(Path(image))
    took = time.monotonic() - began
    if found is None:
        print(f"no {' or '.join(check.objects)} found (confidence {cfg.object_confidence:.0%} needed, {took:.2f} s)")
        return 1
    box = ", ".join(f"{v:.0%}" for v in found.box)
    print(f"{found.label}, {found.confidence:.0%} sure, at {box} (left, top, right, bottom; {took:.2f} s)")
    return 0


def ask_password(name: str) -> str:
    """Twice on a terminal; from standard input otherwise (one line), for scripts."""
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass(f"Password for {name}: ")
        if password != getpass.getpass("Again: "):
            raise ConfigError("the two passwords are not the same")
    if len(password) < 8:
        raise ConfigError("use a password of at least 8 characters")
    return password


def cmd_users(cfg: Config, command: str, name: Optional[str], role: str) -> int:
    store = Store(cfg.db_file)
    try:
        if command == "users":
            users = store.users()
            if not users:
                print("no logins yet: homesec adduser NAME")
            for u in users:
                print(f"{u['name']:<20} {u['role']:<7} created {u['created_at']}   last login {u['last_login'] or 'never'}")
            return 0
        if not name:
            raise ConfigError(f"give a user name: homesec {command} NAME")
        if command == "adduser":
            store.add_user(name, ask_password(name), role)
            store.audit("adduser", name, "", role)
            print(f"login {name} added ({role})")
        elif command == "passwd":
            if not store.set_password(name, ask_password(name)):
                raise ConfigError(f"there is no user {name}")
            store.audit("passwd", name)
            print(f"password of {name} changed; its sessions are logged out")
        elif command == "deluser":
            if not store.delete_user(name):
                raise ConfigError(f"there is no user {name}")
            store.audit("deluser", name)
            print(f"login {name} removed")
        return 0
    finally:
        store.close()


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
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "snapshot", "mailtest", "slacktest", "check", "adduser", "passwd", "deluser",
                                 "users"),
                        help="run (default): watch, mail and post to Slack; snapshot: mail one picture now; "
                             "mailtest / slacktest: send a test mail / Slack message; "
                             "check IMAGE: run the person check on a picture")
    parser.add_argument("image", nargs="?", help="picture for the check command; user name for adduser/passwd/deluser")
    parser.add_argument("--role", choices=("admin", "viewer"), default="admin",
                        help="for adduser: admin (everything) or viewer (watch only)")
    parser.add_argument("--env", metavar="FILE", help=f"settings file (default: first of $HOMESEC_ENV, ./.env, {DEFAULT_ENV_FILE})")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug messages")
    args = parser.parse_args(argv)

    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt, stream=sys.stderr)

    try:
        cfg = Config.load(args.env)
        if not args.verbose:
            logging.getLogger().setLevel(cfg.log_level.upper())
        if args.command == "check":
            return cmd_check(cfg, args.image)
        if args.command in ("adduser", "passwd", "deluser", "users"):
            return cmd_users(cfg, args.command, args.image, args.role)
        return {"run": cmd_run, "snapshot": cmd_snapshot, "mailtest": cmd_mailtest,
                "slacktest": cmd_slacktest}[args.command](cfg)
    except ConfigError as exc:
        log.error("settings: %s", exc)
        return EX_CONFIG
    except (CameraError, OSError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
