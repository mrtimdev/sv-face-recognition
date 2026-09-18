import queue
import unittest
from dataclasses import replace

import numpy as np

from face_attendance.attendance import AttendanceService
from face_attendance.channels import LatestValue
from face_attendance.config import Config, parse_source
from face_attendance.models import FaceTrack, FramePacket, SaveResult, State


class FakePersistence:
    def __init__(self, accepts=True):
        self.startup = LatestValue()
        self.results = queue.Queue()
        self.jobs, self.events = [], []
        self.accepts = accepts

    def submit(self, job):
        if self.accepts:
            self.jobs.append(job)
        return self.accepts

    def observe(self, event):
        self.events.append(event)


def verified(track_id=1, employee_id="E001", now=10.0):
    return FaceTrack(track_id, (20, 100, 120, 20), now - 4, now,
                     employee_id=employee_id, employee_name="Employee " + employee_id,
                     last_recognized=now, identity_valid=True, confirmation_count=4,
                     verified_presence=3.1, last_evidence_at=now,
                     flow_ok=True, last_flow_at=now, state=State.VERIFYING)


class AttendanceTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.worker = FakePersistence()
        self.service = AttendanceService(self.config, self.worker)
        self.worker.startup.put(({}, ""))
        self.service.poll({}, 10, 1000)
        self.frame = np.full((240, 320, 3), 57, np.uint8)
        self.packet = FramePacket(100, 10, 1000, 1, self.frame)

    def test_capture_is_clean_and_success_requires_commit(self):
        track = verified()
        tracks = {1: track}
        jobs = self.service.update(tracks, self.packet, 10, True)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(track.state, State.CAPTURING)
        self.assertIsNone(self.service.last_attendance)
        self.frame[:] = 255
        self.assertTrue(np.all(jobs[0].frame == 57))
        self.assertEqual(self.service.update(tracks, self.packet, 10.1, True), [])
        self.worker.results.put(SaveResult(jobs[0], "saved", 1000))
        events = self.service.poll(tracks, 10.2, 1000.2)
        self.assertEqual(events[0][0], "success")
        self.service.update(tracks, self.packet, 10.2, True)
        self.assertEqual(track.state, State.SUCCESS)
        self.assertAlmostEqual(track.cooldown_remaining, 29.8)

    def test_employees_independent_and_same_employee_deduplicated(self):
        tracks = {1: verified(1, "A"), 2: verified(2, "B"), 3: verified(3, "A")}
        jobs = self.service.update(tracks, self.packet, 10, True)
        self.assertEqual({job.employee_id for job in jobs}, {"A", "B"})
        self.assertEqual(len(jobs), 2)
        self.worker.results.put(SaveResult(jobs[0], "saved", 1000))
        self.service.poll(tracks, 10.1, 1000.1)
        self.assertEqual(tracks[2].state, State.CAPTURING)
        self.assertIn("B", self.service.pending)

    def test_failed_save_never_starts_cooldown_and_requires_reverification(self):
        track = verified()
        job = self.service.update({1: track}, self.packet, 10, True)[0]
        self.worker.results.put(SaveResult(job, "error", error="disk full"))
        events = self.service.poll({1: track}, 10.1, 1000.1)
        self.assertEqual(events[0][0], "error")
        self.assertEqual(track.state, State.ERROR)
        self.assertIsNone(self.service.last_attendance)
        self.assertEqual(self.service.cooldowns.remaining("E001", 10.1), 0)
        self.assertEqual(track.verified_presence, 0)
        self.assertEqual(self.service.update({1: track}, self.packet, 10.2, True), [])

    def test_blocked_capture_conditions(self):
        for changes in ({"visible": False}, {"ambiguous": True}, {"identity_valid": False},
                        {"last_seen": 8}, {"last_recognized": 8}, {"flow_ok": False},
                        {"confirmation_count": 1}, {"verified_presence": 2.9}):
            with self.subTest(changes=changes):
                track = verified()
                for field, value in changes.items():
                    setattr(track, field, value)
                self.assertEqual(self.service.update({1: track}, self.packet, 10, True), [])
        self.assertEqual(self.service.update({1: verified()}, self.packet, 10, False), [])
        self.assertEqual(self.service.update({1: verified()}, self.packet, 12, True), [])

    def test_restart_cooldown_and_post_cooldown_verification(self):
        self.worker.startup.put(({"E001": 995}, ""))
        self.service.poll({}, 10, 1000)
        track = verified()
        self.assertEqual(self.service.update({1: track}, self.packet, 10, True), [])
        self.assertEqual(track.state, State.COOLDOWN)
        self.assertEqual(track.cooldown_remaining, 25)
        track.last_seen = track.last_recognized = track.last_flow_at = 36
        track.verified_presence = 3.1  # Evidence gathered during cooldown must not bypass verification.
        packet = replace(self.packet, captured_at=36, wall_time=1026)
        self.assertEqual(self.service.update({1: track}, packet, 36, True), [])
        self.assertEqual(track.verified_presence, 0)

    def test_lost_track_still_receives_employee_cooldown(self):
        track = verified()
        job = self.service.update({1: track}, self.packet, 10, True)[0]
        self.worker.results.put(SaveResult(job, "saved", 1000))
        self.service.poll({}, 11, 1001)
        self.assertAlmostEqual(self.service.cooldowns.remaining("E001", 11), 29)
        self.assertEqual(self.service.pending, {})

    def test_queue_full_and_storage_startup_failure(self):
        self.worker.accepts = False
        track = verified()
        self.assertEqual(self.service.update({1: track}, self.packet, 10, True), [])
        self.assertEqual(track.state, State.ERROR)
        self.assertEqual(self.service.pending, {})
        self.worker.startup.put((None, "database unavailable"))
        self.service.poll({}, 10, 1000)
        self.assertFalse(self.service.ready)
        self.assertEqual(self.service.update({1: verified()}, self.packet, 10, True), [])

    def test_unknown_replacing_cooldown_track_is_not_shown_as_recorded(self):
        track = verified()
        track.identity_valid = False
        track.state = State.UNKNOWN
        self.service.cooldowns.synchronize("E001", 1000, 10, 1000)
        self.service.update({1: track}, self.packet, 10, True)
        self.assertEqual(track.state, State.UNKNOWN)
        self.assertEqual(self.worker.jobs, [])

    def test_sources_and_validation(self):
        self.assertEqual(parse_source("0"), 0)
        self.assertEqual(parse_source("1"), 1)
        self.assertEqual(parse_source("rtsp://camera"), "rtsp://camera")
        with self.assertRaises(ValueError):
            replace(self.config, detection_interval=0)
