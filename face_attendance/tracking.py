"""Sparse optical flow between detector observations, with one-to-one association."""
import math

import cv2
import numpy as np

from .geometry import association_cost, clamp_box, iou
from .liveness import LivenessChecker
from .models import FaceTrack, State, TrackHint


class FaceTracker:
    def __init__(self, config):
        self.config = config
        self.tracks = {}
        self.next_id = 1
        self.previous_gray = None
        self.scale = 1.0
        self.generation = None
        self.last_result_sequence = -1
        self.liveness = LivenessChecker()

    def clear(self):
        self.tracks.clear()
        self.previous_gray = None
        self.last_result_sequence = -1
        self.liveness.clear()

    def suspend(self):
        for track in self.tracks.values():
            track.visible = False
            track.flow_ok = False
            track.invalidate_identity()

    def _features(self, gray, box):
        t, r, b, l = [int(v * self.scale) for v in box]
        height, width = gray.shape
        t, b = max(0, t), min(height, b)
        l, r = max(0, l), min(width, r)
        if b - t < 6 or r - l < 6:
            return None
        points = cv2.goodFeaturesToTrack(gray[t:b, l:r], maxCorners=24, qualityLevel=0.015,
                                         minDistance=4, blockSize=3)
        if points is not None:
            points += np.float32([l, t])
        return points

    def advance(self, packet):
        """Called on each new displayed camera frame; never encodes a face."""
        frame = packet.frame
        height, width = frame.shape[:2]
        if self.generation != packet.generation:
            self.clear()
            self.generation = packet.generation
        self.scale = min(1.0, self.config.tracker_width / width)
        size = (max(1, round(width * self.scale)), max(1, round(height * self.scale)))
        gray = cv2.cvtColor(cv2.resize(frame, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        if self.previous_gray is not None and self.previous_gray.shape != gray.shape:
            self.clear()
        for track in list(self.tracks.values()):
            if packet.captured_at - track.last_seen > self.config.session_timeout:
                del self.tracks[track.track_id]
                continue
            track.flow_ok = False
            if self.previous_gray is not None and track.points is not None and len(track.points) >= 4:
                moved, status, _ = cv2.calcOpticalFlowPyrLK(
                    self.previous_gray, gray, track.points, None, winSize=(15, 15), maxLevel=2,
                    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 12, 0.03))
                if moved is not None:
                    back, back_status, _ = cv2.calcOpticalFlowPyrLK(
                        gray, self.previous_gray, moved, None, winSize=(15, 15), maxLevel=2)
                    if back is not None:
                        good = ((status.ravel() == 1) & (back_status.ravel() == 1)
                                & (np.linalg.norm(back - track.points, axis=2).ravel() < 1.5))
                        if np.count_nonzero(good) >= 4:
                            delta = (moved[good] - track.points[good]).reshape(-1, 2)
                            dx, dy = np.median(delta, axis=0) / self.scale
                            box = track.bounding_box
                            if math.hypot(dx, dy) < max(box[1] - box[3], box[2] - box[0]) * 0.4:
                                track.bounding_box = clamp_box(
                                    (box[0] + dy, box[1] + dx, box[2] + dy, box[3] + dx), width, height)
                                track.points = moved[good].reshape(-1, 1, 2)
                                track.flow_ok, track.last_flow_at = True, packet.captured_at
                                flow_var = float(np.var(delta, axis=0).sum())
                                self.liveness.update_motion(track.track_id, flow_var)
            if not track.flow_ok:
                track.verified_presence = max(0.0, track.verified_presence - 0.3)
                track.points = self._features(gray, track.bounding_box)
            # Store positions by source sequence to compensate for worker latency.
            track.box_history.append((packet.sequence, track.bounding_box))
            if packet.captured_at - track.last_seen > self.config.detection_fresh_sec:
                track.visible = False
                track.invalidate_identity()
        self.previous_gray = gray
        self._mark_overlaps()

    def _mark_overlaps(self):
        active = [track for track in self.tracks.values() if track.visible]
        for track in active:
            track.ambiguous = False
        for i, track in enumerate(active):
            for other in active[i + 1:]:
                if iou(track.bounding_box, other.bounding_box) > 0.2:
                    track.ambiguous = other.ambiguous = True
                    track.invalidate_identity()
                    other.invalidate_identity()

    def hints(self):
        hints = []
        for track in self.tracks.values():
            if not track.visible:
                continue
            # Near capture, request a final fresh encoding at the faster interval.
            # This avoids waiting a whole stable recheck period after reaching 3s.
            near_capture = (track.verified_presence >= self.config.capture_after_sec - self.config.stable_recheck_sec
                            and track.state not in (State.CAPTURING, State.SUCCESS, State.COOLDOWN))
            stable = track.identity_valid and not track.ambiguous and not near_capture
            hints.append(TrackHint(track.track_id, track.bounding_box, track.employee_id,
                                   track.last_recognized, track.last_encoded_sequence, stable))
        return tuple(hints)

    @staticmethod
    def _historic_box(track, sequence):
        for seq, box in reversed(track.box_history):
            if seq <= sequence:
                return box
        return track.bounding_box

    def apply(self, result, now):
        packet = result.packet
        if (packet.generation != self.generation or packet.sequence <= self.last_result_sequence
                or now - packet.captured_at > self.config.max_result_age_sec):
            return False
        self.last_result_sequence = packet.sequence
        if result.error:
            self.suspend()
            return False
        old_tracks = list(self.tracks.values())
        assignments, used = {}, set()
        edges = []
        for index, detection in enumerate(result.detections):
            for track in old_tracks:
                if track.first_seen > packet.captured_at:
                    continue
                box = self._historic_box(track, packet.sequence)
                cost = association_cost(box, detection.bounding_box)
                if math.isfinite(cost):
                    edges.append((cost, index, track.track_id))
        for cost, index, track_id in sorted(edges):
            if index not in assignments and track_id not in used:
                assignments[index] = track_id
                used.add(track_id)
        for track in old_tracks:
            if track.track_id not in used and track.first_seen <= packet.captured_at:
                track.visible = False
                track.invalidate_identity()
        for index, detection in enumerate(result.detections):
            track = self.tracks.get(assignments.get(index))
            if track is None:
                track = FaceTrack(self.next_id, detection.bounding_box, packet.captured_at,
                                  packet.captured_at, state_since=now)
                self.tracks[track.track_id] = track
                self.next_id += 1
            else:
                historic = self._historic_box(track, packet.sequence)
                # Carry forward motion since the observation, then smooth detector jitter.
                adjusted = tuple(d + current - old for d, current, old in
                                 zip(detection.bounding_box, track.bounding_box, historic))
                track.bounding_box = tuple(0.55 * d + 0.45 * old for d, old in
                                           zip(adjusted, track.bounding_box))
            track.visible, track.last_seen = True, packet.captured_at
            # A skipped encoding can only reuse the exact track that authorized the skip.
            if not detection.encoded and detection.hint_id != track.track_id:
                track.invalidate_identity()
            if detection.encoded:
                self._recognize(track, detection, packet, now)
            if self.previous_gray is not None:
                track.points = self._features(self.previous_gray, track.bounding_box)
        self._mark_overlaps()
        return True

    def _recognize(self, track, detection, packet, now):
        cfg = self.config
        employee = detection.employee
        track.last_encoded_sequence = packet.sequence
        track.recognition_distance = detection.distance
        track.recognition_history.append((packet.captured_at, employee.employee_id if employee else None,
                                          detection.distance))
        if detection.landmarks:
            _blinks, passed = self.liveness.update(track.track_id, detection.landmarks)
            track.liveness_ok = passed
        if detection.face_roi is not None:
            self.liveness.update_texture(track.track_id, detection.face_roi)
            track.liveness_ok = self.liveness.is_live(track.track_id)
        if employee is None:
            track.invalidate_identity()
            if track.pending_event_id is None:
                track.transition(State.UNKNOWN, now)
            return
        if (track.candidate_id != employee.employee_id
                or packet.captured_at - track.last_recognized > cfg.identity_fresh_sec):
            track.invalidate_identity()
            track.candidate_id = employee.employee_id
        track.confirmation_count += 1
        if track.last_evidence_at is not None:
            gap = packet.captured_at - track.last_evidence_at
            if 0 < gap <= cfg.identity_fresh_sec:
                track.verified_presence += gap
            else:
                track.reset_verification()
        track.last_evidence_at = packet.captured_at
        track.last_recognized = packet.captured_at
        if track.confirmation_count >= cfg.min_confirmation_frames:
            track.employee_id, track.employee_name = employee.employee_id, employee.name
            track.identity_valid = True
        # Pending persistence belongs to the original employee even if this track changes.
        if track.pending_event_id is None and track.state not in (State.SUCCESS, State.COOLDOWN, State.ERROR):
            track.transition(State.VERIFYING if track.identity_valid else State.RECOGNIZING, now)
