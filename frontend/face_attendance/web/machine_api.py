"""APIs for other programs, served by ``dashboard.py --web`` beside the browser dashboard.

- ``/v1/faces/encode``: the face verifier SV HRM calls before it saves a profile photo
  or face angle (formerly the separate ``verifier/`` service). Header ``X-Face-Verifier-Key``
  must equal ``SV_FACE_VERIFIER_KEY``.
- ``/api/mobile/**``: the mobile angle-registration app. Employees come from this
  dashboard's synced directory; angle lists, images and uploads are relayed to SV HRM
  with the dashboard's own HRM key, so the phone never holds it. Header ``X-API-KEY``
  must equal ``SV_FACE_MOBILE_KEY``.

Both use a key header instead of the session cookie, so the browser-only ``X-SV-Face``
check in ``server.protect`` does not apply to them (see ``MACHINE_PREFIXES``).
A key shorter than 32 characters switches its API off (503).
"""
import asyncio
import logging
import math
import os
import re
import secrets
import threading
import urllib.parse

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .. import hrm_directory, hrm_sync
from ..face_backend import MODEL_ID

VERIFIER_KEY_ENV = "SV_FACE_VERIFIER_KEY"
MOBILE_KEY_ENV = "SV_FACE_MOBILE_KEY"
MIN_KEY_LENGTH = 32
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS_SIDE = 4096
MAX_PAGE_SIZE = 500
MACHINE_PREFIXES = ("/v1/", "/api/mobile/")
ANGLE_PATTERN = re.compile(r"^[a-z_]{1,32}$")


class FaceEncoder:
    """SFace vectors for every face in an image, with the Live Monitor's own models.

    The backend loads on first use so the dashboard starts as fast as before, and one
    lock serializes model access even if the waiting HTTP request is cancelled.
    """

    def __init__(self, backend_factory=None):
        self.backend_factory = backend_factory
        self.backend = None
        self.lock = threading.Lock()

    def encode(self, content):
        image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or max(image.shape[:2]) > MAX_PIXELS_SIDE:
            raise ValueError("The image cannot be read or exceeds 4096 pixels.")
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        with self.lock:
            if self.backend is None:
                factory = self.backend_factory
                if factory is None:
                    from ..face_backend import OpenCVFaceBackend as factory
                self.backend = factory()
            boxes = self.backend.face_locations(rgb)
            vectors = self.backend.face_encodings(rgb, boxes) if boxes else []
        return {"model": MODEL_ID, "vectors": [np.asarray(vector).tolist() for vector in vectors]}


def _key_guard(secret, header, name):
    """*secret* is the key, or a function returning it (so a key changed in Settings applies at once)."""
    async def check(request: Request):
        current = secret() if callable(secret) else secret
        if len(current or "") < MIN_KEY_LENGTH:
            raise HTTPException(503, f"The {name} API is switched off on this dashboard.")
        supplied = request.headers.get(header, "")
        if not secrets.compare_digest(supplied.encode(), current.encode()):
            raise HTTPException(401, f"Invalid {name} credentials.")
    return Depends(check)


PAIRING_SECONDS = 5 * 60
PAIRING_ATTEMPTS = 5


class PhonePairing:
    """A 6-digit code shown on the computer that a phone on the same Wi-Fi trades for the mobile key.

    One code at a time; it ends after PAIRING_SECONDS, after PAIRING_ATTEMPTS wrong tries (so it
    can't be guessed) or once a phone has used it. ``identity()`` returns (id, name, key).
    """

    def __init__(self, identity, clock=None):
        import time
        self.identity = identity
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._code = None
        self._expires = 0.0
        self._attempts = 0
        self.paired_devices = []      # Names of phones paired with the current code, newest last.

    def start(self):
        with self._lock:
            self._code = f"{secrets.randbelow(1_000_000):06d}"
            self._expires = self._clock() + PAIRING_SECONDS
            self._attempts = 0
            return self._code

    def cancel(self):
        with self._lock:
            self._code = None

    @property
    def active(self):
        with self._lock:
            return self._code is not None and self._clock() < self._expires

    def seconds_left(self):
        with self._lock:
            return max(0, int(self._expires - self._clock())) if self._code else 0

    def claim(self, code, device=""):
        """The key for the right code while it lasts, else None."""
        with self._lock:
            if self._code is None or self._clock() >= self._expires:
                self._code = None
                return None
            if not secrets.compare_digest(str(code).strip().encode(), self._code.encode()):
                self._attempts += 1
                if self._attempts >= PAIRING_ATTEMPTS:
                    self._code = None
                return None
            self._code = None
        _id, _name, key = self.identity()
        self.paired_devices.append(str(device or "A phone")[:60])
        return key


