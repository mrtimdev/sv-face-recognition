"""Responsive OpenCV rendering. This module never mutates attendance state."""
import math
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

import cv2
import numpy as np

from .models import State
from .anti_spoof import live_sample


GREEN = (100, 225, 95)
RED = (95, 90, 250)
AMBER = (85, 195, 250)
WHITE = (242, 244, 242)
GRAY = (145, 155, 162)
DARK = (27, 24, 20)
LINE = (61, 60, 53)
FONT = cv2.FONT_HERSHEY_SIMPLEX


@lru_cache(maxsize=1024)
def text_size(value, scale, thickness):
    return cv2.getTextSize(value, FONT, scale, thickness)[0]


def duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


@dataclass(frozen=True)
class Layout:
    feed_width: int
    height: int

    @property
    def scale(self):
        return max(0.75, min(2.0, self.height / 720))

    @property
    def hud_width(self):
        return max(190, int(self.feed_width * 0.225))

    def px(self, value):
        return max(1, round(value * self.scale))

    def font(self, value=0.55):
        return round(max(0.36, value * self.scale), 3)


def tint(image, box, color, alpha):
    x1, y1, x2, y2 = map(int, box)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(image.shape[1], x2), min(image.shape[0], y2)
    if x2 <= x1 or y2 <= y1 or alpha <= 0:
        return
    roi = image[y1:y2, x1:x2]
    cv2.addWeighted(roi, 1 - alpha, np.full_like(roi, color), alpha, 0, dst=roi)


def text(image, value, position, color=WHITE, scale=0.5, thickness=1, max_width=None):
    value = str(value).encode("ascii", "replace").decode("ascii")
    if max_width:
        while value and text_size(value, scale, thickness)[0] > max_width:
            value = value[:-4] + "..." if len(value) > 4 else value[:-1]
    cv2.putText(image, value, tuple(map(int, position)), FONT, scale, color, thickness, cv2.LINE_AA)


