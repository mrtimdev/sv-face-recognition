"""Opt-in mobile enrollment API. Run separately from the desktop process."""
import base64
import asyncio
import hmac
import hashlib
import json
import secrets
import os
import sqlite3
import threading
import time
from time import monotonic as frame_time
from contextlib import contextmanager
import uuid
from pathlib import Path

import cv2
import numpy as np
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from anyio import CancelScope

from .config import ROOT
from .users import UserRepository, AccountDisabledError
from .settings import Settings
from .enrollment import (EnrollmentService, FACE_BACKEND_LOCK, CATALOG_LOCK, FaceDuplicateIndex,
                         frame_quality, write_employee, write_encodings)
from .models import Employee
from .template_store import read_template_bundle, TEMPLATE_MODEL
from .anti_spoof import AntiSpoofService, live_sample

from .guided_enrollment import ANGLES, PosePolicy, StableWindow, Candidate, UniFacePoseEstimator

MAX_IMAGE_BYTES = 2_000_000
MAX_ACTIVE_ENROLLMENTS = 256
MAX_STREAM_CONNECTIONS = 16


def _check_jpeg_dimensions(raw):
    """Inspect JPEG headers before OpenCV allocates a decompressed image."""
    if raw[:2] != b'\xff\xd8':
        raise ValueError('Send a valid JPEG camera frame')
    offset, dimensions = 2, None
    while offset < len(raw):
        if raw[offset] != 255:
            break
        while offset < len(raw) and raw[offset] == 255:
            offset += 1
        if offset >= len(raw):
            break
        marker = raw[offset]
        offset += 1
        if marker == 0xDA:
            if dimensions is None:
                break
            return
        if marker in (0, 0xD8, 0xD9):
            break
        if marker == 1 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(raw):
            break
        length = int.from_bytes(raw[offset:offset+2], 'big')
        if length < 2 or offset + length > len(raw):
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if dimensions is not None or length < 8:
                break
            height = int.from_bytes(raw[offset+3:offset+5], 'big')
            width = int.from_bytes(raw[offset+5:offset+7], 'big')
            if not (0 < width <= 2048 and 0 < height <= 2048):
                raise ValueError('Invalid image or image exceeds 2048 pixels')
            dimensions = (width, height)
        offset += length
    raise ValueError('Send a valid JPEG camera frame')


