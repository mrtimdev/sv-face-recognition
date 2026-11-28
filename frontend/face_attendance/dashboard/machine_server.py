"""Serve the machine APIs (face verifier, mobile app) from the desktop dashboard.

The same routes as ``dashboard.py --web`` (see ``web/machine_api.py``), on a background
thread, so SV HRM and the phone app can use the desktop app instead of the web server.

The phone app link is on by default (Settings > Phone app on the same Wi-Fi): this computer
makes its own mobile key and id (``ensure_phone_link``), and a phone finds it on the Wi-Fi and
pairs with the 6-digit code from Pair a phone; nobody types an address or a key.
``SV_FACE_MOBILE_KEY`` still overrides that key, and ``SV_FACE_VERIFIER_KEY`` (at least 32
characters) switches the face verifier on. ``SV_FACE_API_HOST`` (default 0.0.0.0) and
``SV_FACE_API_PORT`` (default 8080) choose where it listens.
"""
import logging
import os
import secrets
import socket
import sys
import threading

from ..hrm_sync import config_from_settings, local_ip

HOST_ENV = "SV_FACE_API_HOST"
PORT_ENV = "SV_FACE_API_PORT"
DEFAULT_PORT = 8080


def ensure_phone_link(settings):
    """Give this computer a mobile key and id the first time; True when settings changed."""
    changed = False
    if len(settings.mobile_link_key or "") < 32:
        settings.mobile_link_key = secrets.token_urlsafe(32)
        changed = True
    if not settings.mobile_link_id:
        settings.mobile_link_id = secrets.token_hex(8)
        changed = True
    return changed


def new_phone_key(settings):
    """Disconnect every paired phone: they need a new code to connect again."""
    settings.mobile_link_key = secrets.token_urlsafe(32)


class _DesktopRuntime:
    """What the machine routes need from ``request.app.state.runtime``."""

    def __init__(self, settings_getter, angles):
        self._settings_getter = settings_getter
        self.angles = angles     # AngleTemplateWorker; its request() is thread-safe.

    @property
    def settings(self):
        return self._settings_getter()

    @property
    def hrm(self):
        return config_from_settings(self.settings)   # Follows settings changed in the app.


class MachineServer:
    def __init__(self, settings_getter, angles, verifier_key=None, mobile_key=None,
                 host=None, port=None, encoder=None):
        from ..web.machine_api import MOBILE_KEY_ENV, VERIFIER_KEY_ENV, PhonePairing
        self._settings_getter = settings_getter
        self.verifier_key = os.environ.get(VERIFIER_KEY_ENV, "") if verifier_key is None else verifier_key
        self._fixed_mobile_key = os.environ.get(MOBILE_KEY_ENV, "") if mobile_key is None else mobile_key
        self.host = host or os.environ.get(HOST_ENV) or "0.0.0.0"
        self.port = int(port if port is not None else os.environ.get(PORT_ENV) or DEFAULT_PORT)
        self._runtime = _DesktopRuntime(settings_getter, angles)
        self._encoder = encoder
        self._server = None
        self._thread = None
        self.pairing = PhonePairing(self.identity)

    def mobile_key(self):
        """The key phones use now: a fixed one, else this computer's own while the link is on."""
        if self._fixed_mobile_key:
            return self._fixed_mobile_key
        settings = self._settings_getter()
        if settings is None or not getattr(settings, "mobile_link_enabled", False):
            return ""
        return settings.mobile_link_key or ""

    def identity(self):
        settings = self._settings_getter()
        desktop_id = getattr(settings, "mobile_link_id", "") if settings is not None else ""
        return desktop_id, socket.gethostname().removesuffix(".local"), self.mobile_key()

    def phone_address(self):
        """What a phone on this Wi-Fi connects to, e.g. http://192.168.1.20:8080."""
        return f"http://{local_ip()}:{self.port}"

    @property
    def enabled(self):
        from ..web.machine_api import MIN_KEY_LENGTH
        return max(len(self.verifier_key), len(self.mobile_key())) >= MIN_KEY_LENGTH

    def create_app(self):
        from fastapi import FastAPI
        from ..web.machine_api import create_router
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.state.runtime = self._runtime
        app.include_router(create_router(self.verifier_key, self.mobile_key, self._encoder,
                                         pairing=self.pairing))
        return app

    def start(self):
        """Start in the background; False when switched off or the web packages are missing."""
        if not self.enabled or self._thread is not None:
            return False
        try:
            import uvicorn
            app = self.create_app()
        except ImportError as exc:
            logging.warning("Machine APIs are off: %s. Run: pip install -r requirements-web.txt", exc)
            return False
        # A windowed Windows exe has no console. Use the dashboard's file logger
        # there, since Uvicorn's console formatter calls sys.stdout.isatty().
        has_console = sys.stdout is not None and sys.stderr is not None
        config = uvicorn.Config(app, host=self.host, port=self.port, workers=1,
                                log_config=uvicorn.config.LOGGING_CONFIG if has_console else None,
                                log_level="warning", timeout_graceful_shutdown=5)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._serve, name="machine-api", daemon=True)
        self._thread.start()
        return True

    def reconfigure(self):
        """After Settings change: start when the phone link was just switched on.

        Switching it off needs no restart: the key check answers 503 at once. The server keeps
        running for the face verifier if that has a key, and stays idle otherwise.
        """
        if not self.enabled:
            self.pairing.cancel()
            return False
        return self.start()

    def _serve(self):
        try:
            self._server.run()
        except BaseException:      # uvicorn exits with SystemExit when the port is taken.
            logging.exception("Machine APIs could not run on %s:%s", self.host, self.port)
            return
        if not self._server.started:
            logging.warning("Machine APIs did not start on %s:%s (is the port in use?)",
                            self.host, self.port)

    @property
    def running(self):
        return self._server is not None and self._server.started and not self._server.should_exit

    def stop(self, timeout=5):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
        self._server = self._thread = None
