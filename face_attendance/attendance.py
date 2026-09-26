"""Per-employee attendance eligibility; no database or image I/O on this thread."""
import queue
import uuid

from .anti_spoof import SAMPLE_MAX_AGE
from .liveness import MAX_SAMPLE_GAP
from .models import CaptureJob, ObservationEvent, State


class CooldownManager:
    def __init__(self, duration):
        self.duration = duration
        self.deadlines = {}

    def synchronize(self, employee_id, recorded_at, now, wall_time):
        remaining = max(0.0, self.duration - (wall_time - recorded_at))
        self.deadlines[employee_id] = now + remaining

    def remaining(self, employee_id, now):
        return max(0.0, self.deadlines.get(employee_id, now) - now)


class AttendanceService:
    def __init__(self, config, persistence):
        self.config, self.persistence = config, persistence
        self.cooldowns = CooldownManager(config.cooldown_sec)
        self.pending = {}  # employee_id -> event_id; survives a lost/replaced face track
        self.ready = False
        self.storage_error = ""
        self.last_attendance = None
        self.last_observed = {}

    def poll(self, tracks, now, wall_time):
        events = []
        startup = self.persistence.startup.take()
        if startup is not None:
            cooldowns, error = startup
            self.ready, self.storage_error = cooldowns is not None, error
            for employee_id, epoch in (cooldowns or {}).items():
                self.cooldowns.synchronize(employee_id, epoch, now, wall_time)
        while True:
            try:
                result = self.persistence.results.get_nowait()
            except queue.Empty:
                break
            job = result.job
            self.pending.pop(job.employee_id, None)
            track = tracks.get(job.track_id)
            if track and track.pending_event_id == job.event_id:
                track.pending_event_id = None
            if result.outcome in ("saved", "duplicate"):
                self.cooldowns.synchronize(job.employee_id, result.recorded_at, now, wall_time)
                if result.outcome == "saved":
                    self.last_attendance = result
                    events.append(("success", result))
                if track and track.employee_id == job.employee_id:
                    track.success_at = now if result.outcome == "saved" else None
                    track.error = ""
                    track.reset_verification()
                    track.transition(State.SUCCESS if result.outcome == "saved" else State.COOLDOWN, now)
            else:
                events.append(("error", result))
                if track and track.employee_id == job.employee_id:
                    track.error, track.retry_at = result.error, now + self.config.retry_sec
                    track.reset_verification()
                    track.transition(State.ERROR, now)
        return events

    def observe(self, tracks, now, wall_time):
        for track in tracks.values():
            if not track.visible:
                continue
            if track.identity_valid and not (
                    track.liveness_ok and 0 <= now - track.last_liveness_at <= MAX_SAMPLE_GAP
                    and track.spoof_ok and 0 <= now - track.last_spoof_at <= SAMPLE_MAX_AGE):
                continue
            name = track.employee_name if track.identity_valid else "UNKNOWN"
            if not track.identity_valid and track.state != State.UNKNOWN:
                continue
            key = track.employee_id if track.identity_valid else "UNKNOWN"
            interval = self.config.log_cooldown_sec if track.identity_valid else self.config.alert_cooldown_sec
            if now - self.last_observed.get(key, float("-inf")) >= interval:
                self.persistence.observe(ObservationEvent(name, wall_time, name == "UNKNOWN"))
                self.last_observed[key] = now

    def update(self, tracks, packet, now, camera_connected):
        captures = []
        cfg = self.config
        for track in tracks.values():
            fresh = (camera_connected and packet is not None and track.visible and not track.ambiguous
                     and now - packet.captured_at <= cfg.camera_stale_sec
                     and now - track.last_seen <= cfg.detection_fresh_sec
                     and now - track.last_recognized <= cfg.identity_fresh_sec)
            if not fresh:
                track.reset_verification()
            track.cooldown_remaining = self.cooldowns.remaining(track.employee_id, now)
            if track.pending_event_id or track.employee_id in self.pending:
                if fresh and track.identity_valid:
                    track.transition(State.CAPTURING, now)
                elif track.state != State.UNKNOWN:
                    track.transition(State.RECOGNIZING, now)
                continue
            if track.cooldown_remaining > 0:
                track.reset_verification()
                if fresh and track.identity_valid:
                    state = State.SUCCESS if (track.success_at is not None
                                               and now - track.success_at < cfg.success_duration) else State.COOLDOWN
                    track.transition(state, now)
                elif track.state != State.UNKNOWN:
                    track.transition(State.RECOGNIZING, now)
                continue
            if track.state == State.ERROR and now < track.retry_at:
                continue
            if not fresh or not track.identity_valid:
                if track.state != State.UNKNOWN:
                    track.transition(State.RECOGNIZING, now)
                continue
            if track.state in (State.COOLDOWN, State.SUCCESS):
                track.reset_verification()
                track.transition(State.READY, now)
            # Interpolate feedback; only measured, identity-matched presence
            # and a recent encoding authorize saving.
            extra = (min(max(0.0, now - track.last_evidence_at), cfg.stable_recheck_sec)
                     if track.last_evidence_at is not None and track.flow_ok else 0.0)
            measured = track.verified_presence / cfg.capture_after_sec
            track.verification_progress = min(1.0 if measured >= 1 else 0.99,
                                               (track.verified_presence + extra) / cfg.capture_after_sec)
            track.transition(State.VERIFYING, now)
            eligible = (measured >= 1 and track.confirmation_count >= cfg.min_confirmation_frames
                        and track.flow_ok and now - track.last_flow_at <= cfg.detection_fresh_sec
                        and now - track.last_recognized <= cfg.detection_fresh_sec
                        and track.spoof_ok
                        and 0 <= now - track.last_spoof_at <= SAMPLE_MAX_AGE
                        and track.liveness_ok
                        and 0 <= now - track.last_liveness_at <= MAX_SAMPLE_GAP
                        and self.ready)
            evidence = track.evidence_packet
            eligible = (eligible and evidence is not None and packet is not None
                        and evidence.generation == packet.generation
                        and evidence.sequence <= packet.sequence
                        and 0 <= now - evidence.captured_at <= SAMPLE_MAX_AGE
                        and evidence.captured_at == track.last_spoof_at)
            if not eligible:
                continue
            track.transition(State.CONFIRMED, now)
            job = CaptureJob(str(uuid.uuid4()), track.track_id, track.employee_id, track.employee_name,
                             evidence.wall_time, track.verified_presence, evidence.frame.copy())
            if self.persistence.submit(job):
                self.pending[job.employee_id] = job.event_id
                track.pending_event_id = job.event_id
                track.transition(State.CAPTURING, now)
                captures.append(job)
            else:
                track.error = "Storage queue busy"
                track.retry_at = now + cfg.retry_sec
                track.transition(State.ERROR, now)
        return captures
