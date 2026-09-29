from dataclasses import replace
from pathlib import Path
import unittest
import wave

from face_attendance.settings import Settings
from face_attendance.sound_events import EventSounds, SOUND_DIR, SOUND_LABELS


class Player:
    available = True
    def __init__(self, path, cooldown_sec=0):
        self.name = Path(path).stem
        self.plays = self.stops = 0
    def play(self):
        self.plays += 1
        return True
    def stop(self):
        self.stops += 1


class SoundEventTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.sounds = EventSounds(replace(Settings(), alert_path='/missing.wav'), Player, lambda: self.now)
        self.sounds.camera(True, True)

    def sample(self, at, tracks):
        self.now = at
        self.sounds.tracks(tracks)

    def test_bundled_wavs_are_distinct_valid_short_pcm(self):
        contents = set()
        for event in SOUND_LABELS:
            path = SOUND_DIR / (event + '.wav')
            with wave.open(str(path)) as wav:
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getnchannels(), 1)
                self.assertLess(wav.getnframes() / wav.getframerate(), .85)
            contents.add(path.read_bytes())
        self.assertEqual(len(contents), len(SOUND_LABELS))

    def test_detected_is_debounced_and_group_coalesced(self):
        tracks = [{'track_id': i, 'visible': True, 'state': 'RECOGNIZING'} for i in range(5)]
        self.sample(0, tracks)
        self.sample(.2, tracks)
        self.assertEqual(self.sounds.players['detected'].plays, 0)
        self.sample(.4, tracks)
        self.sample(4, tracks)
        self.assertEqual(self.sounds.players['detected'].plays, 1)

    def test_no_face_is_one_shot_after_departure_never_startup(self):
        self.sample(0, [])
        self.sample(10, [])
        self.assertEqual(self.sounds.players['lost'].plays, 0)
        self.sample(11, [{'track_id': 1, 'visible': True}])
        self.sample(12, [])
        self.sample(13, [])
        self.assertEqual(self.sounds.players['lost'].plays, 0)
        self.sample(14.1, [])
        self.sample(30, [])
        self.assertEqual(self.sounds.players['lost'].plays, 1)

    def test_quality_blocked_unknown_is_guidance_not_unenrolled(self):
        track = {'track_id': 1, 'visible': True, 'state': 'UNKNOWN', 'quality_ok': False}
        self.sample(0, [track])
        self.sample(2, [track])
        self.assertEqual(self.sounds.players['guidance'].plays, 1)
        self.assertEqual(self.sounds.players['unknown'].plays, 0)
        track['quality_ok'] = True
        self.sample(4, [track])
        self.sample(6, [track])
        self.sample(16, [track])
        self.assertEqual(self.sounds.players['unknown'].plays, 1)

    def test_confirmation_needs_all_checks_and_never_repeats(self):
        track = dict(track_id=1, visible=True, state='VERIFYING', identity_valid=True,
                     spoof_ok=True, liveness_ok=False, quality_ok=True)
        self.sample(0, [track])
        self.sample(2, [track])
        self.assertEqual(self.sounds.players['verified'].plays, 0)
        track['liveness_ok'] = True
        self.sample(3, [track])
        self.sample(6, [track])
        self.assertEqual(self.sounds.players['verified'].plays, 1)

    def test_priority_cooldown_mute_and_preview(self):
        self.assertTrue(self.sounds.play('guidance'))
        self.assertFalse(self.sounds.play('detected'))
        self.assertTrue(self.sounds.play('success'))
        self.assertGreater(self.sounds.players['guidance'].stops, 0)
        self.now = 2
        self.assertFalse(self.sounds.play('guidance'))
        self.sounds.settings.sounds_enabled = False
        self.assertFalse(self.sounds.play('error'))
        self.assertTrue(self.sounds.play('error', preview=True))

    def test_disconnect_is_distinct_from_no_face_and_pause_is_silent(self):
        self.sample(0, [{'track_id': 1, 'visible': True}])
        self.sounds.camera(False, True)
        self.sample(3, [])
        self.assertEqual(self.sounds.players['disconnected'].plays, 1)
        self.assertEqual(self.sounds.players['lost'].plays, 0)
        self.sounds.camera(False, True)
        self.sounds.camera(True, True)
        self.sounds.camera(True, False)
        self.assertEqual(self.sounds.players['disconnected'].plays, 1)
