"""Built-in, debounced dashboard cues. One owner prevents overlapping audio."""
from pathlib import Path
import time

from .audio import SoundPlayer

SOUND_DIR = Path(__file__).resolve().parent / "assets" / "sounds"
SOUND_LABELS = {
    "detected": "Face detected", "lost": "No face in view", "unknown": "Not enrolled",
    "guidance": "Adjust position or lighting", "verified": "Verification complete",
    "success": "Attendance saved", "error": "Error", "disconnected": "Camera disconnected",
}
COOLDOWNS = {"detected": 3., "lost": 8., "unknown": 8., "guidance": 10.,
             "verified": 2., "success": 1., "error": 3., "disconnected": 8.}
PRIORITY = {"detected": 1, "lost": 1, "guidance": 2, "unknown": 3,
            "verified": 4, "success": 5, "error": 6, "disconnected": 6}


class EventSounds:
    def __init__(self, settings, player_factory=SoundPlayer, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.players = {key: player_factory(SOUND_DIR / (key + '.wav'), cooldown_sec=0)
                        for key in SOUND_LABELS}
        # Existing custom success WAVs still work; every other cue is built in.
        custom = Path(settings.alert_path).expanduser()
        if custom.is_file():
            self.players['success'] = player_factory(custom, cooldown_sec=0)
        self._last = {}
        self._active = None
        self._active_until = float('-inf')
        self.reset()

    @property
    def available(self):
        return any(player.available for player in self.players.values())

    def reset(self):
        self._tracks = {}
        self._had_face = False
        self._empty_since = None
        self._connected = False

    def _enabled(self, event):
        if not self.settings.sounds_enabled:
            return False
        if event in ('detected', 'lost'):
            return self.settings.sound_detection
        if event == 'guidance':
            return self.settings.sound_guidance
        if event == 'unknown':
            return self.settings.sound_unknown
        return True

    def play(self, event='success', preview=False):
        now = self.clock()
        if event not in self.players or (not preview and not self._enabled(event)):
            return False
        if not preview and now - self._last.get(event, float('-inf')) < COOLDOWNS[event]:
            return False
        if self._active and now < self._active_until:
            if not preview and PRIORITY[event] <= PRIORITY[self._active]:
                return False
        # Terminate the preceding cue even if the system player ran late.
        if self._active:
            self.players[self._active].stop()
        if not self.players[event].play():
            return False
        self._last[event] = now
        self._active, self._active_until = event, now + .85
        return True

    def camera(self, connected, active):
        if not active:
            self.stop()
            self.reset()
            return
        if self._connected and not connected:
            self.play('disconnected')
            self.reset()
        self._connected = connected

    def tracks(self, tracks):
        if not self._connected:
            return
        now = self.clock()
        visible = [track for track in tracks if track.get('visible')]
        for tid in list(self._tracks):
            if now - self._tracks[tid]['last'] > 3.:
                del self._tracks[tid]
        if visible:
            self._had_face = True
            self._empty_since = None
        elif self._had_face:
            if self._empty_since is None:
                self._empty_since = now
            if now - self._empty_since >= 2. and self.play('lost'):
                self._had_face = False
        candidates = []
        for track in visible:
            state = self._tracks.setdefault(track['track_id'],
                {'first': now, 'last': now, 'sent': set(), 'reason': '', 'reason_since': now})
            state['last'] = now
            condition = ('guidance' if not track.get('quality_ok', True) or track.get('ambiguous') else
                         'unknown' if track.get('state') == 'UNKNOWN' else '')
            if state['reason'] != condition:
                state['reason'], state['reason_since'] = condition, now
            event = None
            if (track.get('identity_valid') and track.get('spoof_ok') and track.get('liveness_ok')
                    and track.get('quality_ok', True) and not track.get('ambiguous')
                    and track.get('state') not in ('SUCCESS', 'COOLDOWN', 'CAPTURING')
                    and 'verified' not in state['sent']):
                event = 'verified'
            elif condition and now - state['reason_since'] >= 1.5 and condition not in state['sent']:
                event = condition
            elif now - state['first'] >= .35 and 'detected' not in state['sent']:
                event = 'detected'
            if event and self._enabled(event):
                candidates.append((PRIORITY[event], event, state))
        # Coalesce simultaneous people into one cue; never queue stale sounds.
        if candidates:
            _, event, _ = max(candidates, key=lambda item: item[0])
            if self.play(event):
                for _, candidate, state in candidates:
                    if candidate == event:
                        state['sent'].add(event)
                        state['sent'].add('detected')

    def stop(self):
        for player in self.players.values():
            player.stop()
        self._active = None
