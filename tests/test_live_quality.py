import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from tests.test_dashboard import _app, temp_settings


class LiveQualityTests(unittest.TestCase):
    def test_live_daily_total_latency_guidance_and_sound_preview(self):
        from PyQt6.QtWidgets import QLabel
        from face_attendance.dashboard.app import MainWindow
        app = _app()
        with tempfile.TemporaryDirectory() as directory, patch('face_attendance.dashboard.screens.live.EventSounds') as sounds:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            try:
                live = window.screens[0]
                live.on_stats(dict(status='CONNECTED', verified_today=12, known_faces=2,
                                   latency_ms=108.4, storage_ready=True, quality_blocked=1,
                                   quality_details='Track 2: Improve lighting'))
                self.assertIn('108 ms', live.cam_pipeline.text())
                # Real card values must come from persisted attendance, not visible identities.
                self.assertIn('12', [label.text() for label in live.cards['verified'].findChildren(
                    QLabel)])
                live.sound_choice.setCurrentIndex(live.sound_choice.findData('unknown'))
                live.test_sound_button.click()
                sounds.return_value.play.assert_called_with('unknown', preview=True)
                settings_screen = window.screens[-1]
                self.assertTrue(settings_screen.fields['sounds_enabled'].isChecked())
                settings_screen.fields['sound_guidance'].setChecked(False)
                self.assertFalse(settings_screen.collect()['sound_guidance'])
                app.processEvents()
            finally:
                window.close()
                app.processEvents()

    def test_stats_exclude_bad_quality_from_unknown_and_use_persisted_total(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        from face_attendance.models import State
        from tests.test_attendance import verified
        from tests.test_pipeline import FakeBackend
        _app()
        with tempfile.TemporaryDirectory() as directory:
            engine = AttendanceEngine(temp_settings(directory), backend=FakeBackend(), use_lock=False)
            track = verified()
            track.state, track.quality_ok, track.identity_valid = State.UNKNOWN, False, False
            track.quality_prompt = 'Hold still'
            engine.tracker.tracks[1] = track
            engine.persistence.daily_totals = {'date': datetime.now().date().isoformat(), 'count': 7}
            stats = engine._statistics('CONNECTED', 30, '')
            self.assertEqual(stats['verified_today'], 7)
            self.assertEqual(stats['unknown_faces'], 0)
            self.assertEqual(stats['quality_blocked'], 1)
            self.assertIn('Hold still', stats['quality_details'])
            engine.persistence.daily_totals = {'date': '2000-01-01', 'count': 100}
            self.assertIsNone(engine._statistics('CONNECTED', 30, '')['verified_today'])
            engine.shutdown()