async def _read_body(request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Image exceeds 10 MB.")
    if not content:
        raise HTTPException(422, "The image is empty.")
    return bytes(content)


def _fail(status, message):
    """The error shape the mobile app reads (``message``), like SV HRM's own API."""
    return JSONResponse({"success": False, "message": message}, status_code=status)


def mobile_employee(row):
    return {"employeeCode": row.get("employee_id", ""),
            "employeeName": row.get("employee_name", ""),
            "firstName": row.get("first_name", ""),
            "lastName": row.get("last_name", ""),
            "departmentName": row.get("department", ""),
            "positionName": row.get("position", ""),
            "hasPhoto": bool(row.get("has_photo")),
            "faceEnrolled": bool(hrm_directory.is_enrolled(row))}


def create_router(verifier_key=None, mobile_key=None, encoder=None, pairing=None):
    verifier_key = os.environ.get(VERIFIER_KEY_ENV, "") if verifier_key is None else verifier_key
    mobile_key = os.environ.get(MOBILE_KEY_ENV, "") if mobile_key is None else mobile_key
    encoder = encoder or FaceEncoder()
    router = APIRouter()

    # No key: how a phone on the same Wi-Fi finds this computer and pairs with it. Hello
    # gives only a name and a random id; the key needs the code shown on this computer.
    @router.get("/api/mobile/link/hello")
    def hello():
        if pairing is None:
            return _fail(404, "This dashboard pairs phones from the desktop app only.")
        desktop_id, name, key = pairing.identity()
        if len(key or "") < MIN_KEY_LENGTH:
            return _fail(503, "The phone app is switched off in this computer's Settings.")
        return {"success": True, "data": {"service": "sv-face", "id": desktop_id, "name": name,
                                          "pairing": pairing.active}}

    @router.post("/api/mobile/link/pair")
    async def pair(request: Request):
        if pairing is None:
            return _fail(404, "This dashboard pairs phones from the desktop app only.")
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _fail(422, "Send the pairing code as JSON.")
        if not pairing.active:
            return _fail(409, "Choose Pair a phone in Settings on the computer to get a code.")
        key = pairing.claim(str(body.get("code", ""))[:12], str(body.get("device", ""))[:60])
        if key is None:
            return _fail(403, "That code is wrong or has expired. Check the code on the computer.")
        desktop_id, name, _key = pairing.identity()
        return {"success": True, "data": {"key": key, "id": desktop_id, "name": name}}

    @router.post("/v1/faces/encode",
                 dependencies=[_key_guard(verifier_key, "x-face-verifier-key", "face verifier")])
    async def encode(request: Request):
        content = await _read_body(request)
        try:
            result = await asyncio.to_thread(encoder.encode, content)
        except (ValueError, cv2.error) as exc:
            # SV HRM only sees the status, so record why here.
            logging.warning("Face verifier rejected a %d-byte image (starts %s): %s",
                            len(content), content[:4].hex(), exc)
            raise HTTPException(422, "The image could not be verified.") from exc
        except Exception as exc:
            logging.exception("Face verification failed")
            raise HTTPException(503, "Face verification is unavailable.") from exc
        return JSONResponse(result)

    mobile = APIRouter(prefix="/api/mobile", dependencies=[_key_guard(mobile_key, "x-api-key", "mobile")])

    def service(request: Request):
        return request.app.state.runtime

    async def relay(runtime, method, path, **kwargs):
        if not runtime.hrm.configured:
            return _fail(503, "The dashboard is not connected to SV HRM. Set its HRM address and API key.")
        try:
            return await asyncio.to_thread(hrm_sync._request, runtime.hrm, method, path, **kwargs)
        except hrm_sync.HRMError as exc:
            status = exc.status if 400 <= exc.status < 500 else 502
            return _fail(status, str(exc))

    def employee_path(code, angle=None):
        code = code.strip()
        if not code or len(code) > 64:
            raise HTTPException(422, "Check the employee code.")
        path = "/employees/" + urllib.parse.quote(code, safe="") + "/face-angles"
        if angle is not None:
            if not ANGLE_PATTERN.fullmatch(angle):
                raise HTTPException(422, "Unknown face angle.")
            path += "/" + angle
        return path

    @mobile.get("/employees")
    def employees(runtime=Depends(service), search: str = Query("", max_length=100),
                  status: str = Query("ACTIVE", max_length=20), page: int = Query(0, ge=0),
                  size: int = Query(50, ge=1, le=MAX_PAGE_SIZE)):
        directory = hrm_directory.load(hrm_directory.cache_path(runtime.settings))
        if not directory.synced:
            return _fail(503, "The dashboard has not synced employees from SV HRM yet. Try again shortly.")
        # The directory keeps active employees only.
        rows = list(directory.employees) if status.strip().upper() in ("", "ACTIVE") else []
        query = search.strip().casefold()
        if query:
            rows = [row for row in rows
                    if query in f"{row.get('employee_name', '')} {row.get('employee_id', '')}".casefold()]
        rows.sort(key=lambda row: str(row.get("employee_id", "")).casefold())
        total = len(rows)
        chunk = rows[page * size:(page + 1) * size]
        return {"success": True, "data": {"employees": [mobile_employee(row) for row in chunk],
                                          "page": page, "size": size, "totalElements": total,
                                          "totalPages": math.ceil(total / size),
                                          "syncedAt": directory.fetched_at}}

    @mobile.get("/face-angles/angles")
    async def angles(runtime=Depends(service)):
        return await relay(runtime, "GET", "/face-angles/angles")

    @mobile.get("/employees/{code}/face-angles")
    async def employee_angles(code: str, runtime=Depends(service)):
        return await relay(runtime, "GET", employee_path(code))

    @mobile.get("/employees/{code}/face-angles/{angle}/image")
    async def angle_image(code: str, angle: str, runtime=Depends(service)):
        result = await relay(runtime, "GET", employee_path(code, angle) + "/image",
                             raw_bytes=True, limit=5 * 1024 * 1024)
        if isinstance(result, Response):
            return result
        return Response(result, media_type="image/jpeg")

    @mobile.post("/employees/{code}/face-angles/{angle}")
    async def upload_angle(code: str, angle: str, request: Request, runtime=Depends(service)):
        path = employee_path(code, angle)
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            return _fail(415, "Send the angle image as multipart/form-data.")
        body = await _read_body(request)
        result = await relay(runtime, "POST", path, body=body, content_type=content_type)
        if isinstance(result, Response):
            return result
        runtime.angles.request()   # Pick the new angle up for the Live Monitor now.
        return result

    router.include_router(mobile)
    return router
