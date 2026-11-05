"""Resource bounds and authenticated stream lifecycle regressions."""
import asyncio
import importlib

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect

from tests.test_mobile_api import setup, streaming_setup
from face_attendance.mobile_api import _RequestSizeLimit, _SignInLimiter


def test_sign_in_limiter_bounds_hosts_and_expires_old_entries(monkeypatch):
    module = importlib.import_module('face_attendance.mobile_api')
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    limiter = _SignInLimiter(max_hosts=2)
    limiter.check('host1', 'admin')
    limiter.check('host2', 'admin')
    with pytest.raises(HTTPException) as error:
        limiter.check('host3', 'admin')
    assert error.value.status_code == 429
    assert len(limiter.hosts) == 2
    clock[0] += 301
    limiter.check('host3', 'admin')
    assert list(limiter.hosts) == ['host3']


def test_rotating_usernames_cannot_bypass_sign_in_rate_limit(setup, monkeypatch):
    module = importlib.import_module('face_attendance.mobile_api')
    monkeypatch.setattr(module.UserRepository, 'authenticate', lambda *args: None)
    client = setup[1]
    for index in range(30):
        assert client.post('/auth/sign-in', json={'username':f'user{index}', 'password':'wrong'}).status_code == 401
    assert client.post('/auth/sign-in', json={'username':'new-name', 'password':'wrong'}).status_code == 429


def test_http_body_limit_rejects_oversized_json_before_parsing(setup):
    client = setup[1]
    result = client.post('/auth/sign-in', content=b' ' * 9000,
                         headers={'Content-Type':'application/json'})
    assert result.status_code == 413


def test_http_body_limit_counts_chunks_without_content_length():
    called = []
    sent = []
    messages = iter([
        {'type':'http.request', 'body':b'x' * 5000, 'more_body':True},
        {'type':'http.request', 'body':b'x' * 5000, 'more_body':False},
    ])
    async def endpoint(scope, receive, send):
        called.append(True)
    async def receive():
        return next(messages)
    async def send(message):
        sent.append(message)
    asyncio.run(_RequestSizeLimit(endpoint)({'type':'http', 'path':'/auth/sign-in'}, receive, send))
    assert not called
    assert sent[0]['status'] == 413


def test_active_enrollment_limit_preserves_resume_and_cancel(setup, monkeypatch):
    module = importlib.import_module('face_attendance.mobile_api')
    monkeypatch.setattr(module, 'MAX_ACTIVE_ENROLLMENTS', 1)
    _, client, headers, _ = setup
    payload = {'employeeId':'E1', 'name':'First'}
    first = client.post('/enrollments', headers=headers, json=payload).json()
    assert client.post('/enrollments', headers=headers, json=payload).json()['enrollmentId'] == first['enrollmentId']
    assert client.post('/enrollments', headers=headers, json={'employeeId':'E2', 'name':'Second'}).status_code == 429
    assert client.delete('/enrollments/' + first['enrollmentId'], headers=headers).status_code == 200
    assert client.post('/enrollments', headers=headers, json={'employeeId':'E2', 'name':'Second'}).status_code == 200


def test_reset_rechecks_token_revocation(setup):
    from face_attendance.users import UserRepository
    from face_attendance.settings import Settings
    path, client, _, _ = setup
    settings = Settings()
    settings.db_path = str(path / 'attendance.db')
    UserRepository(settings).create_user('admin', 'Admin', '', '', 'correct-password', role='admin')
    token = client.post('/auth/sign-in', json={'username':'admin', 'password':'correct-password'}).json()['accessToken']
    headers = {'Authorization':'Bearer ' + token}
    eid = client.post('/enrollments', headers=headers, json={'employeeId':'E1', 'name':'Alice'}).json()['enrollmentId']
    with client.websocket_connect('/enrollments/' + eid + '/stream') as ws:
        ws.send_json({'accessToken':token})
        assert ws.receive_json()['type'] == 'ready'
        assert client.post('/auth/sign-out', headers=headers).status_code == 200
        ws.send_json({'type':'reset'})
        assert ws.receive_json()['status'] == 401


def test_stream_connection_capacity_released_after_disconnect(streaming_setup, monkeypatch):
    module = importlib.import_module('face_attendance.mobile_api')
    monkeypatch.setattr(module, 'MAX_STREAM_CONNECTIONS', 1)
    client, eid, _ = streaming_setup
    endpoint = '/enrollments/' + eid + '/stream'
    with client.websocket_connect(endpoint) as first:
        first.send_json({'accessToken':'t' * 32})
        assert first.receive_json()['type'] == 'ready'
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(endpoint):
                pass
    with client.websocket_connect(endpoint) as replacement:
        replacement.send_json({'accessToken':'t' * 32})
        assert replacement.receive_json()['type'] == 'ready'


def test_cancelled_enrollment_cannot_open_stream(streaming_setup):
    client, eid, _ = streaming_setup
    client.delete('/enrollments/' + eid, headers={'Authorization':'Bearer ' + 't' * 32})
    with client.websocket_connect('/enrollments/' + eid + '/stream') as ws:
        ws.send_json({'accessToken':'t' * 32})
        assert ws.receive_json()['status'] == 409


