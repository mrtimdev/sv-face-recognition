"""Live Monitor: preview, engine control, live statistics, status and logs."""
from datetime import datetime
from dataclasses import replace
from pathlib import Path

import threading

import cv2
from PyQt6.QtCore import QEvent, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel,
                             QDialog, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QPushButton, QSplitter, QToolButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ... import hrm_directory
from ...hrm_sync import HRMError, config_from_settings, configuration_problem
from ...settings import MAX_LIVE_CAMERAS, describe_source
from ...config import ATTENDANCE_MODES
from ...storage import context_path, write_jpeg
from ..access import guard
from ..icons import IconLabel, apply_button_icon, make_icon
from ..bridge import to_pixmap
from ...sound_events import EventSounds, SOUND_LABELS
from ...audio import format_checkin_speech
from ..theme import palette, tone_color
from ..widgets import (ActivityFeed, Card, StatCard,
                       ToastBar, ToggleSwitch, VideoView)
from ..widgets.camera_bar import CameraBar, CameraDialog
from ..widgets.tile_controls import SplitterAnimator, TileControls
from ..widgets.activity_detail import ActivityDetailDialog, angle_photos, enrollment_photos, load_photo

STATUS_TONES = {"CONNECTED": "ok", "CONNECTING": "warn", "RECONNECTING": "warn",
                "CAMERA DISCONNECTED": "bad", "STOPPED": "idle"}

STATUS_ROW_ICONS = (("camera", "Camera"), ("database", "Database"),
                    ("shield", "Recognition Engine"), ("folder", "Storage"),
                    ("bolt", "Auto Recording"))

LOG_ROW_LIMIT = 250
PREVIEW_MIN_HEIGHT = 520
TILE_MIN_SIZE = QSize(120, 90)  # Small, so splitters can animate a tile shut smoothly.


