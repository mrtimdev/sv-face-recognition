"""Sign-in dialogs: account login and the legacy dashboard PIN.

Both share a two-pane shell: a painted brand panel on the left and the form on
the right.  Credentials are checked on a worker thread so a slow database never
freezes the window.  Progress is reported in stages ("connect", "verify"); each
stage stays on screen for a minimum time and the progress line eases between
milestones, so a fast local login reads as a smooth sequence instead of a flash.
"""
import math
import random

from PyQt6.QtCore import (QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, Qt,
                          QThread, QTimer, QVariantAnimation, pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from ..settings import hash_pin
from ..users import AccountDisabledError, UserRepository
from .icons import IconLabel, make_pixmap
from .theme import palette
from .threads import start_detached
from .widgets.form import FormField, TextInput, repolish
from .widgets.loading import LoadingButton, ProgressLine

MAX_ATTEMPTS = 5
DISABLED_MESSAGE = "This account has been disabled. Ask an administrator to enable it again."
STAGE_MIN_MS = 380
SUCCESS_HOLD_MS = 560
LOCKOUT_CLOSE_MS = 1800
FEATURES = ("Liveness-verified check-ins", "Anti-spoof face protection", "Role-based access control")

class _SignInWorker(QThread):
    """Authenticates off the GUI thread; every signal carries the attempt token."""

    stage = pyqtSignal(int, str)
    result = pyqtSignal(int, str, object)   # token, outcome, user or error text

    def __init__(self, token, settings, username, password):
        super().__init__()
        self._token = token
        self._settings = settings
        self._username = username
        self._password = password

    def run(self):
        try:
            user = UserRepository(self._settings).authenticate(
                self._username, self._password,
                on_stage=lambda name: self.stage.emit(self._token, name))
        except AccountDisabledError:
            self.result.emit(self._token, "disabled", None)
            return
        except Exception as exc:
            self.result.emit(self._token, "error", str(exc) or type(exc).__name__)
            return
        self.result.emit(self._token, "ok" if user is not None else "wrong", user)


class AuthHero(QWidget):
    """Painted brand panel: gradient, dot grid, glass face tile and a live scan line."""

    WIDTH = 360

    def __init__(self, version="", parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._version = version
        self._mode = "idle"
        self._t = 0.0
        self._flash = 0.0
        rng = random.Random(11)
        self._stars = [(rng.random(), rng.random(), rng.uniform(0.8, 1.8),
                        rng.uniform(0, math.tau), rng.uniform(0.4, 1.2)) for _ in range(24)]
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._flash_anim = QVariantAnimation(self)
        self._flash_anim.valueChanged.connect(self._on_flash)
        self._flash_anim.finished.connect(self._flash_done)

    def mode(self):
        return self._mode

    def set_mode(self, mode):
        self._mode = mode
        self._flash_anim.stop()
        if mode == "ok":
            self._flash_anim.setStartValue(0.0)
            self._flash_anim.setEndValue(1.0)
            self._flash_anim.setDuration(360)
            self._flash_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._flash_anim.start()
        elif mode == "bad":
            self._flash_anim.setStartValue(1.0)
            self._flash_anim.setEndValue(0.0)
            self._flash_anim.setDuration(1400)
            self._flash_anim.setEasingCurve(QEasingCurve.Type.InQuad)
            self._flash_anim.start()
        else:
            self._flash = 0.0
        self.update()

    def _on_flash(self, value):
        self._flash = float(value)
        self.update()

    def _flash_done(self):
        if self._mode == "bad":
            self._mode = "idle"

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self):
        self._t += 0.033 * (2.4 if self._mode == "busy" else 1.0)
        self.update()

    def _accent(self):
        if self._mode == "ok":
            return QColor("#4ADE80")
        if self._mode == "bad" and self._flash > 0.05:
            return QColor("#F87171")
        return QColor("#4ADE80") if self._mode == "busy" else QColor("#93C5FD")

    def _font(self, pixels, weight=QFont.Weight.Normal):
        font = QFont(self.font())
        font.setPixelSize(pixels)
        font.setWeight(weight)
        return font

    def paintEvent(self, event):
        w, h = float(self.width()), float(self.height())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        background = QLinearGradient(0, 0, w * 0.7, h)
        background.setColorAt(0.0, QColor("#2563EB"))
        background.setColorAt(0.42, QColor("#1E3A8A"))
        background.setColorAt(1.0, QColor("#0B1220"))
        p.fillRect(self.rect(), background)
        for cx, cy, radius, color in ((w * 0.95, h * 0.05, w * 0.75, QColor(96, 165, 250, 95)),
                                      (w * 0.05, h * 1.0, w * 0.85, QColor(124, 58, 237, 70))):
            glow = QRadialGradient(cx, cy, radius)
            glow.setColorAt(0.0, color)
            transparent = QColor(color)
            transparent.setAlpha(0)
            glow.setColorAt(1.0, transparent)
            p.fillRect(self.rect(), glow)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        for x in range(20, int(w), 22):
            for y in range(20, int(h), 22):
                p.drawEllipse(QPointF(x, y), 0.9, 0.9)
        for sx, sy, radius, phase, speed in self._stars:
            twinkle = 0.5 + 0.5 * math.sin(self._t * speed * 2.2 + phase)
            y = (sy * h - self._t * 7 * speed) % h
            p.setBrush(QColor(191, 219, 254, int(30 + 80 * twinkle)))
            p.drawEllipse(QPointF(sx * w, y), radius, radius)

        tile = QRectF(40, 60, 84, 84)
        center = tile.center()
        accent = self._accent()
        for index in range(3):
            phase = ((self._t / 3.2) + index / 3.0) % 1.0
            ring = QColor(accent)
            ring.setAlpha(int((1.0 - phase) * 70))
            p.setPen(QPen(ring, 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            radius = 44 + phase * 74
            p.drawEllipse(center, radius, radius)

        p.setPen(QPen(QColor(255, 255, 255, 70), 1.2))
        p.setBrush(QColor(255, 255, 255, 30))
        p.drawRoundedRect(tile, 22, 22)
        if self._flash > 0.01:
            halo = QColor(accent)
            halo.setAlpha(int(200 * self._flash))
            p.setPen(QPen(halo, 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(tile.adjusted(-4, -4, 4, 4), 25, 25)
        glyph = make_pixmap("face-id", 44, "#FFFFFF", 1.8, ratio=self.devicePixelRatioF())
        p.drawPixmap(QRectF(center.x() - 22, center.y() - 22, 44, 44), glyph, QRectF(glyph.rect()))

        sweep = 0.5 - 0.5 * math.cos(self._t * 2.1)
        scan_y = tile.top() + 14 + sweep * (tile.height() - 28)
        line = QLinearGradient(tile.left() + 10, 0, tile.right() - 10, 0)
        edge = QColor(accent)
        edge.setAlpha(0)
        mid = QColor(accent)
        mid.setAlpha(230)
        line.setColorAt(0.0, edge)
        line.setColorAt(0.5, mid)
        line.setColorAt(1.0, edge)
        p.setPen(QPen(line, 2.0))
        p.drawLine(QPointF(tile.left() + 10, scan_y), QPointF(tile.right() - 10, scan_y))
        glow = QRadialGradient(center.x(), scan_y, 40)
        soft = QColor(accent)
        soft.setAlpha(46)
        glow.setColorAt(0.0, soft)
        glow.setColorAt(1.0, edge)
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(QRectF(tile.left() + 6, scan_y - 9, tile.width() - 12, 18), glow)

        left, width = 40.0, w - 80
        p.setPen(QColor("#FFFFFF"))
        p.setFont(self._font(26, QFont.Weight.Bold))
        p.drawText(QRectF(left, tile.bottom() + 30, width, 36),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Face ID Attendance")
        p.setPen(QColor(255, 255, 255, 180))
        p.setFont(self._font(13))
        p.drawText(QRectF(left, tile.bottom() + 66, width, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "Secure  •  Accurate  •  Smarter")

        check = make_pixmap("check", 12, "#0B1220", 3.0, ratio=self.devicePixelRatioF())
        top = tile.bottom() + 122
        p.setFont(self._font(13))
        for index, text in enumerate(FEATURES):
            y = top + index * 34
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#4ADE80"))
            p.drawEllipse(QRectF(left, y + 2, 18, 18))
            p.drawPixmap(QRectF(left + 3, y + 5, 12, 12), check, QRectF(check.rect()))
            p.setPen(QColor(255, 255, 255, 225))
            p.drawText(QRectF(left + 30, y, width - 30, 22),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

        footer = "SV Trucking Face Recognition"
        if self._version:
            footer = f"{self._version}  •  {footer}"
        p.setPen(QColor(255, 255, 255, 115))
        p.setFont(self._font(11))
        p.drawText(QRectF(left, h - 50, width, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, footer)
        p.end()


class _AlertBanner(QWidget):
    """Inline error box; hidden, it takes no room at all (spacing included)."""

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 16)
        box = QFrame()
        box.setObjectName("authAlert")
        outer.addWidget(box)
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        self.icon = IconLabel("alert", 16, palette(theme)["danger"])
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel("")
        self.label.setObjectName("authAlertText")
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        self.hide()

    def show_message(self, text):
        self.label.setText(text)
        self.show()

    def text(self):
        return self.label.text() if self.isVisible() else ""


class _SignInDialog(QDialog):
    """Two-pane shell shared by account sign-in and PIN unlock.

    Subclasses provide ``_validate``, ``_start_work`` and the stage table; the
    shell owns the progress choreography, attempts, lockout and the shake.
    """

    busy_text = "Signing in…"
    stages = {}

    def __init__(self, window_title, eyebrow, title, subtitle, submit_text, footer="",
                 theme="light", version="", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._attempts = 0
        self._busy = False
        self._token = 0
        self._queue = []
        self._shake_anim = None
        self._shake_origin = QPoint()
        self.setWindowTitle(window_title)
        self.setFixedSize(860, 540)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.hero = AuthHero(version)
        root.addWidget(self.hero)

        panel = QFrame()
        panel.setObjectName("authForm")
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.progress = ProgressLine(theme, height=3)
        column.addWidget(self.progress)

        content = QVBoxLayout()
        content.setContentsMargins(52, 42, 52, 26)
        content.setSpacing(0)
        eyebrow_label = QLabel(eyebrow.upper())
        eyebrow_label.setObjectName("eyebrow")
        eyebrow_label.setContentsMargins(3, 0, 0, 0)
        content.addWidget(eyebrow_label)
        content.addSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("authTitle")
        self.title_label.setContentsMargins(2, 0, 0, 0)
        content.addWidget(self.title_label)
        content.addSpacing(4)
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("authSubtitle")
        subtitle_label.setContentsMargins(3, 0, 0, 0)
        subtitle_label.setWordWrap(True)
        content.addWidget(subtitle_label)
        content.addSpacing(24)
        self.fields = QVBoxLayout()
        self.fields.setSpacing(12)
        content.addLayout(self.fields)
        self.options = QHBoxLayout()
        self.options.setContentsMargins(3, 10, 3, 0)
        content.addLayout(self.options)
        content.addSpacing(20)
        self.alert = _AlertBanner(theme)
        content.addWidget(self.alert)
        self.submit = LoadingButton(submit_text, "arrow-right", trailing_icon=True)
        self.submit.setObjectName("primary")
        self.submit.setMinimumHeight(46)
        self.submit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit.setAutoDefault(False)
        self.submit.clicked.connect(self._submit)
        content.addWidget(self.submit)
        content.addSpacing(10)
        self.status = QLabel("")
        self.status.setObjectName("authStatus")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setMinimumHeight(18)
        content.addWidget(self.status)
        content.addStretch(1)
        self.footer = QLabel(footer)
        self.footer.setObjectName("authFooter")
        self.footer.setContentsMargins(3, 0, 0, 0)
        self.footer.setWordWrap(True)
        if not footer:
            self.footer.hide()
        content.addWidget(self.footer)
        column.addLayout(content, 1)
        root.addWidget(panel, 1)

        self._stage_timer = QTimer(self)
        self._stage_timer.setSingleShot(True)
        self._stage_timer.timeout.connect(self._pump)
        self._worker = None

    # --- subclass hooks ------------------------------------------------------
    def _validate(self):
        return True

    def _start_work(self, token):
        raise NotImplementedError

    def _set_inputs_enabled(self, enabled):
        pass

    def _after_failure(self):
        pass

    def _welcome_text(self, result):
        return "Welcome back"

    # --- flow ------------------------------------------------------------------
    @property
    def attempts(self):
        return self._attempts

    def is_busy(self):
        return self._busy

    def _submit(self):
        if self._busy or not self.submit.isEnabled() or not self._validate():
            return
        self._busy = True
        self._token += 1
        self._queue.clear()
        self._stage_timer.stop()
        self.alert.hide()
        self._set_inputs_enabled(False)
        self.submit.start_loading(self.busy_text)
        self.hero.set_mode("busy")
        self.progress.start(target=0.08, duration=220)
        self._start_work(self._token)

    def _post(self, token, kind, payload=None):
        if token != self._token or not self._busy:
            return
        self._queue.append((kind, payload))
        self._pump()

    def _pump(self):
        if self._stage_timer.isActive() or not self._queue:
            return
        kind, payload = self._queue.pop(0)
        if kind in self.stages:
            target, text = self.stages[kind]
            self.progress.advance(target, 520)
            self._set_status(text)
            self._stage_timer.start(STAGE_MIN_MS)
        elif kind == "success":
            self._succeed(payload)
        elif kind == "failure":
            self._fail(payload)
        elif kind == "blocked":
            self._blocked(payload)
        elif kind == "error":
            self._error(payload)

    def _succeed(self, result):
        self._busy = False
        self.progress.finish("ok")
        self.hero.set_mode("ok")
        self.submit.set_success(self._welcome_text(result))
        self._set_status("Loading your workspace…", "ok")
        QTimer.singleShot(SUCCESS_HOLD_MS, self._finish_accept)

    def _finish_accept(self):
        if self.isVisible():
            self.accept()

    def _settle(self):
        self._busy = False
        self._queue.clear()
        self.progress.fail()
        self.hero.set_mode("bad")
        self.submit.stop_loading()
        self._set_inputs_enabled(True)
        self._set_status("")

    def _fail(self, message):
        self._attempts += 1
        remaining = MAX_ATTEMPTS - self._attempts
        self._settle()
        if remaining <= 0:
            self.alert.show_message("Too many failed attempts. The dashboard will close.")
            self._set_inputs_enabled(False)
            self.submit.setEnabled(False)
            QTimer.singleShot(LOCKOUT_CLOSE_MS, self.reject)
        else:
            plural = "attempt" if remaining == 1 else "attempts"
            self.alert.show_message(f"{message} {remaining} {plural} left.")
            self._after_failure()
        self._shake()

    def _blocked(self, message):
        """Right credentials, but the account may not sign in; not a failed attempt."""
        self._settle()
        self.alert.show_message(message)
        self._after_failure()
        self._shake()

    def _error(self, detail):
        self._settle()
        detail = " ".join(str(detail).split())
        if len(detail) > 140:
            detail = detail[:137] + "…"
        self.alert.show_message(f"Couldn't reach the user database. {detail}".strip())

    def _set_status(self, text, tone=None):
        self.status.setText(text)
        repolish(self.status, "tone", tone)

    def _shake(self):
        if self._shake_anim is not None:
            self._shake_anim.stop()
            self.move(self._shake_origin)
        self._shake_origin = self.pos()
        animation = QPropertyAnimation(self, b"pos", self)
        animation.setDuration(420)
        offsets = (0, -12, 10, -8, 6, -3, 0)
        for index, dx in enumerate(offsets):
            animation.setKeyValueAt(index / (len(offsets) - 1), self._shake_origin + QPoint(dx, 0))
        animation.start()
        self._shake_anim = animation

    def done(self, result):
        # Results of an attempt still in flight must not act on a closed dialog.
        self._token += 1
        self._busy = False
        self._stage_timer.stop()
        super().done(result)


class UserLoginDialog(_SignInDialog):
    """Username and password sign-in against the ``users`` table."""

    busy_text = "Signing in…"
    stages = {
        "connect": (0.36, "Connecting to the user database…"),
        "verify": (0.74, "Verifying your credentials…"),
    }

    def __init__(self, settings, theme="light", parent=None, version="", notice=""):
        super().__init__("Face ID Attendance - Sign In", "Secure sign-in", "Welcome back",
                         "Sign in to manage attendance, employees and access.", "Sign in",
                         footer="New here? An administrator can create your account "
                                "in Users & Access.",
                         theme=theme, version=version, parent=parent)
        self._settings = settings
        self.current_user = None
        self.remember = True

        self.username = TextInput("Enter your username", icon="user", theme=theme)
        self.password = TextInput("Enter your password", icon="lock", password=True, theme=theme)
        self.username_field = FormField("Username", self.username, theme=theme)
        self.password_field = FormField("Password", self.password, theme=theme)
        self.fields.addWidget(self.username_field)
        self.fields.addWidget(self.password_field)

        self.remember_check = QCheckBox("Remember me on this device")
        self.remember_check.setChecked(True)
        self.options.addWidget(self.remember_check)
        self.options.addStretch(1)
        self.forgot = QPushButton("Forgot password?")
        self.forgot.setObjectName("linkButton")
        self.forgot.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forgot.setAutoDefault(False)
        self.forgot.clicked.connect(self._explain_reset)
        self.options.addWidget(self.forgot)

        self.username.returnPressed.connect(self.password.setFocus)
        self.password.returnPressed.connect(self._submit)
        self.username.setFocus()
        if notice:
            self.alert.show_message(notice)

    def _explain_reset(self):
        self._set_status("Ask an administrator to reset it in Users & Access.")

    def _validate(self):
        missing = []
        if not self.username.text().strip():
            self.username_field.set_error("Enter your username.")
            missing.append(self.username)
        if not self.password.text():
            self.password_field.set_error("Enter your password.")
            missing.append(self.password)
        if missing:
            missing[0].setFocus()
        return not missing

    def _start_work(self, token):
        self._post(token, "connect")
        worker = _SignInWorker(token, self._settings, self.username.text().strip(),
                               self.password.text())
        worker.stage.connect(self._on_stage)
        worker.result.connect(self._on_result)
        # Parked, not owned: closing the dialog mid-attempt must not end the thread.
        self._worker = worker
        start_detached(worker)

    def _on_stage(self, token, name):
        self._post(token, name)

    def _on_result(self, token, outcome, payload):
        if outcome == "ok":
            self._post(token, "success", payload)
        elif outcome == "wrong":
            self._post(token, "failure", "Incorrect username or password.")
        elif outcome == "disabled":
            self._post(token, "blocked", DISABLED_MESSAGE)
        else:
            self._post(token, "error", payload)

    def _succeed(self, user):
        self.current_user = user
        self.remember = self.remember_check.isChecked()
        super()._succeed(user)

    def _welcome_text(self, user):
        name = (getattr(user, "full_name", "") or getattr(user, "username", "")).split()
        return f"Welcome back, {name[0]}" if name else "Welcome back"

    def _set_inputs_enabled(self, enabled):
        for widget in (self.username, self.password, self.remember_check, self.forgot):
            widget.setEnabled(enabled)

    def _after_failure(self):
        self.password.clear()
        self.password.setFocus()


class LoginDialog(_SignInDialog):
    """Dashboard PIN unlock, used when no user accounts exist yet."""

    busy_text = "Checking PIN…"
    stages = {"verify": (0.7, "Checking your PIN…")}

    def __init__(self, pin_hash, parent=None, theme="light", version=""):
        super().__init__("Face ID Attendance - Login", "Dashboard lock", "Unlock the dashboard",
                         "Enter the dashboard PIN to continue.", "Unlock",
                         footer="The PIN can be changed or removed in Settings.",
                         theme=theme, version=version, parent=parent)
        self._pin_hash = pin_hash
        self.pin = TextInput("Enter the dashboard PIN", icon="lock", password=True, theme=theme)
        self.pin_field = FormField("PIN", self.pin, theme=theme)
        self.fields.addWidget(self.pin_field)
        self.pin.returnPressed.connect(self._submit)
        self.pin.setFocus()

    def _validate(self):
        if not self.pin.text():
            self.pin_field.set_error("Enter the PIN.")
            self.pin.setFocus()
            return False
        return True

    def _start_work(self, token):
        correct = hash_pin(self.pin.text()) == self._pin_hash
        self._post(token, "verify")
        if correct:
            self._post(token, "success", None)
        else:
            self._post(token, "failure", "Incorrect PIN.")

    def _welcome_text(self, _result):
        return "Unlocked"

    def _set_inputs_enabled(self, enabled):
        self.pin.setEnabled(enabled)

    def _after_failure(self):
        self.pin.clear()
        self.pin.setFocus()
