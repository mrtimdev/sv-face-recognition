"""Head-pose guidance and bounded continuous evidence for mobile enrollment."""
from dataclasses import dataclass
import math
import os

import numpy as np

ANGLES = ('FRONT', 'LEFT', 'RIGHT', 'UP', 'DOWN', 'LEFT_DIAGONAL', 'RIGHT_DIAGONAL')


class PosePolicy:
    """UniFace degrees: yaw negative=left, pitch negative=up.

    All thresholds are application policies requiring device calibration.
    """
    def __init__(self):
        self.front = float(os.getenv('SV_POSE_FRONT_MAX', '10'))
        self.turn_min = float(os.getenv('SV_POSE_THRESHOLD', '15'))
        self.yaw_max = float(os.getenv('SV_POSE_YAW_MAX', '35'))
        self.pitch_max = float(os.getenv('SV_POSE_PITCH_MAX', '30'))
        self.roll_max = float(os.getenv('SV_POSE_ROLL_MAX', '12'))
        self.yaw_sign = float(os.getenv('SV_POSE_YAW_SIGN', '1'))
        self.pitch_sign = float(os.getenv('SV_POSE_PITCH_SIGN', '1'))
        if not (0 < self.front < self.turn_min < min(self.yaw_max, self.pitch_max) < 60
                and 0 < self.roll_max < 30 and abs(self.yaw_sign) == 1 and abs(self.pitch_sign) == 1):
            raise ValueError('Invalid enrollment pose configuration')

    def normalize(self, yaw, pitch, roll):
        values = (float(yaw)*self.yaw_sign, float(pitch)*self.pitch_sign, float(roll))
        if not all(math.isfinite(v) for v in values):
            raise ValueError('Head pose unavailable')
        return values

    def contains(self, angle, pose, margin=0):
        yaw, pitch, roll = pose
        if not all(math.isfinite(v) for v in pose) or abs(roll) > self.roll_max + margin:
            return False
        front = lambda value: abs(value) <= self.front + margin
        turn = lambda value, limit: self.turn_min-margin <= value <= limit+margin
        if angle == 'FRONT':
            return front(yaw) and front(pitch)
        if angle == 'LEFT':
            return turn(-yaw,self.yaw_max) and front(pitch)
        if angle == 'RIGHT':
            return turn(yaw,self.yaw_max) and front(pitch)
        if angle == 'UP':
            return front(yaw) and turn(-pitch,self.pitch_max)
        if angle == 'DOWN':
            return front(yaw) and turn(pitch,self.pitch_max)
        if angle == 'LEFT_DIAGONAL':
            return turn(-yaw,self.yaw_max) and turn(-pitch,self.pitch_max)
        if angle == 'RIGHT_DIAGONAL':
            return turn(yaw,self.yaw_max) and turn(-pitch,self.pitch_max)
        return False

    def classify(self, pose):
        return next((a for a in ANGLES if self.contains(a,pose)),None)


@dataclass
class Candidate:
    vector: np.ndarray
    pose: tuple
    quality: float


class StableWindow:
    def __init__(self, min_span=1.0, min_frames=6, max_gap=.75, max_variation=6):
        self.min_span, self.min_frames = min_span, min_frames
        self.max_gap, self.max_variation = max_gap, max_variation
        self.reset()

    def reset(self):
        self.first = self.last = None
        self.count = 0
        self.low = self.high = self.smoothed = None
        self.best = None

    def observe(self, at, candidate, *, eligible=True):
        pose = np.asarray(candidate.pose,dtype=float)
        if not np.isfinite(pose).all():
            self.reset()
            return False
        if self.last is not None and (at <= self.last or at-self.last > self.max_gap):
            self.reset()
        if self.count and np.max(np.maximum(self.high,pose)-np.minimum(self.low,pose)) > self.max_variation:
            self.reset()
        if not self.count:
            self.first = at
            self.low = self.high = self.smoothed = pose.copy()
        else:
            self.low = np.minimum(self.low,pose)
            self.high = np.maximum(self.high,pose)
            self.smoothed = .4*pose + .6*self.smoothed
        self.last = at
        self.count += 1
        # Exit-margin frames maintain continuity but cannot supply a template.
        if eligible and (self.best is None or candidate.quality > self.best.quality):
            self.best = candidate
        return self.best is not None and self.count >= self.min_frames and at-self.first >= self.min_span

    @property
    def fraction(self):
        if not self.count:
            return 0.0
        return min(1.0,self.count/self.min_frames,(self.last-self.first)/self.min_span)


class UniFacePoseEstimator:
    """Lazy model ownership; caller serializes inference. Never changes SFace."""
    def __init__(self):
        self.model = None

    def estimate(self, bgr_crop):
        if self.model is None:
            try:
                from .config import ROOT
                os.environ.setdefault('UNIFACE_CACHE_DIR',str(ROOT / '.cache' / 'uniface'))
                from uniface.headpose import HeadPose
                from uniface.constants import HeadPoseWeights
                self.model = HeadPose(model_name=HeadPoseWeights.RESNET18,
                                      providers=['CPUExecutionProvider'])
            except Exception as exc:
                raise RuntimeError('UniFace head-pose model unavailable. Install requirements-mobile.txt and prepare the model with scripts/run_mobile_api.py --prepare-model.') from exc
        result = self.model.estimate(bgr_crop)
        return result.yaw,result.pitch,result.roll
