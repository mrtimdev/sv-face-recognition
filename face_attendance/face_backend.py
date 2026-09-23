"""OpenCV YuNet + SFace identity matching and MediaPipe Face Mesh landmarks.

All models run locally with OpenCV DNN. No dlib or face_recognition dependency.
The small compatibility API keeps camera/enrollment workers backend-agnostic.
"""
import hashlib
import threading
from pathlib import Path

import cv2
import numpy as np

from .geometry import iou

MODEL_ID = 'opencv-sface-2021dec-v1'
MODEL_DIR = Path(__file__).resolve().parent / 'assets' / 'recognition'
MODEL_HASHES = {
    'yunet.onnx': '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4',
    'sface.onnx': '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79',
    'face_mesh.onnx': '3ca77cf59c18e4da0eccb46695bf604683fa564253e3385892981a5c274fb10f',
}


class OpenCVFaceBackend:
    model_id = MODEL_ID

    def __init__(self, model_dir=MODEL_DIR):
        paths = {}
        for name, digest in MODEL_HASHES.items():
            path = Path(model_dir) / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f'Recognition model damaged: {name}; reinstall application')
            paths[name] = str(path)
        self.detector = cv2.FaceDetectorYN.create(paths['yunet.onnx'], '', (320, 320), .8, .3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(paths['sface.onnx'], '')
        self.mesh = cv2.dnn.readNetFromONNX(paths['face_mesh.onnx'])
        self.mesh.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.mesh.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self._image = None
        self._rows = np.empty((0, 15), np.float32)

    @staticmethod
    def _box(row):
        x, y, w, h = map(float, row[:4])
        return y, x + w, y + h, x

    def face_locations(self, image, model=None):
        """RGB image in; top/right/bottom/left boxes out."""
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, rows = self.detector.detect(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        self._image = image
        self._rows = rows if rows is not None else np.empty((0, 15), np.float32)
        return [self._box(row) for row in self._rows]

    def promote(self, image, sx, sy):
        """Reuse YuNet's five points from the detection image at full resolution."""
        rows = self._rows.copy()
        rows[:, [0, 2, 4, 6, 8, 10, 12]] *= sx
        rows[:, [1, 3, 5, 7, 9, 11, 13]] *= sy
        self._rows, self._image = rows, image

    def _row(self, image, box):
        if self._image is not image:
            self.face_locations(image)
        if not len(self._rows):
            raise ValueError('Face alignment unavailable')
        overlaps = [iou(box, self._box(row)) for row in self._rows]
        best = int(np.argmax(overlaps))
        if overlaps[best] < .5:
            raise ValueError('Face alignment does not match the detection')
        return self._rows[best]

    def face_encodings(self, image, boxes):
        bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        result = []
        for box in boxes:
            aligned = self.recognizer.alignCrop(bgr, self._row(image, box))
            vector = self.recognizer.feature(aligned).reshape(-1).astype(np.float32)
            norm = float(np.linalg.norm(vector))
            if vector.shape != (128,) or not np.all(np.isfinite(vector)) or norm < 1e-8:
                raise ValueError('Invalid SFace embedding')
            result.append(vector / norm)
        return result

    @staticmethod
    def face_distance(known, candidate):
        """Cosine distance: smaller is closer. Not the old dlib Euclidean metric."""
        known = np.asarray(known, dtype=float)
        candidate = np.asarray(candidate, dtype=float)
        norms = np.linalg.norm(known, axis=1)
        norm = np.linalg.norm(candidate)
        distances = np.full(len(known), np.inf)
        valid = (norms > 1e-8) & np.isfinite(norms)
        if norm > 1e-8 and np.isfinite(norm):
            distances[valid] = np.clip(1 - (known[valid] @ candidate) / (norms[valid] * norm), 0, 2)
        return distances

    def face_landmarks(self, image, boxes, model=None):
        result = []
        for box in boxes:
            row = self._row(image, box)
            x, y, w, h = map(float, row[:4])
            # A partly visible face can produce plausible but wrong eyelids.
            # Never let these initialize the adaptive expression baseline.
            height, width = image.shape[:2]
            if x < 0 or y < 0 or x + w > width or y + h > height:
                result.append({})
                continue
            eyes = row[4:8].reshape(2, 2)
            eyes = eyes[np.argsort(eyes[:, 0])]
            axis = eyes[1] - eyes[0]
            angle = float(np.degrees(np.arctan2(axis[1], axis[0])))
            center = (x + w / 2, y + h / 2)
            side = max(w, h) * 1.5
            if side <= 0:
                result.append({})
                continue
            matrix = cv2.getRotationMatrix2D(center, angle, 192 / side)
            matrix[:, 2] += np.array([96 - center[0], 96 - center[1]])
            crop = cv2.warpAffine(image, matrix, (192, 192))
            blob = cv2.dnn.blobFromImage(crop, 1 / 255., swapRB=False)
            self.mesh.setInput(blob)
            points, score = self.mesh.forward(['landmarks', 'score'])
            # The score is a logit: 0 corresponds to probability .5.
            if not np.all(np.isfinite(points)) or not np.isfinite(score).all() or float(score[0, 0]) < 0:
                result.append({})
                continue
            inverse = cv2.invertAffineTransform(matrix)
            xy = points[0, :, :2] @ inverse[:, :2].T + inverse[:, 2]
            select = lambda indices: [tuple(map(float, xy[i])) for i in indices]
            # Six-point eyelid contours plus lip positions expected by LivenessChecker.
            top = select([61, 40, 37, 0, 267, 270, 291, 308, 312, 13, 82, 78])
            bottom = select([291, 321, 314, 17, 84, 91, 61, 78, 87, 14, 317, 308])
            result.append({'left_eye': select([33, 160, 158, 133, 153, 144]),
                           'right_eye': select([362, 385, 387, 263, 373, 380]),
                           'nose_bridge': select([168, 6, 197, 1]),
                           'top_lip': top, 'bottom_lip': bottom})
        return result


_local = threading.local()


def get_backend():
    if not hasattr(_local, 'backend'):
        _local.backend = OpenCVFaceBackend()
    return _local.backend
