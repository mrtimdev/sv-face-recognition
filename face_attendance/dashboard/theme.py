"""Dashboard palette and stylesheet, derived from the terminal overlay colours.

The BGR constants in ``face_attendance.ui`` map to these RGB values, so the Qt
dashboard and the OpenCV window look like the same product.
"""

# BGR (100, 225, 95) -> RGB #5FE164, and so on for the terminal palette.
PALETTES = {
    "dark": {
        "bg": "#14181B",
        "panel": "#1B2024",
        "panel_alt": "#20262B",
        "border": "#353C3D",
        "text": "#F2F4F2",
        "muted": "#A29B91",
        "accent": "#5FE164",
        "danger": "#FA5A5F",
        "warn": "#FAC355",
        "info": "#6BB8FF",
        "video_bg": "#0E1113",
        "selection": "#2C3A31",
        "shadow": "#0A0D0F",
    },
    "light": {
        "bg": "#F2F4F2",
        "panel": "#FFFFFF",
        "panel_alt": "#E9EBE7",
        "border": "#C9CEC6",
        "text": "#1B1A17",
        "muted": "#6B6B63",
        "accent": "#1E9E4A",
        "danger": "#C43C46",
        "warn": "#B97A16",
        "info": "#2A6FB0",
        "video_bg": "#20262B",
        "selection": "#D6E8DA",
        "shadow": "#C2C7BF",
    },
}

TONE_COLORS = {"ok": "accent", "warn": "warn", "bad": "danger", "idle": "muted", "info": "info"}


def stylesheet(theme="dark"):
    """A single application stylesheet; widgets only need object names."""
    c = palette(theme)
    return f"""
QWidget {{
    background-color: {c['bg']};
    color: {c['text']};
    font-size: 13px;
}}
QDialog, QMainWindow, QScrollArea, QStackedWidget, QSplitter {{ background-color: {c['bg']}; }}
QLabel {{ background: transparent; }}
QLabel#appTitle {{ font-size: 17px; font-weight: 600; }}
QLabel#screenTitle {{ font-size: 19px; font-weight: 600; }}
QLabel#screenSubtitle, QLabel#statHint, QLabel#muted, QLabel#emptyBody {{ color: {c['muted']}; }}
QLabel#sectionTitle {{ font-size: 14px; font-weight: 600; color: {c['accent']}; }}
QLabel#emptyTitle {{ font-size: 16px; font-weight: 600; }}

QFrame#card, QFrame#statCard, QFrame#panel {{
    background-color: {c['panel']};
    border: 1px solid {c['border']};
    border-radius: 10px;
}}
QFrame#toast {{
    background-color: {c['panel_alt']};
    border: 1px solid {c['border']};
    border-left: 4px solid {c['info']};
    border-radius: 6px;
}}
QFrame#toast[tone="ok"] {{ border-left-color: {c['accent']}; }}
QFrame#toast[tone="bad"] {{ border-left-color: {c['danger']}; }}
QFrame#toast[tone="warn"] {{ border-left-color: {c['warn']}; }}
QLabel#statTitle {{ color: {c['muted']}; font-size: 11px; }}
QLabel#statValue {{ font-size: 21px; font-weight: 600; }}
QLabel#statValue[tone="ok"] {{ color: {c['accent']}; }}
QLabel#statValue[tone="warn"] {{ color: {c['warn']}; }}
QLabel#statValue[tone="bad"] {{ color: {c['danger']}; }}
QLabel#statValue[tone="idle"] {{ color: {c['text']}; }}

QFrame#pill {{ border-radius: 9px; border: 1px solid {c['border']}; background-color: {c['panel_alt']}; }}
QLabel#pillText {{ font-size: 11px; font-weight: 600; }}
QFrame#pill[tone="ok"] {{ border-color: {c['accent']}; }}
QFrame#pill[tone="ok"] QLabel#pillText {{ color: {c['accent']}; }}
QFrame#pill[tone="warn"] {{ border-color: {c['warn']}; }}
QFrame#pill[tone="warn"] QLabel#pillText {{ color: {c['warn']}; }}
QFrame#pill[tone="bad"] {{ border-color: {c['danger']}; }}
QFrame#pill[tone="bad"] QLabel#pillText {{ color: {c['danger']}; }}
QFrame#pill[tone="idle"] QLabel#pillText {{ color: {c['muted']}; }}

QPushButton {{
    background-color: {c['panel_alt']};
    border: 1px solid {c['border']};
    border-radius: 7px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton:pressed {{ background-color: {c['selection']}; }}
QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; }}
QPushButton#primary {{ background-color: {c['accent']}; color: {c['shadow']}; border: none; font-weight: 600; }}
QPushButton#danger {{ border-color: {c['danger']}; color: {c['danger']}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QPlainTextEdit {{
    background-color: {c['panel']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {c['accent']};
    selection-color: {c['shadow']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateEdit:focus, QTimeEdit:focus {{ border-color: {c['accent']}; }}
QComboBox QAbstractItemView {{
    background-color: {c['panel']};
    border: 1px solid {c['border']};
    selection-background-color: {c['selection']};
}}

QTableView, QTableWidget, QListWidget, QTreeWidget {{
    background-color: {c['panel']};
    alternate-background-color: {c['panel_alt']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    gridline-color: {c['border']};
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
QHeaderView::section {{
    background-color: {c['panel_alt']};
    border: none;
    border-right: 1px solid {c['border']};
    border-bottom: 1px solid {c['border']};
    padding: 6px 8px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background-color: {c['panel_alt']}; border: none; }}

QListWidget#nav {{ border: none; border-radius: 0px; background-color: {c['panel']}; padding: 8px 6px; }}
QListWidget#nav::item {{ padding: 10px 12px; border-radius: 7px; margin: 2px 0px; color: {c['muted']}; }}
QListWidget#nav::item:selected {{ background-color: {c['selection']}; color: {c['accent']}; font-weight: 600; }}
QListWidget#nav::item:hover {{ color: {c['text']}; }}

QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 8px; background-color: {c['panel']}; }}
QTabBar::tab {{
    background: transparent; padding: 8px 16px; margin-right: 4px;
    border-top-left-radius: 7px; border-top-right-radius: 7px; color: {c['muted']};
}}
QTabBar::tab:selected {{ background-color: {c['panel']}; color: {c['accent']}; font-weight: 600; }}

QGroupBox {{
    border: 1px solid {c['border']};
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    background-color: {c['panel']};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {c['accent']}; font-weight: 600; }}

QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px; background-color: {c['panel_alt']}; text-align: center; }}
QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 5px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 5px; min-width: 30px; }}
QStatusBar {{ background-color: {c['panel']}; border-top: 1px solid {c['border']}; color: {c['muted']}; }}
QToolTip {{ background-color: {c['panel_alt']}; color: {c['text']}; border: 1px solid {c['border']}; }}
QCheckBox, QRadioButton {{ background: transparent; spacing: 6px; }}
QSplitter::handle {{ background-color: {c['border']}; }}
"""


def palette(theme="dark"):
    return PALETTES.get(theme, PALETTES["dark"])
