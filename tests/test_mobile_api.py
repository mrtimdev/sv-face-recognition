import importlib
import numpy as np
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from face_attendance.mobile_api import pose_angle, create_app, ANGLES

@pytest.mark.parametrize('yaw,pitch,expected', [(0,0,'FRONT'),(-25,0,'LEFT'),(25,0,'RIGHT'),(0,-25,'UP'),(0,25,'DOWN'),(-25,-25,'LEFT_DIAGONAL'),(25,-25,'RIGHT_DIAGONAL')])
def test_pose(yaw,pitch,expected):
    assert pose_angle(yaw,pitch,0) == expected
    assert pose_angle(yaw,pitch,30) is None

@pytest.fixture
def setup(tmp_path,monkeypatch):
    module = importlib.import_module('face_attendance.mobile_api')
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setenv('SV_MOBILE_TOKEN','t'*32)
    key = Fernet.generate_key()
    monkeypatch.setenv('SV_ENROLLMENT_KEY',key.decode())
    return tmp_path, TestClient(create_app()), {'Authorization':'Bearer '+'t'*32}, key

def test_auth_progress_resume_and_incomplete_completion(setup):
    path,client,headers,key = setup
    assert client.post('/enrollments',json={'employeeId':'E1','name':'Alice'}).status_code == 401
    started = client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()
    eid = started['enrollmentId']
    assert started['completedAngles'] == 0 and started['nextAngle'] == 'FRONT'
    assert client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()['enrollmentId'] == eid
    assert TestClient(create_app()).get('/enrollments/'+eid,headers=headers).json() == started
    assert client.post('/enrollments/'+eid+'/complete',headers=headers).status_code == 409
    rejected = client.post('/enrollments/'+eid+'/captures',headers=headers,json={'image':'bad'}).json()
    assert rejected['rejectionReason'] and rejected['completedAngles'] == 0
    assert client.delete('/enrollments/'+eid,headers=headers).json()['status'] == 'CANCELLED'

def test_complete_publishes_all_templates_and_is_idempotent(setup):
    import sqlite3
    from face_attendance.template_store import read_templates
    path,client,headers,key = setup
    eid = client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()['enrollmentId']
    cipher = Fernet(key)
    with sqlite3.connect(path/'mobile_enrollment.db') as db:
        for angle in ANGLES:
            vector = np.ones(128,dtype=np.float32)/np.sqrt(128)
            db.execute('INSERT INTO face_enrollment_samples VALUES(?,?,?,?,?,?)',(eid,angle,cipher.encrypt(vector.tobytes()),0,0,0))
    result = client.post('/enrollments/'+eid+'/complete',headers=headers)
    assert result.status_code == 200 and result.json()['status'] == 'COMPLETED'
    assert len(read_templates(path/'encodings_sface.pickle')['Alice']) == 7
    assert client.post('/enrollments/'+eid+'/complete',headers=headers).json()['completedAngles'] == 7
    assert client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).status_code == 409
    assert client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice','reenroll':True}).status_code == 200

def test_failed_reenrollment_preserves_old_profile(setup, monkeypatch):
    from face_attendance.enrollment import write_encodings, write_employee
    from face_attendance.template_store import read_templates
    module = importlib.import_module('face_attendance.mobile_api')
    path,client,headers,key = setup
    original = np.eye(1,128,dtype=np.float32)[0]
    write_encodings(path/'encodings_sface.pickle',{'Alice':[original]})
    write_employee(path/'employees.json','Alice','E1')
    eid = client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice','reenroll':True}).json()['enrollmentId']
    assert client.post('/enrollments/'+eid+'/complete',headers=headers).status_code == 409
    np.testing.assert_array_equal(read_templates(path/'encodings_sface.pickle')['Alice'][0],original)
    client.delete('/enrollments/'+eid,headers=headers)
    np.testing.assert_array_equal(read_templates(path/'encodings_sface.pickle')['Alice'][0],original)


