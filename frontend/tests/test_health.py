from face_attendance.health import healthy


def test_process_alive_is_not_sufficient_for_attendance_health():
    payload = dict(updated_at=100, status='CONNECTED', paused=False, storage_ready=True,
                   workers_alive=True, recognition_ok=True, frame_age=.1, queue=0, queue_max=8)
    assert healthy(payload, now=105)
    for change in ({'updated_at': 0}, {'paused': True}, {'frame_age': 5},
                   {'storage_ready': False}, {'workers_alive': False},
                   {'queue': 8}, {'recognition_ok': False}):
        assert not healthy(payload | change, now=105)
