"""Application lifecycle and coordination; business rules live in services."""
import logging
import time

import cv2
import numpy as np

from .attendance import AttendanceService
from .camera import CameraManager
from .catalog import FaceCatalog
from .models import RecognitionRequest
from .persistence import PersistenceWorker
from .recognition import RecognitionService, RecognitionWorker
from .tracking import FaceTracker
from .ui import UIRenderer


class AttendanceApplication:
    def __init__(self, config):
        self.config = config
        self.catalog = FaceCatalog(config.encodings_path, config.employees_path)
        cv2.setNumThreads(config.opencv_threads)
        self.camera = CameraManager(config)
        self.recognition = RecognitionWorker(RecognitionService(config, self.catalog))
        self.persistence = PersistenceWorker(config, self.catalog.employee_map)
        self.tracker = FaceTracker(config)
        self.attendance = AttendanceService(config, self.persistence)
        self.renderer = UIRenderer(config)

    def run(self):
        cfg = self.config
        logging.info("Loaded %s face samples for %s employees", len(self.catalog.encodings), self.catalog.enrolled_count)
        for first, second in self.catalog.duplicate_identities():
            logging.warning("Identical enrollment for %r and %r; ambiguous matches will be rejected. "
                            "Correct enrollment or map aliases to the same employee ID.", first, second)
        logging.info("Database: %s | Captures: %s | Press Q to exit", cfg.db_path, cfg.capture_dir)
        frame = np.full((cfg.camera_height, cfg.camera_width, 3), (32, 29, 25), np.uint8)
        sequence, submitted, generation = -1, -cfg.detection_interval, None
        preview_fps, count, fps_since = 0.0, 0, time.monotonic()
        recognition_error = ""
        window = "Face ID - Employee Attendance"
        try:
            self.persistence.start()
            self.recognition.start()
            self.camera.start()
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            while True:
                started = now = time.monotonic()
                wall_time = time.time()
                packet = self.camera.frames.get()
                status = self.camera.status(now)
                new_frame = packet is not None and packet.sequence != sequence and status == "CONNECTED"
                if new_frame:
                    if packet.generation != generation:
                        submitted, generation = -cfg.detection_interval, packet.generation
                    sequence, frame = packet.sequence, packet.frame
                    self.tracker.advance(packet)
                    count += 1
                if status != "CONNECTED":
                    self.tracker.suspend()
                result = self.recognition.results.take()
                if result is not None and status == "CONNECTED":
                    if self.tracker.apply(result, now):
                        recognition_error = ""
                    elif result.error:
                        recognition_error = result.error
                if new_frame and sequence - submitted >= cfg.detection_interval:
                    self.recognition.requests.put(RecognitionRequest(packet, self.tracker.hints()))
                    submitted = sequence
                for kind, saved in self.attendance.poll(self.tracker.tracks, now, wall_time):
                    self.renderer.animations.result(kind, saved, now)
                for job in self.attendance.update(self.tracker.tracks, packet, now, status == "CONNECTED"):
                    self.renderer.animations.capture(job, now)
                self.attendance.observe(self.tracker.tracks, now, wall_time)
                elapsed = now - fps_since
                if elapsed >= 1.0:
                    preview_fps, count, fps_since = count / elapsed, 0, now
                image = self.renderer.render(frame, self.tracker.tracks, now, wall_time, status,
                                             self.camera.fps, preview_fps, self.catalog.enrolled_count,
                                             self.attendance, recognition_error)
                cv2.imshow(window, image)
                # GUI event polling also paces rendering. No sleep or I/O in this loop.
                delay_ms = max(1, int((1 / cfg.target_fps - (time.monotonic() - started)) * 1000))
                key = cv2.waitKey(delay_ms) & 0xFF
                if key in (ord("q"), 27) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            self.camera.close()
            self.recognition.close()
            self.persistence.close()
            cv2.destroyAllWindows()
