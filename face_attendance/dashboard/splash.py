"""Animated splash screen with real environment checks and loading progress."""
import math
import random
from pathlib import Path

from PyQt6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPointF,
                          QPropertyAnimation, QRectF, QSequentialAnimationGroup,
                          Qt, QThread, QTimer, pyqtProperty, pyqtSignal)
from PyQt6.QtGui import (QColor, QFont, QFontDatabase, QLinearGradient,
                          QPainter, QPainterPath, QPen, QRadialGradient)
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from .theme import PALETTES

_C = PALETTES["dark"]

FADE_OUT_MS = 500

CHECK_LABELS = [
    ("settings", "Loading Settings"),
    ("face_models", "Face Recognition Models"),
    ("antispoof_models", "Anti-Spoof Models"),
    ("camera", "Camera Access"),
    ("enrolled", "Enrolled Employees"),
    ("database", "Database Connection"),
]


class _Particle:
    __slots__ = ("x", "y", "r", "speed", "alpha", "phase")

    def __init__(self, w, h):
        self.x = random.uniform(0, w)
        self.y = random.uniform(0, h)
        self.r = random.uniform(1.2, 3.0)
        self.speed = random.uniform(0.15, 0.6)
        self.alpha = random.uniform(0.08, 0.3)
        self.phase = random.uniform(0, math.tau)


