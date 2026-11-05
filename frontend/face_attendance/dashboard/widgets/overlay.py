"""Modal laid over its host window: a dimmed scrim with a centred card."""
from PyQt6.QtCore import QEvent, QPropertyAnimation, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QDialog, QFrame, QHBoxLayout, QVBoxLayout


class WindowOverlay(QDialog):
    """Frameless dialog that covers its host; subclasses fill ``self.column``.

    A *dismissible* overlay closes on Escape or a click on the scrim; one that
    is not (e.g. while the application shuts down) only closes through
    ``done()``.
    """

    CARD_WIDTH = 408
    SCRIM_ALPHA = {"light": 120, "dark": 150}

    def __init__(self, theme="light", parent=None, dismissible=True, title=""):
        super().__init__(parent)
        self._theme = theme
        self._host = parent
        self._dismissible = dismissible
        self.setObjectName("plain")
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.card = QFrame()
        self.card.setObjectName("alertCard")
        self.card.setFixedWidth(self.CARD_WIDTH)
        row.addWidget(self.card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        self.column = QVBoxLayout(self.card)
        self.column.setContentsMargins(28, 28, 28, 22)
        self.column.setSpacing(0)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(140)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        if parent is not None:
            parent.installEventFilter(self)
            self._cover_host()
        else:
            self.resize(self.CARD_WIDTH + 48, 440)

    def _cover_host(self):
        self.setGeometry(self._host.geometry())

    def eventFilter(self, watched, event):
        if watched is self._host and event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            self._cover_host()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.setWindowOpacity(0.0)
        self._fade.start()

    def paintEvent(self, event):
        if self._host is None:
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(4, 12, 26, self.SCRIM_ALPHA.get(self._theme, 130)))
        painter.end()

    def mousePressEvent(self, event):
        # A click on the dimmed backdrop dismisses the overlay, like Escape.
        if self._dismissible and not self.card.geometry().contains(event.position().toPoint()):
            self.reject()
            return
        super().mousePressEvent(event)

    def reject(self):
        if self._dismissible:
            super().reject()

    def done(self, result):
        if self._host is not None:
            self._host.removeEventFilter(self)
        super().done(result)