class LogRow(QWidget):
    """One log line: level chip, monospace timestamp and message."""

    def __init__(self, level, stamp, message, theme="light", parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 1, 6, 1)
        row.setSpacing(10)
        self.tag = QLabel(level)
        self.tag.setObjectName("logTag")
        self.tag.setProperty("level", level)
        self.tag.setFixedWidth(52)
        self.tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.tag)
        self.stamp = QLabel(stamp)
        self.stamp.setObjectName("logTimestamp")
        row.addWidget(self.stamp)
        self.message = QLabel(message)
        self.message.setObjectName("logMessage")
        self.message.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.message, 1)

    def set_theme(self, theme):
        for widget in (self.tag, self.stamp, self.message):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class LiveScreen(QWidget):
    detachRequested = pyqtSignal()
    flashPreviewRequested = pyqtSignal()
    viewAllRequested = pyqtSignal()
    settingsRequested = pyqtSignal()
    settingsApplied = pyqtSignal(object, bool)
    _detailAnglesReady = pyqtSignal(str, bool)      # Employee code, and whether HRM answered.

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._detailAnglesReady.connect(self._on_detail_angles)
        self.cards = {}
        self._status_rows = {}
        self._log_rows = []
        self._theme = settings.theme
        self._camera_status = "STOPPED"
        self._storage_ready = False
        self._sound = EventSounds(settings)
        self._build()
        self._connect()
        self.escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.escape_shortcut.activated.connect(self._exit_fullscreen)
        self.escape_shortcut.setEnabled(False)
        self._sync_running(engine.running)

    # ── build ────────────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("liveScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.main_scroll = scroll

        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(12)

        # ── KPI cards ───────────────────────────────────────────────────
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        card_defs = (
            ("enrolled", "Enrolled", "-", "users", "blue", "Employees in catalog"),
            ("verified", "Verified Today", "0", "check-circle", "green", "Employees checked in today"),
            ("waiting", "Waiting", "-", "clock", "orange", "Presence accumulating"),
            ("unknown", "Unknown", "0", "alert", "purple", "Faces not in catalog"),
            ("inactive", "Inactive", "0", "alert", "orange", "Attendance blocked"),
        )
        for key, label, value, icon, tone, hint in card_defs:
            card = StatCard(label, value, hint=hint, icon=icon, tone=tone)
            self.cards[key] = card
            cards_row.addWidget(card)
        self._build_control_actions()

        # Keep the preview and activity visible together; setup stays below.
        # Drag the divider to give the cameras or the activity list more room.
        body = QSplitter(Qt.Orientation.Horizontal)
        body.setObjectName("liveBodySplitter")
        body.setChildrenCollapsible(False)
        body.setHandleWidth(12)
        body.addWidget(self._build_camera_card())
        body.addWidget(self._build_activity_card())
        body.setStretchFactor(0, 7)
        body.setStretchFactor(1, 3)
        body.setSizes([7000, 3000])
        body.setStyleSheet("QSplitter#liveBodySplitter::handle { background: transparent; }")
        self.body_splitter = body

        root.addWidget(body, 1)
        root.addLayout(cards_row)
        diagnostics = QHBoxLayout()
        diagnostics.setSpacing(12)
        diagnostics.addWidget(self._build_requirements_card(), 1)
        diagnostics.addWidget(self._build_status_card(), 1)
        root.addLayout(diagnostics)
        root.addWidget(self._build_log_card())

        self.toast = ToastBar(theme=self._theme)
        root.addWidget(self.toast)

        self.problem = QLabel("")
        self.problem.setObjectName("emptyBody")
        self.problem.setWordWrap(True)
        self.problem.hide()
        root.addWidget(self.problem)

        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

    def _build_requirements_card(self):
        card = Card("Attendance requirements", "Choose one option", icon="shield", theme=self._theme)
        self.requirements_card = card
        self.requirement_group = QButtonGroup(self)
        self.requirement_group.setExclusive(True)
        self.requirement_boxes = {}
        for mode, label in ATTENDANCE_MODES.items():
            checkbox = QCheckBox(label)
            checkbox.setProperty("attendance_mode", mode)
            checkbox.setChecked(mode == self.settings.attendance_mode)
            self.requirement_group.addButton(checkbox)
            self.requirement_boxes[mode] = checkbox
            card.add(checkbox)
        self.requirement_note = QLabel()
        self.requirement_note.setWordWrap(True)
        card.add(self.requirement_note)
        self.apply_requirements_button = QPushButton("Apply requirements")
        self.apply_requirements_button.setToolTip("Save this option and restart a running engine to clear old verification evidence")
        self.apply_requirements_button.clicked.connect(self._apply_requirements)
        actions = QHBoxLayout()
        actions.addWidget(self.apply_requirements_button)
        self.test_sound_button = QPushButton("Test alert sound")
        self.test_sound_button.clicked.connect(self._test_sound)
        self.sound_choice = QComboBox()
        for key, label in SOUND_LABELS.items():
            self.sound_choice.addItem(label, key)
        self.sound_choice.setCurrentIndex(self.sound_choice.findData("success"))
        self.sound_choice.setAccessibleName("Sound to preview")
        actions.addWidget(self.sound_choice)
        actions.addWidget(self.test_sound_button)
        card.add_layout(actions)
        self.requirement_group.buttonToggled.connect(lambda *_: self._update_requirements_note())
        self._sync_requirements()
        return card

    def _sync_requirements(self):
        self.requirement_boxes[self.settings.attendance_mode].setChecked(True)
        self._update_requirements_note()

    def _update_requirements_note(self):
        active = getattr(self.engine, "config", None)
        mode = active.attendance_mode if self.engine.running and active else self.settings.attendance_mode
        selected = self.requirement_group.checkedButton()
        pending = selected.property("attendance_mode") if selected else mode
        note = (f"Selected: {ATTENDANCE_MODES[pending]}. Click Apply requirements to activate. "
                if pending != mode else "")
        self.requirement_note.setText(
            note + f"{'Active' if self.engine.running else 'On next start'}: {ATTENDANCE_MODES[mode]}. "
            "Face recognition and anti-spoof checks remain required.")

    def _apply_requirements(self):
        if not guard(self, "manage_settings", "change the attendance requirements"):
            return
        mode = self.requirement_group.checkedButton().property("attendance_mode")
        candidate = replace(self.settings, attendance_mode=mode)
        try:
            candidate.to_config()
            candidate._path = candidate.save(getattr(self.settings, "_path", None))
        except (OSError, ValueError) as exc:
            self.toast.show_message(f"Could not save attendance requirements: {exc}", "bad")
            return
        running = self.engine.running
        self.settings = candidate
        self.engine.settings = candidate
        try:
            if running:
                self.engine.restart(candidate)
            self.settingsApplied.emit(candidate, running)
            self._sync_requirements()
            self.toast.show_message(f"Saved: {ATTENDANCE_MODES[mode]}", "ok")
        except (OSError, ValueError, RuntimeError) as exc:
            self.settingsApplied.emit(candidate, False)
            self._sync_requirements()
            self.toast.show_message(f"Requirements saved; engine restart failed: {exc}", "bad")

    def _test_sound(self):
        if not self._sound.available:
            self.toast.show_message("Audio playback unavailable on this system.", "bad")
        elif not self._sound.play(self.sound_choice.currentData(), preview=True):
            self.toast.show_message("Sound is busy or playback failed; try again shortly.", "warn")

    def _build_camera_card(self):
        card = Card(theme=self._theme, divider=False)
        card.header.hide()
        self.camera_card = card

        self.live_badge = QLabel("LIVE")
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.setFixedHeight(22)
        card.actions.addWidget(self.live_badge)

        # Which saved cameras Live Monitor runs; each gets its own tile.
        self.camera_bar = CameraBar(theme=self._theme)
        self.camera_bar.toggled.connect(self._set_camera_selected)
        self.camera_bar.soloRequested.connect(lambda index: self._apply_cameras([index]))
        self.camera_bar.addRequested.connect(lambda: self._edit_camera(None))
        self.camera_bar.editRequested.connect(self._edit_camera)
        self.camera_bar.removeRequested.connect(self._remove_camera)
        card.add(self.camera_bar)

        self.gear_button = QPushButton()
        self.gear_button.setObjectName("iconButton")
        self.gear_button.setFixedSize(34, 34)
        self.gear_button.setToolTip("Camera settings")
        self.gear_button.setAccessibleName("Camera settings")
        self.gear_button.clicked.connect(self._open_settings)
        self.gear_button.setParent(self)
        self.gear_button.hide()

        self.expand_button = QPushButton()
        self.expand_button.setObjectName("iconButton")
        self.expand_button.setFixedSize(34, 34)
        self.expand_button.setToolTip("Toggle the on-screen picture guide")
        self.expand_button.setAccessibleName("Toggle picture guide")
        self.expand_button.clicked.connect(self._toggle_guide)
        card.actions.addWidget(self.expand_button)

        self.camera_box = QComboBox()
        self.camera_box.addItem(describe_source(self.settings.source))
        self.camera_box.setEnabled(False)
        self.camera_box.setFixedWidth(130)
        self.camera_box.setToolTip("Change the source on the Settings screen")
        self.camera_box.setParent(self)
        self.camera_box.hide()

        self.video = VideoView(theme=self._theme, message="Position your face in the frame",
                               subtitle="The engine is running and ready to detect")
        self.video.setMinimumSize(TILE_MIN_SIZE)
        self.preview_hud = self.video.enable_controls()
        self.preview_hud.set_source(describe_source(self.settings.source))
        self.preview_hud.record.clicked.connect(self._toggle_engine)
        self.preview_hud.pause.clicked.connect(self._toggle_pause)
        self.preview_hud.restart.clicked.connect(self._restart)
        self.preview_hud.snapshot.clicked.connect(self._save_snapshot)
        self.preview_hud.capture.clicked.connect(self._save_snapshot)
        self.preview_hud.flash.clicked.connect(self.flashPreviewRequested.emit)
        self.preview_hud.settings.clicked.connect(self._open_settings)
        self.preview_hud.captures.clicked.connect(self._open_captures)
        self.preview_hud.fullscreen.clicked.connect(self._toggle_fullscreen)
        self.preview_hud.detach.clicked.connect(self.detachRequested.emit)
        self.preview_hud.more.setMenu(self.more_button.menu())
        self.preview_hud.more.setPopupMode(self.preview_hud.more.ToolButtonPopupMode.InstantPopup)
        frame = QFrame()
        frame.setObjectName("cameraFrame")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        # Tiles live in splitters: drag a divider to resize them; enlarging animates.
        self.tile_rows = QSplitter(Qt.Orientation.Vertical)
        self.tile_rows.setObjectName("tileSplitter")
        self.tile_row_splitters = [QSplitter(Qt.Orientation.Horizontal) for _row in range(2)]
        for splitter in [self.tile_rows] + self.tile_row_splitters:
            splitter.setChildrenCollapsible(True)
            splitter.setHandleWidth(8)
            splitter.setOpaqueResize(True)
        for row in self.tile_row_splitters:
            self.tile_rows.addWidget(row)
        self.tile_row_splitters[0].addWidget(self.video)
        self.tile_rows.setMinimumHeight(PREVIEW_MIN_HEIGHT)
        self.tile_rows.setStyleSheet(
            "QSplitter::handle { background: transparent; }"
            "QSplitter::handle:hover { background: rgba(59,130,246,0.55); border-radius: 3px; }")
        self._row_animator = SplitterAnimator(self.tile_rows, self)
        self._column_animators = [SplitterAnimator(row, self) for row in self.tile_row_splitters]
        frame_layout.addWidget(self.tile_rows)
        self.extra_videos = []  # Tiles for the 2nd..4th selected camera.
        self._switchers = []    # One camera switcher per tile.
        self._focused = None    # Tile position enlarged by a double-click.
        self.video.installEventFilter(self)
        self.preview_hud.installEventFilter(self)
        self._layout_tiles(animate=False)
        card.add(frame, 1)

        info = QHBoxLayout()
        info.setSpacing(14)
        self.cam_source_label = QLabel("Source: -")
        self.cam_source_label.setObjectName("cameraValue")
        self.cam_source_label.setMaximumWidth(150)
        self.cam_resolution = QLabel("Resolution: -")
        self.cam_resolution.setObjectName("cameraInfo")
        self.cam_fps_label = QLabel("FPS: -")
        self.cam_fps_label.setObjectName("cameraInfo")
        self.cam_pipeline = QLabel("Pipeline: idle")
        self.cam_pipeline.setObjectName("cameraInfo")
        # Several cameras: engine-wide controls sit under the tiles like a player bar.
        info.addWidget(self._build_engine_strip())
        info.addWidget(self.cam_source_label)
        info.addStretch(1)
        info.addWidget(self.cam_pipeline)
        info.addWidget(self.cam_resolution)
        info.addWidget(self.cam_fps_label)
        card.add_layout(info)
        multi = len(self._tile_views()) > 1
        self.engine_strip.setVisible(multi)
        self.cam_source_label.setVisible(not multi)
        return card

    # ── cameras ──────────────────────────────────────────────────────────

    def _build_engine_strip(self):
        """Engine-wide controls shown above the grid when several cameras run."""
        strip = QWidget()
        strip.setObjectName("engineStrip")
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.strip_start = QPushButton("Start")
        self.strip_start.clicked.connect(self._toggle_engine)
        self.strip_pause = QPushButton("Pause all")
        self.strip_pause.clicked.connect(self._toggle_pause)
        self.strip_restart = QPushButton("Restart")
        self.strip_restart.setToolTip("Restart the engine and every camera")
        self.strip_restart.clicked.connect(self._restart)
        self.strip_fullscreen = QPushButton()
        self.strip_fullscreen.setToolTip("Full screen")
        self.strip_fullscreen.clicked.connect(self._toggle_fullscreen)
        self.strip_more = QPushButton()
        self.strip_more.setToolTip("More monitor actions")
        self.strip_more.setMenu(self.more_button.menu())
        for button in (self.strip_start, self.strip_pause, self.strip_restart,
                       self.strip_fullscreen, self.strip_more):
            button.setObjectName("stripButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(button.toolTip() or button.text())
            row.addWidget(button)
        self.engine_strip = strip
        self._style_engine_strip()
        return strip

    def _style_engine_strip(self):
        c = palette(self._theme)
        self.engine_strip.setStyleSheet(f"""
            QWidget#engineStrip {{ background: transparent; }}
            QPushButton#stripButton {{ color: {c['text']}; background: {c['panel_alt']};
                border: 1px solid {c['border']}; border-radius: 15px; padding: 5px 12px;
                font-size: 13px; font-weight: 600; min-height: 20px; }}
            QPushButton#stripButton:hover {{ background: {c['hover']}; border-color: {c['primary']}; }}
            QPushButton#stripButton::menu-indicator {{ image: none; width: 0; }}
            QPushButton#stripButton[tone="go"] {{ color: white; background: {c['success']};
                border-color: {c['success']}; }}
            QPushButton#stripButton[tone="stop"] {{ color: {c['danger']}; border-color: {c['danger_border']}; }}
        """)
        icon_color = c["text"]
        self.strip_restart.setIcon(make_icon("restart", 15, icon_color))
        self.strip_fullscreen.setIcon(make_icon("expand", 15, icon_color))
        self.strip_more.setIcon(make_icon("more", 15, icon_color))
        self._sync_engine_strip()

    def _sync_engine_strip(self, running=None):
        if not hasattr(self, "strip_start"):
            return
        running = self.engine.running if running is None else running
        c = palette(self._theme)
        self.strip_start.setText("Stop" if running else "Start")
        self.strip_start.setToolTip("Stop the engine and every camera" if running else "Start every camera")
        self.strip_start.setIcon(make_icon("stop" if running else "play", 15,
                                           c["danger"] if running else "#FFFFFF"))
        self.strip_start.setProperty("tone", "stop" if running else "go")
        self.strip_start.style().unpolish(self.strip_start)
        self.strip_start.style().polish(self.strip_start)
        paused = self.engine.paused
        self.strip_pause.setEnabled(bool(running))
        self.strip_pause.setText("Resume all" if paused else "Pause all")
        self.strip_pause.setIcon(make_icon("play" if paused else "pause", 15, c["text"]))
        self.strip_pause.setToolTip("Resume recording on every camera" if paused
                                    else "Pause recording on every camera; previews stay live")
        self.strip_restart.setEnabled(bool(running))

    def _tile_views(self):
        return [self.video] + self.extra_videos

    def _layout_tiles(self, animate=True):
        """One tile per selected camera: 1 full, 2 side by side, 3-4 in two rows."""
        selected = self.settings.selected_cameras()
        while len(self.extra_videos) < len(selected) - 1:
            view = VideoView(theme=self._theme, message="Engine stopped",
                             subtitle="Press Start Engine to begin")
            view.setMinimumSize(TILE_MIN_SIZE)
            view.installEventFilter(self)
            self.extra_videos.append(view)
        while len(self.extra_videos) > len(selected) - 1:
            view = self.extra_videos.pop()
            view.setParent(None)
            view.deleteLater()
        views = self._tile_views()
        multi = len(views) > 1
        if self._focused is not None and self._focused >= len(views):
            self._focused = None
        rows = [views[:2], views[2:]]
        fresh = set()
        for splitter, row_views in zip(self.tile_row_splitters, rows):
            for position, view in enumerate(row_views):
                if splitter.indexOf(view) < 0:
                    fresh.add(view)
                splitter.insertWidget(position, view)
            splitter.setVisible(bool(row_views))
        # Single camera: the full HUD. Several: compact per-tile controls + engine strip.
        self.preview_hud.setVisible(not multi)
        if hasattr(self, "engine_strip"):
            self.engine_strip.setVisible(multi)
            self.cam_source_label.setVisible(not multi)
        entries = self.settings.camera_entries()
        for position, (view, (index, entry)) in enumerate(zip(views, selected)):
            self._dress_tile(position, view, index, entry, entries, multi)
        self.camera_bar.set_cameras(entries, [index for index, _entry in selected])
        first = selected[0][1]
        self.preview_hud.set_source(f"{first['name']} · {describe_source(first['source'])}"
                                    if len(entries) > 1 else describe_source(first["source"]))
        self._resize_tiles(animate, fresh)

    def _dress_tile(self, position, view, index, entry, entries, multi):
        """Camera switcher top-left, per-camera controls bottom-right."""
        paused = self.engine.camera_paused(index)
        switcher = view.corner
        if switcher is None:
            switcher = QToolButton()
            switcher.setObjectName("tileSwitcher")
            switcher.setCursor(Qt.CursorShape.PointingHandCursor)
            switcher.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            switcher.setStyleSheet(
                "QToolButton#tileSwitcher { color: white; background: rgba(2,6,23,0.72);"
                " border: 1px solid rgba(255,255,255,0.18); border-radius: 12px;"
                " padding: 4px 10px; font-size: 12px; font-weight: 700; }"
                "QToolButton#tileSwitcher:hover { background: rgba(30,58,138,0.85); }"
                "QToolButton#tileSwitcher::menu-indicator { image: none; width: 0; }")
            switcher.setMenu(QMenu(switcher))
            view.set_corner(switcher)
        menu = switcher.menu()
        menu.clear()
        for other, other_entry in enumerate(entries):
            action = menu.addAction(other_entry["name"])
            action.setCheckable(True)
            action.setChecked(other == index)
            action.setToolTip(describe_source(other_entry["source"]))
            action.triggered.connect(lambda _checked, position=position, other=other:
                                     self._switch_tile(position, other))
        if multi:
            menu.addSeparator()
            menu.addAction("Back to grid" if self._focused == position else "Enlarge  (double-click)",
                           lambda position=position: self._toggle_focus(position))
        switcher.setText(f"{entry['name']}{'  ·  Paused' if paused else ''}  ▾")
        switcher.setToolTip(f"{describe_source(entry['source'])}\nSwitch the camera shown in this tile")
        switcher.setVisible(len(entries) > 1)
        controls = view.footer
        if controls is None and multi:
            controls = TileControls(view)
            controls.pauseClicked.connect(lambda position=position: self._toggle_camera_paused(position))
            controls.snapshotClicked.connect(lambda position=position: self._save_snapshot(position=position))
            controls.reconnectClicked.connect(lambda position=position: self._reconnect_camera(position))
            controls.enlargeClicked.connect(lambda position=position: self._toggle_focus(position))
            view.set_footer(controls)
        if controls is not None:
            controls.setVisible(multi)
            controls.set_paused(paused)
            controls.set_enlarged(self._focused == position)
            controls.set_running(self.engine.running)
        view._place_corner()

    def _resize_tiles(self, animate=True, fresh=()):
        """Equal tiles, or one enlarged tile; eased so the change is easy to follow."""
        views = self._tile_views()
        rows = [views[:2], views[2:]]
        focus = self._focused
        if focus is None:
            row_weights = [1, 1] if rows[1] else [1, 0]
            column_weights = [[1] * len(row) for row in rows]
        else:
            row_weights = [1, 0] if focus < 2 else [0, 1]
            column_weights = [[1 if views.index(view) == focus else 0 for view in row] for row in rows]
        self._tile_targets = (row_weights, column_weights)
        for splitter, animator, row, weights in zip(self.tile_row_splitters, self._column_animators,
                                                   rows, column_weights):
            if not row:
                continue
            if animate and fresh:
                # Newly added tiles grow out of zero width.
                sizes = splitter.sizes()
                total = sum(sizes) or 1
                old = [size for view, size in zip(row, sizes) if view not in fresh]
                share = total / max(1, len(old))
                splitter.setSizes([0 if view in fresh else round(share) for view in row])
            animator.animate(weights, animate)
        self._row_animator.animate(row_weights, animate)
        for position, view in enumerate(views):
            if view.footer is not None:
                view.footer.set_enlarged(focus == position)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonDblClick:
            views = self._tile_views()
            view = self.video if watched is self.preview_hud else watched
            if view in views and len(views) > 1:
                self._toggle_focus(views.index(view))
                return True
        return super().eventFilter(watched, event)

    def _toggle_focus(self, position):
        """Enlarge one tile; the other cameras keep recognizing in the background."""
        self._focused = None if self._focused == position else position
        self._layout_tiles()

    def _toggle_camera_paused(self, position):
        if not guard(self, "control_engine", "pause or resume a camera"):
            return
        selected = self.settings.selected_cameras()
        if position >= len(selected):
            return
        index = selected[position][0]
        self.engine.set_camera_paused(index, not self.engine.camera_paused(index))

    def _on_camera_paused(self, _index, _paused):
        # Re-label the tiles only: keep any sizes the user dragged.
        entries = self.settings.camera_entries()
        views = self._tile_views()
        for position, (view, (index, entry)) in enumerate(zip(views, self.settings.selected_cameras())):
            self._dress_tile(position, view, index, entry, entries, len(views) > 1)

    def _reconnect_camera(self, position):
        if not guard(self, "control_engine", "reconnect a camera"):
            return
        if self.engine.reconnect_camera(position):
            views = self._tile_views()
            if position < len(views):
                views[position].show_message("Reconnecting…", "Opening the stream again")

    def _switch_tile(self, position, index):
        chosen = [shown for shown, _entry in self.settings.selected_cameras()]
        if chosen[position] == index:
            return
        if index in chosen:  # Already on another tile: swap the two.
            other = chosen.index(index)
            chosen[other] = chosen[position]
        chosen[position] = index
        self._apply_cameras(chosen)

    def _set_camera_selected(self, index, selected):
        if not guard(self, "control_engine", "choose the cameras to run"):
            self._layout_tiles()
            return
        chosen = [position for position, _entry in self.settings.selected_cameras()]
        if selected and index not in chosen:
            if len(chosen) >= MAX_LIVE_CAMERAS:
                self.toast.show_message(f"Up to {MAX_LIVE_CAMERAS} cameras run at once. "
                                        "Hide one first.", "warn")
                self._layout_tiles()
                return
            chosen.append(index)
        elif not selected and index in chosen:
            if len(chosen) == 1:
                self.toast.show_message("At least one camera stays on. Show another one first.", "info")
                self._layout_tiles()
                return
            chosen.remove(index)
        else:
            return
        self._apply_cameras(chosen)

    def _edit_camera(self, index):
        if not guard(self, "manage_settings", "add or edit cameras"):
            return
        entries = self.settings.camera_entries()
        entry = None if index is None else entries[index]
        taken = [other["name"] for position, other in enumerate(entries) if position != index]
        dialog = CameraDialog(entry, taken, theme=self._theme, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = [shown for shown, _entry in self.settings.selected_cameras()]
        if index is None:
            entries.append(dialog.values())
            if len(chosen) < MAX_LIVE_CAMERAS:
                chosen.append(len(entries) - 1)  # A new camera shows up right away.
            restart = True
        else:
            restart = index in chosen and entries[index]["source"] != dialog.values()["source"]
            entries[index] = dialog.values()
        self._apply_cameras(chosen, entries, restart)

    def _remove_camera(self, index):
        if not guard(self, "manage_settings", "remove cameras"):
            return
        entries = self.settings.camera_entries()
        if len(entries) <= 1:
            return
        answer = QMessageBox.question(self, "Remove camera",
                                      f"Remove “{entries[index]['name']}” from the saved cameras?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        chosen = [shown for shown, _entry in self.settings.selected_cameras()]
        del entries[index]
        remaining = [shown - (shown > index) for shown in chosen if shown != index]
        self._apply_cameras(remaining or [0], entries, restart=index in chosen)

    def _apply_cameras(self, chosen, cameras=None, restart=None):
        """Save the camera list and choice; restart a running engine when it changes."""
        changes = {"live_cameras": list(chosen)}
        if cameras is not None:
            changes.update(cameras=cameras, source=cameras[0]["source"])
        previous = self.settings.selected_cameras()
        candidate = replace(self.settings, **changes)
        try:
            candidate.to_config()
            candidate._path = candidate.save(getattr(self.settings, "_path", None))
        except (OSError, ValueError) as exc:
            self.toast.show_message(f"Could not save the camera choice: {exc}", "bad")
            return
        running = self.engine.running
        if restart is None:
            restart = candidate.selected_cameras() != previous
        restart = running and restart
        self.settings = candidate
        self.engine.settings = candidate
        self._layout_tiles()
        if restart:
            self._sync_running(True)
        self.settingsApplied.emit(candidate, restart)
        if restart:
            self._restart()

    def _permission_source(self):
        """A local webcam among the selected cameras needs macOS camera access first."""
        sources = [entry["source"] for _index, entry in self.settings.selected_cameras()]
        return next((source for source in sources if str(source).strip().isdecimal()), sources[0])

    def _on_channel_frame(self, position, frame):
        if 0 < position <= len(self.extra_videos):
            self.extra_videos[position - 1].set_frame(frame)

    def _build_control_actions(self):
        # Keep action state in one place; visible controls live on the video HUD.
        self.start_button = QPushButton("Start Engine")
        self.start_button.setObjectName("controlButtonSuccess")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("controlButton")
        self.restart_button = QPushButton("Restart")
        self.restart_button.setObjectName("controlButton")
        self.snapshot_button = QPushButton("Snapshot")
        self.snapshot_button.setObjectName("controlButton")
        self.captures_button = QPushButton("Captures")
        self.captures_button.setObjectName("controlButton")
        self.more_button = QPushButton("More")
        self.more_button.setObjectName("controlButton")
        self.more_button.setProperty("overflowMenu", True)
        self.more_button.setToolTip("More monitor actions")
        menu = QMenu(self)
        menu.addAction("Open captures folder", self._open_captures)
        menu.addAction("Restart the engine", self._restart)
        self.camera_settings_action = menu.addAction("Camera settings", self._open_settings)
        menu.addAction("Toggle picture guide", self._toggle_guide)
        self.fullscreen_action = menu.addAction("Enter full screen", self._toggle_fullscreen)
        self.detach_action = menu.addAction("Open in new window", self.detachRequested.emit)
        menu.addAction("Preview capture flash", self.flashPreviewRequested.emit)
        menu.addAction("Liveness test…", self._open_liveness_test)
        self.more_button.setMenu(menu)
        self.fullscreen_button = QPushButton("Full Screen")
        self.fullscreen_button.setObjectName("controlButton")
        self.fullscreen_button.setToolTip("Toggle full-screen monitor")
        apply_button_icon(self.fullscreen_button, "expand", palette(self._theme)["text_secondary"])
        for button in (self.start_button, self.pause_button, self.restart_button,
                       self.snapshot_button, self.more_button, self.captures_button,
                       self.fullscreen_button):
            button.setParent(self)
            button.hide()

    def _build_log_card(self):
        card = Card("System Logs", "", icon="list", compact=True, theme=self._theme)
        self.log_card = card
        self.auto_toggle = ToggleSwitch(True, theme=self._theme)
        self.auto_toggle.setToolTip("Scroll to the newest entry automatically")
        card.actions.addWidget(self.auto_toggle)
        self.log_toggle = QPushButton("Hide")
        self.log_toggle.setObjectName("ghostButton")
        self.log_toggle.clicked.connect(self._toggle_logs)
        card.actions.addWidget(self.log_toggle)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("iconButtonFlat")
        self.clear_button.clicked.connect(self.clear_logs)
        card.actions.addWidget(self.clear_button)

        self.log_list = QListWidget()
        self.log_list.setObjectName("logList")
        self.log_list.setFixedHeight(95)
        card.add(self.log_list)
        return card

    def _build_status_card(self):
        card = Card("System Status", "", icon="shield", theme=self._theme)
        self.status_card = card
        card.setMinimumHeight(220)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        self.banner.setMinimumHeight(50)
        banner_row = QHBoxLayout(self.banner)
        banner_row.setContentsMargins(12, 8, 12, 8)
        banner_row.setSpacing(10)
        self._banner_icon = IconLabel("check-circle", 20)
        banner_row.addWidget(self._banner_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        banner_text = QVBoxLayout()
        banner_text.setSpacing(1)
        self.engine_status_label = QLabel("Stopped")
        self.engine_status_label.setObjectName("bannerTitle")
        self.engine_status_detail = QLabel("Engine is not running")
        self.engine_status_detail.setObjectName("bannerBody")
        banner_text.addWidget(self.engine_status_label)
        banner_text.addWidget(self.engine_status_detail)
        banner_row.addLayout(banner_text)
        banner_row.addStretch(1)
        card.add(self.banner)

        for key, (icon_name, label) in zip(("camera", "database", "recognition",
                                            "storage", "recording"), STATUS_ROW_ICONS):
            row_frame = QFrame()
            row_frame.setObjectName("statusRow")
            row_frame.setMinimumHeight(24)
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(6, 3, 6, 3)
            row.setSpacing(10)
            icon = IconLabel(icon_name, 16)
            row.addWidget(icon)
            name = QLabel(label)
            name.setObjectName("statusItem")
            row.addWidget(name)
            row.addStretch(1)
            value = QLabel("Idle")
            value.setObjectName("statusValue")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(value)
            dot = IconLabel("dot", 10)
            row.addWidget(dot)
            card.add(row_frame)
            self._status_rows[key] = (dot, value, icon)
        self.sync_status_label = QLabel("Backend: waiting for HRM connection")
        self.sync_status_label.setObjectName("muted")
        self.sync_status_label.setWordWrap(True)
        self.sync_status_label.setToolTip(
            "Face detection, liveness and matching run on this device. "
            "Attendance sync starts after a local save, retries every 5 seconds, "
            "and resumes on reconnect. HRM must acknowledge delivery. "
            "Rejected or unassigned events can be reviewed in Attendance Report.")
        card.add(self.sync_status_label)
        return card

    def set_sync_status(self, status):
        self.sync_status_label.setText("Backend: " + status)

    def _build_activity_card(self):
        card = Card("Live Activity", "Attendance updates", icon="list", theme=self._theme)
        self.activity_card = card
        card.setMinimumHeight(150)
        self.view_all_button = QPushButton("View attendance report  →")
        self.view_all_button.setObjectName("linkButton")
        self.view_all_button.clicked.connect(self._view_all_activity)
        self.activity_count = QLabel("0 saved")
        self.activity_count.setObjectName("chip")
        self.activity_count.setFixedHeight(24)
        self.activity_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.actions.addWidget(self.activity_count)

        session = QLabel("THIS SESSION · NEWEST FIRST")
        session.setObjectName("activitySection")
        card.add(session)

        self.activity = ActivityFeed(theme=self._theme, max_rows=8)
        self.activity.rowActivated.connect(self._open_activity_detail)
        scroll = QScrollArea()
        scroll.setObjectName("scrollFeed")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(100)
        scroll.setWidget(self.activity)
        self.activity_scroll = scroll
        card.add(scroll, 1)
        card.add(self.view_all_button)
        return card

    # ── wiring ───────────────────────────────────────────────────────────

    def _connect(self):
        self.engine.frameReady.connect(self.on_frame)
        self.engine.channelFrameReady.connect(self._on_channel_frame)
        self.engine.cameraPausedChanged.connect(self._on_camera_paused)
        self.engine.statsReady.connect(self.on_stats)
        self.engine.tracksReady.connect(self._on_tracks)
        self.engine.captureTriggered.connect(self.on_capture)
        self.engine.unknownFaceAlert.connect(self.on_unknown_capture)
        self.engine.inactiveEmployeeAlert.connect(self.on_inactive_capture)
        self.engine.attendanceSaved.connect(self.on_saved)
        self.engine.attendanceFailed.connect(self.on_failed)
        self.engine.runningChanged.connect(self._sync_running)
        self.engine.pausedChanged.connect(lambda _paused: self._update_pause_button())
        self.engine.errorRaised.connect(self.on_error)
        self.engine.logMessage.connect(self.on_log)
        self.start_button.clicked.connect(self._toggle_engine)
        self.pause_button.clicked.connect(self._toggle_pause)
        self.restart_button.clicked.connect(self._restart)
        self.snapshot_button.clicked.connect(self._save_snapshot)
        self.captures_button.clicked.connect(self._open_captures)
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)

    # ── engine control ───────────────────────────────────────────────────

    def _open_settings(self):
        if getattr(self, "_fs_active", False):
            self._exit_fullscreen()
        self.settingsRequested.emit()

    def _view_all_activity(self):
        if getattr(self, "_fs_active", False):
            self._exit_fullscreen()
        self.viewAllRequested.emit()

    def _toggle_engine(self):
        if not guard(self, "control_engine", "start or stop the engine"):
            return
        try:
            if self.engine.running:
                self.engine.stop()
            else:
                from ..camera_permission import with_camera_permission
                with_camera_permission(self._permission_source(), self.engine.start, self.on_error, context=self)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            self.on_error(str(exc))

    def _toggle_pause(self):
        if not guard(self, "control_engine", "pause or resume attendance recording"):
            return
        self.engine.toggle_paused()
        self._update_pause_button()

    def _restart(self):
        if not guard(self, "control_engine", "restart the engine"):
            return
        try:
            from ..camera_permission import with_camera_permission
            with_camera_permission(self._permission_source(), self.engine.restart, self.on_error, context=self)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            self.on_error(str(exc))
        self._update_pause_button()

    def _toggle_guide(self):
        self.video.set_guide(not self.video._guide)

    def _toggle_logs(self):
        visible = not self.log_list.isVisible()
        self.log_list.setVisible(visible)
        self.log_toggle.setText("Hide" if visible else "Show")

    def _save_snapshot(self, _checked=False, position=0):
        if not guard(self, "export_data", "save camera snapshots"):
            return
        frame = self.engine.grab_clean_frame(position)
        if frame is None:
            self.toast.show_message("No camera frame yet - start the engine first.", "warn")
            return
        selected = self.settings.selected_cameras()
        label = ""
        if len(selected) > 1 and position < len(selected):
            label = "".join(ch if ch.isalnum() else "_" for ch in selected[position][1]["name"]).strip("_")
            label = f"{label}_" if label else ""
        default = str(Path(self.settings.capture_dir) /
                      f"snapshot_{label}{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
        target, _ = QFileDialog.getSaveFileName(self, "Save snapshot", default, "JPEG image (*.jpg)")
        if not target:
            return
        try:
            write_jpeg(target, frame)
            ok = True
        except OSError:
            ok = False
        self.toast.show_message(f"Snapshot saved to {target}" if ok else "Could not write the snapshot",
                                "ok" if ok else "bad")

    def _open_liveness_test(self):
        """Run the liveness check on the bundled sample photos (and offer the demo employee)."""
        from .liveness import LivenessTestDialog
        dialog = LivenessTestDialog(self.settings, self.engine, self._theme, self.window())
        dialog.finished.connect(dialog.deleteLater)
        self.liveness_dialog = dialog
        dialog.open()

    def _open_captures(self):
        if not guard(self, "export_data", "open the captures folder"):
            return
        directory = Path(self.settings.capture_dir)
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def clear_logs(self):
        self.log_list.clear()
        self._log_rows = []

    def apply_access(self, access):
        """Hide shortcuts into screens this account cannot open."""
        self.view_all_button.setVisible(access.allows_nav("Attendance Report"))
        can_configure = access.allows_nav("Settings")
        self.preview_hud.settings.setVisible(can_configure)
        self.camera_settings_action.setVisible(can_configure)

    # ── slots ────────────────────────────────────────────────────────────

    def on_frame(self, frame):
        had_frame = self.video.has_frame()
        self.video.set_frame(frame)
        if not had_frame:  # Per-frame restyling of the controls cost GUI time for nothing.
            self._sync_preview_controls()

    def on_stats(self, stats):
        # KPI cards
        faces = stats.get("faces", 0)
        self.cards["enrolled"].set_value(stats.get("enrolled", 0))
        self.cards["enrolled"].set_hint(describe_source(self.settings.source))
        known = stats.get("known_faces", 0)
        verified = stats.get("verified_today")
        self.cards["verified"].set_value("-" if verified is None else verified, "ok" if verified else "idle")
        presence = stats.get("presence", 0.0)
        target = max(0.1, float(stats.get("capture_after", 1.0)))
        self.cards["waiting"].set_value(
            f"{presence:.1f}s",
            "warn" if 0 < presence < target else "ok" if presence >= target else "idle")
        self.cards["waiting"].set_hint(f"Need {target:.1f}s verified presence")
        unknown = stats.get("unknown_faces", 0)
        self.cards["unknown"].set_value(unknown, "bad" if unknown else "idle")

        inactive = stats.get("inactive_faces", 0)
        self.cards["inactive"].set_value(inactive, "bad" if inactive else "idle")
        self.cards["inactive"].set_hint(stats.get("inactive_details") or "Attendance blocked")

        # Camera strip + overlay chips
        status = stats.get("status", "STOPPED")
        self._camera_status = status
        self._sound.camera(status == "CONNECTED", self.engine.running and not self.engine.paused)
        self._storage_ready = stats.get("storage_ready", False)
        fps = stats.get("camera_fps", 0)
        self.cam_fps_label.setText(f"FPS: {fps:.1f}")
        source = describe_source(self.settings.source)
        self.cam_source_label.setToolTip(source)
        self.cam_source_label.setText(self.cam_source_label.fontMetrics().elidedText(
            source, Qt.TextElideMode.ElideRight, 150))
        resolution = stats.get("resolution") or stats.get("resolution_text")
        if resolution:
            self.cam_resolution.setText(f"Resolution: {resolution}")
            self.video.set_overlay(resolution=resolution, fps=fps, faces=faces)
        else:
            width, height = stats.get("width"), stats.get("height")
            if width and height:
                self.cam_resolution.setText(f"Resolution: {int(width)} x {int(height)}")
                self.video.set_overlay(resolution=f"{int(width)} x {int(height)}",
                                       fps=fps, faces=faces)
            else:
                self.video.set_overlay(fps=fps, faces=faces)
        shown = [index for index, _entry in self.settings.selected_cameras()]
        statuses = {}
        for camera in stats.get("cameras", ()):
            position = camera.get("position", 0)
            if position < len(shown):
                statuses[shown[position]] = camera.get("status")
            if 0 < position <= len(self.extra_videos):
                self.extra_videos[position - 1].set_overlay(fps=camera.get("fps", 0))
        self.camera_bar.set_statuses(statuses if self.engine.running else {})
        self._sync_preview_controls()
        self.preview_hud.set_metrics(self.video._resolution, fps)
        queue = stats.get("queue", 0)
        queue_max = max(1, stats.get("queue_max", 1))
        latency = stats.get("latency_ms", 0)
        self.cam_pipeline.setText(f"Analysis: {latency:.0f} ms · Queue: {queue}/{queue_max}")

        # Live badge visibility
        connected = status == "CONNECTED"
        self.live_badge.setVisible(connected and self.engine.running and not self.engine.paused)

        # System status rows
        self._set_status_row("camera", "Connected" if connected else status.title(), connected)
        self._set_status_row("database", "Ready" if stats.get("storage_ready", False)
                             else "Not Ready", stats.get("storage_ready", False))
        self._set_status_row("recognition", "Running" if self.engine.running else "Idle",
                             self.engine.running)
        staging = stats.get('staging_queue', 0)
        self._set_status_row("storage", f"{queue}/{queue_max} queued" +
                             (f" · {staging} staging" if staging else ""), queue < queue_max)
        self._set_status_row("recording", "Paused" if self.engine.paused else
                             ("Enabled" if self.engine.running else "Off"),
                             self.engine.running and not self.engine.paused)

        # Problems
        problems = [t for t in (stats.get("storage_error", ""),
                                stats.get("recognition_error", "")) if t]
        if self.engine.running:
            self.engine_status_detail.setText(
                f"{stats['quality_blocked']} face(s) need adjustment" if stats.get("quality_blocked") else
                "Face recognized; liveness not verified" if stats.get("liveness_blocked")
                else "Engine is active and ready to detect")
        self.engine_status_detail.setToolTip(
            ("Turn off Portrait/background blur or beauty filters if enabled. "
             "Keep your whole face visible in even light.\n" + stats.get("liveness_details", ""))
            if stats.get("liveness_blocked") else "")
        if stats.get("quality_blocked"):
            self.engine_status_detail.setToolTip(stats.get("quality_details", ""))
        if not stats.get("storage_ready", False):
            problems.insert(0, "Attendance storage is not ready; recording is disabled.")
        if problems:
            self.problem.setText(" | ".join(problems))
            self.problem.show()
        else:
            self.problem.hide()

    def _on_tracks(self, tracks):
        if self.engine.running and not self.engine.paused:
            self._sound.tracks(tracks)

    def _set_status_row(self, key, text, ok):
        dot, value, _icon = self._status_rows[key]
        dot.set_icon_color(tone_color(self._theme, "ok" if ok else "bad"))
        value.setText(text)
        value.setStyleSheet(
            f"color: {tone_color(self._theme, 'ok' if ok else 'bad')}; font-weight: 600;")

    # ── events ───────────────────────────────────────────────────────────

    def on_inactive_capture(self, info):
        self._log("WARN", f"Inactive employee {info['employee_name']} ({info['employee_id']}): "
                  f"attendance blocked; capture saved: {info['snapshot']}")
        self.toast.show_message(f"Inactive employee: {info['employee_name']} — attendance blocked", "bad")
        self._add_activity(info["employee_name"], "Inactive — blocked", "Scan attempt captured",
                           info["employee_id"],
                           evidence={"capture": load_photo(info["snapshot"]),
                                     "snapshot": info["snapshot"],
                                     "captured_at": info["captured_at"],
                                     "detail": "Inactive employee scan; no attendance recorded."})

    def on_unknown_capture(self, info):
        if not info.get("snapshot"):
            self._log("INFO", f"Unknown face detected: track #{info.get('track_id', '?')}")
            return
        self._log("INFO", f"Unknown / Not enrolled face saved: {info['snapshot']}")
        self._add_activity("Unknown / Not enrolled", "Face saved",
                           f"Track #{info['track_id']} · {info['age']:.1f}s in view",
                           evidence={"capture": load_photo(info["snapshot"]),
                                     "snapshot": info["snapshot"],
                                     "captured_at": info["captured_at"],
                                     "detail": "Unrecognized face; identity and liveness are unverified."})

    def on_capture(self, job):
        self._log("INFO", f"CAPTURED  {job.employee_name} ({job.employee_id}), "
                  f"{job.duration:.1f}s verified")
        self.toast.show_message(
            f"Captured {job.employee_name} \u2014 saving attendance...", "warn")

    def on_saved(self, result):
        text = format_checkin_speech(result.job.last_name, result.job.employee_name)
        try:
            self._sound.play(text=text)
        except TypeError:
            self._sound.play()
        recorded = datetime.fromtimestamp(result.recorded_at or 0).strftime("%H:%M:%S")
        self._log("INFO", f"{result.job.employee_name} ({result.job.employee_id}) "
                  f"recorded at {recorded}, {result.job.duration:.1f}s verified")
        self.toast.show_message(f"Attendance recorded for {result.job.employee_name}", "ok")
        self._add_activity(result.job.employee_name, "Verified",
                           f"{result.job.duration:.1f}s verified presence",
                           result.job.employee_id, time_text=recorded,
                           evidence=self._capture_evidence(result))

    def on_failed(self, result):
        self._sound.play("error")
        self._log("ERROR", f"Save failed: {result.error}")
        self.toast.show_message(f"Attendance save failed: {result.error}", "bad")
        self._add_activity(result.job.employee_name, "Save failed", str(result.error),
                           result.job.employee_id, evidence=self._capture_evidence(result))

    def on_error(self, message):
        self._sound.play("error")
        self._log("ERROR", str(message))
        self.toast.show_message(str(message), "bad")

    def on_log(self, message):
        self._log("INFO", str(message))

    # ── state sync ───────────────────────────────────────────────────────

    def _sync_running(self, running):
        self._sound.stop()
        self._sound.reset()
        self._camera_status = "CONNECTING" if running else "STOPPED"
        self._sync_requirements()
        self.pause_button.setEnabled(bool(running))
        self._update_pause_button(retint=False)

        if running:
            self.start_button.setText("Stop Engine")
            self.start_button.setObjectName("controlButtonDanger")
            self.engine_status_label.setText("Running")
            self.engine_status_detail.setText("Engine is active and ready to detect")
            self.banner.setProperty("tone", "ok")
            self._banner_icon.set_icon("check-circle")
            self._banner_icon.set_icon_color(tone_color(self._theme, "ok"))
            self.video.show_message("Position your face in the frame",
                                    "The engine is watching for enrolled employees")
            for view in self.extra_videos:
                view.show_message("Connecting…", "Waiting for this camera")
        else:
            self.start_button.setText("Start Engine")
            self.start_button.setObjectName("controlButtonSuccess")
            self.engine_status_label.setText("Stopped")
            self.engine_status_detail.setText("Engine is not running")
            self.banner.setProperty("tone", "idle")
            self._banner_icon.set_icon("alert")
            self._banner_icon.set_icon_color(tone_color(self._theme, "muted"))
            self.video.show_message("Engine stopped", "Press Start Engine to begin")
            self.camera_bar.set_statuses({})
            for view in self.extra_videos:
                view.show_message("Engine stopped", "Press Start Engine to begin")
                view.set_overlay(resolution="", fps=0)
            self.cards["verified"].set_value("-", "idle")
            self.cards["waiting"].set_value("-")
            self.cards["unknown"].set_value("0", "idle")
            self.cards["inactive"].set_value("0", "idle")
            self.video.set_overlay(faces=0)
            self.video.set_overlay(resolution="", fps=0)
            for row_key in self._status_rows:
                self._set_status_row(row_key, "Idle", True)
            self.live_badge.setVisible(False)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)
        self._retint_buttons(running=running)
        self._sync_preview_controls(running)

    def _update_pause_button(self, retint=True):
        paused = self.engine.paused
        if paused:
            self._sound.stop()
            self._sound.reset()
        label = "Resume" if paused else "Pause"
        self.pause_button.setText(label)
        self.pause_button.setObjectName("softButton" if paused else "controlButton")
        self.pause_button.style().unpolish(self.pause_button)
        self.pause_button.style().polish(self.pause_button)
        self._sync_preview_controls()
        if retint:
            self._retint_buttons()

    def _sync_preview_controls(self, running=None):
        running = self.engine.running if running is None else running
        self.preview_hud.set_state(running, self.engine.paused, self._camera_status,
                                   self._storage_ready, self.video.has_frame())
        self.live_badge.setVisible(running and not self.engine.paused and
                                  self._camera_status == "CONNECTED")
        self._sync_engine_strip(running)
        for view in self._tile_views():
            if view.footer is not None:
                view.footer.set_running(running)

    def _retint_buttons(self, running=None):
        """Buttons carry vector glyphs, so re-colour them when state or theme changes."""
        colors = palette(self._theme)
        if running is None:
            running = self.engine.running
        plan = (
            (self.start_button, "stop" if running else "play"),
            (self.pause_button, "play" if self.engine.paused else "pause"),
            (self.restart_button, "restart"),
            (self.snapshot_button, "image"),
            (self.captures_button, "folder"),
            (self.more_button, "more"),
            (self.fullscreen_button, "expand"),
            (self.gear_button, "gear"),
            (self.expand_button, "face-id"),
            (self.clear_button, "trash"),
        )
        for button, name in plan:
            object_name = button.objectName()
            if object_name in ("primary", "success", "controlButtonSuccess"):
                color = "#FFFFFF"
            elif object_name in ("danger", "controlButtonDanger"):
                color = colors["danger"]
            elif object_name in ("iconButton", "iconButtonFlat", "ghostButton"):
                color = colors["muted"]
            else:
                color = colors["text_secondary"]
            apply_button_icon(button, name, color, size=14)
        self.start_button.setText("Stop Engine" if running else "Start Engine")

    # ── logging ──────────────────────────────────────────────────────────

    def _log(self, level, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem()
        row = LogRow(level, stamp, message, theme=self._theme)
        self.log_list.addItem(item)
        self.log_list.setItemWidget(item, row)
        self._log_rows.append(row)
        while self.log_list.count() > LOG_ROW_LIMIT:
            self.log_list.takeItem(0)
            if self._log_rows:
                self._log_rows.pop(0)
        if self.auto_toggle.isChecked():
            self.log_list.scrollToBottom()

    def _capture_evidence(self, result):
        def preview(frame):
            if getattr(frame, "size", 0):
                height, width = frame.shape[:2]
                scale = min(1.0, 1200 / max(height, width))
                if scale < 1:
                    frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
            return to_pixmap(frame)

        capture = load_photo(result.snapshot)
        if capture.isNull():
            capture = preview(result.job.frame)
        context = context_path(result.snapshot) if result.snapshot else None
        context = str(context) if context is not None and context.is_file() else ""
        return {"capture": capture,
                "face_box": result.job.face_box,
                "context_path": context,
                "context_capture": None if context else preview(result.job.context_frame),
                "captured_at": datetime.fromtimestamp(result.job.captured_at).strftime("%d %b %Y · %H:%M:%S"),
                "duration": f"{result.job.duration:.1f}s",
                "event_id": result.job.event_id}

    def _open_activity_detail(self, entry):
        if getattr(self, "_activity_dialog", None) is not None:
            self._activity_dialog.raise_()
            return
        # The same rule as matching: connected to HRM means angles, otherwise local samples.
        directory = hrm_directory.load(hrm_directory.cache_path(self.settings))
        on_angles = directory.synced and bool(directory.employees)
        code = str(entry.get("employee_id") or "").strip()
        try:
            photos = (angle_photos(self.settings, code) if on_angles else
                      enrollment_photos(self.settings, entry.get("employee_id"), self.engine.employee_rows()))
        except (OSError, ValueError, EOFError):
            photos = []
        dialog = ActivityDetailDialog(entry, photos, self._theme, self.window(), angles=on_angles)
        self._activity_dialog = dialog
        self._activity_dialog_code = code
        dialog.finished.connect(self._close_activity_detail)
        dialog.open()
        if on_angles:
            self._fetch_detail_angles(code)

    def _fetch_detail_angles(self, code):
        """Bring the window's images up to date from HRM without holding up the window."""
        config, settings = config_from_settings(self.settings), self.settings
        if not code or configuration_problem(config):
            self._on_detail_angles(code, False)
            return

        def work():
            try:
                hrm_directory.fetch_saved_angles(config, settings, code)
                answered = True
            except (HRMError, OSError):
                answered = False
            try:
                self._detailAnglesReady.emit(code, answered)
            except RuntimeError:
                pass  # The window closed meanwhile.

        threading.Thread(target=work, name="hrm-detail-angles", daemon=True).start()

    def _on_detail_angles(self, code, answered):
        dialog = getattr(self, "_activity_dialog", None)
        if dialog is None or code != getattr(self, "_activity_dialog_code", None):
            return      # Closed, or now showing someone else.
        if answered:
            dialog.set_angle_photos(angle_photos(self.settings, code))
        else:
            dialog.stop_loading_angles()

    def _close_activity_detail(self, _result):
        dialog = self._activity_dialog
        self._activity_dialog = None
        if dialog is not None:
            dialog.deleteLater()

    def _add_activity(self, name, status, detail, employee_id="", time_text=None, evidence=None):
        if status == "Verified":
            self._saved_activity_count = getattr(self, "_saved_activity_count", 0) + 1
            self.activity_count.setText(f"{self._saved_activity_count} saved")
        self.activity.add(
            name,
            detail=detail,
            status="Check-in saved" if status == "Verified" else status,
            tone="ok" if status == "Verified" else "bad",
            time_text=time_text or datetime.now().strftime("%H:%M:%S"),
            subtitle=employee_id,
            evidence=evidence,
        )

    # ── settings / lifecycle ─────────────────────────────────────────────

    # ── fullscreen ────────────────────────────────────────────────────────

    def set_detached(self, detached):
        self.preview_hud.set_detached(detached)
        self.detach_action.setText("Return to dashboard" if detached else "Open in new window")

    def _toggle_fullscreen(self):
        win = self.window()
        if not win:
            return
        if getattr(self, "_fs_active", False):
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if getattr(self, "_fs_active", False):
            return
        win = self.window()
        self._fs_active = True
        self._was_maximized = win.isMaximized()
        self.fullscreen_action.setText("Exit full screen")
        self.preview_hud.set_fullscreen(True)
        self.escape_shortcut.setEnabled(True)

        # remember which panels to hide
        self._fs_hidden = []
        for w in self._fs_panels():
            if w.isVisible():
                self._fs_hidden.append(w)
                w.hide()

        # hide the main window chrome
        for attr in ("sidebar", "header"):
            w = getattr(win, attr, None)
            if w:
                self._fs_hidden.append(w)
                w.hide()

        # expand video
        self.tile_rows.setMinimumHeight(0)
        win.showFullScreen()

    def _exit_fullscreen(self):
        if not getattr(self, "_fs_active", False):
            return
        win = self.window()
        self._fs_active = False
        self.escape_shortcut.setEnabled(False)

        self.fullscreen_action.setText("Enter full screen")
        self.preview_hud.set_fullscreen(False)

        for w in getattr(self, "_fs_hidden", []):
            w.show()
        self._fs_hidden = []

        self.tile_rows.setMinimumHeight(PREVIEW_MIN_HEIGHT)
        if self._was_maximized:
            win.showMaximized()
        else:
            win.showNormal()

    def _fs_panels(self):
        panels = []
        for key in self.cards:
            panels.append(self.cards[key])
        for w in (self.requirements_card, self.status_card, self.log_card):
            panels.append(w)
        # hide the controls row buttons (they are in the floating overlay)
        for btn in (self.start_button, self.pause_button, self.restart_button,
                    self.snapshot_button, self.captures_button, self.more_button,
                    self.fullscreen_button):
            panels.append(btn)
        return panels

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and getattr(self, "_fs_active", False):
            self._exit_fullscreen()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        self._exit_fullscreen()
        super().hideEvent(event)

    def on_settings_changed(self, settings):
        self.settings = settings
        self._layout_tiles()
        self._sound.stop()
        self._sound = EventSounds(settings)
        self._sync_requirements()
        if self.camera_box.count():
            self.camera_box.setItemText(0, describe_source(settings.source))

    def set_theme(self, theme):
        self._theme = theme
        self.video.set_theme(theme)
        for view in self.extra_videos:
            view.set_theme(theme)
        self.camera_bar.set_theme(theme)
        self._style_engine_strip()
        self.toast.set_theme(theme)
        self.auto_toggle.set_theme(theme)
        self.activity.set_theme(theme)
        for card in self.cards.values():
            card.set_theme(theme)
        for panel in (self.camera_card, self.log_card, self.status_card, self.activity_card,
                      self.requirements_card):
            panel.set_theme(theme)
        for dot, _value, icon in self._status_rows.values():
            icon.set_icon_color(palette(theme)["muted"])
        for row in self._log_rows:
            row.set_theme(theme)
        self._retint_buttons()

    def close(self):
        self._exit_fullscreen()
        self._sound.stop()
        super().close()
