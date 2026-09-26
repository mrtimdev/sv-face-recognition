"""User-editable dashboard settings persisted to ``settings.json``.

``Config`` stays the single source of defaults and validation: the dashboard
only stores overrides and rebuilds a validated ``Config`` from them. Because
``Config`` is frozen and the workers capture it at construction time, saved
changes take effect when the engine is restarted.
"""
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, fields, replace
from pathlib import Path

from .config import ROOT, Config, parse_source


SETTINGS_PATH = ROOT / "settings.json"
LOCK_PATH = ROOT / "attendance.lock"
THEMES = ("dark", "light")


def hash_pin(pin):
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def mask_url_credentials(value):
    """Never show an RTSP password on screen or in a log line."""
    text = str(value)
    if "://" not in text or "@" not in text:
        return text
    scheme, remainder = text.split("://", 1)
    credentials, host = remainder.rsplit("@", 1)
    if ":" in credentials:
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:{'*' * 8}@{host}"
    return text


def describe_source(value):
    source = parse_source(value)
    if isinstance(source, int):
        return f"Webcam {source}"
    if "://" in str(source):
        return mask_url_credentials(source)
    return Path(str(source)).name


@dataclass
class Settings:
    """Everything the dashboard can change; one flat, JSON-friendly record."""

    recognition_backend: str = "opencv-sface-2021dec-v1"
    attendance_mode: str = "blink"
    source: str = "0"
    camera_width: int = 1280
    camera_height: int = 720
    target_fps: float = 30.0
    detection_scale: float = 0.25
    detection_interval: int = 2
    recognition_interval: int = 3
    stable_recheck_sec: float = 0.5
    face_tolerance: float = 0.5
    identity_margin: float = 0.035
    min_confirmation_frames: int = 2
    capture_after_sec: float = 3.0
    cooldown_sec: float = 30.0
    opencv_threads: int = 1
    max_detect_faces: int = 5
    camera_timeout_ms: int = 2500
    reconnect_sec: float = 1.0
    persistence_queue_size: int = 8
    db_path: str = str(Config().db_path)
    capture_dir: str = str(Config().capture_dir)
    encodings_path: str = str(Config().encodings_path)
    employees_path: str = str(Config().employees_path)
    log_path: str = str(Config().log_path)
    alert_path: str = str(Config().alert_path)
    theme: str = "dark"
    report_page_size: int = 100
    report_work_start: str = ""  # opt-in "Late" label in reports; "" disables it
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_notify_capture: bool = True
    telegram_notify_unknown: bool = True
    dashboard_pin_hash: str = ""

    def to_config(self):
        """Build a validated ``Config``; raises ``ValueError`` for bad values."""
        values = {"source": parse_source(self.source)}
        for field in fields(Config):
            if field.name in ("source",):
                continue
            if hasattr(self, field.name):
                values[field.name] = getattr(self, field.name)
        for name in ("db_path", "capture_dir", "encodings_path", "employees_path", "log_path", "alert_path"):
            values[name] = Path(str(values[name])).expanduser()
        if self.theme not in THEMES:
            raise ValueError(f"theme must be one of {', '.join(THEMES)}")
        if int(self.report_page_size) <= 0:
            raise ValueError("report_page_size must be positive")
        if self.report_work_start:
            parts = str(self.report_work_start).split(":")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise ValueError("report_work_start must be HH:MM or empty")
            if not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
                raise ValueError("report_work_start must be a valid time of day")
        return replace(Config(), **values)

    def update(self, values):
        """Apply raw (string) values from the settings form in place."""
        for key, value in values.items():
            if not hasattr(self, key):
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    setattr(self, key, bool(value))
                elif isinstance(current, float):
                    setattr(self, key, float(value))
                elif isinstance(current, int):
                    setattr(self, key, int(float(value)))
                else:
                    setattr(self, key, str(value))
            except (TypeError, ValueError):
                raise ValueError(f"{key} expects a number") from None

    def to_dict(self):
        return {key: getattr(self, key) for key in
                (field.name for field in fields(type(self)))}

    @classmethod
    def load(cls, path=None):
        path = Path(path or SETTINGS_PATH)
        if not path.exists():
            settings = cls()
            settings._path = path
            return settings
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("settings.json must contain a JSON object")
        settings = cls()
        settings._path = path
        # Old distances and vectors belong to dlib, even though both embeddings
        # have 128 values. Redirect old settings without modifying the old file.
        if data.get("recognition_backend") != settings.recognition_backend:
            old = Path(str(data.get("encodings_path", Config().encodings_path)))
            if old.name != "encodings_sface.pickle":
                data["encodings_path"] = str(old.with_name(old.stem + "_sface" + old.suffix))
            data["face_tolerance"] = .5
            data["identity_margin"] = .035
            data["recognition_backend"] = settings.recognition_backend
        settings.update({key: value for key, value in data.items()
                         if key in settings.to_dict()})  # Unknown keys are ignored.
        return settings

    def save(self, path=None):
        """Atomic write, so a crash cannot leave a half-saved configuration."""
        path = Path(path or getattr(self, "_path", SETTINGS_PATH))
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False,
                                             encoding="utf-8")
        temporary = Path(handle.name)
        try:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
        handle.close()
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    @classmethod
    def reset(cls, path=None):
        Path(path or SETTINGS_PATH).unlink(missing_ok=True)
        return cls()
