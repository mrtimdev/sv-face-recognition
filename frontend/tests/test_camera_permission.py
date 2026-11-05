"""Camera authorization routes are tested without opening an OS dialog."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import Qt
from face_attendance.dashboard import camera_permission as permission


class CameraPermissionTests(unittest.TestCase):
    def setUp(self):
        permission._PENDING.clear()
        self.action, self.error = Mock(), Mock()

    def check(self, status):
        thread = object()
        app = NS(thread=lambda: thread, checkPermission=Mock(return_value=status), requestPermission=Mock())
        return app, patch.multiple(permission, _packaged_camera_permission=lambda: True), \
            patch.object(permission.QCoreApplication, 'instance', return_value=app), \
            patch.object(permission.QThread, 'currentThread', return_value=thread)

    def test_source_or_network_runs_do_not_call_qt_permission_backend(self):
        with patch.object(permission, '_packaged_camera_permission', return_value=False), \
                patch.object(permission.QCoreApplication, 'instance') as application:
            permission.with_camera_permission(0, self.action, self.error)
            application.assert_not_called()
        self.action.assert_called_once()
        self.action.reset_mock()
        with patch.object(permission, '_packaged_camera_permission', return_value=True), \
                patch.object(permission.QCoreApplication, 'instance') as application:
            permission.with_camera_permission('rtsp://camera/stream', self.action, self.error)
            application.assert_not_called()
        self.action.assert_called_once()

    def test_granted_starts_without_request(self):
        app, packaged, instance, thread = self.check(Qt.PermissionStatus.Granted)
        with packaged, instance, thread:
            permission.with_camera_permission(0, self.action, self.error)
        self.action.assert_called_once()
        app.requestPermission.assert_not_called()

    def test_denied_reports_actionable_message_without_start(self):
        app, packaged, instance, thread = self.check(Qt.PermissionStatus.Denied)
        with packaged, instance, thread:
            permission.with_camera_permission(0, self.action, self.error)
        self.action.assert_not_called()
        self.assertIn('Privacy & Security', self.error.call_args.args[0])
        app.requestPermission.assert_not_called()

    def test_undetermined_waits_for_granted_callback_and_coalesces_requests(self):
        app, packaged, instance, thread = self.check(Qt.PermissionStatus.Undetermined)
        with packaged, instance, thread:
            permission.with_camera_permission(0, self.action, self.error)
            permission.with_camera_permission(0, self.action, self.error)
            app.requestPermission.assert_called_once()
            self.action.assert_not_called()
            callback = app.requestPermission.call_args.args[1]
            callback(NS(status=lambda: Qt.PermissionStatus.Granted))
        self.action.assert_called_once()
        self.assertEqual(permission._PENDING, set())

    def test_callback_denial_does_not_start(self):
        app, packaged, instance, thread = self.check(Qt.PermissionStatus.Undetermined)
        with packaged, instance, thread:
            permission.with_camera_permission(0, self.action, self.error)
            app.requestPermission.call_args.args[1](NS(status=lambda: Qt.PermissionStatus.Denied))
        self.action.assert_not_called()
        self.error.assert_called_once_with(permission.DENIED_MESSAGE)

    def test_failed_request_releases_pending_state(self):
        app, packaged, instance, thread = self.check(Qt.PermissionStatus.Undetermined)
        app.requestPermission.side_effect = RuntimeError('backend unavailable')
        with packaged, instance, thread:
            permission.with_camera_permission(0, self.action, self.error)
        self.assertFalse(permission._PENDING)
        self.action.assert_not_called()
        self.assertIn('unavailable', self.error.call_args.args[0])

    def test_non_gui_thread_is_rejected_before_request(self):
        app, packaged, instance, thread = self.check(Qt.PermissionStatus.Undetermined)
        with packaged, instance, patch.object(permission.QThread, 'currentThread', return_value=object()):
            permission.with_camera_permission(0, self.action, self.error)
        self.action.assert_not_called()
        app.requestPermission.assert_not_called()
        self.assertIn('GUI thread', self.error.call_args.args[0])

    def test_grant_after_window_closes_cannot_start_capture(self):
        app, packaged, instance, thread = self.check(Qt.PermissionStatus.Undetermined)
        context = NS(window=lambda: NS(isVisible=lambda: False))
        with packaged, instance, thread:
            permission.with_camera_permission(0, self.action, self.error, context=context)
            app.requestPermission.call_args.args[1](NS(status=lambda: Qt.PermissionStatus.Granted))
        self.action.assert_not_called()
