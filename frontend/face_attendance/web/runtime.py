"""Run the existing attendance engine and HRM services without a desktop window."""
import asyncio
from contextlib import suppress
import logging
import time

import cv2
from PyQt6.QtCore import QCoreApplication, QEvent

from .. import hrm_directory, hrm_servers
from ..dashboard.engine import AttendanceEngine
from ..dashboard.hrm_angles import AngleTemplateWorker
from ..dashboard.hrm_attendance import AttendanceUplink
from ..dashboard.hrm_live import HrmLiveLink
from ..dashboard.lock import EngineLock
from ..hrm_sync import config_from_settings
from ..settings import LOCK_PATH


def encode_preview(frame):
    height, width = frame.shape[:2]
    if width > 960:
        frame = cv2.resize(frame, (960, max(1, height * 960 // width)),
                           interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
    return jpeg.tobytes() if ok else None


class WebRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.lock = EngineLock(LOCK_PATH)
        self.engine = None
        self.tasks = []
        self.frame = None
        self.jpeg = None
        self.preview_generation = 0
        self.preview_requested = float("-inf")
        self.refresh = asyncio.Event()
        self.syncing = False
        self.sync_error = ""
        self.closing = False

    async def open(self, autostart):
        self.lock.acquire()
        self.qt = QCoreApplication.instance() or QCoreApplication(["sv-face-web"])
        choice = hrm_servers.profile(self.settings)
        if choice.mode == hrm_servers.AUTO:
            await asyncio.to_thread(hrm_servers.resolve, choice)
        self.hrm = config_from_settings(self.settings)
        self.engine = AttendanceEngine(self.settings, use_lock=False)
        self.engine.frameReady.connect(self.set_frame)
        self.engine.logMessage.connect(logging.info)
        self.engine.errorRaised.connect(logging.warning)
        self.link = HrmLiveLink()
        self.uplink = AttendanceUplink(self.link, lambda: self.engine.config)
        self.angles = AngleTemplateWorker(lambda: self.settings)
        self.engine.attendanceSaved.connect(lambda _result: self.uplink.kick())
        self.link.employeesChanged.connect(self.refresh.set)
        self.link.anglesChanged.connect(lambda _code: self.angles.request())
        self.link.anglesVerified.connect(lambda _code: self.angles.request())
        self.link.configure(self.hrm)
        self.tasks = [asyncio.create_task(self.pump()), asyncio.create_task(self.sync_directory())]
        if autostart:
            self.control("start")

    async def close(self):
        self.closing = True
        self.refresh.set()
        try:
            if hasattr(self, "link"):
                self.link.stop()
            if hasattr(self, "uplink"):
                self.uplink.close()
            if self.engine is not None:
                self.engine.shutdown()
        finally:
            try:
                if self.tasks:
                    pump, sync = self.tasks
                    pump.cancel()
                    with suppress(asyncio.CancelledError):
                        await pump
                    await sync
            finally:
                self.lock.release()

    def set_frame(self, frame):
        self.frame = frame

    def control(self, action):
        if action == "start" and not self.engine.running:
            try:
                self.engine.start()
            except Exception:
                self.engine.stop()
                raise
        elif action == "stop":
            self.engine.stop()
            self.engine.set_paused(False)
            self.preview_generation += 1
            self.frame = self.jpeg = None
        elif action in {"pause", "resume"}:
            if not self.engine.running:
                raise ValueError("Start the engine first.")
            self.engine.set_paused(action == "pause")

    async def pump(self):
        next_preview = next_catalog_check = 0.0
        while True:
            self.qt.processEvents()
            self.qt.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            now = time.monotonic()
            if now >= next_catalog_check:
                next_catalog_check = now + 2
                try:
                    self.engine.reload_if_catalog_changed()
                except Exception:
                    logging.exception("Could not reload the employee catalog")
            if (now >= next_preview and now - self.preview_requested < 3
                    and self.frame is not None and self.engine.running):
                next_preview = now + 0.15
                frame, self.frame = self.frame, None
                generation = self.preview_generation
                try:
                    jpeg = await asyncio.to_thread(encode_preview, frame)
                except cv2.error:
                    logging.exception("Could not encode the camera preview")
                else:
                    if self.engine.running and generation == self.preview_generation:
                        self.jpeg = jpeg, now
            await asyncio.sleep(0.025)

    async def sync_directory(self):
        if not self.hrm.configured:
            return
        while not self.closing:
            self.refresh.clear()
            self.syncing = True
            self.sync_error = ""
            try:
                await asyncio.to_thread(hrm_directory.sync, self.hrm,
                                        hrm_directory.cache_path(self.settings))
            except Exception as exc:
                logging.warning("Could not sync the SV HRM employee directory: %s", exc)
                self.sync_error = "Could not sync the employee directory. Check the server log."
            finally:
                self.syncing = False
            if self.closing:
                return
            self.angles.request()
            try:
                await asyncio.wait_for(self.refresh.wait(), timeout=300)
            except TimeoutError:
                pass
