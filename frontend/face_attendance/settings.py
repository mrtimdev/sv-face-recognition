"""User-editable dashboard settings persisted to ``settings.json``.

``Config`` stays the single source of defaults and validation: the dashboard
only stores overrides and rebuilds a validated ``Config`` from them. Because
``Config`` is frozen and the workers capture it at construction time, saved
changes take effect when the engine is restarted.
"""
import json
import os
import tempfile
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass, field as dataclass_field, fields, replace
from pathlib import Path

from .config import ROOT, Config, parse_source


SETTINGS_PATH = ROOT / "settings.json"
LOCK_PATH = ROOT / "attendance.lock"
THEMES = ("dark", "light")
MAX_LIVE_CAMERAS = 4


def mask_url_credentials(value):
    """Never show an RTSP password on screen or in a log line."""
    text = str(value)
    if "://" not in text:
        return text
    try:
        parts = urlsplit(text)
        authority = parts.netloc
        if "@" in authority:
            credentials, host = authority.rsplit("@", 1)
            if ":" in credentials:
                authority = f"{credentials.split(':', 1)[0]}:********@{host}"
        # Query strings and fragments frequently carry bearer credentials;
        # neither is needed in camera labels or capture metadata.
        return urlunsplit((parts.scheme, authority, parts.path,
                           "********" if parts.query else "",
                           "********" if parts.fragment else ""))
    except ValueError:
        return "Camera URL (redacted)"


def source_kind(source):
    """device, url or file, from what the source text looks like."""
    text = str(source).strip()
    return "device" if text.isdecimal() else "url" if "://" in text else "file"


def camera_problem(name, source, kind=None):
    """Why this camera cannot be saved, or "" when it can."""
    source = str(source).strip()
    kind = kind or source_kind(source)
    label = str(name).strip() or "This camera"
    if not source:
        return f"{label}: enter a camera index, network URL or file path"
    if kind == "device" and not source.isdecimal():
        return f"{label}: a webcam source must be a device index such as 0 or 1"
    if kind == "url" and "://" not in source:
        return f"{label}: a network URL starts with rtsp:// or http://"
    if kind == "file" and not Path(source).exists():
        return f"{label}: video file not found"
    return ""


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
    detection_scale: float = 0.5
    detection_interval: int = 2
    recognition_interval: int = 3
    stable_recheck_sec: float = 0.5
    face_tolerance: float = 0.5
    identity_margin: float = 0.035
    min_confirmation_frames: int = 2
    capture_after_sec: float = 1.0
    cooldown_sec: float = 30.0
    opencv_threads: int = 1
    max_detect_faces: int = 5
    quality_min_face_px: int = 80
    quality_min_sharpness: float = 25.0
    camera_timeout_ms: int = 2500
    reconnect_sec: float = 1.0
    persistence_queue_size: int = 8
    db_backend: str = "sqlite"
    db_host: str = ""
    db_port: int = 0
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""
    db_sslmode: str = ""
    db_path: str = str(Config().db_path)
    capture_dir: str = str(Config().capture_dir)
    encodings_path: str = str(Config().encodings_path)
    employees_path: str = str(Config().employees_path)
    log_path: str = str(Config().log_path)
    alert_path: str = str(Config().alert_path)
    sounds_enabled: bool = True
    sound_detection: bool = True
    sound_guidance: bool = True
    sound_unknown: bool = True
    sound_inactive: bool = True
    sound_lost: bool = True
    sound_recognized: bool = True
    sound_multiple: bool = True
    sound_spoof: bool = True
    sound_liveness: bool = True
    sound_verified: bool = True
    sound_success: bool = True
    sound_error: bool = True
    sound_disconnected: bool = True
    sound_reconnected: bool = True
    theme: str = "dark"
    report_page_size: int = 100
    report_work_start: str = ""  # opt-in "Late" label in reports; "" disables it
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_notify_capture: bool = True
    telegram_notify_unknown: bool = True
    auto_capture_unknown: bool = True
    auto_capture_inactive: bool = True
    telegram_notify_inactive: bool = True
    update_auto_check: bool = True
    update_skipped_version: str = ""
    update_last_checked: str = ""     # ISO timestamp of the last successful check
    ai_provider: str = "openai"
    ai_profiles: str = ""             # JSON per provider; see ai_providers.load_profiles
    hrm_api_url: str = ""             # One fixed SV HRM server (first-run wizard, older installs); see hrm_servers
    hrm_server_mode: str = ""         # auto, online or office; see hrm_servers
    hrm_online_url: str = ""          # SV HRM over the internet; empty uses the default
    hrm_office_url: str = ""          # SV HRM on the office network; empty uses this computer's address
    hrm_api_key: str = ""             # X-API-KEY for /api/face-recognition/** on that server
    hrm_auto_upload: bool = True      # Send each generated face angle to HRM as soon as it is saved
    # Phones on the same Wi-Fi pair with this computer by a 6-digit code (machine_server.py);
    # the key is made here and handed to each paired phone, never typed.
    mobile_link_enabled: bool = True
    mobile_link_key: str = ""
    mobile_link_id: str = ""          # Lets a paired phone find this computer again after its IP changes
    # Saved cameras, each {"name", "source"}. The first one always uses ``source``
    # above, so older settings files and code that sets ``source`` keep working.
    cameras: list = dataclass_field(default_factory=list)
    live_cameras: list = dataclass_field(default_factory=lambda: [0])  # indices shown in Live Monitor

    def camera_entries(self):
        """Every saved camera; at least one, the first using ``source``."""
        entries = [dict(entry) for entry in self.cameras]
        if not entries:
            entries = [{"name": "Camera 1", "source": str(self.source)}]
        entries[0]["source"] = str(self.source)
        for number, entry in enumerate(entries, 1):
            entry["name"] = entry.get("name") or f"Camera {number}"
        return entries

    def selected_cameras(self):
        """The (index, entry) pairs Live Monitor runs, at most MAX_LIVE_CAMERAS."""
        entries = self.camera_entries()
        chosen = []
        for index in self.live_cameras:
            if 0 <= index < len(entries) and index not in chosen:
                chosen.append(index)
        chosen = chosen[:MAX_LIVE_CAMERAS] or [0]
        return [(index, entries[index]) for index in chosen]

    def to_config(self):
        """Build a validated ``Config``; raises ``ValueError`` for bad values."""
        from .destination import destination_id
        from .hrm_servers import current_url
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
        for entry in self.camera_entries():
            if not str(entry["source"]).strip():
                raise ValueError(f"{entry['name']} has no source")
            parse_source(entry["source"])
        values["hrm_destination"] = destination_id(current_url(self))
        return replace(Config(), **values)

    def update(self, values):
        """Apply raw (string) values from the settings form in place."""
        for key, value in values.items():
            if not hasattr(self, key):
                continue
            current = getattr(self, key)
            if key == "cameras":
                self.cameras = _camera_list(value)
                continue
            if key == "live_cameras":
                self.live_cameras = _index_list(value)
                continue
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
        values = {key: getattr(self, key) for key in
                  (field.name for field in fields(type(self)))}
        values["cameras"] = self.camera_entries()
        values["live_cameras"] = list(self.live_cameras)
        return values

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


def _camera_list(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError("cameras must be a list")
    cameras = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("each camera needs a name and a source")
        cameras.append({"name": str(entry.get("name") or "").strip(),
                        "source": str(entry.get("source") or "").strip()})
    return cameras


def _index_list(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError("live_cameras must be a list")
    try:
        return [int(index) for index in value]
    except (TypeError, ValueError):
        raise ValueError("live_cameras expects camera numbers") from None
