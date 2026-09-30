"""Form building blocks shared by the sign-in, user and profile dialogs.

``FormField`` stacks a label, an input wrapped in a focus ring and a helper line
that doubles as the inline error.  ``TextInput`` adds a leading glyph and, for
secrets, a reveal toggle.  The role and permission pickers are clickable cards
rather than bare radio buttons and checkboxes.
"""
import re

from PyQt6.QtCore import QEvent, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                             QVBoxLayout, QWidget)

from ..icons import make_icon
from ..theme import palette
from .stat_card import IconTile
from .toggle import ToggleSwitch

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def repolish(widget, name, value):
    """Set a dynamic property and re-apply the stylesheet when it changes."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def password_strength(password):
    """Score 0-4 with a label; length matters most, character variety adds the rest."""
    if not password:
        return 0, ""
    if len(password) < 6:
        return 0, "Too short"
    variety = sum(bool(re.search(pattern, password))
                  for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    points = (len(password) >= 10) + (len(password) >= 14) + (variety >= 2) + (variety >= 3)
    score = min(4, 1 + points)
    return score, ("Weak", "Fair", "Good", "Strong")[score - 1]


class TextInput(QLineEdit):
    """Line edit with a leading glyph and, for secrets, a show/hide toggle."""

    def __init__(self, placeholder="", icon=None, password=False, theme="light", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._icon_name = icon
        self._revealed = False
        self._lead = None
        self._reveal = None
        self.setPlaceholderText(placeholder)
        self.setMinimumHeight(40)
        if icon:
            self._lead = self.addAction(make_icon(icon, 16), QLineEdit.ActionPosition.LeadingPosition)
        if password:
            self.setEchoMode(QLineEdit.EchoMode.Password)
            self._reveal = self.addAction(make_icon("eye", 16), QLineEdit.ActionPosition.TrailingPosition)
            self._reveal.triggered.connect(self.toggle_reveal)
        self.set_theme(theme)

    @property
    def revealed(self):
        return self._revealed

    def toggle_reveal(self):
        self.set_revealed(not self._revealed)

    def set_revealed(self, revealed):
        if self._reveal is None:
            return
        self._revealed = bool(revealed)
        self.setEchoMode(QLineEdit.EchoMode.Normal if self._revealed else QLineEdit.EchoMode.Password)
        self._paint_icons()

    def set_theme(self, theme):
        self._theme = theme
        self._paint_icons()

    def _paint_icons(self):
        muted = palette(self._theme)["muted"]
        if self._lead is not None:
            self._lead.setIcon(make_icon(self._icon_name, 16, muted, ratio=2.0))
        if self._reveal is not None:
            self._reveal.setIcon(make_icon("eye-off" if self._revealed else "eye", 16, muted, ratio=2.0))
            self._reveal.setToolTip("Hide password" if self._revealed else "Show password")


class FieldRing(QFrame):
    """Soft focus halo around an input; QSS has no box-shadow to draw one."""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.setObjectName("fieldRing")
        self.editor = editor
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(editor)
        editor.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            repolish(self, "focused", event.type() == QEvent.Type.FocusIn)
        return super().eventFilter(watched, event)

    def set_invalid(self, invalid):
        repolish(self, "invalid", bool(invalid))
        repolish(self.editor, "invalid", bool(invalid))


class FormField(QWidget):
    """Label, ringed input and a helper line that turns into the inline error."""

    def __init__(self, label, editor, help_text="", optional=False, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        self.editor = editor
        self._help = help_text
        self._theme = theme
        self._error = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.label = QLabel()
        self.label.setObjectName("fieldLabel")
        self.label.setContentsMargins(3, 0, 0, 0)
        self._label_text = label
        self._optional = optional
        self._render_label()
        layout.addWidget(self.label)
        self.ring = FieldRing(editor)
        layout.addWidget(self.ring)
        self.help_label = QLabel(help_text)
        self.help_label.setObjectName("fieldHelp")
        self.help_label.setContentsMargins(3, 0, 0, 0)
        self.help_label.setWordWrap(True)
        layout.addWidget(self.help_label)
        if not help_text:
            self.help_label.hide()
        if isinstance(editor, QLineEdit):
            editor.textEdited.connect(lambda _text: self.clear_error())

    def _render_label(self):
        muted = palette(self._theme)["muted"]
        suffix = (f' <span style="color:{muted}; font-weight:400;">(optional)</span>'
                  if self._optional else "")
        self.label.setText(f"{self._label_text}{suffix}")

    @property
    def has_error(self):
        return bool(self._error)

    def set_error(self, message):
        self._error = str(message)
        self.ring.set_invalid(True)
        self.help_label.setText(self._error)
        repolish(self.help_label, "tone", "bad")
        self.help_label.setVisible(True)

    def clear_error(self):
        if not self._error:
            return
        self._error = ""
        self.ring.set_invalid(False)
        self.set_hint(self._help)

    def set_hint(self, text, tone=None):
        """Neutral or toned helper text; an active error keeps priority."""
        if self._error:
            return
        self.help_label.setText(text)
        repolish(self.help_label, "tone", tone)
        self.help_label.setVisible(bool(text))

    def set_theme(self, theme):
        self._theme = theme
        self._render_label()
        if hasattr(self.editor, "set_theme"):
            self.editor.set_theme(theme)


class StrengthMeter(QWidget):
    """Four segments that fill and recolour with ``password_strength``."""

    COLORS = {0: "danger", 1: "danger", 2: "warn", 3: "info", 4: "success"}

    def __init__(self, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        self._theme = theme
        self._score = 0
        self._active = False
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 0, 3, 0)
        row.setSpacing(10)
        self._bar = _Segments(self)
        row.addWidget(self._bar, 1)
        self.label = QLabel("")
        self.label.setObjectName("fieldHelp")
        self.label.setMinimumWidth(62)
        self.label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.label)
        self.setVisible(False)

    def set_password(self, password):
        score, text = password_strength(password)
        self._score = score
        self._active = bool(password)
        self.setVisible(self._active)
        self.label.setText(text)
        color = palette(self._theme)[self.COLORS[score]]
        self.label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self._bar.update()

    def score(self):
        return self._score

    def set_theme(self, theme):
        self._theme = theme
        self._bar.update()


class _Segments(QWidget):
    def __init__(self, meter):
        super().__init__(meter)
        self._meter = meter
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        c = palette(self._meter._theme)
        score = self._meter._score
        lit = max(1, score) if self._meter._active else 0
        fill = QColor(c[StrengthMeter.COLORS[score]])
        empty = QColor(c["border"])
        gap = 5.0
        width = (self.width() - gap * 3) / 4
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(4):
            painter.setBrush(fill if index < lit else empty)
            painter.drawRoundedRect(QRectF(index * (width + gap), 0, width, self.height()), 3, 3)
        painter.end()


class _RadioMark(QWidget):
    """Painted radio circle for ``ChoiceCard`` (QSS indicators need a real QRadioButton)."""

    def __init__(self, card):
        super().__init__(card)
        self._card = card
        self.setFixedSize(20, 20)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        c = palette(self._card._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(1.5, 1.5, 17, 17)
        if self._card.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c["primary"]))
            painter.drawEllipse(rect)
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(rect.adjusted(5.5, 5.5, -5.5, -5.5))
        else:
            painter.setPen(QPen(QColor(c["input_border"]), 1.5))
            painter.setBrush(QColor(c["card"]))
            painter.drawEllipse(rect)
        painter.end()


class _ClickableCard(QFrame):
    """A QFrame (so QSS can style it) that behaves like a focusable button."""

    clicked = pyqtSignal()

    def __init__(self, object_name, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pressed = False
        self._checked = False
        self.setProperty("checked", False)

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        repolish(self, "checked", checked)
        self._on_checked(checked)

    def _on_checked(self, checked):
        pass

    def mousePressEvent(self, event):
        self._pressed = event.button() == Qt.MouseButton.LeftButton
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._pressed and self.isEnabled() and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        self._pressed = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _text_column(title, body, title_name, body_name):
        column = QVBoxLayout()
        column.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName(title_name)
        body_label = QLabel(body)
        body_label.setObjectName(body_name)
        body_label.setWordWrap(True)
        column.addWidget(title_label)
        column.addWidget(body_label)
        return column, title_label, body_label


class ChoiceCard(_ClickableCard):
    """Selectable card with an icon, a title, a description and a radio mark."""

    def __init__(self, icon, title, body, tone="blue", theme="light", parent=None):
        super().__init__("choiceCard", parent)
        self._theme = theme
        self._tone = tone
        self.setAccessibleName(title)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)
        self.tile = IconTile(icon, tone=tone, size=38, theme=theme)
        row.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignTop)
        column, self.title_label, self.body_label = self._text_column(
            title, body, "choiceTitle", "choiceBody")
        row.addLayout(column, 1)
        self.mark = _RadioMark(self)
        row.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignTop)

    def _on_checked(self, checked):
        self.mark.update()

    def set_theme(self, theme):
        self._theme = theme
        self.tile.set_tone(self._tone, theme)
        self.mark.update()


class PermissionTile(_ClickableCard):
    """One permission: coloured glyph while granted, grey when not, plus a switch."""

    toggled = pyqtSignal(bool)

    def __init__(self, icon, title, body, tone="blue", checked=False, theme="light", parent=None):
        super().__init__("permTile", parent)
        self._theme = theme
        self._tone = tone
        self.setAccessibleName(title)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        self.tile = IconTile(icon, tone="gray", size=32, theme=theme)
        row.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignVCenter)
        column, self.title_label, self.body_label = self._text_column(
            title, body, "permTitle", "permBody")
        row.addLayout(column, 1)
        # Built in its final state so tiles do not animate when a dialog opens.
        self.switch = ToggleSwitch(checked, theme=theme)
        self.switch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.switch.toggled.connect(self._switch_toggled)
        row.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.clicked.connect(self.switch.toggle)
        super().setChecked(checked)
        self.tile.set_tone(tone if checked else "gray", theme)

    def _switch_toggled(self, checked):
        self.setChecked(checked)
        self.toggled.emit(checked)

    def _on_checked(self, checked):
        if self.switch.isChecked() != checked:
            self.switch.blockSignals(True)
            self.switch.setChecked(checked)
            self.switch.blockSignals(False)
            self.switch._animate(checked)
        self.tile.set_tone(self._tone if checked else "gray", self._theme)

    def set_theme(self, theme):
        self._theme = theme
        self.switch.set_theme(theme)
        self.tile.set_tone(self._tone if self.isChecked() else "gray", theme)


class SectionHeading(QWidget):
    """Eyebrow title with a hairline that runs to the right edge."""

    def __init__(self, text, hint="", parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        column = QVBoxLayout(self)
        column.setContentsMargins(3, 0, 0, 0)
        column.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.title = QLabel(text.upper())
        self.title.setObjectName("eyebrow")
        row.addWidget(self.title)
        line = QFrame()
        line.setObjectName("hLine")
        line.setFixedHeight(1)
        row.addWidget(line, 1, Qt.AlignmentFlag.AlignVCenter)
        self.trailing = QHBoxLayout()
        self.trailing.setSpacing(6)
        row.addLayout(self.trailing)
        column.addLayout(row)
        self.hint = QLabel(hint)
        self.hint.setObjectName("formSectionHint")
        self.hint.setWordWrap(True)
        column.addWidget(self.hint)
        if not hint:
            self.hint.hide()
