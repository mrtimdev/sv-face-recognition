"""Real Qt ownership and lifecycle tests; no camera or recognition model runs."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PyQt6.QtTest import QTest

from tests.test_dashboard import _app, temp_settings


class DetachedPagesTests(unittest.TestCase):
    def setUp(self):
        from face_attendance.dashboard.app import MainWindow
        self.application = _app()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = temp_settings(self.temp.name)
        settings._path = Path(self.temp.name) / 'settings.json'
        self.window = MainWindow(settings, use_lock=False)
        self.window.show()
        self.application.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.application.processEvents()

    def assert_closed(self, external):
        from PyQt6 import sip
        self.assertTrue(sip.isdeleted(external) or not external.isVisible())

    def detached(self, index):
        result = self.window.detach_page(index)
        self.application.processEvents()
        controller = self.window._detached_pages[index]
        self.assertIs(controller.window, result)
        self.assertIs(controller.screen, self.window.screens[index])
        self.assertTrue(controller.is_detached)
        self.assertTrue(result.isWindow())
        self.assertTrue(result.isVisible())
        self.assertIs(self.window.stack.widget(index), controller.host)
        return result, controller

    def test_both_monitors_remain_live_while_report_uses_dashboard(self):
        window = self.window
        originals = list(window.screens)
        count = window.stack.count()
        live_window, live = self.detached(0)
        plates_window, plates = self.detached(6)
        self.assertIsNot(live_window, plates_window)
        window.nav.setCurrentRow(1)
        self.application.processEvents()
        self.assertIs(window.stack.currentWidget(), originals[1])
        self.assertTrue(live.screen.isVisible())
        self.assertTrue(plates.screen.isVisible())
        self.assertEqual(window.screens, originals)
        self.assertIs(live.screen.engine, window.engine)
        self.assertIs(plates.screen.engine, window.engine)
        self.assertEqual(window.stack.count(), count)
        frame = np.full((24, 32, 3), 50, np.uint8)
        with patch.object(live.screen.video, 'set_frame', wraps=live.screen.video.set_frame) as receive:
            window.engine.frameReady.emit(frame)
            receive.assert_called_once_with(frame)
        for index, screen in enumerate(originals):
            window.nav.setCurrentRow(index)
            self.application.processEvents()
            expected = window._detached_pages[index].host if index in (0, 6) else screen
            self.assertIs(window.stack.currentWidget(), expected)
        self.assertTrue(live.host.placeholder.isVisible() or window.nav.currentRow() != 0)
        self.assertTrue(plates.host.placeholder.isVisible())

    def test_close_and_return_reuse_pages_without_stopping_processing(self):
        window = self.window
        live_window, live = self.detached(0)
        plates_window, plates = self.detached(6)
        with patch.object(window.engine, 'stop') as engine_stop, \
                patch.object(plates.screen, 'stop') as plate_stop:
            live_window.close()
            self.application.processEvents()
            self.assertFalse(live.is_detached)
            self.assertIsNone(live.window)
            self.assertIs(window.stack.widget(0), live.screen)
            plates_window.return_button.click()
            self.application.processEvents()
            self.assertIs(window.attach_page(6), plates.screen)
            self.assertFalse(plates.is_detached)
            self.assertIsNone(plates.window)
            self.assertIs(window.stack.widget(6), plates.screen)
            engine_stop.assert_not_called()
            plate_stop.assert_not_called()

    def test_repeat_detach_restores_minimized_existing_window(self):
        external, controller = self.detached(0)
        external.showMinimized()
        self.application.processEvents()
        self.assertIs(self.window.detach_page(0), external)
        self.application.processEvents()
        self.assertFalse(external.isMinimized())
        self.assertIs(controller.screen.window(), external)
        self.window.nav.setCurrentRow(0)
        controller.host.show_window_button.click()
        self.application.processEvents()
        self.assertIs(controller.window, external)
        controller.host.return_button.click()
        self.application.processEvents()
        self.assertFalse(controller.is_detached)
        self.assertIs(self.window.stack.currentWidget(), controller.screen)

    def test_hud_and_menu_toggle_same_page_and_native_close_preserves_other_navigation(self):
        for index in (0, 6):
            with self.subTest(index=index):
                screen = self.window.screens[index]
                self.window.nav.setCurrentRow(index)
                screen.preview_hud.detach.click()
                self.application.processEvents()
                controller = self.window._detached_pages[index]
                self.assertTrue(controller.is_detached)
                self.assertEqual(screen.detach_action.text(), 'Return to dashboard')
                screen.detach_action.trigger()
                self.application.processEvents()
                self.assertFalse(controller.is_detached)
                self.assertEqual(screen.detach_action.text(), 'Open in new window')
                external, controller = self.detached(index)
                self.window.nav.setCurrentRow(1)
                external.close()
                self.application.processEvents()
                self.assertEqual(self.window.nav.currentRow(), 1)
                self.assertIs(self.window.stack.currentWidget(), self.window.screens[1])

    def test_fullscreen_affects_only_external_window_and_redocking_restores_chrome(self):
        for index in (0, 6):
            with self.subTest(index=index):
                external, controller = self.detached(index)
                self.window.nav.setCurrentRow(1)
                controller.screen._toggle_fullscreen()
                self.application.processEvents()
                self.assertTrue(external.isFullScreen())
                self.assertFalse(self.window.isFullScreen())
                self.assertTrue(self.window.header.isVisible())
                self.assertTrue(self.window.sidebar.isVisible())
                self.window.attach_page(index)
                self.application.processEvents()
                self.assertFalse(controller.screen._fs_active)
                self.assertIs(controller.screen.window(), self.window)
                self.assertFalse(self.window.isFullScreen())
                self.assertTrue(self.window.header.isVisible())

    def test_return_from_native_fullscreen_reopens_as_normal_window(self):
        # The OS titlebar fullscreen control does not set the screen HUD flag.
        for index in (0, 6):
            with self.subTest(index=index):
                external, controller = self.detached(index)
                external.showFullScreen()
                self.application.processEvents()
                self.assertTrue(external.isFullScreen())
                self.assertFalse(getattr(controller.screen, '_fs_active', False))
                controller.screen.detach_action.trigger()
                self.application.processEvents()
                self.assertFalse(controller.is_detached)
                self.assertFalse(external.isVisible())
                self.assertFalse(external.isFullScreen())
                reopened, _ = self.detached(index)
                self.assertIs(reopened, external)
                # Let native fullscreen animations finish, rather than only
                # inspecting the synchronous Qt state immediately after show.
                QTest.qWait(1000)
                self.assertFalse(reopened.isFullScreen())
                self.assertTrue(reopened.header.isVisible())
                self.assertFalse(self.window.isFullScreen())
                self.window.attach_page(index)

    def test_native_fullscreen_close_and_dashboard_return_restore_window_state(self):
        for index in (0, 6):
            for close_window in (True, False):
                with self.subTest(index=index, close_window=close_window):
                    external, controller = self.detached(index)
                    external.showFullScreen()
                    self.application.processEvents()
                    if close_window:
                        external.close()
                    else:
                        controller.host.return_button.click()
                    self.application.processEvents()
                    self.assertFalse(controller.is_detached)
                    self.assertFalse(external.isVisible())
                    self.assertFalse(external.isFullScreen())
                    self.assertFalse(self.window.isFullScreen())
                    reopened, _ = self.detached(index)
                    self.assertFalse(reopened.isFullScreen())
                    self.assertTrue(reopened.header.isVisible())
                    self.window.attach_page(index)

    def test_detach_from_native_dashboard_fullscreen_restores_dashboard(self):
        for index in (0, 6):
            with self.subTest(index=index):
                self.window.nav.setCurrentRow(index)
                self.window.showFullScreen()
                self.application.processEvents()
                external, controller = self.detached(index)
                self.assertFalse(self.window.isFullScreen())
                self.assertFalse(external.isFullScreen())
                self.assertTrue(self.window.header.isVisible())
                self.assertTrue(self.window.sidebar.isVisible())
                self.window.attach_page(index)

    def test_return_from_hud_fullscreen_can_reopen_and_toggle_again(self):
        for index in (0, 6):
            with self.subTest(index=index):
                external, controller = self.detached(index)
                for _ in range(2):
                    controller.screen._toggle_fullscreen()
                    self.application.processEvents()
                    self.assertTrue(external.isFullScreen())
                    controller.screen.detach_action.trigger()
                    self.application.processEvents()
                    self.assertFalse(controller.screen._fs_active)
                    self.assertFalse(external.isVisible())
                    self.assertFalse(external.isFullScreen())
                    self.assertIs(self.window.detach_page(index), external)
                    QTest.qWait(1000)
                    self.assertFalse(external.isFullScreen())
                    self.assertTrue(external.header.isVisible())
                self.window.attach_page(index)

    def test_access_is_shared_and_revocation_redocks_sensitive_windows(self):
        from face_attendance.dashboard.access import access_for
        from face_attendance.users import User
        detached = [self.detached(index) for index in (0, 6)]
        for external, controller in detached:
            self.assertIs(access_for(controller.screen), self.window.access)
            self.assertIs(external.access, self.window.access)
        report_only = User('test', 'reader', 'Reader', '', '', '', 'user', ['view_reports'])
        self.window.access.set_user(report_only)
        self.application.processEvents()
        self.assertEqual(self.window.nav.currentRow(), 1)
        for external, controller in detached:
            self.assertFalse(controller.is_detached)
            self.assert_closed(external)
            self.assertFalse(controller.screen.isVisible())

    def test_settings_and_theme_reach_original_detached_screens(self):
        controllers = [self.detached(index)[1] for index in (0, 6)]
        updated = replace(self.window.settings, theme='dark', source=3)
        self.window._broadcast_settings(updated, False)
        self.application.processEvents()
        for controller in controllers:
            self.assertIs(controller.screen.settings, updated)
            self.assertEqual(controller.screen._theme, 'dark')
            self.assertTrue(controller.window.isVisible())

    def test_root_close_closes_external_windows_without_resurrecting_them(self):
        detached = [self.detached(index) for index in (0, 6)]
        self.window.close()
        self.application.processEvents()
        self.assertFalse(self.window.isVisible())
        for external, controller in detached:
            self.assert_closed(external)
            self.assertFalse(controller.is_detached)

    def test_repeated_detach_cycles_reuse_one_window_and_keep_stack_order(self):
        for index in (0,6):
            first,controller = self.detached(index)
            for _ in range(4):
                self.window.attach_page(index)
                self.assertIs(self.window.stack.widget(index),controller.screen)
                self.assertIs(self.window.detach_page(index),first)
                self.assertEqual(self.window.stack.count(),len(self.window.screens)+1)
            self.window.attach_page(index)

    def test_flash_follows_live_window_and_settings_can_raise_dashboard(self):
        external,controller = self.detached(0)
        self.assertIs(self.window.capture_flash.parentWidget(),external.centralWidget())
        controller.screen.flashPreviewRequested.emit()
        self.assertTrue(self.window.capture_flash.isVisible())
        self.window.showMinimized()
        controller.screen.settingsRequested.emit()
        self.application.processEvents()
        self.assertFalse(self.window.isMinimized())
        self.assertEqual(self.window.nav.currentRow(),5)
        self.assertTrue(external.isVisible())
        self.window.attach_page(0)
        self.assertIs(self.window.capture_flash.parentWidget(),self.window.centralWidget())

    def test_signout_start_hides_external_windows_and_blocks_reopening(self):
        detached = [self.detached(index) for index in (0,6)]
        from face_attendance.dashboard import app as shell
        with patch.object(shell.ShutdownSequence,'start'):
            self.window._begin_exit('signout')
        for external,controller in detached:
            self.assertFalse(controller.is_detached)
            self.assert_closed(external)
        self.assertIsNone(self.window.detach_page(0))
        # Avoid awaiting a deliberately stubbed shutdown sequence in teardown.
        self.window._exit = None

    def test_new_window_request_is_denied_after_view_access_is_revoked(self):
        from face_attendance.users import User
        report_only = User('test','reader','Reader','','','','user',['view_reports'])
        self.window.access.set_user(report_only)
        with patch.object(self.window.access,'deny') as deny:
            self.assertIsNone(self.window.detach_page(0))
            self.assertIsNone(self.window.detach_page(6))
            self.assertEqual(deny.call_count,2)
        self.assertFalse(any(page.is_detached for page in self.window._detached_pages.values()))

    def test_plate_worker_and_preview_timer_survive_reparenting(self):
        screen = self.window.screens[6]
        packet = SimpleNamespace(sequence=1,generation=1,frame=np.zeros((160,240,3),np.uint8))
        worker = SimpleNamespace(isRunning=lambda:True, preview_packet=lambda:packet,
                                 inference_ms=20, manual=threading.Event(),automatic=threading.Event())
        screen.worker = worker
        screen.timer.start()
        try:
            with patch('license_plate.screen.PlateWorker') as factory:
                external,controller = self.detached(6)
                self.window.nav.setCurrentRow(1)
                screen._preview()
                self.assertTrue(screen.preview.has_frame())
                self.assertIs(screen.worker,worker)
                self.assertTrue(screen.timer.isActive())
                self.window.attach_page(6)
                self.assertIs(screen.worker,worker)
                self.assertTrue(screen.timer.isActive())
                factory.assert_not_called()
        finally:
            screen.timer.stop()
            screen.worker = None

    def test_detach_from_dashboard_fullscreen_restores_dashboard_first(self):
        for index in (0,6):
            with self.subTest(index=index):
                self.window.nav.setCurrentRow(index)
                screen = self.window.screens[index]
                screen._toggle_fullscreen()
                self.assertTrue(self.window.isFullScreen())
                external,controller = self.detached(index)
                self.assertFalse(self.window.isFullScreen())
                self.assertFalse(screen._fs_active)
                self.assertTrue(self.window.header.isVisible())
                self.assertTrue(self.window.sidebar.isVisible())
                self.window.attach_page(index)