def test_oversized_stream_frame_rejected_before_image_decode(streaming_setup, monkeypatch):
    module = importlib.import_module('face_attendance.mobile_api')
    decoded = []
    monkeypatch.setattr(module.cv2, 'imdecode', lambda *args: decoded.append(True))
    client, eid, _ = streaming_setup
    with client.websocket_connect('/enrollments/' + eid + '/stream') as ws:
        ws.send_json({'accessToken':'t' * 32})
        ws.receive_json()
        ws.send_json({'type':'frame', 'frameId':0, 'capturedAtMs':0, 'targetAngle':'FRONT'})
        ws.send_bytes(b'x' * (module.MAX_IMAGE_BYTES + 1))
        assert ws.receive_json()['status'] == 422
    assert not decoded


def test_non_ascii_stream_token_fails_authentication_cleanly(streaming_setup):
    client, eid, _ = streaming_setup
    with client.websocket_connect('/enrollments/' + eid + '/stream') as ws:
        ws.send_json({'accessToken':'អ' * 32})
        assert ws.receive_json()['status'] == 401


def test_cancel_is_idempotent_and_does_not_grow_audit(setup):
    import sqlite3
    path, client, headers, _ = setup
    eid = client.post('/enrollments', headers=headers, json={'employeeId':'E1', 'name':'Alice'}).json()['enrollmentId']
    for _ in range(3):
        assert client.delete('/enrollments/' + eid, headers=headers).json()['status'] == 'CANCELLED'
    with sqlite3.connect(path / 'mobile_enrollment.db') as db:
        assert db.execute("SELECT count(*) FROM enrollment_audit WHERE enrollment_id=? AND event='CANCEL'", (eid,)).fetchone()[0] == 1


def test_oversized_stream_header_disconnects_and_releases_session(streaming_setup):
    client, eid, _ = streaming_setup
    endpoint = '/enrollments/' + eid + '/stream'
    with client.websocket_connect(endpoint) as ws:
        ws.send_json({'accessToken':'t' * 32})
        assert ws.receive_json()['type'] == 'ready'
        ws.send_text(' ' * 4097)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with client.websocket_connect(endpoint) as replacement:
        replacement.send_json({'accessToken':'t' * 32})
        assert replacement.receive_json()['type'] == 'ready'


def test_jpeg_dimensions_are_bounded_before_decompression(streaming_setup, monkeypatch):
    module = importlib.import_module('face_attendance.mobile_api')
    client, eid, frame = streaming_setup
    data = bytearray(frame(0))
    sof = data.index(b'\xff\xc0')
    data[sof+5:sof+7] = (60000).to_bytes(2, 'big')
    decoded = []
    monkeypatch.setattr(module.cv2, 'imdecode', lambda *args: decoded.append(True))
    with client.websocket_connect('/enrollments/' + eid + '/stream') as ws:
        ws.send_json({'accessToken':'t' * 32})
        ws.receive_json()
        ws.send_json({'type':'frame', 'frameId':0, 'capturedAtMs':0, 'targetAngle':'FRONT'})
        ws.send_bytes(data)
        result = ws.receive_json()
        assert result['completedAngles'] == 0
        assert '2048' in result['rejectionReason']
    assert not decoded


def test_legacy_password_upgrades_without_changing_mobile_session_identity(setup):
    import hashlib
    import sqlite3
    from face_attendance.users import UserRepository, PASSWORD_ITERATIONS
    from face_attendance.settings import Settings
    path, client, _, _ = setup
    settings = Settings()
    settings.db_path = str(path / 'attendance.db')
    repo = UserRepository(settings)
    user = repo.create_user('admin', 'Admin', '', '', 'correct-password', role='admin')
    salt = b'a' * 16
    legacy = salt.hex() + ':' + hashlib.pbkdf2_hmac('sha256', b'correct-password', salt, 100_000).hex()
    with sqlite3.connect(settings.db_path) as db:
        db.execute('UPDATE users SET password_hash=? WHERE uuid=?', (legacy, user.uuid))
    result = client.post('/auth/sign-in', json={'username':'admin', 'password':'correct-password'})
    assert result.status_code == 200
    assert repo.get_user(user.uuid).password_hash.startswith(f'pbkdf2_sha256${PASSWORD_ITERATIONS}$')
    headers = {'Authorization':'Bearer ' + result.json()['accessToken']}
    assert client.get('/auth/me', headers=headers).json()['id'] == user.uuid
    eid = client.post('/enrollments', headers=headers, json={'employeeId':'E1', 'name':'Alice'}).json()['enrollmentId']
    with client.websocket_connect('/enrollments/' + eid + '/stream') as ws:
        ws.send_json({'accessToken':result.json()['accessToken']})
        assert ws.receive_json()['type'] == 'ready'
        ws.send_json({'type':'reset'})
        assert ws.receive_json()['type'] == 'reset'
