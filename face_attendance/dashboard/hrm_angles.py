"""Keeps the Live Monitor's face templates current with the angle images in SV HRM, off the GUI thread."""
import logging
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from .. import angle_templates
from ..hrm_sync import HRMError, config_from_settings, configuration_problem


class AngleTemplateWorker(QObject):
    """One background sync at a time; a request during a sync runs one more afterwards.

    The engine notices the changed file by itself (``reload_if_catalog_changed``) and swaps its
    catalog in place, so the camera keeps running.
    """

    updated = pyqtSignal(object)    # angle_templates.SyncResult
    failed = pyqtSignal(str)

    def __init__(self, settings_getter, parent=None, sync=angle_templates.sync):
        super().__init__(parent)
        self._settings_getter = settings_getter
        self._sync = sync
        self._lock = threading.Lock()
        self._running = False
        self._again = False

    def request(self):
        settings = self._settings_getter()
        config = config_from_settings(settings)
        if configuration_problem(config):
            return
        with self._lock:
            if self._running:
                self._again = True
                return
            self._running = True
        threading.Thread(target=self._run, args=(config, angle_templates.store_path(settings)),
                         name="hrm-angle-templates", daemon=True).start()

    def _run(self, config, path):
        while True:
            try:
                result = self._sync(config, path)
            except (HRMError, OSError, ValueError) as exc:
                logging.warning("Could not update the face angle templates: %s", exc)
                self._emit(self.failed, str(exc))
            except Exception as exc:
                # Daemon thread: anything else would be lost silently and stop further syncs.
                logging.exception("Updating the face angle templates failed")
                self._emit(self.failed, str(exc))
            else:
                for problem in result.problems:
                    logging.warning("Face angle not usable: %s", problem)
                self._emit(self.updated, result)
            with self._lock:
                if not self._again:
                    self._running = False
                    return
                self._again = False

    @staticmethod
    def _emit(signal, value):
        try:
            signal.emit(value)
        except RuntimeError:
            pass  # The window closed meanwhile.