def test_model_mismatch_and_relocated_database(setup, monkeypatch):
    import sqlite3
    path,client,headers,key = setup
    relocated = path/'storage'/'sessions.db'
    monkeypatch.setenv('SV_ENROLLMENT_DB_PATH',str(relocated))
    other = TestClient(create_app())
    eid = other.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()['enrollmentId']
    assert relocated.exists()
    assert client.get('/enrollments/'+eid,headers=headers).status_code == 404
    with sqlite3.connect(relocated) as db:
        db.execute("UPDATE face_enrollments SET model_version='different-model' WHERE id=?",(eid,))
    assert other.get('/enrollments/'+eid,headers=headers).status_code == 409

def test_admin_sign_in_logout_and_live_permissions(setup):
    from face_attendance.users import UserRepository
    from face_attendance.settings import Settings
    path,client,headers,key = setup
    settings = Settings()
    settings.db_path = str(path/'attendance.db')
    repo = UserRepository(settings)
    admin = repo.create_user('admin','Enrollment Admin','','','correct-password',role='admin')
    repo.create_user('employee','Ordinary User','','','correct-password',role='user')
    assert client.post('/auth/sign-in',json={'username':'admin','password':'wrong'}).status_code == 401
    assert client.post('/auth/sign-in',json={'username':'employee','password':'correct-password'}).status_code == 403
    result = client.post('/auth/sign-in',json={'username':'admin','password':'correct-password'})
    assert result.status_code == 200
    session_headers = {'Authorization':'Bearer '+result.json()['accessToken']}
    assert client.get('/auth/me',headers=session_headers).json()['username'] == 'admin'
    assert client.get('/employees',headers=session_headers).status_code == 200
    repo.update_user(admin.uuid,disabled=True)
    assert client.get('/employees',headers=session_headers).status_code == 403
    repo.update_user(admin.uuid,disabled=False)
    assert client.post('/auth/sign-out',headers=session_headers).status_code == 200
    assert client.get('/employees',headers=session_headers).status_code == 401

def test_employee_directory_and_login_rate_limit(setup):
    path,client,headers,key = setup
    started = client.post('/enrollments',headers=headers,json={'employeeId':'NEW-1','name':'New Employee'}).json()
    rows = client.get('/employees',headers=headers).json()['employees']
    assert rows[0]['employeeId'] == 'NEW-1'
    assert rows[0]['enrollmentId'] == started['enrollmentId']
    for _ in range(5):
        assert client.post('/auth/sign-in',json={'username':'missing','password':'wrong'}).status_code == 401
    assert client.post('/auth/sign-in',json={'username':'missing','password':'wrong'}).status_code == 429

def test_different_employees_can_share_display_name(setup):
    import sqlite3
    from face_attendance.catalog import load_employee_map
    path,client,headers,key = setup
    first = client.post('/enrollments',headers=headers,json={'employeeId':'A','name':'Alex'}).json()
    second = client.post('/enrollments',headers=headers,json={'employeeId':'B','name':'Alex'}).json()
    cipher = Fernet(key)
    with sqlite3.connect(path/'mobile_enrollment.db') as db:
        for index,result in enumerate((first,second)):
            for angle in ANGLES:
                vector = np.eye(1,128,index,dtype=np.float32)[0]
                db.execute('INSERT INTO face_enrollment_samples VALUES(?,?,?,?,?,?)',(result['enrollmentId'],angle,cipher.encrypt(vector.tobytes()),0,0,0))
    for result in (first,second):
        assert client.post('/enrollments/'+result['enrollmentId']+'/complete',headers=headers).status_code == 200
    employees = load_employee_map(path/'employees.json')
    assert {r.employee_id for r in employees.values()} == {'A','B'}
    assert {r.name for r in employees.values()} == {'Alex'}