class _CheckWorker(QThread):
    """Runs environment checks off the GUI thread, emitting progress."""

    checkResult = pyqtSignal(str, bool, str)   # key, passed, detail
    allDone = pyqtSignal()

    def __init__(self, settings):
        super().__init__()
        self._settings = settings

    def run(self):
        for key, _ in CHECK_LABELS:
            passed, detail = getattr(self, f"_check_{key}")()
            self.checkResult.emit(key, passed, detail)
            self.msleep(180)
        self.allDone.emit()

    def _check_settings(self):
        try:
            cfg = self._settings.to_config()
            mode = cfg.attendance_mode.replace("_", " + ").title()
            return True, f"Mode: {mode}"
        except Exception as exc:
            return False, str(exc)[:60]

    def _check_face_models(self):
        try:
            from ..face_backend import MODEL_DIR, MODEL_HASHES
            missing = [n for n in MODEL_HASHES if not (Path(MODEL_DIR) / n).exists()]
            if missing:
                return False, f"Missing: {', '.join(missing)}"
            return True, f"{len(MODEL_HASHES)} models verified"
        except Exception as exc:
            return False, str(exc)[:60]

    def _check_antispoof_models(self):
        try:
            from ..anti_spoof import MODEL_DIR, MODELS
            missing = [name for name, _, _ in MODELS if not (Path(MODEL_DIR) / name).exists()]
            if missing:
                return False, f"Missing: {', '.join(missing)}"
            return True, f"{len(MODELS)} models verified"
        except Exception as exc:
            return False, str(exc)[:60]

    def _check_camera(self):
        try:
            import cv2
            source = self._settings.source
            src = int(source) if str(source).isdecimal() else source
            if isinstance(src, int):
                cap = cv2.VideoCapture(src)
                ok = cap.isOpened()
                cap.release()
                return ok, f"Webcam {src} ready" if ok else f"Webcam {src} unavailable"
            return True, f"Source: {Path(str(src)).name if '://' not in str(src) else 'stream'}"
        except Exception as exc:
            return False, str(exc)[:60]

    def _check_enrolled(self):
        try:
            from ..template_store import read_templates
            path = Path(self._settings.encodings_path)
            if not path.exists():
                return False, "No enrollment data found"
            data = read_templates(path)
            total_templates = sum(len(v) for v in data.values())
            return True, f"{len(data)} employees, {total_templates} templates"
        except Exception as exc:
            return False, str(exc)[:60]

    def _check_database(self):
        try:
            from ..database import connect
            backend = getattr(self._settings, "db_backend", "sqlite")
            if backend == "sqlite":
                db_path = Path(self._settings.db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = connect(self._settings, readonly=True)
            conn.integrity_check()
            exists = conn.table_exists("attendance")
            conn.close()
            label = backend.upper() if backend != "sqlite" else "Database"
            if exists:
                return True, f"{label} OK"
            return True, f"{label} — new (will be created)"
        except Exception as exc:
            return False, str(exc)[:60]


class SplashScreen(QWidget):
    """Full-screen animated splash with real environment loading checks."""

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.resize(520, 680)

        self._settings = settings
        self._icon_scale = 0.0
        self._icon_opacity = 0.0
        self._glow_intensity = 0.0
        self._ring1 = 0.0
        self._ring1_alpha = 0.0
        self._ring2 = 0.0
        self._ring2_alpha = 0.0
        self._ring3 = 0.0
        self._ring3_alpha = 0.0
        self._orbit_angle = 0.0
        self._scanner_pos = -0.1
        self._text_opacity = 0.0
        self._text_slide = 20.0
        self._sub_opacity = 0.0
        self._fade_out = 1.0
        self._particle_time = 0.0

        self._check_states = {}
        self._check_spinner_angle = 0.0
        self._checks_complete = False
        self._all_passed = True
        self._completed_count = 0

        random.seed(42)
        self._particles = [_Particle(520, 680) for _ in range(35)]

        self._build_animations()
        self._finished_callback = None
        self._worker = None

        self._btn_quit = self._make_button("Quit", self._on_quit)
        self._btn_relaunch = self._make_button("Relaunch", self._on_relaunch)
        self._btn_continue = self._make_button("Continue", self._on_continue)
        self._btn_quit.hide()
        self._btn_relaunch.hide()
        self._btn_continue.hide()

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(16)
        self._tick_timer.timeout.connect(self._tick)

    # ── animated properties ────────────────────────────────────────────────

    @pyqtProperty(float)
    def iconScale(self):
        return self._icon_scale

    @iconScale.setter
    def iconScale(self, v):
        self._icon_scale = v
        self.update()

    @pyqtProperty(float)
    def iconOpacity(self):
        return self._icon_opacity

    @iconOpacity.setter
    def iconOpacity(self, v):
        self._icon_opacity = v
        self.update()

    @pyqtProperty(float)
    def glowIntensity(self):
        return self._glow_intensity

    @glowIntensity.setter
    def glowIntensity(self, v):
        self._glow_intensity = v
        self.update()

    @pyqtProperty(float)
    def ring1(self):
        return self._ring1

    @ring1.setter
    def ring1(self, v):
        self._ring1 = v
        self.update()

    @pyqtProperty(float)
    def ring1Alpha(self):
        return self._ring1_alpha

    @ring1Alpha.setter
    def ring1Alpha(self, v):
        self._ring1_alpha = v
        self.update()

    @pyqtProperty(float)
    def ring2(self):
        return self._ring2

    @ring2.setter
    def ring2(self, v):
        self._ring2 = v
        self.update()

    @pyqtProperty(float)
    def ring2Alpha(self):
        return self._ring2_alpha

    @ring2Alpha.setter
    def ring2Alpha(self, v):
        self._ring2_alpha = v
        self.update()

    @pyqtProperty(float)
    def ring3(self):
        return self._ring3

    @ring3.setter
    def ring3(self, v):
        self._ring3 = v
        self.update()

    @pyqtProperty(float)
    def ring3Alpha(self):
        return self._ring3_alpha

    @ring3Alpha.setter
    def ring3Alpha(self, v):
        self._ring3_alpha = v
        self.update()

    @pyqtProperty(float)
    def orbitAngle(self):
        return self._orbit_angle

    @orbitAngle.setter
    def orbitAngle(self, v):
        self._orbit_angle = v
        self.update()

    @pyqtProperty(float)
    def scannerPos(self):
        return self._scanner_pos

    @scannerPos.setter
    def scannerPos(self, v):
        self._scanner_pos = v
        self.update()

    @pyqtProperty(float)
    def textOpacity(self):
        return self._text_opacity

    @textOpacity.setter
    def textOpacity(self, v):
        self._text_opacity = v
        self.update()

    @pyqtProperty(float)
    def textSlide(self):
        return self._text_slide

    @textSlide.setter
    def textSlide(self, v):
        self._text_slide = v
        self.update()

    @pyqtProperty(float)
    def subOpacity(self):
        return self._sub_opacity

    @subOpacity.setter
    def subOpacity(self, v):
        self._sub_opacity = v
        self.update()

    @pyqtProperty(float)
    def fadeOut(self):
        return self._fade_out

    @fadeOut.setter
    def fadeOut(self, v):
        self._fade_out = v
        self.update()

    # ── animation wiring ──────────────────────────────────────────────────

    def _anim(self, prop, start, end, duration, easing=QEasingCurve.Type.OutCubic):
        a = QPropertyAnimation(self, prop)
        a.setStartValue(start)
        a.setEndValue(end)
        a.setDuration(duration)
        a.setEasingCurve(easing)
        return a

    def _ring_group(self, r_prop, a_prop):
        g = QParallelAnimationGroup(self)
        g.addAnimation(self._anim(r_prop, 0.0, 1.0, 800, QEasingCurve.Type.OutQuad))
        g.addAnimation(self._anim(a_prop, 0.9, 0.0, 800, QEasingCurve.Type.InCubic))
        return g

    def _build_animations(self):
        phase1 = QParallelAnimationGroup(self)
        phase1.addAnimation(self._anim(b"iconScale", 0.2, 1.0, 600, QEasingCurve.Type.OutBack))
        phase1.addAnimation(self._anim(b"iconOpacity", 0.0, 1.0, 400))
        phase1.addAnimation(self._anim(b"glowIntensity", 0.0, 1.0, 700))

        ripple = QSequentialAnimationGroup(self)
        ripple.addAnimation(self._ring_group(b"ring1", b"ring1Alpha"))
        ripple_23 = QParallelAnimationGroup(self)
        r2 = QSequentialAnimationGroup(self)
        r2.addPause(100)
        r2.addAnimation(self._ring_group(b"ring2", b"ring2Alpha"))
        r3 = QSequentialAnimationGroup(self)
        r3.addPause(250)
        r3.addAnimation(self._ring_group(b"ring3", b"ring3Alpha"))
        ripple_23.addAnimation(r2)
        ripple_23.addAnimation(r3)
        ripple.addAnimation(ripple_23)

        phase3 = QParallelAnimationGroup(self)
        phase3.addAnimation(self._anim(b"orbitAngle", 0.0, 360.0, 1400, QEasingCurve.Type.InOutSine))
        scan_seq = QSequentialAnimationGroup(self)
        scan_seq.addPause(200)
        scan_seq.addAnimation(self._anim(b"scannerPos", -0.1, 1.1, 900, QEasingCurve.Type.InOutQuad))
        phase3.addAnimation(scan_seq)

        phase4 = QParallelAnimationGroup(self)
        phase4.addAnimation(self._anim(b"textOpacity", 0.0, 1.0, 450))
        phase4.addAnimation(self._anim(b"textSlide", 20.0, 0.0, 450, QEasingCurve.Type.OutCubic))
        sub_delayed = QSequentialAnimationGroup(self)
        sub_delayed.addPause(150)
        sub_delayed.addAnimation(self._anim(b"subOpacity", 0.0, 1.0, 400))
        phase4.addAnimation(sub_delayed)

        self._sequence = QSequentialAnimationGroup(self)
        self._sequence.addAnimation(phase1)
        self._sequence.addAnimation(ripple)
        self._sequence.addAnimation(phase3)
        self._sequence.addAnimation(phase4)

        fade = self._anim(b"fadeOut", 1.0, 0.0, FADE_OUT_MS, QEasingCurve.Type.InQuad)
        fade.finished.connect(self._on_done)
        self._fade_anim = fade

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, on_finished=None):
        self._finished_callback = on_finished
        self._anim_done = False
        self._center_on_screen()
        self.show()
        self._sequence.start()
        self._sequence.finished.connect(self._on_anim_done)
        self._tick_timer.start()
        self._start_checks()

    def _start_checks(self):
        if self._settings is None:
            self._checks_complete = True
            return
        self._worker = _CheckWorker(self._settings)
        self._worker.checkResult.connect(self._on_check_result)
        self._worker.allDone.connect(self._on_checks_done)
        self._worker.start()

    def _on_check_result(self, key, passed, detail):
        self._check_states[key] = (passed, detail)
        if not passed:
            self._all_passed = False
        self._completed_count += 1
        self.update()

    def _on_anim_done(self):
        self._anim_done = True
        self._try_fade_out()

    def _on_checks_done(self):
        self._checks_complete = True
        if self._all_passed:
            self._try_fade_out()
        else:
            self._show_buttons()

    def _try_fade_out(self):
        if self._anim_done and self._checks_complete:
            QTimer.singleShot(600, self._begin_fade_out)

    def _show_buttons(self):
        w = self.width()
        cx = w / 2
        cy = self.height() / 2 - 80
        btn_y = int(cy + 210 + len(CHECK_LABELS) * 30 + 14)
        btn_w, btn_h, gap = 110, 34, 10
        total_w = btn_w * 3 + gap * 2
        left = int(cx - total_w / 2)

        self._btn_quit.setGeometry(left, btn_y, btn_w, btn_h)
        self._btn_relaunch.setGeometry(left + btn_w + gap, btn_y, btn_w, btn_h)
        self._btn_continue.setGeometry(left + (btn_w + gap) * 2, btn_y, btn_w, btn_h)

        self._btn_quit.show()
        self._btn_relaunch.show()
        self._btn_continue.show()
        self._btn_quit.raise_()
        self._btn_relaunch.raise_()
        self._btn_continue.raise_()

    def _make_button(self, text, callback):
        btn = QPushButton(text, self)
        btn.setFixedHeight(34)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFont(self._get_font(11, True))
        btn.clicked.connect(callback)
        if text == "Quit":
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {_C['danger_bg']}; color: {_C['danger']};"
                f"  border: 1px solid {_C['danger_border']}; border-radius: 10px;"
                f"  padding: 6px 16px; }}"
                f"QPushButton:hover {{ background-color: {_C['danger']}; color: #FFFFFF; }}")
        elif text == "Relaunch":
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {_C['primary']}; color: {_C['primary_fg']};"
                f"  border: 1px solid {_C['primary']}; border-radius: 10px;"
                f"  padding: 6px 16px; }}"
                f"QPushButton:hover {{ background-color: {_C['primary_hover']};"
                f"  border-color: {_C['primary_hover']}; color: {_C['primary_fg']}; }}")
        else:
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {_C['panel_alt']}; color: {_C['text_secondary']};"
                f"  border: 1px solid {_C['border']}; border-radius: 10px;"
                f"  padding: 6px 16px; }}"
                f"QPushButton:hover {{ border-color: {_C['primary']}; color: {_C['primary']}; }}")
        return btn

    def _on_quit(self):
        app = QApplication.instance()
        if app:
            app.quit()

    def _on_relaunch(self):
        import os
        self.hide()
        if self._worker:
            self._worker.wait(2000)
        app = QApplication.instance()
        if app:
            app.quit()
        from .shutdown import relaunch_command
        program, arguments = relaunch_command()
        os.execv(program, [program] + arguments)

    def _on_continue(self):
        self._btn_quit.hide()
        self._btn_relaunch.hide()
        self._btn_continue.hide()
        self._begin_fade_out()

    def _center_on_screen(self):
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center() - self.rect().center())

    def _begin_fade_out(self):
        self._tick_timer.stop()
        self._fade_anim.start()

    def _on_done(self):
        self.hide()
        if self._worker:
            self._worker.wait(2000)
        if self._finished_callback:
            self._finished_callback()

    def cancel(self):
        """Stop for good without running the finished callback (the app is quitting)."""
        from .threads import settle
        self._finished_callback = None
        self._sequence.stop()
        self._fade_anim.stop()
        self._tick_timer.stop()
        # A slow camera probe may outlive the splash; it must not die mid-run.
        settle(self._worker)
        self.hide()

    def _tick(self):
        self._particle_time += 0.016
        self._check_spinner_angle += 5.0
        self.update()

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._fade_out)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2 - 80

        self._paint_background(p, w, h, cx, cy)
        self._paint_particles(p, w, h)
        self._paint_rings(p, cx, cy)
        self._paint_orbit_dots(p, cx, cy)
        self._paint_icon_tile(p, cx, cy)
        self._paint_scanner(p, cx, cy)
        self._paint_text(p, w, h, cx, cy)
        self._paint_checks(p, w, h, cx, cy)
        self._paint_footer(p, w, h)
        p.end()

    def _paint_background(self, p, w, h, cx, cy):
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor("#060D1A"))
        grad.setColorAt(0.5, QColor(_C["bg"]))
        grad.setColorAt(1.0, QColor("#060D1A"))
        p.fillRect(self.rect(), grad)

        if self._glow_intensity > 0.01:
            radial = QRadialGradient(cx, cy, 160)
            c1 = QColor(_C["primary"])
            c1.setAlpha(int(35 * self._glow_intensity))
            c2 = QColor(_C["primary"])
            c2.setAlpha(int(12 * self._glow_intensity))
            radial.setColorAt(0.0, c1)
            radial.setColorAt(0.5, c2)
            radial.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(self.rect(), radial)

    def _paint_particles(self, p, w, h):
        t = self._particle_time
        for pt in self._particles:
            drift_x = math.sin(t * pt.speed + pt.phase) * 12
            drift_y = math.cos(t * pt.speed * 0.7 + pt.phase) * 8
            px = (pt.x + drift_x) % w
            py = (pt.y - t * pt.speed * 20 + drift_y) % h
            twinkle = 0.5 + 0.5 * math.sin(t * 2.0 + pt.phase)
            c = QColor(_C["primary"])
            c.setAlphaF(pt.alpha * twinkle * self._fade_out)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPointF(px, py), pt.r, pt.r)

    def _paint_rings(self, p, cx, cy):
        for radius_val, alpha_val in ((self._ring1, self._ring1_alpha),
                                      (self._ring2, self._ring2_alpha),
                                      (self._ring3, self._ring3_alpha)):
            if alpha_val < 0.01:
                continue
            r = 48 + radius_val * 100
            c = QColor(_C["primary"])
            c.setAlphaF(alpha_val * 0.45)
            pen = QPen(c, 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)

    def _paint_orbit_dots(self, p, cx, cy):
        if self._orbit_angle < 0.5:
            return
        orbit_r = 68
        angle_progress = min(1.0, self._orbit_angle / 360.0)
        for i in range(4):
            base = self._orbit_angle + i * 90
            rad = math.radians(base)
            dx = cx + orbit_r * math.cos(rad)
            dy = cy + orbit_r * math.sin(rad)
            dot_alpha = angle_progress * (0.7 - i * 0.12)
            dot_size = 3.5 - i * 0.5
            c = QColor(_C["primary"])
            c.setAlphaF(max(0.0, dot_alpha))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPointF(dx, dy), dot_size, dot_size)

    def _paint_icon_tile(self, p, cx, cy):
        if self._icon_opacity < 0.01:
            return
        p.save()
        p.setOpacity(self._fade_out * self._icon_opacity)
        p.translate(cx, cy)
        s = self._icon_scale
        p.scale(s, s)

        tile_size = 96
        half = tile_size / 2
        tile_rect = QRectF(-half, -half, tile_size, tile_size)

        shadow = QColor(0, 0, 0, 60)
        shadow_rect = tile_rect.adjusted(0, 4, 0, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(shadow)
        p.drawRoundedRect(shadow_rect, 22, 22)

        tile_grad = QLinearGradient(tile_rect.topLeft(), tile_rect.bottomRight())
        tile_grad.setColorAt(0.0, QColor("#1E40AF"))
        tile_grad.setColorAt(1.0, QColor("#3B82F6"))
        p.setBrush(tile_grad)
        p.drawRoundedRect(tile_rect, 22, 22)

        inner_glow = QRadialGradient(0, -half * 0.3, tile_size * 0.8)
        inner_glow.setColorAt(0.0, QColor(255, 255, 255, 25))
        inner_glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(inner_glow)
        p.drawRoundedRect(tile_rect, 22, 22)

        border_c = QColor(255, 255, 255, 20)
        p.setPen(QPen(border_c, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(tile_rect, 22, 22)

        self._draw_face_icon(p, 56)
        p.restore()

    def _paint_scanner(self, p, cx, cy):
        if self._scanner_pos < 0.0 or self._scanner_pos > 1.0:
            return
        p.save()
        half = 48
        scan_y = cy - half + self._scanner_pos * (half * 2)
        intensity = 1.0 - abs(self._scanner_pos - 0.5) * 2

        grad = QLinearGradient(cx - 40, 0, cx + 40, 0)
        c = QColor(_C["success"])
        c.setAlphaF(0.0)
        grad.setColorAt(0.0, c)
        c2 = QColor(_C["success"])
        c2.setAlphaF(max(0.1, intensity * 0.7))
        grad.setColorAt(0.5, c2)
        c3 = QColor(_C["success"])
        c3.setAlphaF(0.0)
        grad.setColorAt(1.0, c3)
        p.setPen(QPen(grad, 2.0))
        p.drawLine(QPointF(cx - 40, scan_y), QPointF(cx + 40, scan_y))

        glow_rect = QRectF(cx - 42, scan_y - 8, 84, 16)
        glow_grad = QRadialGradient(cx, scan_y, 44)
        gc = QColor(_C["success"])
        gc.setAlpha(int(intensity * 20))
        glow_grad.setColorAt(0.0, gc)
        glow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(glow_rect, glow_grad)
        p.restore()

    def _paint_text(self, p, w, h, cx, cy):
        text_base_y = cy + 80

        if self._text_opacity > 0.01:
            p.save()
            p.setOpacity(self._fade_out * self._text_opacity)
            font = self._get_font(24, True)
            p.setFont(font)
            p.setPen(QColor(_C["text"]))
            p.drawText(QRectF(0, text_base_y + self._text_slide, w, 40),
                       Qt.AlignmentFlag.AlignCenter, "Face ID Attendance")
            p.restore()

        if self._sub_opacity > 0.01:
            p.save()
            p.setOpacity(self._fade_out * self._sub_opacity)

            sub_font = self._get_font(12, False)
            p.setFont(sub_font)
            p.setPen(QColor(_C["muted"]))
            p.drawText(QRectF(0, text_base_y + self._text_slide + 38, w, 22),
                       Qt.AlignmentFlag.AlignCenter, "Secure  •  Accurate  •  Smarter")

            pill_y = text_base_y + self._text_slide + 66
            pill_text = "v2.0.0"
            pill_font = self._get_font(10, True)
            p.setFont(pill_font)
            pill_w = p.fontMetrics().horizontalAdvance(pill_text) + 20
            pill_h = 22
            pill_rect = QRectF(cx - pill_w / 2, pill_y, pill_w, pill_h)
            pill_bg = QColor(_C["primary"])
            pill_bg.setAlpha(30)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pill_bg)
            p.drawRoundedRect(pill_rect, pill_h / 2, pill_h / 2)
            p.setPen(QColor(_C["primary"]))
            p.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, pill_text)
            p.restore()

    def _paint_checks(self, p, w, h, cx, cy):
        p.save()
        p.setOpacity(self._fade_out)

        check_top = cy + 210
        row_h = 30
        list_w = 340
        left_x = cx - list_w / 2

        # progress bar background
        bar_y = check_top - 18
        bar_w = list_w
        bar_h = 4
        bar_rect = QRectF(left_x, bar_y, bar_w, bar_h)
        bar_bg = QColor(_C["border"])
        bar_bg.setAlpha(80)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bar_bg)
        p.drawRoundedRect(bar_rect, 2, 2)

        # progress bar fill
        total = len(CHECK_LABELS)
        progress = self._completed_count / total if total else 0
        if progress > 0:
            fill_rect = QRectF(left_x, bar_y, bar_w * progress, bar_h)
            fill_color = QColor(_C["success"]) if self._all_passed else QColor(_C["warn"])
            p.setBrush(fill_color)
            p.drawRoundedRect(fill_rect, 2, 2)

        # progress text
        pct_font = self._get_font(10, True)
        p.setFont(pct_font)
        pct_text = f"Initializing... {int(progress * 100)}%"
        if self._checks_complete:
            pct_text = "All systems ready" if self._all_passed else "Some checks need attention"
        pct_color = QColor(_C["muted"])
        p.setPen(pct_color)
        p.drawText(QRectF(left_x, bar_y - 20, list_w, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, pct_text)

        # check items
        for i, (key, label) in enumerate(CHECK_LABELS):
            y = check_top + i * row_h
            state = self._check_states.get(key)

            if state is None:
                if self._completed_count == i:
                    self._paint_spinner(p, left_x + 8, y + row_h / 2, 6)
                    p.setPen(QColor(_C["primary"]))
                    p.setFont(self._get_font(11, True))
                    p.drawText(QRectF(left_x + 24, y, list_w - 24, row_h),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                               f"Checking {label}...")
                else:
                    dot_c = QColor(_C["muted"])
                    dot_c.setAlpha(60)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(dot_c)
                    p.drawEllipse(QPointF(left_x + 8, y + row_h / 2), 3, 3)
                    p.setPen(QColor(_C["muted"]))
                    p.setFont(self._get_font(11, False))
                    label_c = QColor(_C["muted"])
                    label_c.setAlpha(100)
                    p.setPen(label_c)
                    p.drawText(QRectF(left_x + 24, y, list_w - 24, row_h),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                               label)
            else:
                passed, detail = state
                if passed:
                    self._paint_check_icon(p, left_x + 8, y + row_h / 2, 6, _C["success"])
                else:
                    self._paint_x_icon(p, left_x + 8, y + row_h / 2, 5, _C["warn"])

                p.setPen(QColor(_C["text"]))
                p.setFont(self._get_font(11, True))
                p.drawText(QRectF(left_x + 24, y, list_w * 0.5, row_h),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           label)

                detail_color = QColor(_C["success"] if passed else _C["warn"])
                detail_color.setAlpha(200)
                p.setPen(detail_color)
                p.setFont(self._get_font(10, False))
                p.drawText(QRectF(left_x + 24 + list_w * 0.5, y, list_w * 0.5 - 24, row_h),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           detail)

        p.restore()

    def _paint_spinner(self, p, cx, cy, r):
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        angle = self._check_spinner_angle
        for i in range(8):
            a = math.radians(angle + i * 45)
            dx = cx + r * math.cos(a)
            dy = cy + r * math.sin(a)
            c = QColor(_C["primary"])
            c.setAlphaF(0.15 + (7 - i) / 7.0 * 0.85)
            p.setBrush(c)
            dot_r = 1.8 - i * 0.1
            p.drawEllipse(QPointF(dx, dy), dot_r, dot_r)
        p.restore()

    def _paint_check_icon(self, p, cx, cy, r, color):
        p.save()
        bg = QColor(color)
        bg.setAlpha(30)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawEllipse(QPointF(cx, cy), r + 2, r + 2)

        pen = QPen(QColor(color), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        path = QPainterPath()
        path.moveTo(cx - r * 0.45, cy + r * 0.05)
        path.lineTo(cx - r * 0.05, cy + r * 0.45)
        path.lineTo(cx + r * 0.55, cy - r * 0.4)
        p.drawPath(path)
        p.restore()

    def _paint_x_icon(self, p, cx, cy, r, color):
        p.save()
        bg = QColor(color)
        bg.setAlpha(30)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawEllipse(QPointF(cx, cy), r + 2, r + 2)

        pen = QPen(QColor(color), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        d = r * 0.35
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))
        p.restore()

    def _paint_footer(self, p, w, h):
        vis = max(self._sub_opacity, 1.0 if self._completed_count > 0 else 0.0)
        if vis < 0.01:
            return
        p.save()
        p.setOpacity(self._fade_out * vis * 0.5)
        p.setFont(self._get_font(10, False))
        p.setPen(QColor(_C["muted"]))
        p.drawText(QRectF(0, h - 44, w, 20), Qt.AlignmentFlag.AlignCenter,
                   "© 2026 SV Trucking Face Recognition. All rights reserved.")
        p.restore()

    def _draw_face_icon(self, p, size):
        s = size / 24.0
        p.save()
        p.scale(s, s)
        p.translate(-12, -12)

        pen = QPen(QColor("#FFFFFF"), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)

        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            x = 12 + 9.0 * sx
            y = 12 + 9.0 * sy
            p.drawLine(QPointF(x, y), QPointF(x - 4.8 * sx, y))
            p.drawLine(QPointF(x, y), QPointF(x, y - 4.8 * sy))

        p.drawLine(QPointF(9.0, 10.2), QPointF(9.0, 12.4))
        p.drawLine(QPointF(15.0, 10.2), QPointF(15.0, 12.4))

        path = QPainterPath()
        path.moveTo(8.6, 15.2)
        path.quadTo(12.0, 18.2, 15.4, 15.2)
        p.drawPath(path)

        p.restore()

    @staticmethod
    def _get_font(size, bold):
        available = set(QFontDatabase.families())
        family = next(
            (n for n in ("Helvetica Neue", "SF Pro Display", "Segoe UI",
                         "DejaVu Sans", "Arial")
             if n in available),
            QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family(),
        )
        font = QFont(family, size)
        if bold:
            font.setWeight(QFont.Weight.Bold)
        return font
