"""Same-origin dashboard API. HRM credentials and tokens stay server-side."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import csv
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
import io
import logging
from pathlib import Path
import secrets
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import angle_templates, hrm_auth, hrm_directory
from ..config import ATTENDANCE_MODES
from ..enrollment import employee_rows
from ..hrm_sync import HRMError
from ..report import AttendanceReader, day_start, preset_range
from ..settings import Settings, describe_source
from ..users import AccountDisabledError, User
from .runtime import WebRuntime

STATIC = Path(__file__).with_name("static")
COOKIE = "sv_face_session"
SESSION_SECONDS = 8 * 60 * 60
ACCOUNT_CHECK_SECONDS = 60
MAX_SESSIONS = 128
STATS_FIELDS = ("status", "camera_fps", "faces", "enrolled", "verified_today", "queue",
                "storage_ready", "latency_ms", "liveness_blocked", "quality_blocked")


@dataclass
class Session:
    user: User
    expires: float
    checked: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class CameraSettings(BaseModel):
    source: str = Field(default="", max_length=2048)
    attendance_mode: Literal["face", "blink", "smile", "blink_and_smile"]


def create_app(settings_path=None, autostart=False):
    sessions = {}
    login_slots = asyncio.Semaphore(4)

    @asynccontextmanager
    async def lifespan(app):
        runtime = WebRuntime(Settings.load(settings_path))
        app.state.runtime = runtime
        try:
            await runtime.open(autostart)
            yield
        finally:
            sessions.clear()
            await runtime.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def protect(request, call_next):
        # Cross-site forms cannot set this header. Cross-origin JavaScript needs
        # a successful preflight, and this application serves no CORS headers.
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("x-sv-face") != "1":
            response = JSONResponse({"detail": "Request must come from the dashboard."}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse({"detail": "Check the submitted values and try again."}, status_code=422)

    def runtime(request: Request):
        return request.app.state.runtime

    async def account(request: Request, service=Depends(runtime)):
        token = request.cookies.get(COOKIE)
        session = sessions.get(token)
        if session is None or time.monotonic() >= session.expires:
            sessions.pop(token, None)
            raise HTTPException(401, "Sign in to continue.")
        async with session.lock:
            now = time.monotonic()
            if now - session.checked >= ACCOUNT_CHECK_SECONDS:
                try:
                    fresh = await asyncio.to_thread(hrm_auth.check_account, service.hrm, session.user)
                except HRMError as exc:
                    if hrm_auth.token_expired(session.user.hrm_token):
                        sessions.pop(token, None)
                        raise HTTPException(401, "Your SV HRM session expired.") from exc
                    raise HTTPException(503, "Could not verify your SV HRM session. Try again.") from exc
                if fresh is None or fresh.disabled:
                    sessions.pop(token, None)
                    raise HTTPException(401, "Your SV HRM session ended.")
                session.user, session.checked = fresh, time.monotonic()
            if sessions.get(token) is not session or time.monotonic() >= session.expires:
                raise HTTPException(401, "Sign in to continue.")
            return session.user

    def require(*permissions):
        async def permitted(user=Depends(account)):
            if not all(permission in (user.permissions or []) for permission in permissions):
                raise HTTPException(403, "Your SV HRM account does not have access to this action.")
            return user
        return Depends(permitted)

    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/api/session")
    async def sign_in(credentials: Credentials, request: Request, response: Response,
                      service=Depends(runtime)):
        if login_slots.locked():
            raise HTTPException(429, "Sign-in is busy. Try again shortly.")
        async with login_slots:
            try:
                user = await asyncio.to_thread(hrm_auth.sign_in, service.hrm,
                                               credentials.username.strip(), credentials.password)
            except (AccountDisabledError, hrm_auth.NoAccessError) as exc:
                raise HTTPException(403, "This SV HRM account cannot access Face ID.") from exc
            except HRMError as exc:
                raise HTTPException(503, "SV HRM sign-in is unavailable. Try again.") from exc
        if user is None:
            raise HTTPException(401, "Check your username and password.")
        now = time.monotonic()
        for token, session in list(sessions.items()):
            if now >= session.expires:
                del sessions[token]
        sessions.pop(request.cookies.get(COOKIE), None)
        if len(sessions) >= MAX_SESSIONS:
            raise HTTPException(503, "The dashboard has reached its session limit.")
        token = secrets.token_urlsafe(32)
        sessions[token] = Session(user, now + SESSION_SECONDS, now)
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")
        return user_info(user)

    @app.get("/api/session")
    async def session(user=Depends(account)):
        return user_info(user)

    @app.delete("/api/session", status_code=204)
    async def sign_out(request: Request, response: Response):
        sessions.pop(request.cookies.get(COOKIE), None)
        response.delete_cookie(COOKIE, httponly=True, secure=request.url.scheme == "https",
                               samesite="strict")

    @app.get("/api/live", dependencies=[require("view_dashboard")])
    async def live(service=Depends(runtime)):
        engine = service.engine
        stats = {key: engine.stats.get(key) for key in STATS_FIELDS}
        return dict(stats, running=engine.running, paused=engine.paused,
                    source=camera_label(service.settings.source),
                    hrm_status=service.uplink.status)

    @app.get("/api/preview", dependencies=[require("view_dashboard")])
    async def preview(service=Depends(runtime)):
        now = time.monotonic()
        service.preview_requested = now
        jpeg = service.jpeg
        if service.engine.running and jpeg and jpeg[0] and now - jpeg[1] < 2:
            return Response(jpeg[0], media_type="image/jpeg")
        return Response(status_code=204)

    @app.post("/api/engine/{action}", status_code=204, dependencies=[require("control_engine")])
    async def engine(action: Literal["start", "stop", "pause", "resume"], service=Depends(runtime)):
        try:
            service.control(action)
        except ValueError as exc:
            raise HTTPException(409, "Could not apply this engine action. Check the camera settings.") from exc
        except Exception as exc:
            logging.exception("Engine action failed")
            raise HTTPException(409, "The engine action failed. Check the server log.") from exc

    @app.get("/api/settings", dependencies=[require("manage_settings")])
    async def settings(service=Depends(runtime)):
        return settings_info(service)

    @app.put("/api/settings", dependencies=[require("manage_settings")])
    async def save_settings(values: CameraSettings, service=Depends(runtime)):
        if service.engine.running:
            raise HTTPException(409, "Stop the engine before changing camera settings.")
        updated = replace(service.settings, source=values.source.strip() or service.settings.source,
                          attendance_mode=values.attendance_mode)
        try:
            updated.to_config()
            updated.save(settings_path)
        except (ValueError, OSError) as exc:
            logging.exception("Could not save camera settings")
            raise HTTPException(400, "Could not save camera settings. Check the source and server log.") from exc
        service.settings = service.engine.settings = updated
        return settings_info(service)

    @app.get("/api/attendance", dependencies=[require("view_reports")])
    def attendance(request: Request, preset: Literal["today", "hour", "week", "month", "all"] = "today",
                   start: date | None = None, end: date | None = None,
                   search: str = Query("", max_length=100), offset: int = Query(0, ge=0)):
        report_filter = report_range(preset, start, end)
        reader = AttendanceReader(runtime(request).settings)
        try:
            rows = reader.records(*report_filter, search=search, limit=50, offset=offset)
            total = reader.count(*report_filter, search=search)
            if reader.error:
                raise HTTPException(503, "The attendance database is unavailable.")
            outbox = reader.outbox_counts()
            for row in rows:
                row.pop("snapshot")
            return {"rows": rows, "total": total, "outbox": outbox}
        finally:
            reader.close()

    @app.get("/api/attendance.csv", dependencies=[require("view_reports", "export_data")])
    def attendance_csv(request: Request, preset: Literal["today", "hour", "week", "month", "all"] = "today",
                       start: date | None = None, end: date | None = None,
                       search: str = Query("", max_length=100)):
        report_filter = report_range(preset, start, end)
        reader = AttendanceReader(runtime(request).settings)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        columns = ("time", "name", "employee_id", "status", "duration", "event_id")
        writer.writerow(("Recorded at (local)", "Employee", "Employee ID", "Status",
                         "Verified presence (s)", "Event ID"))
        try:
            rows = reader.records(*report_filter, search=search, limit=100001)
            if reader.error:
                raise HTTPException(503, "The attendance database is unavailable.")
            if len(rows) > 100000:
                raise HTTPException(422, "Choose a shorter date range to export up to 100,000 records.")
            writer.writerows([spreadsheet_value(row[key]) for key in columns] for row in rows)
        finally:
            reader.close()
        return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="attendance.csv"'})

    @app.get("/api/employees", dependencies=[require("manage_employees")])
    def employees(request: Request, search: str = Query("", max_length=100), offset: int = Query(0, ge=0)):
        service = runtime(request)
        cfg = service.settings.to_config()
        directory = hrm_directory.load(hrm_directory.cache_path(cfg))
        angles = angle_templates.counts(angle_templates.load(angle_templates.store_path(cfg)))
        rows = hrm_directory.merge(employee_rows(cfg.encodings_path, cfg.employees_path), directory, angles)
        query = search.strip().casefold()
        rows = [row for row in rows if query in f"{row['employee_name']} {row['employee_id']}".casefold()]
        fields = ("employee_name", "employee_id", "department", "status", "hrm_status", "samples", "angles")
        return {"rows": [{key: row.get(key) for key in fields} for row in rows[offset:offset + 50]],
                "total": len(rows), "syncing": service.syncing or service.refresh.is_set(),
                "sync_error": service.sync_error}

    @app.post("/api/employees/sync", status_code=204, dependencies=[require("manage_employees")])
    async def refresh_directory(service=Depends(runtime)):
        if not service.hrm.configured:
            raise HTTPException(409, "Set the SV HRM API key in the server settings to enable sync.")
        service.refresh.set()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def report_range(preset, start, end):
    if start is None and end is None:
        return preset_range(preset)
    if start is None or end is None or start > end or end == date.max:
        raise HTTPException(422, "Choose a valid start and end date.")
    try:
        return day_start(start), day_start(end + timedelta(days=1))
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(422, "Choose dates within the supported range.") from exc


def spreadsheet_value(value):
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n"))
                                   or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def settings_info(service):
    return {"source": camera_label(service.settings.source), "attendance_mode": service.settings.attendance_mode,
            "modes": ATTENDANCE_MODES, "running": service.engine.running,
            "hrm_configured": service.hrm.configured}


def camera_label(source):
    if "://" not in str(source):
        return describe_source(source)
    try:
        url = urlsplit(source)
        return f"{url.scheme.upper()} camera · {url.hostname}" if url.hostname else "Network camera"
    except ValueError:
        return "Network camera"


def user_info(user):
    return {"name": user.full_name or user.username, "permissions": user.permissions}


def main(argv=None):
    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the Face ID browser dashboard")
    parser.add_argument("--settings", help="Path to settings.json")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", default=8080, type=int, help="HTTP port (default 8080)")
    parser.add_argument("--start", action="store_true", help="Start recording when the server starts")
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.settings, args.start), host=args.host, port=args.port,
                workers=1, timeout_graceful_shutdown=10)
    return 0