@pytest.fixture
def streaming_setup(setup,monkeypatch):
    import cv2
    from face_attendance.face_backend import OpenCVFaceBackend
    module = importlib.import_module('face_attendance.mobile_api')
    class Backend:
        def face_locations(self,image): return [(0,160,160,0)]
        def face_encodings(self,image,boxes): return [np.eye(1,128,dtype=np.float32)[0]]
        face_distance = staticmethod(OpenCVFaceBackend.face_distance)
    class Pose:
        def estimate(self,crop): return (0,0,0)
    class Pad:
        def evaluate(self,image,box): return (1.0,'')
    monkeypatch.setattr(module.EnrollmentService,'_detector',lambda _: Backend())
    monkeypatch.setattr(module,'UniFacePoseEstimator',Pose)
    monkeypatch.setattr(module,'AntiSpoofService',Pad)
    monkeypatch.setattr(module,'frame_quality',lambda *args: ())
    tick = iter([10+i*.25 for i in range(100)])
    monkeypatch.setattr(module,'frame_time',lambda: next(tick))
    client = TestClient(create_app())
    headers = setup[2]
    eid = client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()['enrollmentId']
    def frame(index):
        rng = np.random.default_rng(index)
        return cv2.imencode('.jpg',rng.integers(0,255,(160,160,3),dtype=np.uint8))[1].tobytes()
    return client,eid,frame


def test_stream_auth_stability_and_reconnect(streaming_setup):
    client,eid,frame = streaming_setup
    with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
        ws.send_json({'accessToken':'wrong'})
        assert ws.receive_json()['status'] == 401
    with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
        ws.send_json({'accessToken':'t'*32})
        assert ws.receive_json()['type'] == 'ready'
        for i in range(6):
            ws.send_json({'type':'frame','frameId':i,'capturedAtMs':i*250,'targetAngle':'FRONT'})
            ws.send_bytes(frame(i))
            result = ws.receive_json()
            assert result['frameId'] == i
            assert result['completedAngles'] == (1 if i == 5 else 0)
        assert result['captureAccepted'] and result['nextAngle'] == 'LEFT'
    with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
        ws.send_json({'accessToken':'t'*32})
        assert ws.receive_json()['completedAngles'] == 1


def test_repeated_frames_and_out_of_order_sequence_do_not_enroll(streaming_setup):
    client,eid,frame = streaming_setup
    with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
        ws.send_json({'accessToken':'t'*32}); ws.receive_json()
        data = frame(0)
        for i in range(3):
            ws.send_json({'type':'frame','frameId':i,'capturedAtMs':i*250,'targetAngle':'FRONT'})
            ws.send_bytes(data)
            assert ws.receive_json()['completedAngles'] == 0
        ws.send_json({'type':'frame','frameId':1,'capturedAtMs':1000,'targetAngle':'FRONT'})
        assert ws.receive_json()['status'] == 422


def test_disconnect_resets_unaccepted_evidence(streaming_setup):
    client,eid,frame = streaming_setup
    for attempt in range(2):
        with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
            ws.send_json({'accessToken':'t'*32}); ws.receive_json()
            for i in range(3):
                ws.send_json({'type':'frame','frameId':i,'capturedAtMs':i*250,'targetAngle':'FRONT'})
                ws.send_bytes(frame(attempt*10+i))
                assert ws.receive_json()['completedAngles'] == 0


def test_next_angle_must_match_persistent_front_identity(streaming_setup,monkeypatch):
    client,eid,frame = streaming_setup
    module = importlib.import_module('face_attendance.mobile_api')
    with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
        ws.send_json({'accessToken':'t'*32}); ws.receive_json()
        for i in range(6):
            ws.send_json({'type':'frame','frameId':i,'capturedAtMs':i*250,'targetAngle':'FRONT'})
            ws.send_bytes(frame(i)); result = ws.receive_json()
        assert result['completedAngles'] == 1
        monkeypatch.setattr(module.UniFacePoseEstimator,'estimate',lambda self,crop: (-25,0,0))
        backend_type = type(module.EnrollmentService._detector(None))
        monkeypatch.setattr(backend_type,'face_encodings',lambda self,image,boxes: [np.roll(np.eye(1,128,dtype=np.float32)[0],1)])
        ws.send_json({'type':'frame','frameId':6,'capturedAtMs':1500,'targetAngle':'LEFT'})
        ws.send_bytes(frame(6)); result = ws.receive_json()
        assert result['completedAngles'] == 1
        assert 'front capture' in result['rejectionReason']