class _RequestSizeLimit:
    """Bound JSON accumulation, including requests without Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit = 3_000_000 if scope['path'].endswith('/captures') else 8192
        body = bytearray()
        deadline = time.monotonic() + 30
        while True:
            try:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                message = await asyncio.wait_for(receive(), remaining)
            except asyncio.TimeoutError:
                await JSONResponse({'detail':'Request body timed out'}, status_code=408)(scope, receive, send)
                return
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > limit:
                await JSONResponse({'detail':'Request body too large'}, status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {'type':'http.request', 'body':bytes(body), 'more_body':False}
        await self.app(scope, bounded_receive, send)


class _SignInLimiter:
    """Fixed-size IP buckets; changing usernames cannot bypass the IP budget."""
    def __init__(self, max_hosts=2048, window=300):
        self.max_hosts, self.window = max_hosts, window
        self.hosts = {}
        self.lock = threading.Lock()

    def check(self, host, username):
        now = time.monotonic()
        with self.lock:
            for key, attempts in list(self.hosts.items()):
                recent = [(stamp, user) for stamp, user in attempts if now-stamp < self.window]
                if recent:
                    self.hosts[key] = recent
                else:
                    self.hosts.pop(key)
            recent = self.hosts.get(host, [])
            username = username.casefold()
            if (len(recent) >= 30 or sum(user == username for _, user in recent) >= 5
                    or (host not in self.hosts and len(self.hosts) >= self.max_hosts)):
                raise HTTPException(429, 'Too many sign-in attempts; try again in five minutes')
            self.hosts[host] = recent + [(now, username)]


def pose_angle(yaw, pitch, roll):
    policy = PosePolicy()
    return policy.classify(policy.normalize(yaw,pitch,roll))


class Start(BaseModel):
    employeeId: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    reenroll: bool = False


class SignIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class Capture(BaseModel):
    image: str = Field(max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3))


def load_account_settings():
    """Settings whose database holds the desktop user accounts.

    Shared by the launcher's admin pre-check and the API, so both always read
    the same accounts database.
    """
    settings_path = ROOT / 'settings.json'
    settings = Settings.load(settings_path) if settings_path.exists() else Settings()
    if not settings_path.exists():
        settings.db_path = str(ROOT / 'attendance.db')
    if os.getenv('SV_DB_PATH'):
        settings.db_backend = 'sqlite'
        settings.db_path = os.environ['SV_DB_PATH']
    return settings


def _file_signature(path):
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def create_app():
    token = os.environ.get('SV_MOBILE_TOKEN', '')
    key = os.environ.get('SV_ENROLLMENT_KEY', '')
    if (token and len(token) < 32) or not key:
        raise RuntimeError('Set SV_ENROLLMENT_KEY (Fernet key); optional legacy SV_MOBILE_TOKEN must be 32+ characters')
    cipher = Fernet(key.encode())
    path = Path(os.getenv('SV_ENROLLMENT_DB_PATH', str(ROOT / 'mobile_enrollment.db'))).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    settings_path = ROOT / 'settings.json'
    if settings_path.exists():
        settings = Settings.load(settings_path)
        catalog_path, employees_path = Path(settings.encodings_path), Path(settings.employees_path)
    else:
        catalog_path, employees_path = ROOT / 'encodings_sface.pickle', ROOT / 'employees.json'
    catalog_path = Path(os.getenv('SV_ENCODINGS_PATH', str(catalog_path)))
    employees_path = Path(os.getenv('SV_EMPLOYEES_PATH', str(employees_path)))
    if not catalog_path.is_absolute():
        catalog_path = ROOT / catalog_path
    if not employees_path.is_absolute():
        employees_path = ROOT / employees_path
    users = UserRepository(load_account_settings())
    service = EnrollmentService(catalog_path, employees_path,
                                duplicate_distance=settings.face_tolerance if settings_path.exists() else .5)
    lock = threading.RLock()
    pad = AntiSpoofService()
    tolerance = float(os.getenv("SV_ENROLLMENT_DISTANCE", "0.5"))
    # Stability evidence is deliberately reset on restart; accepted samples persist.
    stability = {}
    pose_policy = PosePolicy()
    pose_estimator = UniFacePoseEstimator()
    streaming = set()
    stream_connections = 0  # Accessed exclusively on the ASGI event loop.
    # The desktop may rewrite the catalog at any time; re-read only when it changes.
    catalog_cache = {'signature': object(), 'templates': {}, 'employees': []}

    def published_catalog():
        signature = (_file_signature(service.encodings_path), _file_signature(service.employees_path))
        if signature != catalog_cache['signature']:
            templates, _ = read_template_bundle(service.encodings_path)
            employees = service.employees()
            records = {row['name']:Employee(row['employee_id'],row['employee_name']) for row in employees}
            catalog_cache.update(signature=signature, templates=templates, employees=employees,
                                 duplicate_index=FaceDuplicateIndex(templates,records))
        return catalog_cache['templates'], catalog_cache['employees']

    def decrypt_vector(blob):
        try:
            return np.frombuffer(cipher.decrypt(blob),dtype=np.float32).copy()
        except InvalidToken as exc:
            raise HTTPException(409,'Pending samples were encrypted with a different server key; cancel and restart this enrollment') from exc

    @contextmanager
    def connect():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS face_enrollments (
          id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, name TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'IN_PROGRESS', reenroll INTEGER NOT NULL,
          model_version TEXT NOT NULL DEFAULT 'opencv-sface-2021dec-v1',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE UNIQUE INDEX IF NOT EXISTS active_mobile_employee
          ON face_enrollments(employee_id) WHERE status='IN_PROGRESS';
        CREATE TABLE IF NOT EXISTS face_enrollment_samples (
          enrollment_id TEXT REFERENCES face_enrollments(id), angle_type TEXT,
          embedding BLOB NOT NULL, yaw REAL, pitch REAL, roll REAL,
          PRIMARY KEY(enrollment_id,angle_type));
        CREATE TABLE IF NOT EXISTS mobile_admin_sessions (
          token_hash TEXT PRIMARY KEY, user_uuid TEXT NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS enrollment_audit (
          enrollment_id TEXT, event TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        if 'display_name' not in {r[1] for r in db.execute('PRAGMA table_info(face_enrollments)')}:
            db.execute("ALTER TABLE face_enrollments ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    os.chmod(path, 0o600)
    app = FastAPI(title='SV Face ID mobile enrollment')
    app.add_middleware(_RequestSizeLimit)
    bearer = HTTPBearer()

    sign_in_limiter = _SignInLimiter()

    def public_user(user):
        return {'id':user.uuid,'username':user.username,'fullName':user.full_name,'role':user.role}

    def authorize(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
        if len(credentials.credentials) > 512:
            raise HTTPException(401,'Invalid access token')
        if token and hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            return None  # Optional compatibility for explicitly configured legacy clients.
        digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
        with connect() as db:
            row = db.execute('SELECT * FROM mobile_admin_sessions WHERE token_hash=?',(digest,)).fetchone()
        if not row or row['expires_at'] <= time.time():
            raise HTTPException(401,'Session expired; sign in again')
        user = users.get_user(row['user_uuid'])
        if not user or user.disabled or user.role != 'admin' or not user.has_permission('manage_employees'):
            raise HTTPException(403,'An active admin account with employee management permission is required')
        return user

    @app.get('/health')
    def health():
        return {'status':'ok','service':'SV Face ID','apiVersion':2,'realtimeEnrollment':True}

    @app.post('/auth/sign-in')
    def sign_in(body: SignIn, request: Request):
        sign_in_limiter.check(request.client.host if request.client else '', body.username.strip())
        try:
            user = users.authenticate(body.username.strip(),body.password)
        except AccountDisabledError:
            user = None
        if user is None:
            raise HTTPException(401,'Invalid username or password, or account disabled')
        if user.role != 'admin' or not user.has_permission('manage_employees'):
            raise HTTPException(403,'An admin account with employee management permission is required')
        value = secrets.token_urlsafe(32)
        expiry = time.time() + 8*60*60
        with connect() as db:
            db.execute('DELETE FROM mobile_admin_sessions WHERE expires_at <= ?',(time.time(),))
            db.execute('INSERT INTO mobile_admin_sessions VALUES(?,?,?)',(hashlib.sha256(value.encode()).hexdigest(),user.uuid,expiry))
        return {'accessToken':value,'tokenType':'bearer','expiresAt':expiry,'user':public_user(user)}

    @app.get('/auth/me')
    def me(user=Depends(authorize)):
        if user is None:
            raise HTTPException(403,'Sign in using an administrator account')
        return public_user(user)

    @app.post('/auth/sign-out', dependencies=[Depends(authorize)])
    def sign_out(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
        with connect() as db:
            db.execute('DELETE FROM mobile_admin_sessions WHERE token_hash=?',(hashlib.sha256(credentials.credentials.encode()).hexdigest(),))
        return {'status':'SIGNED_OUT'}

    def session(db, eid):
        row = db.execute('SELECT * FROM face_enrollments WHERE id=?', (eid,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Enrollment not found')
        if row['model_version'] != TEMPLATE_MODEL:
            raise HTTPException(409, 'Enrollment model changed; start a new session')
        return row

    def progress(db, eid, reason=None):
        row = session(db, eid)
        accepted = {r[0] for r in db.execute('SELECT angle_type FROM face_enrollment_samples WHERE enrollment_id=?', (eid,))}
        if row['status'] == 'COMPLETED':
            accepted = set(ANGLES)
        return dict(employeeId=row['employee_id'], employeeName=row['display_name'] or row['name'], enrollmentId=eid, status=row['status'],
                    totalAngles=7, completedAngles=len(accepted), progressPercentage=round(len(accepted)/7*100, 2),
                    nextAngle=next((a for a in ANGLES if a not in accepted), None),
                    angles=[dict(angle=a, status='ACCEPTED' if a in accepted else 'PENDING') for a in ANGLES],
                    rejectionReason=reason)

    @app.post('/enrollments', dependencies=[Depends(authorize)])
    def start(body: Start):
        body.employeeId = body.employeeId.strip()
        with lock, connect() as db:
            existing = db.execute("SELECT id FROM face_enrollments WHERE employee_id=? AND status='IN_PROGRESS'", (body.employeeId,)).fetchone()
            if existing:
                return progress(db, existing[0])
            if db.execute("SELECT count(*) FROM face_enrollments WHERE status='IN_PROGRESS'").fetchone()[0] >= MAX_ACTIVE_ENROLLMENTS:
                raise HTTPException(429,'Too many active enrollments; finish or cancel an existing enrollment first')
            rows = service.employees()
            employee = next((r for r in rows if r['employee_id'] == body.employeeId), None)
            if employee and not body.reenroll:
                raise HTTPException(409, 'Employee already enrolled; authorize re-enrollment explicitly')
            name = employee['name'] if employee else body.name.strip()
            if not employee and (any(r['name'] == name for r in rows) or db.execute('SELECT 1 FROM face_enrollments WHERE name=? AND employee_id<>?',(name,body.employeeId)).fetchone()):
                name = f"mobile:{body.employeeId}"
            if not name or not body.employeeId.strip():
                raise HTTPException(422, 'Employee ID and name must not be blank')
            conflict, message, _ = service._check_name(name, body.employeeId)
            if conflict:
                raise HTTPException(409, message)
            eid = str(uuid.uuid4())
            db.execute('INSERT INTO face_enrollments(id,employee_id,name,reenroll,display_name) VALUES(?,?,?,?,?)', (eid, body.employeeId, name, body.reenroll, body.name.strip()))
            db.execute('INSERT INTO enrollment_audit(enrollment_id,event) VALUES(?,?)', (eid, 'START'))
            return progress(db, eid)

    @app.get('/enrollments/{eid}', dependencies=[Depends(authorize)])
    def get_progress(eid: str):
        with connect() as db:
            return progress(db, eid)

    def analyze_capture(eid, raw, observed_at, expected_angle=None):
        with lock, connect() as db:
            state = progress(db,eid)
            if state['status'] != 'IN_PROGRESS' or state['nextAngle'] is None:
                return state
            window = stability.setdefault(eid,StableWindow())
            details = {'pose':None,'stabilityProgress':0.0,'captureAccepted':False}
            try:
                if expected_angle is not None and expected_angle != state['nextAngle']:
                    raise ValueError('Pose changed; follow the next instruction')
                if not raw or len(raw) > MAX_IMAGE_BYTES:
                    raise ValueError('Invalid image or image exceeds 2 MB')
                _check_jpeg_dimensions(raw)
                image = cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
                if image is None or max(image.shape[:2]) > 2048:
                    raise ValueError('Invalid image or image exceeds 2048 pixels')
                backend = service._detector()
                rgb = cv2.cvtColor(image,cv2.COLOR_BGR2RGB)
                with FACE_BACKEND_LOCK:
                    boxes = backend.face_locations(rgb)
                    if len(boxes) != 1:
                        raise ValueError('Show exactly one face')
                    box = boxes[0]
                    issues = frame_quality(image,box)
                    if issues:
                        raise ValueError('; '.join(issues))
                    top,right,bottom,left = map(int,box)
                    h,w = image.shape[:2]
                    if top < 0 or left < 0 or bottom > h or right > w:
                        raise ValueError('Keep your whole face visible')
                    crop = image[top:bottom,left:right]
                    pose = pose_policy.normalize(*pose_estimator.estimate(crop))
                    details['pose'] = dict(zip(('yaw','pitch','roll'),pose))
                    margin = 3 if window.count else 0
                    if not pose_policy.contains(state['nextAngle'],pose,margin):
                        raise ValueError('Move to ' + state['nextAngle'])
                    score,error = pad.evaluate(image,box)
                    if not live_sample(score):
                        raise ValueError(error or 'Presentation-attack check rejected capture')
                    vector = backend.face_encodings(rgb,boxes)[0]
                # Continuity within the current angle, plus the persistent front reference.
                if window.best is not None and backend.face_distance([window.best.vector],vector)[0] > tolerance:
                    raise ValueError('Identity changed; try again')
                reference = db.execute("SELECT embedding FROM face_enrollment_samples WHERE enrollment_id=? AND angle_type='FRONT'",(eid,)).fetchone()
                if reference is not None:
                    other = decrypt_vector(reference[0])
                    if backend.face_distance([other],vector)[0] > tolerance:
                        raise ValueError('Identity does not match the front capture')
                published_catalog()
                duplicate = catalog_cache['duplicate_index'].match(vector,state['employeeId'],service.duplicate_distance)
                if duplicate is not None:
                    raise ValueError(f'Face already matches {duplicate.matched_employee_name} '
                                     f'({duplicate.matched_employee_id}); update the existing employee instead')
                sharpness = float(cv2.Laplacian(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),cv2.CV_64F).var())
                # Rank only within this angle, so turned poses are not penalized for non-frontalness.
                ready = window.observe(observed_at,Candidate(vector.copy(),pose,sharpness),
                                       eligible=pose_policy.contains(state['nextAngle'],pose))
                details['pose'] = dict(zip(('yaw','pitch','roll'),map(float,window.smoothed)))
                details['stabilityProgress'] = round(window.fraction,3)
                if not ready:
                    return progress(db,eid,'Hold still for validation') | details
                candidate = window.best
                db.execute('INSERT INTO face_enrollment_samples VALUES(?,?,?,?,?,?)',
                           (eid,state['nextAngle'],cipher.encrypt(candidate.vector.astype(np.float32).tobytes()),*candidate.pose))
                stability.pop(eid,None)
                db.execute('INSERT INTO enrollment_audit(enrollment_id,event) VALUES(?,?)',(eid,'ACCEPT '+state['nextAngle']))
                return progress(db,eid) | details | {'captureAccepted':True,'stabilityProgress':1.0}
            except (ValueError,KeyError,cv2.error) as exc:
                window.reset()
                return progress(db,eid,str(exc)) | details | {'stabilityProgress':0.0}
            except RuntimeError as exc:
                window.reset()
                raise HTTPException(503,str(exc)) from exc

    @app.post('/enrollments/{eid}/captures', dependencies=[Depends(authorize)])
    def capture(eid: str, body: Capture):
        with lock:
            if eid in streaming:
                raise HTTPException(409,'Use the active stream for this enrollment')
            try:
                raw = base64.b64decode(body.image,validate=True)
            except ValueError:
                stability.pop(eid,None)
                with connect() as db:
                    return progress(db,eid,'Invalid image encoding')
            return analyze_capture(eid,raw,frame_time())

    @app.websocket('/enrollments/{eid}/stream')
    async def stream(websocket: WebSocket, eid: str):
        nonlocal stream_connections
        if stream_connections >= MAX_STREAM_CONNECTIONS:
            await websocket.close(code=1013)
            return
        stream_connections += 1
        owns_stream = False

        async def receive_header(timeout):
            text = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
            if len(text) > 4096:
                raise ValueError('Stream header too large')
            return json.loads(text)

        def register():
            nonlocal owns_stream
            with lock, connect() as db:
                state = progress(db,eid)
                if state['status'] != 'IN_PROGRESS':
                    raise HTTPException(409,'This enrollment is no longer active')
                if eid in streaming:
                    raise HTTPException(409,'Another camera is enrolling this employee')
                streaming.add(eid)
                owns_stream = True
                stability.pop(eid,None)
                return state

        def reset_evidence():
            with lock, connect() as db:
                if session(db,eid)['status'] != 'IN_PROGRESS':
                    raise HTTPException(409,'This enrollment is no longer active')
                stability.pop(eid,None)

        def repeated_frame():
            with lock, connect() as db:
                stability.pop(eid,None)
                return progress(db,eid,'Repeated frame; waiting for a new camera frame')

        def release():
            with lock:
                streaming.discard(eid)
                stability.pop(eid,None)

        try:
            await websocket.accept()
            auth = await receive_header(10)
            if not isinstance(auth,dict):
                raise ValueError('Invalid authentication message')
            access_token = auth.get('accessToken','')
            if not isinstance(access_token,str) or not access_token:
                raise HTTPException(401,'Sign in before streaming')
            credentials = HTTPAuthorizationCredentials(scheme='Bearer',credentials=access_token)
            await run_in_threadpool(authorize,credentials)
            state = await run_in_threadpool(register)
            await websocket.send_json({'type':'ready',**state})
            previous_sequence, previous_timestamp = -1,-1
            last_digest = None
            server_origin, client_origin = None,None
            while True:
                header = await receive_header(30)
                if not isinstance(header,dict):
                    raise ValueError('Invalid camera frame header')
                # Reset messages must honor logout, account changes and expiry too.
                await run_in_threadpool(authorize,credentials)
                if header.get('type') == 'reset':
                    await run_in_threadpool(reset_evidence)
                    await websocket.send_json({'type':'reset'})
                    continue
                sequence = header.get('frameId')
                timestamp = header.get('capturedAtMs')
                if (type(sequence) is not int or sequence <= previous_sequence
                    or type(timestamp) not in (int,float) or not np.isfinite(timestamp)
                    or timestamp <= previous_timestamp or header.get('type') != 'frame'
                    or header.get('targetAngle') not in ANGLES):
                    raise HTTPException(422,'Invalid or out-of-order camera frame')
                raw = await asyncio.wait_for(websocket.receive_bytes(),timeout=5)
                if not raw or len(raw) > MAX_IMAGE_BYTES:
                    raise HTTPException(422,'Invalid image or image exceeds 2 MB')
                received = frame_time()
                await run_in_threadpool(authorize,credentials)  # Honor expiry, logout and role changes.
                if server_origin is None:
                    server_origin,client_origin = received,timestamp
                age = received-server_origin-(timestamp-client_origin)/1000
                if age > 2 or age < -.5:
                    raise HTTPException(422,'Camera frames are stale; reconnect the stream')
                digest = hashlib.sha256(raw).digest()
                if digest == last_digest:
                    result = await run_in_threadpool(repeated_frame)
                else:
                    result = await run_in_threadpool(analyze_capture,eid,raw,received,header.get('targetAngle'))
                previous_sequence,previous_timestamp,last_digest = sequence,timestamp,digest
                await websocket.send_json({'type':'progress','frameId':sequence,**result})
        except WebSocketDisconnect:
            pass
        except HTTPException as exc:
            await websocket.send_json({'type':'error','status':exc.status_code,'message':exc.detail})
            await websocket.close(code=1008)
        except (asyncio.TimeoutError,ValueError,TypeError,KeyError):
            await websocket.close(code=1008,reason='Invalid stream message or camera timeout')
        finally:
            try:
                if owns_stream:
                    # ASGI shutdown/cancellation must not strand this enrollment
                    # in the active-stream set and prevent future reconnects.
                    with CancelScope(shield=True):
                        await run_in_threadpool(release)
            finally:
                stream_connections -= 1

    @app.get('/employees', dependencies=[Depends(authorize)])
    def employees():
        with lock, connect() as db:
            catalog = service.employees()
            catalog_ids = {r['employee_id'] for r in catalog}
            directory = {r['employee_id']: {'employeeId':r['employee_id'],'name':r['employee_name'],
                                          'status':'ENROLLED','enrollmentId':None} for r in catalog}
            for row in db.execute('SELECT * FROM face_enrollments ORDER BY created_at, rowid'):
                current = directory.get(row['employee_id'])
                if row['status'] == 'CANCELLED':
                    continue
                if row['status'] == 'COMPLETED' and row['employee_id'] not in catalog_ids:
                    continue  # Profile was removed on the desktop after mobile publication.
                directory[row['employee_id']] = {'employeeId':row['employee_id'],'name':current['name'] if current else (row['display_name'] or row['name']),
                    'status':row['status'],'enrollmentId':row['id']}
            return {'employees':sorted(directory.values(),key=lambda r: r['name'].casefold())}

    @app.get('/employees/{employee_id}/enrollment', dependencies=[Depends(authorize)])
    def employee_status(employee_id: str):
        with connect() as db:
            catalog = service.employees()
            employee = next((r for r in catalog if r['employee_id'] == employee_id), None)
            row = db.execute('SELECT id, status FROM face_enrollments WHERE employee_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1',(employee_id,)).fetchone()
            if row and not (row['status'] == 'COMPLETED' and employee is None):
                return progress(db,row['id'])
            if not employee:
                raise HTTPException(404,'Employee not found')
            return {'employeeId':employee_id,'status':'LEGACY_ENROLLED','enrollmentId':None}

    @app.delete('/enrollments/{eid}', dependencies=[Depends(authorize)])
    def cancel(eid: str):
        with lock, connect() as db:
            row = session(db,eid)
            if row['status'] == 'COMPLETED':
                raise HTTPException(409,'Completed profiles must be managed through the desktop employee screen')
            if row['status'] == 'CANCELLED':
                return progress(db,eid)
            db.execute('DELETE FROM face_enrollment_samples WHERE enrollment_id=?',(eid,))
            db.execute("UPDATE face_enrollments SET status='CANCELLED' WHERE id=?",(eid,))
            db.execute('INSERT INTO enrollment_audit(enrollment_id,event) VALUES(?,?)',(eid,'CANCEL'))
            stability.pop(eid,None)
            return progress(db,eid)

    @app.post('/enrollments/{eid}/complete', dependencies=[Depends(authorize)])
    def complete(eid: str):
        with lock, CATALOG_LOCK, connect() as db:
            state = progress(db,eid)
            if state['status'] == 'COMPLETED':
                return state
            if state['completedAngles'] != 7:
                raise HTTPException(409,'All seven angles must be accepted')
            row = session(db,eid)
            if not row['reenroll'] and any(r['employee_id'] == row['employee_id'] for r in service.employees()):
                raise HTTPException(409,'Employee was enrolled on the desktop meanwhile; cancel and restart with re-enrollment authorized')
            vectors = [decrypt_vector(r[0]) for r in db.execute('SELECT embedding FROM face_enrollment_samples WHERE enrollment_id=?',(eid,))]
            templates, photos = read_template_bundle(service.encodings_path)
            records = {r['name']:Employee(r['employee_id'],r['employee_name']) for r in service.employees()}
            index = FaceDuplicateIndex(templates,records)
            for vector in vectors:
                duplicate = index.match(vector,row['employee_id'],service.duplicate_distance)
                if duplicate is not None:
                    raise HTTPException(409,f'Face already matches {duplicate.matched_employee_name} '
                                        f'({duplicate.matched_employee_id}); update the existing employee instead')
            write_employee(service.employees_path,row['name'],row['employee_id'],display_name=row['display_name'] or row['name'])
            templates[row['name']] = vectors
            photos[row['name']] = []
            write_encodings(service.encodings_path,templates,photos)
            db.execute("UPDATE face_enrollments SET status='COMPLETED' WHERE id=?",(eid,))
            db.execute('DELETE FROM face_enrollment_samples WHERE enrollment_id=?',(eid,))
            db.execute('INSERT INTO enrollment_audit(enrollment_id,event) VALUES(?,?)',(eid,'COMPLETE'))
            return state | {'status':'COMPLETED'}
    return app
