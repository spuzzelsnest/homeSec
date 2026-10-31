#!/usr/bin/python3
"""homeSec - mail a picture when the camera sees movement.

Commands:
  homesec run        watch the camera and mail a picture on movement (the service)
  homesec snapshot   take one picture now and mail it (stop the service first)
  homesec mailtest   send a test mail without using the camera

Settings are read from the first of these files that exists:
  --env FILE, $HOMESEC_ENV, ./.env, /etc/homesec/homesec.env
Environment variables override the file. See homesec.env.example.

Needs only the Raspberry Pi OS packages python3-picamera2 and python3-numpy
(python3-opencv as well for a USB webcam). No pip, no other software.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import smtplib
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Mapping, Optional

import numpy as np

log = logging.getLogger("homesec")

EX_CONFIG = 78  # the systemd unit does not restart the service on this exit code
DEFAULT_ENV_FILE = "/etc/homesec/homesec.env"


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
    smtp_password: str
    smtp_security: str
    smtp_timeout: int
    image_dir: Path
    camera: str
    video_device: str
    resolution: tuple
    detect_size: tuple
    fps: float
    threshold: float
    frames: int
    warmup: float
    cooldown: float
    keep_days: int
    jpeg_quality: int
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
            threshold=r.number("THRESHOLD", 7.0, float, 0, 65025),
            frames=r.number("FRAMES", 2, int, 1, 100),
            warmup=r.number("WARMUP", 5.0, float, 0, 600),
            cooldown=r.number("COOLDOWN", 300.0, float, 1, 86400),
            keep_days=r.number("KEEP_DAYS", 7, int, 1, 3650),
            jpeg_quality=r.number("JPEG_QUALITY", 90, int, 10, 100),
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


class Mailer:
    """Sends pictures from a background thread so a slow mail server never blocks detection."""

    ATTEMPTS = 3
    RETRY_DELAYS = (10, 60)  # seconds before attempt 2 and 3

    def __init__(self, cfg: Config, sender: Callable = send_message):
        self._cfg = cfg
        self._send = sender
        self._queue: queue.Queue = queue.Queue()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._work, name="mailer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, image: Path) -> None:
        self._queue.put(image)

    def close(self, timeout: float = 30) -> None:
        """Finish the mail in progress (without waiting for retries) and stop."""
        self._stopping.set()
        self._queue.put(None)
        self._thread.join(timeout)

    def _work(self) -> None:
        while True:
            image = self._queue.get()
            if image is None:
                return
            self.deliver(image)

    def deliver(self, image: Path) -> bool:
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                self._send(self._cfg, build_message(self._cfg, image))
                log.info("mailed %s to %s", image.name, ", ".join(self._cfg.mail_to))
                return True
            except OSError as exc:
                log.error("mail attempt %d/%d failed: %s", attempt, self.ATTEMPTS, exc)
            if attempt < self.ATTEMPTS and self._stopping.wait(self.RETRY_DELAYS[attempt - 1]):
                break
        log.error("giving up on %s (the picture stays in %s)", image.name, self._cfg.image_dir)
        return False


# --------------------------------------------------------------------------- cameras


class PiCamera:
    """Raspberry Pi camera (ribbon cable, and USB cameras libcamera supports) via picamera2.

    Two streams: "main" (full size, saved as JPEG) and "lores" (small YUV420, used for detection;
    the first plane of YUV420 is the grey image, so no conversion is needed).
    """

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
        self._width, self._height = cfg.detect_size
        config = self._cam.create_video_configuration(
            main={"size": cfg.resolution, "format": "RGB888"},
            lores={"size": cfg.detect_size, "format": "YUV420"},
            controls={"FrameRate": cfg.fps},
        )
        self._cam.configure(config)
        self._cam.options["quality"] = cfg.jpeg_quality
        self._stride = self._cam.stream_configuration("lores")["stride"]

    def start(self) -> None:
        self._cam.start()

    def read_gray(self) -> np.ndarray:
        buf = self._cam.capture_buffer("lores")
        plane = buf[: self._stride * self._height].reshape(self._height, self._stride)
        return np.ascontiguousarray(plane[:, : self._width])

    def capture(self, path: Path) -> None:
        self._cam.capture_file(str(path))

    def close(self) -> None:
        try:
            self._cam.stop()
        finally:
            self._cam.close()


class UsbCamera:
    """USB webcam via OpenCV (python3-opencv)."""

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
                 clock: Callable = time.monotonic, now: Callable = datetime.now):
        self.cfg = cfg
        self.camera = camera
        self.mailer = mailer
        self.detector = MotionDetector(cfg.threshold, cfg.frames)
        self._clock = clock
        self._now = now
        self._started = clock()
        self._last_trigger: Optional[float] = None

    def step(self) -> Optional[Path]:
        t = self._clock()
        moved, score = self.detector.update(self.camera.read_gray())
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
        prune(self.cfg.image_dir, self.cfg.keep_days)
        return path


# --------------------------------------------------------------------------- commands


def cmd_run(cfg: Config) -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    cfg.image_dir.mkdir(parents=True, exist_ok=True)
    prune(cfg.image_dir, cfg.keep_days)
    mailer = Mailer(cfg)
    mailer.start()
    camera = open_camera(cfg)
    try:
        camera.start()
        watcher = Watcher(cfg, camera, mailer)
        log.info("watching for movement (%.1f fps, threshold %.1f, cooldown %ds), mailing to %s",
                 cfg.fps, cfg.threshold, cfg.cooldown, ", ".join(cfg.mail_to))
        interval = 1.0 / cfg.fps
        while not stop.is_set():
            began = time.monotonic()
            watcher.step()
            stop.wait(max(0.0, interval - (time.monotonic() - began)))
        log.info("stopping")
    finally:
        camera.close()
        mailer.close()
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
    parser.add_argument("command", nargs="?", default="run", choices=("run", "snapshot", "mailtest"),
                        help="run (default): watch and mail; snapshot: mail one picture now; "
                             "mailtest: send a test mail")
    parser.add_argument("--env", metavar="FILE", help=f"settings file (default: first of $HOMESEC_ENV, ./.env, {DEFAULT_ENV_FILE})")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug messages")
    args = parser.parse_args(argv)

    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt, stream=sys.stderr)

    try:
        cfg = Config.load(args.env)
        if not args.verbose:
            logging.getLogger().setLevel(cfg.log_level.upper())
        return {"run": cmd_run, "snapshot": cmd_snapshot, "mailtest": cmd_mailtest}[args.command](cfg)
    except ConfigError as exc:
        log.error("settings: %s", exc)
        return EX_CONFIG
    except CameraError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