def test_multiple_faces_clear_incomplete_stability(streaming_setup,monkeypatch):
    client,eid,frame = streaming_setup
    module = importlib.import_module('face_attendance.mobile_api')
    backend_type = type(module.EnrollmentService._detector(None))
    with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
        ws.send_json({'accessToken':'t'*32}); ws.receive_json()
        for i in range(3):
            ws.send_json({'type':'frame','frameId':i,'capturedAtMs':i*250,'targetAngle':'FRONT'})
            ws.send_bytes(frame(i)); ws.receive_json()
        monkeypatch.setattr(backend_type,'face_locations',lambda self,image:[(0,160,160,0)]*2)
        ws.send_json({'type':'frame','frameId':3,'capturedAtMs':750,'targetAngle':'FRONT'})
        ws.send_bytes(frame(3)); result = ws.receive_json()
        assert result['stabilityProgress'] == 0
        assert result['completedAngles'] == 0
        assert 'exactly one face' in result['rejectionReason']


def test_stream_accepts_all_seven_angles_with_uniface_coordinates(streaming_setup,monkeypatch):
    client,eid,frame = streaming_setup
    module = importlib.import_module('face_attendance.mobile_api')
    poses = [(0,0,0),(-25,0,0),(25,0,0),(0,-25,0),(0,25,0),
             (-25,-25,0),(25,-25,0)]
    with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
        ws.send_json({'accessToken':'t'*32}); ws.receive_json()
        for step,(angle,pose) in enumerate(zip(ANGLES,poses)):
            monkeypatch.setattr(module.UniFacePoseEstimator,'estimate',lambda self,crop,p=pose: p)
            for i in range(6):
                index = step*6+i
                ws.send_json({'type':'frame','frameId':index,'capturedAtMs':index*250,'targetAngle':angle})
                ws.send_bytes(frame(index))
                result = ws.receive_json()
            assert result['completedAngles'] == step+1
            assert result['captureAccepted']
        assert result['nextAngle'] is None


def test_invalid_rest_image_breaks_stability(streaming_setup):
    import base64
    client,eid,frame = streaming_setup
    headers = {'Authorization':'Bearer '+'t'*32}
    for i in range(3):
        client.post('/enrollments/'+eid+'/captures',headers=headers,
                    json={'image':base64.b64encode(frame(i)).decode()})
    client.post('/enrollments/'+eid+'/captures',headers=headers,json={'image':'bad'})
    for i in range(3,6):
        result = client.post('/enrollments/'+eid+'/captures',headers=headers,
                             json={'image':base64.b64encode(frame(i)).decode()}).json()
        assert result['completedAngles'] == 0


def _fill_angles(path, key, eid):
    import sqlite3
    cipher = Fernet(key)
    with sqlite3.connect(path/'mobile_enrollment.db') as db:
        for angle in ANGLES:
            db.execute('INSERT INTO face_enrollment_samples VALUES(?,?,?,?,?,?)',
                       (eid,angle,cipher.encrypt(np.ones(128,dtype=np.float32).tobytes()),0,0,0))


def test_desktop_deleted_employee_disappears_from_directory(setup):
    from face_attendance.enrollment import write_encodings
    path,client,headers,key = setup
    eid = client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()['enrollmentId']
    _fill_angles(path,key,eid)
    assert client.post('/enrollments/'+eid+'/complete',headers=headers).status_code == 200
    assert [r['status'] for r in client.get('/employees',headers=headers).json()['employees']] == ['COMPLETED']
    write_encodings(path/'encodings_sface.pickle',{})  # Deleted from the desktop employee screen.
    assert client.get('/employees',headers=headers).json()['employees'] == []
    assert client.get('/employees/E1/enrollment',headers=headers).status_code == 404


def test_completion_refuses_to_overwrite_profile_enrolled_meanwhile(setup):
    from face_attendance.enrollment import write_encodings, write_employee
    from face_attendance.template_store import read_templates
    path,client,headers,key = setup
    eid = client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()['enrollmentId']
    _fill_angles(path,key,eid)
    original = np.eye(1,128,dtype=np.float32)[0]
    write_encodings(path/'encodings_sface.pickle',{'Alice':[original]})
    write_employee(path/'employees.json','Alice','E1')
    assert client.post('/enrollments/'+eid+'/complete',headers=headers).status_code == 409
    assert len(read_templates(path/'encodings_sface.pickle')['Alice']) == 1


