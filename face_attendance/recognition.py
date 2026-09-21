"""HOG detection and face encoding run only on the recognition worker."""
import logging
import threading
import time

import cv2
import numpy as np

from .channels import LatestValue
from .enrollment import FACE_BACKEND_LOCK
from .geometry import association_cost, iou
from .models import Detection, RecognitionResult


class RecognitionService:
    def __init__(self, config, catalog, backend=None):
        if backend is None:
            import face_recognition as backend
        self.backend, self.config, self.catalog = backend, config, catalog
        self.employee_indices = {}
        for index, employee in enumerate(catalog.employees):
            self.employee_indices.setdefault(employee.employee_id, []).append(index)

    def match(self, encoding):
        # compare_faces also computes distances internally; compute them just once.
        with FACE_BACKEND_LOCK:
            distances = self.backend.face_distance(self.catalog.encodings, encoding)
        ranked = sorted((float(np.min(distances[indices])), indices[0])
                        for indices in self.employee_indices.values())
        best, index = ranked[0]
        if best > self.config.face_tolerance:
            return None, best
        if len(ranked) > 1 and ranked[1][0] - best < self.config.identity_margin:
            return None, best
        return self.catalog.employees[index], best

    def process(self, request):
        started = time.monotonic()
        cfg, packet = self.config, request.packet
        height, width = packet.frame.shape[:2]
        small = cv2.resize(packet.frame, (max(1, round(width * cfg.detection_scale)),
                                          max(1, round(height * cfg.detection_scale))),
                           interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        with FACE_BACKEND_LOCK:
            boxes = self.backend.face_locations(rgb, model="hog")
        sx, sy = width / small.shape[1], height / small.shape[0]
        full_boxes = [(t * sy, r * sx, b * sy, l * sx) for t, r, b, l in boxes]
        hints, encode_indices, used_hints = {}, [], set()
        for index, box in enumerate(full_boxes):
            candidates = sorted((association_cost(box, hint.bounding_box), hint.track_id, hint)
                                for hint in request.hints)
            hint = None
            overlapping = any(index != j and iou(box, other) > 0.2
                              for j, other in enumerate(full_boxes))
            if (candidates and candidates[0][0] < 0.85 and not overlapping
                    and (len(candidates) == 1 or candidates[1][0] - candidates[0][0] > 0.2)):
                candidate = candidates[0][2]
                if candidate.track_id not in used_hints:
                    hint = candidate
                    used_hints.add(hint.track_id)
            hints[index] = hint
            due = (hint is None or not hint.stable
                   or packet.captured_at - hint.last_recognized >= cfg.stable_recheck_sec)
            # Unconfirmed faces are retried at a bounded rate; new/ambiguous faces always encode.
            if hint is not None and not hint.stable:
                due = packet.sequence - hint.last_encoded_sequence >= cfg.recognition_interval
            if due:
                encode_indices.append(index)
        encodings = []
        if encode_indices:
            with FACE_BACKEND_LOCK:
                encodings = self.backend.face_encodings(rgb, [boxes[i] for i in encode_indices])
        matches = {}
        for index, encoding in zip(encode_indices, encodings):
            matches[index] = self.match(encoding)
        detections = []
        for index, box in enumerate(full_boxes):
            employee, distance = matches.get(index, (None, None))
            hint = hints[index]
            detections.append(Detection(box, employee, distance, index in encode_indices,
                                        hint.track_id if hint else None))
        return RecognitionResult(packet, tuple(detections), time.monotonic() - started)


class RecognitionWorker:
    def __init__(self, service):
        self.service = service
        self.requests, self.results = LatestValue(), LatestValue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="recognition", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            self.requests.ready.wait(0.1)
            request = self.requests.take()
            if request is None or self.stop_event.is_set():
                continue
            # Do not spend CPU encoding a request that has already expired in the mailbox.
            if time.monotonic() - request.packet.captured_at > self.service.config.max_result_age_sec:
                continue
            try:
                result = self.service.process(request)
            except Exception as exc:
                logging.exception("Face recognition failed")
                result = RecognitionResult(request.packet, (), 0, str(exc))
            self.results.put(result)

    def close(self):
        self.stop_event.set()
        self.requests.ready.set()
        if self.thread.ident is not None:
            self.thread.join(5)
        if self.thread.is_alive():
            logging.error("Recognition backend has not returned; shutting down its daemon thread with the process")
