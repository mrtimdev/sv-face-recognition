"""Request packaged macOS camera permission on Qt's GUI thread before capture.

Unbundled source runs retain their launcher/OS permission behavior. Qt's macOS
permission backend requires application-bundle metadata; offscreen runs must
never trigger an OS privacy dialog.
"""
from pathlib import Path
import plistlib
import sys

from PyQt6.QtCore import QCoreApplication, QThread, Qt
from PyQt6.QtGui import QGuiApplication


DENIED_MESSAGE = ('Camera access is denied. Enable this application in macOS System Settings '
                  '→ Privacy & Security → Camera, then restart the application.')
_PENDING = set()


def _packaged_camera_permission():
    if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
        return False
    if QGuiApplication.platformName() in ('offscreen', 'minimal'):
        return False
    executable = Path(sys.executable)
    for parent in executable.parents:
        if parent.name == 'Contents':
            try:
                with (parent / 'Info.plist').open('rb') as handle:
                    return bool(plistlib.load(handle).get('NSCameraUsageDescription'))
            except (OSError, ValueError, plistlib.InvalidFileException):
                return False
    return False


def with_camera_permission(source, action, on_error, context=None):
    """Run action once granted; a pending request never starts a camera worker."""
    def invoke():
        try:
            if context is not None and _packaged_camera_permission():
                host = context.window()
                if not host.isVisible() or getattr(host, '_exit', None) is not None:
                    return
            if context is not None:
                from .access import guard
                if not guard(context, 'control_engine', 'start camera recognition'):
                    return
            action()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            on_error(str(exc))

    # IP streams and files do not require access to a local camera.
    local = isinstance(source, int) or str(source).strip().isdecimal()
    if not local or not _packaged_camera_permission():
        invoke()
        return
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        on_error('Camera authorization must be requested from the application GUI thread.')
        return
    try:
        from PyQt6.QtCore import QCameraPermission
        permission = QCameraPermission()
        status = app.checkPermission(permission)
        if status == Qt.PermissionStatus.Granted:
            invoke()
        elif status == Qt.PermissionStatus.Denied:
            on_error(DENIED_MESSAGE)
        else:
            key = id(action.__self__) if hasattr(action, '__self__') else id(action)
            if key in _PENDING:
                return
            _PENDING.add(key)

            def finished(result):
                _PENDING.discard(key)
                if result.status() == Qt.PermissionStatus.Granted:
                    invoke()
                else:
                    on_error(DENIED_MESSAGE)

            try:
                app.requestPermission(permission, finished)
            except Exception:
                _PENDING.discard(key)
                raise
    except (ImportError, AttributeError, RuntimeError) as exc:
        on_error(f'Camera authorization is unavailable: {exc}. Restart the application and check macOS Camera privacy settings.')