def corners(image, box, color, thickness=2, arm=None):
    t, r, b, l = map(int, box)
    arm = arm or max(10, min(r - l, b - t) // 4)
    for x, y, dx, dy in ((l, t, 1, 1), (r, t, -1, 1), (l, b, 1, -1), (r, b, -1, -1)):
        cv2.line(image, (x, y), (x + dx * arm, y), color, thickness, cv2.LINE_AA)
        cv2.line(image, (x, y), (x, y + dy * arm), color, thickness, cv2.LINE_AA)


class AnimationManager:
    def __init__(self, config):
        self.config = config
        self.captures = []
        self.notices = []

    def capture(self, job, now):
        self.captures.append((now, job.employee_name, job.event_id))

    def result(self, kind, result, now):
        shutter_end = max((event[0] + self.config.shutter_duration for event in self.captures
                           if event[2] == result.job.event_id), default=now)
        self.notices.append((max(now, shutter_end), kind, result))

    def active(self, now):
        self.captures = [event for event in self.captures if now - event[0] < 0.9]
        self.notices = [event for event in self.notices if now - event[0] < self.config.success_duration]
        return self.captures, [event for event in self.notices if event[0] <= now]


class UIRenderer:
    def __init__(self, config, capture_effects=True):
        self.config = config
        self.capture_effects = capture_effects
        self.animations = AnimationManager(config)
        self.canvas = None
        self.overlay_canvas = None

    def render_overlay(self, clean_frame, tracks, now, wall_time, camera_status, recognition_error=""):
        """Brackets, scan line and notifications only: no HUD, no resizing.

        Used by the dashboard, which draws its statistics as Qt widgets beside
        the video. The camera frame itself is never modified; the caller keeps
        ownership of ``clean_frame`` and receives an internal canvas.
        """
        if self.overlay_canvas is None or self.overlay_canvas.shape != clean_frame.shape:
            self.overlay_canvas = np.empty_like(clean_frame)
        image = self.overlay_canvas
        np.copyto(image, clean_frame)
        height, width = image.shape[:2]
        layout = Layout(width, height)
        scan_y = int((now * height / 5) % height)
        tint(image, (0, scan_y, width, scan_y + layout.px(2)), GREEN, 0.22)
        for track in tracks.values():
            if now - track.last_seen <= self.config.session_timeout:
                self._face(image, track, now, layout)
        captures, notices = self.animations.active(now)
        self._capture(image, captures, now, layout)
        self._notices(image, notices, now, wall_time, layout)
        if camera_status != "CONNECTED":
            self._status(image, camera_status, "Attendance paused until live video returns", layout)
        elif recognition_error:
            self._status(image, "RECOGNITION UNAVAILABLE", "Check the dashboard log", layout)
        return image

    def render(self, clean_frame, tracks, now, wall_time, camera_status, camera_fps,
               preview_fps, enrolled, attendance, recognition_error=""):
        h, w = clean_frame.shape[:2]
        layout = Layout(w, h)
        shape = (h, w + layout.hud_width, 3)
        if self.canvas is None or self.canvas.shape != shape:
            self.canvas = np.empty(shape, dtype=np.uint8)
        image = self.canvas
        image[:, :w] = clean_frame
        image[:, w:] = DARK
        scan_y = int((now * h / 5) % h)
        tint(image, (0, scan_y, w, scan_y + layout.px(2)), GREEN, 0.22)
        for track in tracks.values():
            if now - track.last_seen <= self.config.session_timeout:
                self._face(image[:, :w], track, now, layout)
        captures, notices = self.animations.active(now)
        self._capture(image[:, :w], captures, now, layout)
        self._notices(image[:, :w], notices, now, wall_time, layout)
        self._hud(image, layout, tracks, now, wall_time, camera_status, camera_fps,
                  preview_fps, enrolled, attendance)
        if camera_status != "CONNECTED":
            self._status(image[:, :w], camera_status, "Attendance paused until live video returns", layout)
        elif recognition_error:
            self._status(image[:, :w], "RECOGNITION UNAVAILABLE", "Check terminal logs", layout)
        return image

    def _face(self, image, track, now, layout):
        h, w = image.shape[:2]
        t, r, b, l = map(int, track.bounding_box)
        l, r, t, b = max(2, l), min(w - 3, r), max(2, t), min(h - 3, b)
        if r <= l or b <= t:
            return
        known = bool(track.employee_id) and track.state != State.UNKNOWN
        color = GREEN if known else RED if track.state == State.UNKNOWN else AMBER
        recorded = track.state in (State.SUCCESS, State.COOLDOWN)
        if known and not recorded and track.state != State.CAPTURING:
            color = GREEN if track.spoof_ok and track.liveness_ok else AMBER
        if track.state == State.ERROR:
            color = RED
        fade = 1.0 if track.visible else max(0, 1 - (now - track.last_seen) / self.config.session_timeout)
        color = tuple(int(channel * fade) for channel in color)
        thick = layout.px(2) + (int(math.sin(now * 6) + 1) if track.state == State.UNKNOWN else 0)
        corners(image, (t, r, b, l), color, thick)
        state = track.state
        if not track.visible:
            label = "AUTO RESET" if state == State.UNKNOWN else "REACQUIRING..."
        elif state == State.ERROR:
            label = "SAVE FAILED / RETRYING"
        elif track.identity_valid and not track.spoof_ok and not recorded and state != State.CAPTURING:
            label = "CHECKING LIVE FACE"
        elif state == State.VERIFYING:
            label = (f"VERIFYING  {track.verification_progress * 100:.0f}%"
                     if track.liveness_ok and track.spoof_ok else
                     "CHECKING LIVE FACE" if not track.spoof_ok else "LIVENESS CHECK")
        elif state == State.SUCCESS:
            label = f"RECORDED  |  {math.ceil(track.cooldown_remaining)}s"
        elif state == State.COOLDOWN:
            label = f"COOLDOWN  {math.ceil(track.cooldown_remaining)}s"
        elif state == State.UNKNOWN:
            phase = (now - track.state_since) % 3.0
            label = "AUTO RESET" if phase > 2.4 else "NOT ENROLLED"
            if phase > 2.4:
                tint(image, (l, t, r, b), RED, (3 - phase) * 0.15)
        elif state == State.CAPTURING:
            label = "CAPTURED / SAVING..."
        elif state in (State.CONFIRMED, State.READY):
            label = "READY 100%" if state == State.CONFIRMED else "READY"
        else:
            label = "RECOGNIZING..."
        name = track.employee_name.upper() if known else "UNKNOWN" if state == State.UNKNOWN else "FACE DETECTED"
        liveness_label = (track.spoof_prompt if not track.spoof_ok else
                          "VERIFIED" if track.liveness_ok else track.liveness_prompt)
        # Expressions and PAD are collected together. Prompt the person now,
        # rather than waiting for the full PAD dwell before asking for a blink.
        if live_sample(track.spoof_score) and not track.liveness_ok:
            liveness_label = track.liveness_prompt
        if recorded or state == State.CAPTURING:
            liveness_label = "Attendance recorded" if state != State.CAPTURING else "Saving attendance"
        scale = layout.font(0.49)
        pad, line = layout.px(8), layout.px(22)
        panel_w = min(w - 4, max(r - l, text_size(label, scale, 1)[0] + pad * 2,
                                  text_size(liveness_label, layout.font(0.40), 1)[0] + pad * 2,
                                  min(text_size(name, scale, 1)[0] + pad * 2, layout.px(260))))
        x = max(2, min(l, w - panel_w - 2))
        panel_h = line * 3 + pad
        y = t - panel_h - pad if t > panel_h + pad else min(b + pad, h - panel_h - 2)
        y = max(2, y)
        tint(image, (x, y, x + panel_w, y + panel_h), DARK, 0.82 * fade)
        text(image, name, (x + pad, y + line), color, scale, max_width=panel_w - pad * 2)
        status_color = AMBER if state in (State.VERIFYING, State.RECOGNIZING, State.CAPTURING) else color
        text(image, label, (x + pad, y + line * 2), status_color, scale, max_width=panel_w - pad * 2)
        liveness_color = GRAY if track.liveness_ok and track.spoof_ok else AMBER
        text(image, liveness_label, (x + pad, y + line * 3), liveness_color,
             layout.font(0.40), max_width=panel_w - pad * 2)
        if state == State.VERIFYING:
            bar_y = y + panel_h - layout.px(3)
            cv2.line(image, (x, bar_y), (x + panel_w, bar_y), LINE, layout.px(3))
            progress = (.45 * track.spoof_progress + .25 * track.liveness_progress
                        + .30 * track.verification_progress) if live_sample(track.spoof_score) else 0.0
            cv2.line(image, (x, bar_y), (x + int(panel_w * progress), bar_y), AMBER, layout.px(3))

    def _hud(self, image, layout, tracks, now, wall_time, status, camera_fps, preview_fps, enrolled, attendance):
        x0, h = layout.feed_width, layout.height
        pad = layout.px(20)
        width = layout.hud_width - pad * 2
        x = x0 + pad
        scale = layout.font(0.5)
        active = [t for t in tracks.values() if t.visible and now - t.last_seen <= self.config.detection_fresh_sec]
        known = sum(t.identity_valid for t in active)
        unknown = sum(t.state == State.UNKNOWN for t in active)
        cv2.line(image, (x0, 0), (x0, h), LINE, 1)

        def put(value, ratio, color=WHITE, font=scale, bold=False):
            text(image, value, (x, int(h * ratio)), color, font, max(1, round(layout.scale)) if bold else 1, width)

        put("FACE ATTENDANCE", 0.065, GREEN, layout.font(0.57), True)
        cv2.line(image, (x, int(h * 0.09)), (x + width, int(h * 0.09)), LINE, 1)
        put("CAMERA", 0.14, GRAY, layout.font(0.42))
        put("Connected" if status == "CONNECTED" else status.title(), 0.185,
            GREEN if status == "CONNECTED" else RED)
        put("FPS / CAMERA", 0.25, GRAY, layout.font(0.42))
        put(f"{preview_fps:.1f} / {camera_fps:.1f}", 0.30, WHITE, layout.font(0.73))
        put("EMPLOYEES", 0.37, GRAY, layout.font(0.42))
        put(f"{enrolled} Enrolled", 0.415)
        put("LIVE", 0.485, GRAY, layout.font(0.42))
        put(f"{known} Recognized", 0.53, GREEN)
        put(f"{unknown} Unknown / {len(active)} Active", 0.575, RED if unknown else GRAY, layout.font(0.44))
        cv2.line(image, (x, int(h * 0.62)), (x + width, int(h * 0.62)), LINE, 1)
        put("LAST ATTENDANCE", 0.665, GRAY, layout.font(0.42))
        if attendance.last_attendance:
            result = attendance.last_attendance
            put(result.job.employee_name, 0.71, GREEN)
            put(datetime.fromtimestamp(result.recorded_at).strftime("%I:%M:%S %p"), 0.75, WHITE, layout.font(0.46))
        else:
            put("Waiting for attendance", 0.71, GRAY, layout.font(0.43))
        if not attendance.ready:
            put("STORAGE ERROR" if attendance.storage_error else "STORAGE STARTING", 0.80, RED)
        local = datetime.fromtimestamp(wall_time)
        put(local.strftime("%d %b %Y").upper(), 0.87, GRAY, layout.font(0.43))
        put(local.strftime("%I:%M:%S %p"), 0.915, WHITE, layout.font(0.62))
        put("Q  EXIT", 0.975, GRAY, layout.font(0.38))

    def _capture(self, image, captures, now, layout):
        if not captures or not self.capture_effects:
            return
        age = now - captures[-1][0]
        h, w = image.shape[:2]
        if age < self.config.flash_duration:
            alpha = 0.80 * (1 - age / self.config.flash_duration) ** 2
            tint(image, (0, 0, w, h), WHITE, alpha)
        if age < self.config.shutter_duration:
            progress = age / self.config.shutter_duration
            inset = int((1 - progress) ** 2 * min(w, h) * 0.14) + layout.px(8)
            corners(image, (inset, w - inset, h - inset, inset), WHITE, layout.px(3), layout.px(60))
        text(image, "CAPTURED", (layout.px(22), h - layout.px(24)), WHITE, layout.font(0.5))

    def _notices(self, image, notices, now, wall_time, layout):
        if not notices:
            return
        latest = notices[-1]
        age = now - latest[0]
        fade = min(1.0, age / 0.25, (self.config.success_duration - age) / 0.45)
        if fade <= 0:
            return
        successes = [n for n in notices if n[1] == "success"]
        error = latest[1] == "error"
        shown = [latest] if error else successes[-3:]
        if not shown:
            return
        h, w = image.shape[:2]
        card_w = min(w - layout.px(32), layout.px(490))
        card_h = layout.px(220 + 44 * (len(shown) - 1))
        x, y = (w - card_w) // 2, max(layout.px(12), (h - card_h) // 2)
        roi = image[y:y + card_h, x:x + card_w]
        overlay = roi.copy()
        tint(overlay, (0, 0, card_w, card_h), DARK, 0.9)
        color = RED if error else GREEN
        cv2.rectangle(overlay, (0, 0), (card_w - 1, card_h - 1), color, layout.px(1), cv2.LINE_AA)
        cx, cy = card_w // 2, layout.px(40)
        cv2.circle(overlay, (cx, cy), layout.px(22), color, layout.px(2), cv2.LINE_AA)
        if error:
            text(overlay, "!", (cx - layout.px(4), cy + layout.px(9)), color, layout.font(0.85), 2)
        else:
            cv2.polylines(overlay, [np.array([(cx - layout.px(11), cy), (cx - layout.px(3), cy + layout.px(8)),
                                             (cx + layout.px(12), cy - layout.px(9))], np.int32)],
                          False, color, layout.px(3), cv2.LINE_AA)

        def centered(value, row, font, color=WHITE):
            value = str(value).encode("ascii", "replace").decode("ascii")
            max_width = card_w - layout.px(32)
            while text_size(value, font, 1)[0] > max_width and len(value) > 3:
                value = value[:-4] + "..."
            width = text_size(value, font, 1)[0]
            text(overlay, value, ((card_w - width) // 2, layout.px(row)), color, font)

        centered("ATTENDANCE SAVE FAILED" if error else "ATTENDANCE RECORDED", 89, layout.font(0.63), color)
        for index, (_, _, result) in enumerate(shown):
            centered(result.job.employee_name, 125 + index * 44, layout.font(0.67))
            subtitle = "Retrying while identity remains verified" if error else datetime.fromtimestamp(
                result.recorded_at).strftime("%I:%M:%S %p")
            centered(subtitle, 149 + index * 44, layout.font(0.43), GRAY)
        remaining = max(0, math.ceil(self.config.cooldown_sec - (wall_time - shown[-1][2].recorded_at))) if not error else 0
        centered("Please remain in view" if error else f"You can try again in {remaining}s",
                 191 + 44 * (len(shown) - 1), layout.font(0.48), color)
        cv2.addWeighted(overlay, fade, roi, 1 - fade, 0, dst=roi)

    @staticmethod
    def _status(image, title, subtitle, layout):
        h, w = image.shape[:2]
        tint(image, (0, 0, w, h), DARK, 0.55)
        for value, offset, color in ((title, -10, RED), (subtitle, 26, WHITE)):
            scale = layout.font(0.67 if offset < 0 else 0.45)
            tw = text_size(value, scale, 1)[0]
            text(image, value, (max(10, (w - tw) // 2), h // 2 + layout.px(abs(offset)) * (-1 if offset < 0 else 1)),
                 color, scale, max_width=w - 20)