def test_samples_from_another_key_give_clear_conflict(setup):
    path,client,headers,key = setup
    eid = client.post('/enrollments',headers=headers,json={'employeeId':'E1','name':'Alice'}).json()['enrollmentId']
    _fill_angles(path,Fernet.generate_key(),eid)
    result = client.post('/enrollments/'+eid+'/complete',headers=headers)
    assert result.status_code == 409 and 'different server key' in result.json()['detail']


@pytest.mark.parametrize('protocol',['rest','stream'])
def test_duplicate_face_capture_names_existing_employee_and_keeps_progress(streaming_setup,setup,protocol):
    import base64
    from face_attendance.enrollment import write_encodings,write_employee
    client,eid,frame = streaming_setup
    path,_,headers,_ = setup
    # Distance .45: recognition would match it, but the old hardcoded .4 gate missed it.
    near = np.eye(1,128,dtype=np.float32)[0]*.55 + np.eye(1,128,1,dtype=np.float32)[0]*np.sqrt(1-.55**2)
    write_encodings(path/'encodings_sface.pickle',{'Tim Dev':[near]})
    write_employee(path/'employees.json','Tim Dev','EMP002')
    before = (path/'encodings_sface.pickle').read_bytes(),(path/'employees.json').read_bytes()
    if protocol == 'rest':
        result = client.post('/enrollments/'+eid+'/captures',headers=headers,
                             json={'image':base64.b64encode(frame(0)).decode()}).json()
    else:
        with client.websocket_connect('/enrollments/'+eid+'/stream') as ws:
            ws.send_json({'accessToken':'t'*32}); ws.receive_json()
            ws.send_json({'type':'frame','frameId':0,'capturedAtMs':0,'targetAngle':'FRONT'})
            ws.send_bytes(frame(0))
            result = ws.receive_json()
    assert result['completedAngles'] == 0
    assert 'Tim Dev (EMP002)' in result['rejectionReason']
    assert ((path/'encodings_sface.pickle').read_bytes(),(path/'employees.json').read_bytes()) == before


def test_completion_rechecks_faces_enrolled_after_capture(setup):
    import sqlite3
    from face_attendance.enrollment import write_encodings,write_employee
    path,client,headers,key = setup
    eid = client.post('/enrollments',headers=headers,json={'employeeId':'EMP999','name':'Tim Dev 2'}).json()['enrollmentId']
    _fill_angles(path,key,eid)
    write_encodings(path/'encodings_sface.pickle',{'Tim Dev':[np.ones(128,dtype=np.float32)]})
    write_employee(path/'employees.json','Tim Dev','EMP002')
    before = (path/'encodings_sface.pickle').read_bytes(),(path/'employees.json').read_bytes()
    result = client.post('/enrollments/'+eid+'/complete',headers=headers)
    assert result.status_code == 409
    assert 'Tim Dev (EMP002)' in result.json()['detail']
    assert ((path/'encodings_sface.pickle').read_bytes(),(path/'employees.json').read_bytes()) == before
    with sqlite3.connect(path/'mobile_enrollment.db') as db:
        assert db.execute('SELECT count(*) FROM face_enrollment_samples WHERE enrollment_id=?',(eid,)).fetchone()[0] == 7
    assert client.get('/enrollments/'+eid,headers=headers).json()['status'] == 'IN_PROGRESS'


def test_pending_sessions_cannot_publish_same_face_for_different_employees(setup):
    path,client,headers,key = setup
    ids = [client.post('/enrollments',headers=headers,json={'employeeId':employee_id,'name':name}).json()['enrollmentId']
           for employee_id,name in [('EMP002','Tim Dev'),('EMP999','Tim Dev 2')]]
    for eid in ids:
        _fill_angles(path,key,eid)
    assert client.post('/enrollments/'+ids[0]+'/complete',headers=headers).status_code == 200
    result = client.post('/enrollments/'+ids[1]+'/complete',headers=headers)
    assert result.status_code == 409 and 'Tim Dev (EMP002)' in result.json()['detail']
