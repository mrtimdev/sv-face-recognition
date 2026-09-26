"""Audio uses native playback without blocking the recognition/UI thread."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from face_attendance.audio import SoundPlayer


class AudioTests(unittest.TestCase):
    def test_macos_native_playback_cooldown_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "alert with spaces.wav"
            wav.touch()
            with patch("face_attendance.audio.sys.platform", "darwin"), \
                    patch("face_attendance.audio.Path.is_file", return_value=True), \
                    patch("face_attendance.audio.subprocess.Popen") as spawn, \
                    patch("face_attendance.audio.time.monotonic", side_effect=[0., 1., 4., 5.]):
                process = spawn.return_value
                process.poll.return_value = None
                player = SoundPlayer(wav, cooldown_sec=3.)
                self.assertTrue(player.available)
                self.assertTrue(player.play())
                self.assertFalse(player.play())  # Cooldown.
                self.assertFalse(player.play())  # Still playing; no overlap.
                process.poll.return_value = 0
                self.assertTrue(player.play())
                self.assertEqual(spawn.call_args.args[0], ["/usr/bin/afplay", str(wav.resolve())])
                process.poll.return_value = None
                player.stop()
                process.terminate.assert_called_once()
                process.wait.assert_called_once()

    def test_launch_failure_does_not_consume_cooldown(self):
        with patch("face_attendance.audio.sys.platform", "darwin"), \
                patch("face_attendance.audio.Path.is_file", return_value=True), \
                patch("face_attendance.audio.subprocess.Popen", side_effect=[OSError("unavailable"), Mock()]):
            player = SoundPlayer("alert.wav")
            with self.assertLogs(level="WARNING"):
                self.assertFalse(player.play())
            self.assertTrue(player.play())
            player.stop()

    def test_missing_file_is_optional(self):
        player = SoundPlayer("/missing/alert.wav")
        self.assertFalse(player.available)
        self.assertFalse(player.play())
        player.stop()

    def test_persistence_alert_uses_shared_backend(self):
        from face_attendance.config import Config
        from face_attendance.persistence import PersistenceWorker
        with patch("face_attendance.persistence.SoundPlayer") as factory:
            worker = PersistenceWorker(Config(), {})
            worker._init_audio()
            worker._alert()
            factory.assert_called_once_with(Config().alert_path, Config().alert_cooldown_sec)
            factory.return_value.play.assert_called_once()
