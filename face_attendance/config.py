"""All terminal tuning lives here; CLI arguments override these defaults."""
from dataclasses import dataclass
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    source: object = 0
    camera_width: int = 1280
    camera_height: int = 720
    target_fps: float = 30.0
    detection_scale: float = 0.25
    detection_interval: int = 3
    recognition_interval: int = 5
    stable_recheck_sec: float = 0.75
    face_tolerance: float = 0.5
    identity_margin: float = 0.035
    min_confirmation_frames: int = 3
    capture_after_sec: float = 3.0
    cooldown_sec: float = 30.0
    detection_fresh_sec: float = 0.8
    identity_fresh_sec: float = 1.5
    max_result_age_sec: float = 0.8
    session_timeout: float = 3.0
    tracker_width: int = 480
    flash_duration: float = 0.22
    shutter_duration: float = 0.42
    success_duration: float = 3.5
    retry_sec: float = 2.0
    persistence_queue_size: int = 8
    camera_timeout_ms: int = 2500
    reconnect_sec: float = 1.0
    camera_stale_sec: float = 1.0
    opencv_threads: int = 1
    alert_cooldown_sec: float = 3.0
    log_cooldown_sec: float = 10.0
    encodings_path: Path = ROOT / "encodings.pickle"
    employees_path: Path = ROOT / "employees.json"
    db_path: Path = ROOT / "attendance.db"
    capture_dir: Path = ROOT / "captures"
    log_path: Path = ROOT / "attendance_log.csv"
    alert_path: Path = ROOT / "alert.wav"

    def __post_init__(self):
        for field in ("camera_width", "camera_height", "target_fps", "detection_interval",
                      "recognition_interval", "min_confirmation_frames", "capture_after_sec",
                      "cooldown_sec", "detection_fresh_sec", "identity_fresh_sec",
                      "max_result_age_sec", "session_timeout", "tracker_width",
                      "flash_duration", "shutter_duration", "success_duration", "retry_sec",
                      "persistence_queue_size", "camera_timeout_ms", "reconnect_sec",
                      "camera_stale_sec", "opencv_threads", "stable_recheck_sec"):
            if not math.isfinite(getattr(self, field)) or getattr(self, field) <= 0:
                raise ValueError(f"{field} must be positive")
        if not 0 < self.detection_scale <= 1:
            raise ValueError("detection_scale must be in (0, 1]")
        if not 0 < self.face_tolerance < 1 or not 0 <= self.identity_margin < 1:
            raise ValueError("invalid face tolerance or identity margin")


def parse_source(value):
    """OpenCV distinguishes integer device IDs from string video paths."""
    if value is None:
        return 0
    return int(value) if str(value).isdecimal() else value
