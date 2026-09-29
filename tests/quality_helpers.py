"""Explicit passing quality for legacy fixtures testing identity/PAD in isolation."""
from face_attendance.quality import QualityResult


class PassingQuality:
    def inspect(self, frame, box):
        return QualityResult(True)

    def landmarks(self, result, landmarks, box):
        return result
