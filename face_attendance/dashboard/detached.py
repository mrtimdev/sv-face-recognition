"""Move a monitor page between its tab and a normal, independently sized window.

The page itself moves. Engine connections, preview timers, plate workers and
access control remain the same objects; opening a window never opens a camera.
"""
from PyQt6.QtCore import QObject, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStyle, QVBoxLayout, QWidget


class DetachedPageWindow(QMainWindow):
    returnRequested = pyqtSignal(bool)

    def __init__(self, dashboard, title, screen):
        super().__init__(dashboard, Qt.WindowType.Window)
        self.access = dashboard.access
        self.setWindowTitle(f"{title} — Face ID Attendance")
        self.setWindowIcon(dashboard.windowIcon())
        self.setMinimumSize(860,600)
        self.resize(1200,820)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        self.header = QFrame()
        self.header.setObjectName('headerBar')
        row = QHBoxLayout(self.header)
        row.setContentsMargins(16,8,16,8)
        row.setSpacing(10)
        self.return_button = QPushButton()
        self.return_button.setObjectName('detachedReturnButton')
        self.return_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.return_button.setIconSize(QSize(18,18))
        self.return_button.setFixedSize(32,32)
        self.return_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.return_button.setAccessibleName('Return to dashboard')
        self.return_button.setToolTip('Return to dashboard')
        self.return_button.setAutoDefault(False)
        self.return_button.clicked.connect(lambda: self.returnRequested.emit(True))
        row.addWidget(self.return_button,0,Qt.AlignmentFlag.AlignVCenter)
        heading = QLabel(title)
        heading.setObjectName('detachedWindowTitle')
        row.addWidget(heading)
        row.addStretch(1)
        layout.addWidget(self.header)
        layout.addWidget(screen,1)
        self.setCentralWidget(central)

    def closeEvent(self, event):
        # Closing a monitor window must not close the page or stop its engine.
        event.ignore()
        self.returnRequested.emit(False)


class DetachedPageController(QObject):
    def __init__(self, dashboard, index, title, screen):
        super().__init__(dashboard)
        self.dashboard, self.index, self.title, self.screen = dashboard,index,title,screen
        self.window = None
        self._cached_window = None
        self._geometry = None
        self.host = QWidget(dashboard.stack)
        layout = QVBoxLayout(self.host)
        layout.addStretch(1)
        self.host.placeholder = QFrame()
        self.host.placeholder.setObjectName('card')
        content = QVBoxLayout(self.host.placeholder)
        content.setContentsMargins(28,28,28,28)
        heading = QLabel(f'{title} is open in another window')
        heading.setObjectName('cardTitle')
        heading.setWordWrap(True)
        message = QLabel('Keep the monitors open side by side while using other dashboard pages.')
        message.setObjectName('cardSubtitle')
        message.setWordWrap(True)
        content.addWidget(heading)
        content.addWidget(message)
        actions = QHBoxLayout()
        self.host.show_window_button = QPushButton('Show window')
        self.host.show_window_button.setObjectName('primary')
        self.host.return_button = QPushButton('Return to dashboard')
        self.host.return_button.setObjectName('controlButton')
        actions.addWidget(self.host.show_window_button)
        actions.addWidget(self.host.return_button)
        actions.addStretch(1)
        content.addLayout(actions)
        layout.addWidget(self.host.placeholder)
        layout.addStretch(1)
        self.host.hide()
        self.host.show_window_button.clicked.connect(lambda: dashboard.detach_page(index))
        self.host.return_button.clicked.connect(lambda: dashboard.attach_page(index,activate=True))

    @property
    def is_detached(self):
        return self.window is not None

    def _leave_fullscreen(self):
        if getattr(self.screen,'_fs_active',False):
            self.screen._exit_fullscreen()
        # Native titlebar fullscreen bypasses the screen's HUD flag. Never
        # persist that state in saveGeometry or carry it across reparenting.
        window = self.screen.window()
        if window.isFullScreen():
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowFullScreen)

    def show_window(self):
        if self.window is not None:
            state = self.window.windowState() & ~Qt.WindowState.WindowMinimized
            self.window.setWindowState(state)
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        return self.window

    def detach(self):
        if self.window is not None:
            return self.show_window()
        self._leave_fullscreen()
        stack = self.dashboard.stack
        current = stack.currentWidget()
        stack.removeWidget(self.screen)
        stack.insertWidget(self.index,self.host)
        stack.setCurrentWidget(self.host if current is self.screen else current)
        if self._cached_window is None:
            self._cached_window = DetachedPageWindow(self.dashboard,self.title,self.screen)
            self._cached_window.returnRequested.connect(lambda activate:self.dashboard.attach_page(self.index,activate))
        else:
            self._cached_window.centralWidget().layout().addWidget(self.screen,1)
        self.window = self._cached_window
        self.screen.set_detached(True)
        if self._geometry is not None:
            self.window.restoreGeometry(self._geometry)
        # Qt geometry includes window state; a reused pop-out must start out
        # of fullscreen even when its previous native state was saved there.
        self.window.setWindowState(self.window.windowState() & ~(
            Qt.WindowState.WindowFullScreen | Qt.WindowState.WindowMinimized))
        self.screen.show()
        return self.show_window()

    def attach(self):
        if self.window is None:
            return self.screen
        self._leave_fullscreen()
        window = self.window
        self._geometry = window.saveGeometry()
        self.screen.hide()
        window.centralWidget().layout().removeWidget(self.screen)
        stack = self.dashboard.stack
        current = stack.currentWidget()
        stack.removeWidget(self.host)
        stack.insertWidget(self.index,self.screen)
        stack.setCurrentWidget(self.screen if current is self.host else current)
        self.window = None
        self.screen.set_detached(False)
        self.host.hide()
        window.hide()
        # macOS fullscreen transitions finish asynchronously. Reusing the
        # hidden NSWindow can reapply its pending fullscreen state on show.
        # Release its native resources after moving the page out; Qt rebuilds
        # the window handle on the next show, retaining this QWidget and page.
        window.destroy()
        return self.screen
