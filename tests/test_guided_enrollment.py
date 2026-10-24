import numpy as np
import pytest
from face_attendance.guided_enrollment import Candidate, PosePolicy, StableWindow


def candidate(pose=(0,0,0),quality=1):
    return Candidate(np.eye(1,128,dtype=np.float32)[0],pose,quality)


def test_stability_requires_continuous_window_and_selects_best_capture():
    window = StableWindow()
    for i in range(5):
        assert not window.observe(i*.2,candidate(quality=10 if i == 2 else 1))
    assert window.observe(1.0,candidate())
    assert window.best.quality == 10
    assert window.count == 6


def test_gaps_motion_and_reversed_time_reset_continuity():
    window = StableWindow()
    window.observe(0,candidate())
    window.observe(.2,candidate())
    window.observe(1.1,candidate())
    assert window.count == 1
    window.observe(1.3,candidate((8,0,0)))
    assert window.count == 1
    window.observe(1.2,candidate((8,0,0)))
    assert window.count == 1
    assert window.fraction == 0


def test_bounded_poses_reject_extreme_rotations_and_invalid_values():
    policy = PosePolicy()
    assert policy.classify((80,0,0)) is None
    assert policy.classify((0,0,20)) is None
    assert policy.classify((float('nan'),0,0)) is None
    assert not policy.contains('LEFT',(12,0,0))
    assert policy.contains('LEFT',(-13,0,0),margin=3)
    assert not policy.contains('LEFT',(-13,0,0))


def test_exit_margin_frames_never_replace_strict_pose_template():
    window = StableWindow()
    assert not window.observe(0,candidate(quality=1))
    for i in range(1,6):
        ready = window.observe(i*.2,candidate((0,3,0),quality=100),eligible=False)
    assert ready
    assert window.best.pose == (0,0,0)
    # A gap resets the strict candidate too; relaxed frames alone cannot enroll.
    for i in range(6):
        assert not window.observe(2+i*.2,candidate(),eligible=False)
    assert window.best is None
