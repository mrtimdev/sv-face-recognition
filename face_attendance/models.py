"""Messages crossing threads are immutable; FaceTrack belongs to the UI thread."""
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Tuple


Box = Tuple[float, float, float, float]  # top, right, bottom, left (camera pixels)


class State(str, Enum):
    DETECTING = "DETECTING"
    RECOGNIZING = "RECOGNIZING"
    VERIFYING = "VERIFYING"
    CONFIRMED = "CONFIRMED"
    CAPTURING = "CAPTURING"
    SUCCESS = "SUCCESS"
    COOLDOWN = "COOLDOWN"
    READY = "READY"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Employee:
    employee_id: str
    name: str


@dataclass(frozen=True)
class FramePacket:
    sequence: int
    captured_at: float  # monotonic; state/animation clock
    wall_time: float    # Unix epoch; persisted as UTC
    generation: int     # camera reconnect invalidates in-flight recognition
    frame: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class TrackHint:
    track_id: int
    bounding_box: Box
    employee_id: Optional[str]
    last_recognized: float
    last_encoded_sequence: int
    stable: bool


@dataclass(frozen=True)
class RecognitionRequest:
    packet: FramePacket
    hints: tuple


@dataclass(frozen=True)
class Detection:
    bounding_box: Box
    employee: Optional[Employee] = None
    distance: Optional[float] = None
    encoded: bool = False
    hint_id: Optional[int] = None
    landmarks: Any = None
    face_roi: Any = field(default=None, repr=False, compare=False)
    spoof_score: Optional[float] = None
    spoof_error: str = ""
    spoof_model_scores: tuple = ()


@dataclass(frozen=True)
class RecognitionResult:
    packet: FramePacket
    detections: tuple
    elapsed: float
    error: str = ""


@dataclass
class FaceTrack:
    track_id: int
    bounding_box: Box
    first_seen: float
    last_seen: float
    employee_id: Optional[str] = None
    employee_name: str = ""
    last_recognized: float = float("-inf")
    last_encoded_sequence: int = -1
    recognition_distance: Optional[float] = None
    recognition_history: Any = field(default_factory=lambda: deque(maxlen=8))
    state: State = State.DETECTING
    verification_progress: float = 0.0
    verified_presence: float = 0.0
    cooldown_remaining: float = 0.0
    candidate_id: Optional[str] = None
    confirmation_count: int = 0
    last_evidence_at: Optional[float] = None
    identity_valid: bool = False
    visible: bool = True
    ambiguous: bool = False
    flow_ok: bool = False
    spoof_ok: bool = False
    spoof_score: Optional[float] = None
    spoof_model_scores: tuple = ()
    spoof_prompt: str = "Checking real face..."
    last_spoof_at: float = float("-inf")
    spoof_progress: float = 0.0
    evidence_packet: Any = field(default=None, repr=False, compare=False)
    liveness_ok: bool = False
    liveness_prompt: str = "Look straight at the camera"
    liveness_progress: float = 0.0
    last_liveness_at: float = float("-inf")
    last_flow_at: float = float("-inf")
    flow_lost_at: Optional[float] = None
    state_since: float = 0.0
    retry_at: float = 0.0
    error: str = ""
    pending_event_id: Optional[str] = None
    success_at: Optional[float] = None
    points: Any = field(default=None, repr=False)
    box_history: Any = field(default_factory=lambda: deque(maxlen=90), repr=False)

    def transition(self, state, now):
        if self.state != state:
            self.state, self.state_since = state, now

    def reset_verification(self):
        self.verified_presence = self.verification_progress = 0.0
        self.last_evidence_at = None
        self.evidence_packet = None

    def invalidate_identity(self):
        # Keep the displayed name during brief uncertainty, but revoke capture eligibility.
        self.spoof_ok = False
        self.spoof_score = None
        self.spoof_model_scores = ()
        self.spoof_progress = 0.0
        self.last_spoof_at = float("-inf")
        self.liveness_ok = False
        self.last_liveness_at = float("-inf")
        self.identity_valid = False
        self.confirmation_count = 0
        self.candidate_id = None
        self.reset_verification()


@dataclass(frozen=True)
class CaptureJob:
    event_id: str
    track_id: int
    employee_id: str
    employee_name: str
    captured_at: float
    duration: float
    frame: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class SaveResult:
    job: CaptureJob
    outcome: str  # saved, duplicate, error
    recorded_at: Optional[float] = None
    snapshot: str = ""
    error: str = ""


@dataclass(frozen=True)
class ObservationEvent:
    name: str
    timestamp: float
    alert: bool = False
